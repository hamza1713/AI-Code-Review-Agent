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
                    locked_at REAL,
                    review_result_json TEXT
                );
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_webhook_status ON webhook_jobs(status, created_at);
            """)
            # Migration check: ensure review_result_json exists if table already existed
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(webhook_jobs);")
            columns = [row[1] for row in cursor.fetchall()]
            if "review_result_json" not in columns:
                conn.execute("ALTER TABLE webhook_jobs ADD COLUMN review_result_json TEXT;")
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
        Atomically claim the next eligible job for processing using BEGIN IMMEDIATE.
        Locks against concurrent multi-process worker races and reclaims stale jobs.
        """
        now = time.time()
        timeout_threshold = now - lock_timeout_seconds

        with self._get_connection() as conn:
            # Atomic select and update in an immediate transaction (locks database against concurrent worker race conditions)
            conn.execute("BEGIN IMMEDIATE")
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
                conn.commit()
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

    def complete_job(self, job_id: str, result_json: Optional[str] = None):
        """Mark a job as successfully COMPLETED and persist review results."""
        now = time.time()
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE webhook_jobs
                SET status = 'COMPLETED',
                    locked_at = NULL,
                    updated_at = ?,
                    review_result_json = ?
                WHERE job_id = ?
                """,
                (now, result_json, job_id)
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
        """Retrieve details of a single job including parsed review result."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT job_id, pr_identifier, status, attempts, max_retries, last_error, created_at, updated_at, review_result_json FROM webhook_jobs WHERE job_id = ?",
                (job_id,)
            )
            row = cursor.fetchone()
            if not row:
                return None
            res = dict(row)
            result_json = res.pop("review_result_json", None)
            res["result"] = json.loads(result_json) if result_json else None
            return res

    def list_jobs(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """List jobs matching an optional status filter including parsed review result."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if status:
                cursor.execute(
                    "SELECT job_id, pr_identifier, status, attempts, max_retries, last_error, created_at, updated_at, review_result_json FROM webhook_jobs WHERE status = ? ORDER BY created_at DESC",
                    (status,)
                )
            else:
                cursor.execute(
                    "SELECT job_id, pr_identifier, status, attempts, max_retries, last_error, created_at, updated_at, review_result_json FROM webhook_jobs ORDER BY created_at DESC"
                )
            rows = []
            for r in cursor.fetchall():
                d = dict(r)
                result_json = d.pop("review_result_json", None)
                d["result"] = json.loads(result_json) if result_json else None
                rows.append(d)
            return rows


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
            result = None
            if self.handler:
                result = self.handler(job["payload"])
            else:
                result = self._default_execute_review(job["payload"], pr_id)

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
            self._semaphore.release()

    def _default_execute_review(self, payload: Dict[str, Any], pr_identifier: str) -> Dict[str, Any]:
        """
        Platform-agnostic default review handler.

        Resolves the platform (GitHub / GitLab / Bitbucket / local) from the enqueued
        identifier — not the payload shape — fetches the diff via that client, reviews it
        against a real repo checkout, and posts the verdict back through the same client.
        This makes the durable queue work uniformly across hosting platforms.
        """
        import urllib.parse
        from contextlib import ExitStack
        from code_review_agent.review_service import ReviewService
        from code_review_agent.platform.factory import get_platform_client
        from code_review_agent.bot.checkout import temporary_pr_checkout

        client, ident = get_platform_client(pr_identifier)
        owner, repo, pull_number = ident.owner_or_project, ident.repo_or_slug, ident.pr_id

        diff = client.fetch_pull_request_diff(owner, repo, pull_number)
        if not diff or not diff.strip():
            raise ValueError(f"No diff content available for {pr_identifier}")
        meta = client.fetch_pull_request_metadata(owner, repo, pull_number)

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

        # Post the verdict back through the resolving platform client.
        verdict = (response.verdict or "").upper()
        if "APPROVE" in verdict:
            event = "APPROVE"
        elif "REQUEST" in verdict or "ESCALATE" in verdict:
            event = "REQUEST_CHANGES"
        else:
            event = "COMMENT"
        try:
            client.post_pull_request_review(
                owner=owner, repo=repo, pull_number=pull_number,
                event=event, body=response.full_report,
                commit_id=meta.head_sha or None, comments=response.inline_comments,
            )
        except Exception as post_err:
            logger.error(f"Could not post review for {pr_identifier}: {post_err}")

        return response.model_dump()
