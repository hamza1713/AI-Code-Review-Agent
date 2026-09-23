import hashlib
import os
import json
import re
import time
import uuid
import sqlite3
import threading
import concurrent.futures
from pathlib import Path
from typing import Dict, Any, Optional, List, Callable, Union
from code_review_agent.config import logger


def is_transient_error(error: Any) -> bool:
    """
    Classify whether an error is transient (retryable) or permanent (fail-fast).
    Retryable: 429 rate limits, 500, 502, 503, 504 server errors, timeouts, connection drops.
    Permanent: 400, 401, 403, 404, 422, validation errors, branch guardrail violations.
    """
    err_str = str(error).lower()

    # Check status code attribute on error/exception objects
    status_code = getattr(error, "status_code", None) or getattr(error, "status", None)
    if status_code is not None:
        try:
            code = int(status_code)
            if code in (400, 401, 403, 404, 422):
                return False
            if code in (429, 500, 502, 503, 504):
                return True
        except (ValueError, TypeError):
            pass

    # Check permanent fail-fast patterns first
    permanent_patterns = [
        r"\b40[0134]\b",
        r"\b422\b",
        r"unauthorized",
        r"forbidden",
        r"permission\s*denied",
        r"validationerror",
        r"inputvalidationerror",
        r"invalid\s*diff",
        r"guardrail",
        r"schema\s*error",
        r"not\s*found"
    ]
    for pat in permanent_patterns:
        if re.search(pat, err_str):
            return False

    # Check retryable transient patterns
    transient_patterns = [
        r"\b429\b",
        r"\b50[0234]\b",
        r"ratelimit",
        r"rate\s*limit",
        r"timeout",
        r"timed?\s*out",
        r"connection\s*reset",
        r"connection\s*refused",
        r"service\s*unavailable",
        r"bad\s*gateway",
        r"econnreset",
        r"remote\s*disconnected",
        r"network\s*timeout"
    ]
    for pat in transient_patterns:
        if re.search(pat, err_str):
            return True

    return False


