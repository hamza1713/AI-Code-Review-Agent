import concurrent.futures
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from code_review_agent.config import logger
from code_review_agent.tenancy import get_current_tenant, normalize_tenant_id


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
        r"unsafe\s*diff\s*path",
        r"superseded\s*review",
        r"guardrail",
        r"schema\s*error",
        r"not\s*found",
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
        r"network\s*timeout",
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

    def __init__(
        self, db_path: str | None = None, *, _migration_lock_held: bool = False
    ):
        target = db_path or os.getenv("QUEUE_DB_PATH") or "webhook_jobs.db"
        self.db_path = Path(target).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._on_enqueue_callbacks: list[Callable[[], None]] = []
        if self._queue_migration_needed() and not _migration_lock_held:
            from code_review_agent.operations import backup, file_lock

            with file_lock(str(self.db_path) + ".migration.lock"):
                if self._queue_migration_needed():
                    backup(
                        self.db_path,
                        self.db_path.parent
                        / "backups"
                        / f"pre-queue-migration-{time.time_ns()}.db",
                    )
                self._init_db()
        else:
            self._init_db()

    def _queue_migration_needed(self) -> bool:
        if not self.db_path.exists():
            return False
        from contextlib import closing

        with closing(
            sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True)
        ) as conn:
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='webhook_jobs'"
            ).fetchone():
                return False
            versions = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='schema_versions'"
            ).fetchone()
            version = (
                conn.execute(
                    "SELECT version FROM schema_versions WHERE component='queue'"
                ).fetchone()
                if versions
                else None
            )
            return not version or version[0] < 2

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
            conn.execute("PRAGMA synchronous=FULL;")
            conn.execute("PRAGMA busy_timeout = 30000;")
            self._local.conn = conn
        return conn

    def _init_db(self):
        """Initialize SQLite schema if it does not exist."""
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_versions (component TEXT PRIMARY KEY, version INTEGER NOT NULL)"
            )
            version = conn.execute(
                "SELECT version FROM schema_versions WHERE component='queue'"
            ).fetchone()
            if version and version[0] > 2:
                raise RuntimeError("Queue schema is incompatible with this image")
            conn.execute("INSERT OR IGNORE INTO schema_versions VALUES ('queue',1)")
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
                conn.execute(
                    "ALTER TABLE webhook_jobs ADD COLUMN review_result_json TEXT;"
                )
            if "job_kind" not in columns:
                conn.execute(
                    "ALTER TABLE webhook_jobs ADD COLUMN job_kind TEXT NOT NULL DEFAULT 'webhook';"
                )
            if "idempotency_key" not in columns:
                conn.execute(
                    "ALTER TABLE webhook_jobs ADD COLUMN idempotency_key TEXT;"
                )
            if "worker_id" not in columns:
                conn.execute("ALTER TABLE webhook_jobs ADD COLUMN worker_id TEXT;")
            if "lease_expires_at" not in columns:
                conn.execute(
                    "ALTER TABLE webhook_jobs ADD COLUMN lease_expires_at REAL;"
                )
            if "heartbeat_at" not in columns:
                conn.execute("ALTER TABLE webhook_jobs ADD COLUMN heartbeat_at REAL;")
            if "claim_token" not in columns:
                conn.execute("ALTER TABLE webhook_jobs ADD COLUMN claim_token TEXT")
            if "priority" not in columns:
                conn.execute(
                    "ALTER TABLE webhook_jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 50;"
                )
            if "scheduled_at" not in columns:
                conn.execute("ALTER TABLE webhook_jobs ADD COLUMN scheduled_at REAL;")
            if "current_stage" not in columns:
                conn.execute("ALTER TABLE webhook_jobs ADD COLUMN current_stage TEXT;")
            if "stage_progress" not in columns:
                conn.execute(
                    "ALTER TABLE webhook_jobs ADD COLUMN stage_progress REAL NOT NULL DEFAULT 0.0;"
                )
            if "tenant_id" not in columns:
                conn.execute(
                    "ALTER TABLE webhook_jobs ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';"
                )

            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_webhook_idempotency ON webhook_jobs(idempotency_key);"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_webhook_status ON webhook_jobs(status, created_at);"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_webhook_tenant ON webhook_jobs(tenant_id, status, created_at);"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_webhook_claim ON webhook_jobs(status, scheduled_at, priority DESC, created_at ASC);"
            )

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
            conn.execute("""CREATE TABLE IF NOT EXISTS external_operation_jobs (
                operation_key TEXT NOT NULL, job_id TEXT NOT NULL,
                PRIMARY KEY (operation_key, job_id))""")
            conn.execute("UPDATE schema_versions SET version=2 WHERE component='queue'")
            conn.commit()

    def record_external_operation(
        self,
        operation_key: str,
        operation_type: str,
        target_id: str,
        payload: Any,
        status: str = "SUCCESS",
        result: Any = None,
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
                conn.execute(
                    """
                    INSERT INTO external_operations_log (
                        operation_key, operation_type, target_id, payload_hash,
                        status, result_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                """,
                    (
                        operation_key,
                        operation_type,
                        str(target_id),
                        payload_hash,
                        status,
                        result_json,
                        now,
                        now,
                    ),
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def get_external_operation(self, operation_key: str) -> dict[str, Any] | None:
        """Retrieve recorded external operation by stable operation key."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM external_operations_log WHERE operation_key = ?",
                (operation_key,),
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
                (operation_key,),
            ).fetchone()
            return row is not None

    def enqueue(
        self,
        pr_identifier: str,
        payload: dict[str, Any],
        max_retries: int = 3,
        job_kind: str = "webhook",
        idempotency_key: str | None = None,
        priority: int = 50,
        scheduled_at: float | None = None,
        tenant_id: str | None = None,
    ) -> str:
        """
        Enqueue a new pull request review job into persistent storage.
        If idempotency_key is provided and an existing job matches, returns the existing job_id
        to prevent duplicate work and redundant token spend.
        """
        if max_retries < 1:
            raise ValueError("At least one attempt is required")
        now = time.time()
        sched_time = now if scheduled_at is None else scheduled_at
        payload_str = json.dumps(payload)
        max_capacity = int(os.getenv("MAX_QUEUE_CAPACITY", "100"))

        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if idempotency_key:
                existing = conn.execute(
                    "SELECT job_id, status FROM webhook_jobs WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing:
                    logger.info(
                        f"🔁 Duplicate delivery detected for idempotency_key '{idempotency_key}'; "
                        f"reusing existing job {existing['job_id']} (status={existing['status']})"
                    )
                    conn.commit()
                    return existing["job_id"]

            pending = conn.execute(
                "SELECT COUNT(*) FROM webhook_jobs WHERE status IN ('QUEUED','RETRYING')"
            ).fetchone()[0]
            if pending >= max_capacity:
                raise ValueError("Review queue capacity reached")

            job_id = f"job_{uuid.uuid4().hex[:12]}"
            active_tenant = normalize_tenant_id(tenant_id or get_current_tenant())
            conn.execute(
                """
                INSERT INTO webhook_jobs (
                    job_id, pr_identifier, payload_json, status, attempts, max_retries,
                    created_at, updated_at, job_kind, idempotency_key, priority, scheduled_at,
                    current_stage, stage_progress, tenant_id
                )
                VALUES (?, ?, ?, 'QUEUED', 0, ?, ?, ?, ?, ?, ?, ?, 'QUEUED', 0.0, ?)
                """,
                (
                    job_id,
                    pr_identifier,
                    payload_str,
                    max_retries,
                    now,
                    now,
                    job_kind,
                    idempotency_key,
                    priority,
                    sched_time,
                    active_tenant,
                ),
            )
            conn.commit()

        # Notify any listening worker for 0ms event-driven wakeup
        for cb in self._on_enqueue_callbacks:
            try:
                cb()
            except Exception:
                pass

        logger.info(
            f"📥 Enqueued durable webhook job {job_id} (priority={priority}) for {pr_identifier}"
        )
        return job_id

    def claim_next_job(
        self,
        lock_timeout_seconds=300.0,
        worker_id=None,
        lease_duration=60.0,
        lease_seconds=None,
    ):
        self.recover_orphan_jobs()
        now = time.time()
        token = uuid.uuid4().hex
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT * FROM webhook_jobs
                WHERE status IN ('QUEUED','RETRYING') AND attempts < max_retries
                AND (scheduled_at IS NULL OR scheduled_at<=?)
                ORDER BY priority DESC, created_at ASC LIMIT 1""",
                (now,),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                """UPDATE webhook_jobs SET status='PROCESSING', attempts=attempts+1,
                worker_id=?, claim_token=?, locked_at=?, heartbeat_at=?, lease_expires_at=?,
                updated_at=?, current_stage='PROCESSING', stage_progress=0.05 WHERE job_id=?""",
                (
                    worker_id,
                    token,
                    now,
                    now,
                    now + (lease_seconds or lease_duration),
                    now,
                    row["job_id"],
                ),
            )
        job = dict(row)
        job.update(
            payload=json.loads(job.pop("payload_json")),
            claim_token=token,
            worker_id=worker_id,
            attempts=row["attempts"] + 1,
            status="PROCESSING",
        )
        return job

    def assert_claim(self, job_id, claim_token):
        if not job_id or not claim_token:
            raise PermissionError("Provider writes require an active queued job claim")
        row = (
            self._get_connection()
            .execute(
                """SELECT 1 FROM webhook_jobs WHERE job_id=?
            AND claim_token=? AND status='PROCESSING' AND lease_expires_at>?""",
                (job_id, claim_token, time.time()),
            )
            .fetchone()
        )
        if not row:
            raise PermissionError("Job claim expired, cancelled, or replaced")

    def bind_job_head(self, job_id, claim_token, head_sha):
        """Persist the analyzed head before any write, retaining it across retries."""
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self.assert_claim(job_id, claim_token)
            row = conn.execute(
                "SELECT payload_json FROM webhook_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            payload = json.loads(row[0])
            bound = payload.get("_analyzed_head")
            if not bound:
                payload["_analyzed_head"] = head_sha
                conn.execute(
                    "UPDATE webhook_jobs SET payload_json=? WHERE job_id=?",
                    (json.dumps(payload), job_id),
                )
            return bound or head_sha

    def heartbeat_job(
        self,
        job_id,
        worker_id=None,
        current_stage=None,
        stage_progress=None,
        lease_duration=60.0,
        *,
        claim_token=None,
    ):
        if not claim_token:
            return False
        now = time.time()
        with self._get_connection() as conn:
            cursor = conn.execute(
                """UPDATE webhook_jobs SET heartbeat_at=?,lease_expires_at=?,updated_at=?,
                current_stage=COALESCE(?,current_stage),stage_progress=COALESCE(?,stage_progress)
                WHERE job_id=? AND claim_token=? AND status='PROCESSING' AND lease_expires_at>?""",
                (
                    now,
                    now + lease_duration,
                    now,
                    current_stage,
                    stage_progress,
                    job_id,
                    claim_token,
                    now,
                ),
            )
            return cursor.rowcount == 1

    def complete_job(self, job_id, result_json=None, *, claim_token=None):
        with self._get_connection() as conn:
            cursor = conn.execute(
                """UPDATE webhook_jobs SET status='COMPLETED',locked_at=NULL,
                lease_expires_at=NULL,current_stage='COMPLETED',stage_progress=1,updated_at=?,
                review_result_json=? WHERE job_id=? AND claim_token=? AND status='PROCESSING'
                AND lease_expires_at>?""",
                (time.time(), result_json, job_id, claim_token, time.time()),
            )
            return cursor.rowcount == 1

    def fail_job(
        self,
        job_id,
        error_message="",
        error=None,
        base_delay=None,
        max_delay=180.0,
        transient=None,
        *,
        claim_token=None,
        state=None,
    ):
        message = str(error if error is not None else error_message)
        now = time.time()
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT * FROM webhook_jobs WHERE job_id=? AND claim_token=?
                AND status='PROCESSING' AND lease_expires_at>?""",
                (job_id, claim_token, now),
            ).fetchone()
            if not row:
                return False
            retryable = is_transient_error(message) if transient is None else transient
            status = state or (
                "RETRYING"
                if retryable and row["attempts"] < row["max_retries"]
                else "FAILED"
            )
            delay = min(
                (base_delay if base_delay is not None else 5)
                * 2 ** (row["attempts"] - 1),
                max_delay,
            )
            conn.execute(
                """UPDATE webhook_jobs SET status=?,last_error=?,locked_at=NULL,
                lease_expires_at=NULL,scheduled_at=?,updated_at=? WHERE job_id=? AND claim_token=?""",
                (status, message, now + delay, now, job_id, claim_token),
            )
            return True

    def cancel_job(
        self,
        job_id: str,
        reason: str = "Cancelled by operator",
        tenant_id: str | None = None,
    ) -> bool:
        """Cancel an eligible job in QUEUED, RETRYING, or PROCESSING state with tenant isolation."""
        now = time.time()
        query = """
            UPDATE webhook_jobs
            SET status = 'CANCELLED',
                last_error = ?,
                locked_at = NULL,
                lease_expires_at = NULL,
                updated_at = ?
            WHERE job_id = ? AND status IN ('QUEUED', 'RETRYING', 'PROCESSING')
        """
        params: list[Any] = [reason, now, job_id]
        if tenant_id and tenant_id != "*":
            query += " AND tenant_id = ?"
            params.append(normalize_tenant_id(tenant_id))

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            count = cursor.rowcount
            conn.commit()
            if count > 0:
                logger.info(f"🚫 Webhook job {job_id} marked as CANCELLED.")
                return True
            return False

    def recover_orphan_jobs(self):
        now = time.time()
        with self._get_connection() as conn:
            cursor = conn.execute(
                """UPDATE webhook_jobs SET
                status=CASE WHEN attempts>=max_retries THEN 'FAILED' ELSE 'RETRYING' END,
                last_error='Expired worker lease',locked_at=NULL,lease_expires_at=NULL,claim_token=NULL,
                scheduled_at=?,updated_at=? WHERE status='PROCESSING'
                AND COALESCE(lease_expires_at,locked_at+300,updated_at+300) <= ?""",
                (now, now, now),
            )
            return cursor.rowcount

    def get_job(self, job_id: str, tenant_id: str | None = None) -> dict[str, Any] | None:
        """Retrieve details of a single job including parsed review result with optional tenant isolation."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT job_id, pr_identifier, status, attempts, max_retries, last_error,
                       created_at, updated_at, review_result_json, job_kind, worker_id,
                       priority, scheduled_at, current_stage, stage_progress, tenant_id
                FROM webhook_jobs
                WHERE job_id = ?
                """,
                (job_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            res = dict(row)
            if tenant_id and tenant_id != "*":
                if res.get("tenant_id") and res["tenant_id"] != normalize_tenant_id(tenant_id):
                    return None
            result_json = res.pop("review_result_json", None)
            res["result"] = json.loads(result_json) if result_json else None
            return res

    def list_jobs(
        self,
        status: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List jobs matching an optional status filter and tenant namespace."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            query = """
                SELECT job_id, pr_identifier, status, attempts, max_retries, last_error,
                       created_at, updated_at, review_result_json, job_kind, worker_id,
                       priority, scheduled_at, current_stage, stage_progress, tenant_id
                FROM webhook_jobs
            """
            params: list[Any] = []
            where_clauses: list[str] = []
            if status:
                where_clauses.append("status = ?")
                params.append(status)
            if tenant_id and tenant_id != "*":
                where_clauses.append("tenant_id = ?")
                params.append(normalize_tenant_id(tenant_id))
            if where_clauses:
                query += " WHERE " + " AND ".join(where_clauses)
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

    def get_queue_metrics(self) -> dict[str, Any]:
        """Aggregate queue depth, active worker count, and processing telemetry."""
        now = time.time()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT status, COUNT(*) as cnt FROM webhook_jobs GROUP BY status"
            )
            counts = {row["status"]: row["cnt"] for row in cursor.fetchall()}

            # Active workers: distinct worker_id with active heartbeat in last 60 seconds
            cursor.execute(
                """
                SELECT COUNT(*) FROM webhook_jobs
                WHERE status = 'PROCESSING' AND heartbeat_at IS NOT NULL AND heartbeat_at >= ?
                """,
                (now - 60.0,),
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
                "avg_duration_seconds": round(float(avg_duration), 2),
            }


class WebhookWorker:
    """
    Background worker that continuously pulls and processes jobs from WebhookJobQueue.
    Supports graceful shutdown, concurrency rate limiting, adaptive backoff, and automatic error handling.
    """

    def __init__(
        self,
        queue: WebhookJobQueue,
        handler: Callable[[dict[str, Any]], None] | None = None,
        poll_interval: float = 1.0,
        max_workers: int | None = None,
        worker_id: str | None = None,
    ):
        self.queue = queue
        self.handler = handler
        self.poll_interval = poll_interval
        concurrency = (
            max_workers
            if max_workers is not None
            else int(os.getenv("MAX_CONCURRENT_REVIEWS", "2"))
        )
        self.max_workers = concurrency
        self.worker_id = worker_id or f"worker_{uuid.uuid4().hex[:8]}"
        self._running = False
        self.last_loop_at = time.monotonic()
        self.database_errors = 0
        self._thread: threading.Thread | None = None
        self._wake_event = threading.Event()
        self._cancel_active = threading.Event()
        self._semaphore = threading.Semaphore(self.max_workers)
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix=f"WebhookWorkerPool-{self.worker_id}",
        )
        self.queue.add_enqueue_listener(self._wake_event.set)

    def start(self):
        """Start the background worker thread and recover any orphan jobs."""
        if self._running:
            return

        self.queue.recover_orphan_jobs()
        self._cancel_active.clear()
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name=f"WebhookWorkerThread-{self.worker_id}",
        )
        self._thread.start()
        logger.info(
            f"👷 Webhook background worker {self.worker_id} started (concurrency={self.max_workers})."
        )

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
            self.last_loop_at = time.monotonic()
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
                    self._wake_event.wait(
                        timeout=min(current_sleep, self.poll_interval)
                    )
                    self._wake_event.clear()
                    current_sleep = min(current_sleep * 1.5, max_sleep)
            except Exception as e:
                if isinstance(e, sqlite3.Error):
                    self.database_errors += 1
                logger.error(
                    f"Unexpected error in WebhookWorker loop: {e}", exc_info=True
                )
                self._wake_event.wait(timeout=self.poll_interval)
                self._wake_event.clear()

    def _process_claimed_job(self, job):
        job_id, token = job["job_id"], job["claim_token"]
        stopped, cancelled = threading.Event(), threading.Event()

        def heartbeat():
            while not stopped.wait(0.5):
                try:
                    if self._cancel_active.is_set() or not self.queue.heartbeat_job(
                        job_id, claim_token=token
                    ):
                        cancelled.set()
                        return
                except Exception:
                    cancelled.set()
                    return

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        from code_review_agent.tenancy import set_current_tenant
        set_current_tenant(job.get("tenant_id") or "default")
        try:
            if self.handler:
                result = self.handler(job["payload"])
            else:
                from code_review_agent.review_executor import decode_inputs, run_job

                kind = job.get("job_kind")
                if kind == "browser":
                    operation, payload = "review", decode_inputs(job["payload"])
                elif kind in ("command", "merge"):
                    operation, payload = (
                        ("bot" if kind == "command" else "merge"),
                        job["payload"],
                    )
                    payload["_job_claim"] = {"job_id": job_id, "claim_token": token}
                else:
                    operation, payload = (
                        "webhook",
                        {
                            "payload": job["payload"],
                            "pr_identifier": job["pr_identifier"],
                            "job_id": job_id,
                            "claim_token": token,
                        },
                    )
                result = run_job(
                    operation,
                    payload,
                    cancel_event=cancelled,
                    timeout=float(os.getenv("REVIEW_JOB_TIMEOUT", "900")),
                )
            if hasattr(result, "model_dump"):
                result = result.model_dump()
            self.queue.complete_job(
                job_id, json.dumps(result, default=str), claim_token=token
            )
        except Exception as error:
            state = (
                "RECONCILIATION_REQUIRED"
                if "RECONCILIATION_REQUIRED" in str(error)
                else None
            )
            self.queue.fail_job(
                job_id,
                str(error),
                claim_token=token,
                state=state,
                transient=is_transient_error(error),
            )
        finally:
            stopped.set()
            thread.join(timeout=1)
            self._semaphore.release()

    def _default_execute_review(self, payload, pr_identifier, *, job_id, claim_token):
        return execute_platform_review(
            payload, pr_identifier, job_id=job_id, claim_token=claim_token
        )


def execute_platform_review(payload, pr_identifier, job_id=None, claim_token=None):
    from contextlib import ExitStack

    from code_review_agent.access_policy import validate_pr_target
    from code_review_agent.bot.checkout import temporary_pr_checkout
    from code_review_agent.calibration import FindingOutcomeStore
    from code_review_agent.data_policy import production, repository_scope
    from code_review_agent.diff_parser import DiffParser
    from code_review_agent.pilot import PilotStore
    from code_review_agent.publication import publish
    from code_review_agent.review_service import ReviewService
    from code_review_agent.suppression_store import SuppressionStore

    queue = WebhookJobQueue()
    if os.getenv("REVIEW_ENABLE_POSTING", "false").lower() == "true":
        queue.assert_claim(job_id, claim_token)

    client, ident, host = validate_pr_target(pr_identifier)
    owner, repo, number = ident.owner_or_project, ident.repo_or_slug, ident.pr_id
    meta = client.fetch_pull_request_metadata(owner, repo, number)
    if not meta.head_sha:
        raise ValueError("PR has no immutable head commit")
    if job_id:
        bound_head = queue.bind_job_head(job_id, claim_token, meta.head_sha)
        if bound_head != meta.head_sha:
            from code_review_agent.publication import ReconciliationRequired

            # Retain pending intents at their original head. They must never be
            # silently abandoned while a retry publishes a different revision.
            pending = (
                queue._get_connection()
                .execute(
                    """SELECT 1 FROM external_operations_log o
                JOIN external_operation_jobs j USING(operation_key)
                WHERE j.job_id=? AND o.status<>'SUCCESS' """,
                    (job_id,),
                )
                .fetchone()
            )
            if pending:
                raise ReconciliationRequired(
                    "RECONCILIATION_REQUIRED: PR head changed after a write intent"
                )
            raise ValueError(
                "Superseded review: PR head changed since this job analyzed it"
            )
    diff = client.fetch_pull_request_diff(owner, repo, number)
    DiffParser.parse_diff(diff)
    if (
        client.fetch_pull_request_metadata(owner, repo, number).head_sha
        != meta.head_sha
    ):
        raise ValueError("PR head changed while retrieving diff")
    repo_key = f"{owner}/{repo}"
    publication_target = f"{ident.platform}:{host}:{repo_key}#{number}"
    key = f"review:{ident.platform}:{host}:{repo_key}:{number}:{meta.head_sha}"
    queue = WebhookJobQueue()
    prior = queue.get_external_operation(key)
    original_intent = (prior.get("result") or {}).get("intent", {}) if prior else {}
    saved = original_intent.get("response")
    if saved:
        from code_review_agent.models import ReviewAPIResponse

        try:
            response = ReviewAPIResponse.model_validate(saved)
        except ValueError as error:
            from code_review_agent.publication import ReconciliationRequired

            raise ReconciliationRequired(
                "RECONCILIATION_REQUIRED: saved review intent is invalid"
            ) from error
    else:
        with ExitStack() as stack:
            stack.enter_context(repository_scope(f"{ident.platform}:{host}/{repo_key}"))
            root = stack.enter_context(
                temporary_pr_checkout(
                    owner,
                    repo,
                    head_sha=meta.head_sha,
                    head_ref=meta.head_ref,
                    platform=ident.platform,
                    host=host,
                )
            )
            from code_review_agent.source_snapshot import (
                build_snapshot,
                verified_checkout,
            )

            source_repo = meta.head_repository or repo_key
            source_owner, source_name = source_repo.split("/", 1)
            fetch = (
                (
                    lambda path: client.fetch_source_file(
                        source_owner, source_name, path, meta.head_sha
                    )
                )
                if ident.platform == "github"
                else None
            )
            snapshot = build_snapshot(
                diff,
                meta.head_sha,
                f"{ident.platform}:{host}/{repo_key}",
                root=root,
                fetch=fetch,
            )
            response = ReviewService.execute_review(
                raw_diff=diff,
                repo_root=root if verified_checkout(root, meta.head_sha) else None,
                bypass_limits=True,
                input_snapshot=snapshot.model_dump(),
            )
    if saved and (
        production() or os.getenv("REVIEW_ENABLE_POSTING", "false").lower() == "true"
    ):
        from code_review_agent.reviewer_profile import same_implementation

        if not response.reviewer_profile or (
            production() and not same_implementation(response.reviewer_profile)
        ):
            from code_review_agent.publication import ReconciliationRequired

            raise ReconciliationRequired(
                "RECONCILIATION_REQUIRED: saved review belongs to another reviewer"
            )
    response.analyzed_commit = meta.head_sha
    repo_key = f"{owner}/{repo}"
    suppressions = SuppressionStore()
    suppressed = suppressions.get_suppressed_fingerprints(repo_key)
    probation_rules = set(suppressions.get_probation_rules(repo_key).keys())
    from code_review_agent.eval.runtime import validate_response
    from code_review_agent.review_policy import advisory_summary, apply_finding_policy

    validate_response(response)
    apply_finding_policy(
        response,
        suppressed_fingerprints=suppressed,
        probation_rules=probation_rules,
    )
    event = "COMMENT"
    pilot = PilotStore(str(queue.db_path))
    pilot_repository = f"{ident.platform}:{host}/{repo_key}"
    shadow_mode = os.getenv("SHADOW_MODE", "false").lower() == "true"
    posting = (not shadow_mode) and (
        os.getenv("REVIEW_ENABLE_POSTING", "false").lower() == "true"
    )
    pilot_run = pilot.record(
        pilot_repository,
        str(number),
        response,
        "ADVISORY" if posting else "SHADOW",
        activate=not bool(saved),
    )
    if saved and original_intent.get("event") != "COMMENT":
        from code_review_agent.publication import ReconciliationRequired

        raise ReconciliationRequired(
            "RECONCILIATION_REQUIRED: legacy non-advisory intent requires operator review"
        )
    if shadow_mode:
        response.analyzer_status["publication"] = "SHADOW_RECORDED"
        return response.model_dump()
    if not posting:
        response.analyzer_status["publication"] = "DISABLED"
        return response.model_dump()
    if not response.analysis_complete:
        response.analyzer_status["publication"] = "WITHHELD_INCOMPLETE"
        return response.model_dump()
    if ident.platform != "github":
        response.analyzer_status["publication"] = "UNSUPPORTED_PROVIDER"
        return response.model_dump()
    queue = WebhookJobQueue()
    queue.assert_claim(job_id, claim_token)
    if production():
        pilot.require_advisory(pilot_repository, response.reviewer_profile_id)
    if (
        client.fetch_pull_request_metadata(owner, repo, number).head_sha
        != meta.head_sha
    ):
        raise ValueError("Superseded review: PR head changed before publication")
    # Canonical fingerprints appear in both summary and inline comments.
    for comment in response.inline_comments:
        match = next(
            (
                f
                for f in response.findings
                if f.file_path == comment.path and f.line_number == comment.line
            ),
            None,
        )
        if match:
            comment.fingerprint = match.fingerprint
    earlier = client.list_pull_request_review_comments(owner, repo, number)
    bot = os.getenv("BOT_GITHUB_LOGIN", "review-bot").lower()
    prior_bodies = [
        c.get("body") or ""
        for c in earlier
        if (c.get("user") or {}).get("login", "").lower() == bot
    ]
    comments = [
        c
        for c in response.inline_comments
        if c.fingerprint not in suppressed
        and not any(c.fingerprint and c.fingerprint in body for body in prior_bodies)
    ]
    eligible = {
        f.fingerprint
        for f in response.findings
        if not f.publication_reasons
        and f.fingerprint not in suppressed
        and not any(f.fingerprint and f.fingerprint in body for body in prior_bodies)
    }
    summary = advisory_summary(response, eligible)
    published_fingerprints = (
        original_intent.get("published_fingerprints") if saved else None
    )
    if published_fingerprints is None:
        published_fingerprints = sorted(eligible)
    key = f"review:{ident.platform}:{host}:{repo_key}:{number}:{meta.head_sha}"
    receipt = publish(
        queue,
        job_id=job_id,
        claim_token=claim_token,
        key=key,
        target=publication_target,
        payload={
            "head_sha": meta.head_sha,
            "event": event,
            "body": summary,
            "response": response.model_dump(),
            "published_fingerprints": published_fingerprints,
            "comments": [c.model_dump() for c in comments],
        },
        send=lambda marker: (
            pilot.require_advisory(pilot_repository, response.reviewer_profile_id)
            if production()
            else None,
            client.post_pull_request_review(
                owner=owner,
                repo=repo,
                pull_number=number,
                event=event,
                body=summary + "\n" + marker,
                commit_id=meta.head_sha,
                comments=comments,
            ),
        )[1],
        search=lambda: client.list_pull_request_reviews(owner, repo, number),
    )
    # Receipts must exist before findings become eligible for calibration metrics.
    published_comments = client.list_pull_request_review_comments(owner, repo, number)
    bot = os.getenv("BOT_GITHUB_LOGIN", "review-bot").lower()
    store = FindingOutcomeStore()
    with store.transaction():
        queue.assert_claim(job_id, claim_token)
        for finding in response.findings:
            if (
                finding.fingerprint not in published_fingerprints
                or finding.publication_reasons
            ):
                continue
            comment = next(
                (
                    c
                    for c in published_comments
                    if finding.fingerprint in (c.get("body") or "")
                    and (c.get("user") or {}).get("login", "").lower() == bot
                ),
                {},
            )
            record = store.record_finding(
                repo_key,
                finding.fingerprint,
                category=finding.category,
                severity=finding.severity,
                pr_id=str(number),
                provider=ident.platform,
                host=host,
                head_sha=meta.head_sha,
                provider_review_id=receipt["id"],
                provider_comment_id=comment.get("id", ""),
                evidence_grade=finding.evidence_grade,
                evidence_provenance=finding.evidence_provenance,
                claim_title=finding.name,
                claim_description=finding.description,
                file_path=finding.file_path,
                line_number=finding.line_number,
                metadata={"rule_id": finding.rule_id},
            )
            finding.finding_id = record["finding_id"]
            finding.pr_id = str(number)
            finding.publication_status = "PUBLISHED"
        observed = {f.fingerprint for f in response.findings}
        if response.analysis_complete:
            for previous in store.list_findings(
                repo_key, pr_id=str(number), provider=ident.platform, host=host
            ):
                if previous["fingerprint"] not in observed:
                    store.update_outcome(
                        finding_id=previous["finding_id"],
                        provider=ident.platform,
                        host=host,
                        source="comment_sync_resolved",
                        reason=f"Not observed at {meta.head_sha}",
                    )
    pilot.publication(pilot_run, published_fingerprints, receipt, meta.head_sha)
    response.analyzer_status["publication"] = "PUBLISHED"
    return response.model_dump()
