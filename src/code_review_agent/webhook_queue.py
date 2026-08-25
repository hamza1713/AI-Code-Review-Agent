import os
import json
import time
import uuid
import sqlite3
import threading
import concurrent.futures
from pathlib import Path
from typing import Dict, Any, Optional, List, Callable
from code_review_agent.config import logger


class WebhookJobQueue:
    """
    Durable persistent task queue for webhook processing.
    Survives application restarts and power outages using SQLite WAL mode with ACID transactions.
    """

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = Path(db_path or "webhook_jobs.db").resolve()
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
                    locked_at REAL
                );
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_webhook_status ON webhook_jobs(status, created_at);
            """)
            conn.commit()

    def enqueue(self, pr_identifier: str, payload: Dict[str, Any], max_retries: int = 3) -> str:
        """Enqueue a new pull request review job into persistent storage."""
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        now = time.time()
        payload_str = json.dumps(payload)

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO webhook_jobs (job_id, pr_identifier, payload_json, status, attempts, max_retries, created_at, updated_at)
                VALUES (?, ?, ?, 'QUEUED', 0, ?, ?, ?)
                """,
                (job_id, pr_identifier, payload_str, max_retries, now, now)
            )
            conn.commit()

        # Notify any listening worker for 0ms event-driven wakeup
        for cb in self._on_enqueue_callbacks:
            try:
                cb()
            except Exception:
                pass

        logger.info(f"📥 Enqueued durable webhook job {job_id} for {pr_identifier}")
        return job_id

    def claim_next_job(self, lock_timeout_seconds: float = 300.0) -> Optional[Dict[str, Any]]:
        """
        Atomically claim the next eligible job for processing.
        Also reclaims stale jobs that timed out while in PROCESSING status.
        """
        now = time.time()
        timeout_threshold = now - lock_timeout_seconds

        with self._get_connection() as conn:
            # Atomic select and update in a transaction
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT job_id, pr_identifier, payload_json, status, attempts, max_retries
                FROM webhook_jobs
                WHERE status IN ('QUEUED', 'RETRYING')
                   OR (status = 'PROCESSING' AND (locked_at IS NULL OR locked_at < ?))
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (timeout_threshold,)
            )
            row = cursor.fetchone()
            if not row:
                return None

            job_id = row["job_id"]
            new_attempts = row["attempts"] + 1

            cursor.execute(
                """
                UPDATE webhook_jobs
                SET status = 'PROCESSING',
                    attempts = ?,
                    locked_at = ?,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (new_attempts, now, now, job_id)
            )
            conn.commit()

            return {
                "job_id": job_id,
                "pr_identifier": row["pr_identifier"],
                "payload": json.loads(row["payload_json"]),
                "attempts": new_attempts,
                "max_retries": row["max_retries"],
            }

    def complete_job(self, job_id: str):
        """Mark a job as successfully COMPLETED."""
        now = time.time()
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE webhook_jobs
                SET status = 'COMPLETED',
                    locked_at = NULL,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (now, job_id)
            )
            conn.commit()
        logger.info(f"✅ Webhook job {job_id} marked as COMPLETED.")

    def fail_job(self, job_id: str, error_message: str):
        """Record job failure and schedule retry or mark as FAILED."""
        now = time.time()
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

            new_status = "RETRYING" if attempts < max_retries else "FAILED"

            cursor.execute(
                """
                UPDATE webhook_jobs
                SET status = ?,
                    last_error = ?,
                    locked_at = NULL,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (new_status, error_message, now, job_id)
            )
            conn.commit()

        if new_status == "RETRYING":
            logger.warning(f"⚠️ Webhook job {job_id} failed (attempt {attempts}/{max_retries}); scheduled for RETRY.")
        else:
            logger.error(f"❌ Webhook job {job_id} exceeded max retries ({max_retries}) and marked as FAILED: {error_message}")

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
        """Retrieve details of a single job."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT job_id, pr_identifier, status, attempts, max_retries, last_error, created_at, updated_at FROM webhook_jobs WHERE job_id = ?",
                (job_id,)
            )
            row = cursor.fetchone()
            if not row:
                return None
            return dict(row)

    def list_jobs(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """List jobs matching an optional status filter."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if status:
                cursor.execute(
                    "SELECT job_id, pr_identifier, status, attempts, max_retries, last_error, created_at, updated_at FROM webhook_jobs WHERE status = ? ORDER BY created_at DESC",
                    (status,)
                )
            else:
                cursor.execute(
                    "SELECT job_id, pr_identifier, status, attempts, max_retries, last_error, created_at, updated_at FROM webhook_jobs ORDER BY created_at DESC"
                )
            return [dict(r) for r in cursor.fetchall()]


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
        max_workers: int = 2
    ):
        self.queue = queue
        self.handler = handler
        self.poll_interval = poll_interval
        self.max_workers = max_workers
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._wake_event = threading.Event()
        self._semaphore = threading.Semaphore(max_workers)
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="WebhookWorkerPool"
        )
        self.queue.add_enqueue_listener(self._wake_event.set)

    def start(self):
        """Start the background worker thread and recover any orphan jobs."""
        if self._running:
            return

        self.queue.recover_orphan_jobs()
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="WebhookWorkerThread")
        self._thread.start()
        logger.info("👷 Webhook background worker started.")

    def stop(self, timeout: float = 5.0):
        """Signal worker to stop and wait for active jobs to complete."""
        self._running = False
        self._wake_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._executor.shutdown(wait=False, cancel_futures=True)
        logger.info("🛑 Webhook background worker stopped.")

    def _run_loop(self):
        """Main worker polling loop with adaptive backoff and immediate event wakeups."""
        current_sleep = 0.2
        max_sleep = 5.0

        while self._running:
            try:
                job = self.queue.claim_next_job()
                if job:
                    current_sleep = 0.2
                    self._semaphore.acquire()
                    self._executor.submit(self._process_claimed_job, job)
                else:
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
        try:
            logger.info(f"⚙️ Worker executing review for job {job_id} ({pr_id})")
            if self.handler:
                self.handler(job["payload"])
            else:
                self._default_execute_review(job["payload"])

            self.queue.complete_job(job_id)
        except Exception as e:
            logger.error(f"❌ Error processing job {job_id}: {e}", exc_info=True)
            self.queue.fail_job(job_id, str(e))
        finally:
            self._semaphore.release()

    def _default_execute_review(self, payload: Dict[str, Any]):
        """Default handler running PRCodeReviewFlow."""
        from code_review_agent.main import PRCodeReviewFlow

        pr_data = payload.get("pull_request", {})
        repo_data = payload.get("repository", {})

        owner = repo_data.get("owner", {}).get("login", "")
        repo = repo_data.get("name", "")
        pull_number = pr_data.get("number")

        if not owner or not repo or not pull_number:
            raise ValueError(f"Missing required PR fields (owner/repo/pull_number) in payload")

        pr_identifier = f"{owner}/{repo}/pull/{pull_number}"
        flow = PRCodeReviewFlow()
        flow.state.pr_url = pr_identifier
        flow.kickoff(inputs={"id": f"pr_review_{owner}_{repo}_{pull_number}"})