class WebhookJobQueue:
    """
    Durable persistent task queue for webhook processing.
    Survives application restarts and power outages using SQLite WAL mode with ACID transactions.
    """

    def __init__(self, db_path: Optional[str] = None):
        target = db_path or os.getenv("QUEUE_DB_PATH") or "webhook_jobs.db"
        self.db_path = Path(target).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._on_enqueue_callbacks: List[Callable[[], None]] = []
        self._init_db()

    def add_enqueue_listener(self, callback: Callable[[], None]):
        """Register a callback to be triggered immediately when a new job is enqueued."""
        self._on_enqueue_callbacks.append(callback)

    def _get_connection(self) -> sqlite3.Connection:
        """Get thread-local SQLite connection with busy timeout and WAL journal mode."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.db_path), timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            self._local.conn = conn
        return conn

    def _init_db(self):
        """Initialize SQLite schema if it does not exist."""
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS webhook_jobs (
                    job_id TEXT PRIMARY KEY,
                    pr_identifier TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_retries INTEGER NOT NULL DEFAULT 3,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    locked_at REAL,
                    review_result_json TEXT
                );
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_webhook_status ON webhook_jobs(status, created_at);
            """)
            # Migration checks: ensure all extended columns exist
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(webhook_jobs);")
            columns = {row[1] for row in cursor.fetchall()}
            if "review_result_json" not in columns:
                conn.execute("ALTER TABLE webhook_jobs ADD COLUMN review_result_json TEXT;")
            if "job_kind" not in columns:
                conn.execute("ALTER TABLE webhook_jobs ADD COLUMN job_kind TEXT NOT NULL DEFAULT 'webhook';")
            if "idempotency_key" not in columns:
                conn.execute("ALTER TABLE webhook_jobs ADD COLUMN idempotency_key TEXT;")
            if "worker_id" not in columns:
                conn.execute("ALTER TABLE webhook_jobs ADD COLUMN worker_id TEXT;")
            if "lease_expires_at" not in columns:
                conn.execute("ALTER TABLE webhook_jobs ADD COLUMN lease_expires_at REAL;")
            if "heartbeat_at" not in columns:
                conn.execute("ALTER TABLE webhook_jobs ADD COLUMN heartbeat_at REAL;")
            if "priority" not in columns:
                conn.execute("ALTER TABLE webhook_jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 50;")
            if "scheduled_at" not in columns:
                conn.execute("ALTER TABLE webhook_jobs ADD COLUMN scheduled_at REAL;")
            if "current_stage" not in columns:
                conn.execute("ALTER TABLE webhook_jobs ADD COLUMN current_stage TEXT;")
            if "stage_progress" not in columns:
                conn.execute("ALTER TABLE webhook_jobs ADD COLUMN stage_progress REAL NOT NULL DEFAULT 0.0;")

            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_webhook_idempotency ON webhook_jobs(idempotency_key);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_webhook_status ON webhook_jobs(status, created_at);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_webhook_claim ON webhook_jobs(status, scheduled_at, priority DESC, created_at ASC);")

            # External side-effect idempotency log
            conn.execute("""
                CREATE TABLE IF NOT EXISTS external_operations_log (
                    operation_key TEXT PRIMARY KEY,
                    operation_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_ext_ops_target
                ON external_operations_log(target_id, operation_type);
            """)
            conn.commit()

    def record_external_operation(
        self,
        operation_key: str,
        operation_type: str,
        target_id: str,
        payload: Any,
        status: str = "SUCCESS",
        result: Any = None
    ) -> bool:
        """
        Record an external side-effect operation in SQLite WAL with unique constraint on operation_key.
        Returns True if newly recorded, False if duplicate operation_key exists (idempotency hit).
        """
        now = time.time()
        payload_bytes = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        payload_hash = hashlib.sha256(payload_bytes).hexdigest()
        result_json = json.dumps(result, default=str) if result is not None else None

        with self._get_connection() as conn:
            try:
                conn.execute("""
                    INSERT INTO external_operations_log (
                        operation_key, operation_type, target_id, payload_hash,
                        status, result_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                """, (operation_key, operation_type, str(target_id), payload_hash, status, result_json, now, now))
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def get_external_operation(self, operation_key: str) -> Optional[Dict[str, Any]]:
        """Retrieve recorded external operation by stable operation key."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM external_operations_log WHERE operation_key = ?",
                (operation_key,)
            ).fetchone()
            if not row:
                return None
            res = dict(row)
            if res.get("result_json"):
                try:
                    res["result"] = json.loads(res["result_json"])
                except Exception:
                    res["result"] = res["result_json"]
            return res

    def is_operation_executed(self, operation_key: str) -> bool:
        """Check if an external side-effect operation has already been executed."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM external_operations_log WHERE operation_key = ? AND status = 'SUCCESS'",
                (operation_key,)
            ).fetchone()
            return row is not None

    def enqueue(
        self,
        pr_identifier: str,
        payload: Dict[str, Any],
        max_retries: int = 3,
        job_kind: str = "webhook",
        idempotency_key: Optional[str] = None,
        priority: int = 50,
        scheduled_at: Optional[float] = None,
    ) -> str:
        """
        Enqueue a new pull request review job into persistent storage.
        If idempotency_key is provided and an existing job matches, returns the existing job_id
        to prevent duplicate work and redundant token spend.
        """
        now = time.time()
        sched_time = now if scheduled_at is None else scheduled_at
        payload_str = json.dumps(payload)
        max_capacity = int(os.getenv("MAX_QUEUE_CAPACITY", "100"))

        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if idempotency_key:
                existing = conn.execute(
                    "SELECT job_id, status FROM webhook_jobs WHERE idempotency_key = ?",
                    (idempotency_key,)
                ).fetchone()
                if existing:
                    logger.info(
                        f"🔁 Duplicate delivery detected for idempotency_key '{idempotency_key}'; "
                        f"reusing existing job {existing['job_id']} (status={existing['status']})"
                    )
                    conn.commit()
                    return existing["job_id"]

            pending = conn.execute(
                "SELECT COUNT(*) FROM webhook_jobs WHERE status IN ('QUEUED','PROCESSING','RETRYING')"
            ).fetchone()[0]
            if pending >= max_capacity:
                raise ValueError("Review queue capacity reached")

            job_id = f"job_{uuid.uuid4().hex[:12]}"
            conn.execute(
                """
                INSERT INTO webhook_jobs (
                    job_id, pr_identifier, payload_json, status, attempts, max_retries,
                    created_at, updated_at, job_kind, idempotency_key, priority, scheduled_at,
                    current_stage, stage_progress
                )
                VALUES (?, ?, ?, 'QUEUED', 0, ?, ?, ?, ?, ?, ?, ?, 'QUEUED', 0.0)
                """,
                (job_id, pr_identifier, payload_str, max_retries, now, now, job_kind, idempotency_key, priority, sched_time)
            )
            conn.commit()

        # Notify any listening worker for 0ms event-driven wakeup
        for cb in self._on_enqueue_callbacks:
            try:
                cb()
            except Exception:
                pass

        logger.info(f"📥 Enqueued durable webhook job {job_id} (priority={priority}) for {pr_identifier}")
        return job_id

    def claim_next_job(
        self,
        lock_timeout_seconds: float = 300.0,
        worker_id: Optional[str] = None,
        lease_duration: float = 60.0,
        lease_seconds: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Atomically claim the next eligible job for processing using BEGIN IMMEDIATE.
        Locks against concurrent multi-process worker races and reclaims stale jobs.
        """
        if lease_seconds is not None:
            lease_duration = lease_seconds
        now = time.time()
        timeout_threshold = now - lock_timeout_seconds
        stale_heartbeat_threshold = now - 90.0
        w_id = worker_id or f"worker_{os.getpid()}"

        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT job_id, pr_identifier, payload_json, status, attempts, max_retries, job_kind, priority, current_stage, stage_progress
                FROM webhook_jobs
                WHERE (status = 'QUEUED' AND (scheduled_at IS NULL OR scheduled_at <= ?))
                   OR (status = 'RETRYING' AND (scheduled_at IS NULL OR scheduled_at <= ?))
                   OR (status = 'PROCESSING' AND (
                        (lease_expires_at IS NOT NULL AND lease_expires_at < ?)
                        OR (heartbeat_at IS NOT NULL AND heartbeat_at < ?)
                        OR (locked_at IS NOT NULL AND locked_at < ?)
                   ))
                ORDER BY priority DESC, created_at ASC
                LIMIT 1
                """,
                (now, now, now, stale_heartbeat_threshold, timeout_threshold)
            )
            row = cursor.fetchone()
            if not row:
                conn.commit()
                return None

            job_id = row["job_id"]
            new_attempts = row["attempts"] + 1
            lease_expires = now + lease_duration

            cursor.execute(
                """
                UPDATE webhook_jobs
                SET status = 'PROCESSING',
                    worker_id = ?,
                    attempts = ?,
                    locked_at = ?,
                    lease_expires_at = ?,
                    heartbeat_at = ?,
                    current_stage = 'PROCESSING',
                    stage_progress = 0.05,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (w_id, new_attempts, now, lease_expires, now, now, job_id)
            )
            conn.commit()

            return {
                "job_id": job_id,
                "pr_identifier": row["pr_identifier"],
                "payload": json.loads(row["payload_json"]),
                "attempts": new_attempts,
                "max_retries": row["max_retries"],
                "job_kind": row["job_kind"],
                "priority": row["priority"],
                "worker_id": w_id,
                "current_stage": "PROCESSING",
                "stage_progress": 0.05,
            }

    def heartbeat_job(
        self,
        job_id: str,
        worker_id: Optional[str] = None,
        current_stage: Optional[str] = None,
        stage_progress: Optional[float] = None,
        lease_duration: float = 60.0
    ) -> bool:
        """Pulse heartbeat to extend lease and report live stage progress."""
        now = time.time()
        new_lease = now + lease_duration
        with self._get_connection() as conn:
            cursor = conn.cursor()
            query = """
                UPDATE webhook_jobs
                SET heartbeat_at = ?,
                    lease_expires_at = ?,
                    updated_at = ?
            """
            params: List[Any] = [now, new_lease, now]
            if current_stage is not None:
                query += ", current_stage = ?"
                params.append(current_stage)
            if stage_progress is not None:
                query += ", stage_progress = ?"
                params.append(stage_progress)
            query += " WHERE job_id = ? AND status = 'PROCESSING'"
            params.append(job_id)
            if worker_id:
                query += " AND (worker_id = ? OR worker_id IS NULL)"
                params.append(worker_id)
            cursor.execute(query, tuple(params))
            conn.commit()
            return cursor.rowcount > 0

    def complete_job(self, job_id: str, result_json: Optional[str] = None):
        """Mark a job as successfully COMPLETED and persist review results."""
        now = time.time()
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE webhook_jobs
                SET status = 'COMPLETED',
                    locked_at = NULL,
                    lease_expires_at = NULL,
                    current_stage = 'COMPLETED',
                    stage_progress = 1.0,
                    updated_at = ?,
                    review_result_json = ?
                WHERE job_id = ?
                """,
                (now, result_json, job_id)
            )
            conn.commit()
        logger.info(f"✅ Webhook job {job_id} marked as COMPLETED.")

    def fail_job(
        self,
        job_id: str,
        error_message: str = "",
        error: Optional[str] = None,
        base_delay: Optional[float] = None,
        max_delay: float = 180.0,
        transient: Optional[bool] = None
    ):
        """
        Record job failure with Phase 5 error classification:
        Retry only transient network/5xx/429 errors; fail-fast immediately on 401/403/validation errors.
        """
        if error is not None:
            error_message = error
        now = time.time()
        configured_base_delay = base_delay if base_delay is not None else float(os.getenv("QUEUE_RETRY_DELAY", "0.0"))

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT attempts, max_retries FROM webhook_jobs WHERE job_id = ?",
                (job_id,)
            )
            row = cursor.fetchone()
            if not row:
                return

            attempts = row["attempts"]
            max_retries = row["max_retries"]

            # Evaluate error classification
            is_trans = transient if transient is not None else is_transient_error(error_message)

            if not is_trans:
                new_status = "FAILED"
                delay = 0.0
                logger.info(f"🚫 Fail-fast permanent error detected for job {job_id} ({error_message}); retry suppressed.")
            else:
                new_status = "RETRYING" if attempts < max_retries else "FAILED"
                delay = 0.0
                if new_status == "RETRYING" and configured_base_delay > 0:
                    import random
                    delay = min(configured_base_delay * (2 ** max(0, attempts - 1)), max_delay)
                    delay += random.uniform(0.1, 1.0)

            scheduled_at = now + delay

            cursor.execute(
                """
                UPDATE webhook_jobs
                SET status = ?,
                    last_error = ?,
                    locked_at = NULL,
                    lease_expires_at = NULL,
                    scheduled_at = ?,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (new_status, error_message, scheduled_at, now, job_id)
            )
            conn.commit()

        if new_status == "RETRYING":
            logger.warning(f"⚠️ Webhook job {job_id} failed with transient error (attempt {attempts}/{max_retries}); scheduled for RETRY in {delay:.1f}s.")
        else:
            logger.error(f"❌ Webhook job {job_id} marked as FAILED ({new_status}): {error_message}")

    def cancel_job(self, job_id: str, reason: str = "Cancelled by operator") -> bool:
        """Cancel an eligible job in QUEUED, RETRYING, or PROCESSING state."""
        now = time.time()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE webhook_jobs
                SET status = 'CANCELLED',
                    last_error = ?,
                    locked_at = NULL,
                    lease_expires_at = NULL,
                    updated_at = ?
                WHERE job_id = ? AND status IN ('QUEUED', 'RETRYING', 'PROCESSING')
                """,
                (reason, now, job_id)
            )
            count = cursor.rowcount
            conn.commit()
            if count > 0:
                logger.info(f"🚫 Webhook job {job_id} marked as CANCELLED.")
                return True
            return False

    def recover_orphan_jobs(self) -> int:
        """
        Recover jobs that were left in 'PROCESSING' state when the server crashed or restarted.
        Resets them to 'QUEUED' so they are not silently lost.
        """
        now = time.time()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE webhook_jobs
                SET status = 'QUEUED',
                    last_error = 'Recovered after ungraceful process restart mid-job',
                    locked_at = NULL,
                    lease_expires_at = NULL,
                    updated_at = ?
                WHERE status = 'PROCESSING'
                """,
                (now,)
            )
            count = cursor.rowcount
            conn.commit()

        if count > 0:
            logger.warning(f"🔄 Recovered {count} orphan/interrupted webhook job(s) after process restart.")
        return count

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve details of a single job including parsed review result."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT job_id, pr_identifier, status, attempts, max_retries, last_error,
                       created_at, updated_at, review_result_json, job_kind, worker_id,
                       priority, scheduled_at, current_stage, stage_progress
                FROM webhook_jobs
                WHERE job_id = ?
                """,
                (job_id,)
            )
            row = cursor.fetchone()
            if not row:
                return None
            res = dict(row)
            result_json = res.pop("review_result_json", None)
            res["result"] = json.loads(result_json) if result_json else None
            return res

    def list_jobs(self, status: Optional[str] = None, limit: Optional[int] = None, offset: int = 0) -> List[Dict[str, Any]]:
        """List jobs matching an optional status filter including parsed review result."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            query = """
                SELECT job_id, pr_identifier, status, attempts, max_retries, last_error,
                       created_at, updated_at, review_result_json, job_kind, worker_id,
                       priority, scheduled_at, current_stage, stage_progress
                FROM webhook_jobs
            """
            params: List[Any] = []
            if status:
                query += " WHERE status = ?"
                params.append(status)
            if limit is not None:
                query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
                params.extend([limit, offset])
            else:
                query += " ORDER BY created_at DESC"

            cursor.execute(query, tuple(params))
            rows = []
            for r in cursor.fetchall():
                d = dict(r)
                result_json = d.pop("review_result_json", None)
                d["result"] = json.loads(result_json) if result_json else None
                rows.append(d)
            return rows

    def get_queue_metrics(self) -> Dict[str, Any]:
        """Aggregate queue depth, active worker count, and processing telemetry."""
        now = time.time()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT status, COUNT(*) as cnt FROM webhook_jobs GROUP BY status")
            counts = {row["status"]: row["cnt"] for row in cursor.fetchall()}

            # Active workers: distinct worker_id with active heartbeat in last 60 seconds
            cursor.execute(
                """
                SELECT COUNT(DISTINCT worker_id) FROM webhook_jobs
                WHERE status = 'PROCESSING' AND heartbeat_at IS NOT NULL AND heartbeat_at >= ?
                """,
                (now - 60.0,)
            )
            active_workers = cursor.fetchone()[0] or 0

            # Average completion duration (updated_at - created_at)
            cursor.execute(
                """
                SELECT AVG(updated_at - created_at) FROM webhook_jobs
                WHERE status = 'COMPLETED'
                """
            )
            avg_duration = cursor.fetchone()[0] or 0.0

            return {
                "queued": counts.get("QUEUED", 0),
                "processing": counts.get("PROCESSING", 0),
                "retrying": counts.get("RETRYING", 0),
                "completed": counts.get("COMPLETED", 0),
                "failed": counts.get("FAILED", 0),
                "cancelled": counts.get("CANCELLED", 0),
                "total": sum(counts.values()),
                "active_workers": active_workers,
                "avg_duration_seconds": round(float(avg_duration), 2)
            }


class WebhookWorker:
    """
    Background worker that continuously pulls and processes jobs from WebhookJobQueue.
    Supports graceful shutdown, concurrency rate limiting, adaptive backoff, and automatic error handling.
    """

    def __init__(
        self,
        queue: WebhookJobQueue,
        handler: Optional[Callable[[Dict[str, Any]], None]] = None,
        poll_interval: float = 1.0,
        max_workers: Optional[int] = None,
        worker_id: Optional[str] = None,
    ):
        self.queue = queue
        self.handler = handler
        self.poll_interval = poll_interval
        concurrency = max_workers if max_workers is not None else int(os.getenv("MAX_CONCURRENT_REVIEWS", "4"))
        self.max_workers = concurrency
        self.worker_id = worker_id or f"worker_{uuid.uuid4().hex[:8]}"
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._wake_event = threading.Event()
        self._cancel_active = threading.Event()
        self._semaphore = threading.Semaphore(self.max_workers)
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix=f"WebhookWorkerPool-{self.worker_id}"
        )
        self.queue.add_enqueue_listener(self._wake_event.set)

    def start(self):
        """Start the background worker thread and recover any orphan jobs."""
        if self._running:
            return

        self.queue.recover_orphan_jobs()
        self._cancel_active.clear()
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name=f"WebhookWorkerThread-{self.worker_id}")
        self._thread.start()
        logger.info(f"👷 Webhook background worker {self.worker_id} started (concurrency={self.max_workers}).")

    def stop(self, timeout: float = 5.0):
        """Signal worker to stop and wait for active jobs to complete."""
        self._running = False
        self._cancel_active.set()
        self._wake_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._executor.shutdown(wait=False, cancel_futures=True)
        logger.info(f"🛑 Webhook background worker {self.worker_id} stopped.")

    def _run_loop(self):
        """Main worker polling loop with adaptive backoff and immediate event wakeups."""
        current_sleep = 0.2
        max_sleep = 5.0

        while self._running:
            try:
                if not self._semaphore.acquire(timeout=0.2):
                    continue
                try:
                    job = self.queue.claim_next_job(worker_id=self.worker_id)
                    if job:
                        current_sleep = 0.2
                        self._executor.submit(self._process_claimed_job, job)
                    else:
                        self._semaphore.release()
                except Exception:
                    self._semaphore.release()
                    raise
                if not job:
                    self._wake_event.wait(timeout=min(current_sleep, self.poll_interval))
                    self._wake_event.clear()
                    current_sleep = min(current_sleep * 1.5, max_sleep)
            except Exception as e:
                logger.error(f"Unexpected error in WebhookWorker loop: {e}", exc_info=True)
                self._wake_event.wait(timeout=self.poll_interval)
                self._wake_event.clear()

    def _process_claimed_job(self, job: Dict[str, Any]):
        """Execute review handler on a claimed job and record completion or failure."""
        job_id = job["job_id"]
        pr_id = job["pr_identifier"]
        stop_heartbeat = threading.Event()

        def _heartbeat_loop():
            while not stop_heartbeat.wait(15.0):
                try:
                    self.queue.heartbeat_job(job_id, worker_id=self.worker_id)
                except Exception:
                    pass

        hb_thread = threading.Thread(target=_heartbeat_loop, daemon=True, name=f"Heartbeat-{job_id}")
        hb_thread.start()

        try:
            logger.info(f"⚙️ Worker {self.worker_id} executing review for job {job_id} ({pr_id})")
            self.queue.heartbeat_job(job_id, worker_id=self.worker_id, current_stage="INGESTION", stage_progress=0.15)
            result = None
            if self.handler:
                result = self.handler(job["payload"])
            else:
                from code_review_agent.review_executor import run_job, decode_inputs
                self.queue.heartbeat_job(job_id, worker_id=self.worker_id, current_stage="REVIEW", stage_progress=0.45)
                if job.get("job_kind") == "browser":
                    result = run_job("review", decode_inputs(job["payload"]), cancel_event=self._cancel_active)
                else:
                    result = run_job("webhook", {"payload": job["payload"], "pr_identifier": pr_id}, cancel_event=self._cancel_active)

            self.queue.heartbeat_job(job_id, worker_id=self.worker_id, current_stage="SYNTHESIS", stage_progress=0.90)

            result_str = None
            if result is not None:
                if isinstance(result, (dict, list)):
                    result_str = json.dumps(result, default=str)
                elif hasattr(result, "model_dump_json"):
                    result_str = result.model_dump_json()
                elif hasattr(result, "dict"):
                    result_str = json.dumps(result.dict(), default=str)
                else:
                    result_str = str(result)

            self.queue.complete_job(job_id, result_json=result_str)
        except Exception as e:
            logger.error(f"❌ Error processing job {job_id}: {e}", exc_info=True)
            self.queue.fail_job(job_id, str(e))
        finally:
            stop_heartbeat.set()
            hb_thread.join(timeout=1.0)
            self._semaphore.release()

    def _default_execute_review(self, payload, pr_identifier):
        return execute_platform_review(payload, pr_identifier)


def execute_platform_review(payload: Dict[str, Any], pr_identifier: str) -> Dict[str, Any]:
    """
    Platform-agnostic review handler.

    Resolves the platform (GitHub / GitLab / Bitbucket / local) from the enqueued
    identifier, tracks commit statuses & check runs, checks persistent finding suppressions,
    performs bidirectional comment synchronization, and posts final verdicts.
    """
    import urllib.parse
    from contextlib import ExitStack
    from code_review_agent.review_service import ReviewService
    from code_review_agent.platform.factory import get_platform_client
    from code_review_agent.bot.checkout import temporary_pr_checkout
    from code_review_agent.suppression_store import SuppressionStore
    from code_review_agent.comment_synchronizer import CommentSynchronizer

    client, ident = get_platform_client(pr_identifier)
    owner, repo, pull_number = ident.owner_or_project, ident.repo_or_slug, ident.pr_id

    diff = client.fetch_pull_request_diff(owner, repo, pull_number)
    if not diff or not diff.strip():
        raise ValueError(f"No diff content available for {pr_identifier}")
    meta = client.fetch_pull_request_metadata(owner, repo, pull_number)

    # 1. Update initial status check / check-run to in_progress / pending
    if meta.head_sha:
        try:
            client.create_or_update_check_run(
                owner=owner,
                repo=repo,
                head_sha=meta.head_sha,
                name="AI Code Review",
                status="in_progress",
                title="AI Code Review in Progress",
                summary="Multi-agent security, quality, and architecture analysis underway..."
            )
        except Exception as e:
            logger.warning(f"Could not post initial commit status/check-run for {pr_identifier}: {e}")

    # 2. Run multi-agent review
    with ExitStack() as stack:
        if ident.platform == "local":
            repo_root = str(getattr(client, "repo_dir", "."))
        else:
            host = urllib.parse.urlparse(ident.raw_identifier).netloc or None if "://" in ident.raw_identifier else None
            repo_root = stack.enter_context(
                temporary_pr_checkout(
                    owner, repo,
                    head_sha=meta.head_sha, head_ref=meta.head_ref,
                    platform=ident.platform, host=host,
                )
            )
        # bypass_limits: trusted internal path — no per-IP throttling or paste-size cap.
        response = ReviewService.execute_review(raw_diff=diff, repo_root=repo_root, bypass_limits=True)

    # 3. Load active suppressions for repository
    suppression_store = SuppressionStore()
    repo_key = f"{owner}/{repo}"
    suppressed_fps = suppression_store.get_suppressed_fingerprints(repo_key)

    # 4. Synchronize comments, deduplicate, and auto-resolve fixed findings
    deduped_comments, resolved_fps, _ = CommentSynchronizer.synchronize(
        client=client,
        owner=owner,
        repo=repo,
        pull_number=pull_number,
        current_comments=response.inline_comments or [],
        current_findings=response.findings or [],
        head_sha=meta.head_sha,
        suppressed_fingerprints=suppressed_fps
    )

    # 5. Determine verdict (account for suppressed blocking findings)
    verdict = (getattr(response, "verdict", None) or "").upper()
    if "APPROVE" in verdict:
        event = "APPROVE"
    elif "REQUEST" in verdict or "ESCALATE" in verdict:
        raw_findings = getattr(response, "findings", None) or []
        blocking_findings = [
            f for f in raw_findings
            if getattr(f, "severity", "") in ("CRITICAL", "HIGH")
        ]
        if blocking_findings and suppressed_fps and all(
            (getattr(f, "fingerprint", "") or "").lower() in suppressed_fps
            for f in blocking_findings
        ):
            # Phase 5 Severity Ceilings: CRITICAL findings require explicit override to auto-approve
            critical_blockers = [f for f in blocking_findings if getattr(f, "severity", "") == "CRITICAL"]
            all_critical_overridden = all(
                suppression_store.has_critical_override(repo_key, getattr(f, "fingerprint", ""))
                for f in critical_blockers
            )
            if not critical_blockers or all_critical_overridden:
                event = "APPROVE"
                response.verdict = "APPROVE"
            else:
                event = "REQUEST_CHANGES"
                logger.warning(
                    f"⚠️ PR {pr_identifier} contains suppressed CRITICAL findings without explicit override; "
                    f"severity ceiling prevents auto-approval."
                )
        else:
            event = "REQUEST_CHANGES"
    else:
        event = "COMMENT"

    # 6. Post review report with deduplicated comments
    try:
        client.post_pull_request_review(
            owner=owner, repo=repo, pull_number=pull_number,
            event=event, body=response.full_report,
            commit_id=meta.head_sha or None, comments=deduped_comments,
        )
    except Exception as post_err:
        logger.error(f"Could not post review for {pr_identifier}: {post_err}")

    # 7. Update final commit status / check-run
    if meta.head_sha:
        conclusion = "success" if event == "APPROVE" else "failure"
        annotations = [
            {
                "path": f.file_path,
                "start_line": f.line_number,
                "end_line": f.line_number,
                "annotation_level": "failure" if f.severity in ("CRITICAL", "HIGH") else "warning",
                "title": f.rule_id or f.name,
                "message": f.description
            }
            for f in (response.findings or [])
            if (f.fingerprint or "").lower() not in suppressed_fps
        ]
        try:
            client.create_or_update_check_run(
                owner=owner,
                repo=repo,
                head_sha=meta.head_sha,
                name="AI Code Review",
                status="completed",
                conclusion=conclusion,
                title=f"AI Code Review: {response.verdict}",
                summary=f"Analysis completed with verdict **{response.verdict}** ({len(response.findings)} findings, {len(deduped_comments)} new comments).",
                annotations=annotations
            )
        except Exception as e:
            logger.warning(f"Could not post final commit status/check-run for {pr_identifier}: {e}")

    return response.model_dump()
