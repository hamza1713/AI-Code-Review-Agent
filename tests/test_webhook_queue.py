"""
Unit tests for WebhookJobQueue and WebhookWorker.
Verifies job persistence, crash recovery across process restarts, retry backoff on failure,
and queue state transitions.
"""

import time
import pytest
from code_review_agent.webhook_queue import WebhookJobQueue, WebhookWorker


class TestWebhookQueueAndWorker:
    """Test suite for persistent webhook task queue and crash recovery."""

    def test_job_enqueue_and_claim_lifecycle(self, tmp_path):
        """Verify normal lifecycle: enqueue -> claim -> complete."""
        db_file = tmp_path / "test_jobs.db"
        queue = WebhookJobQueue(db_path=str(db_file))

        # Enqueue job
        payload = {"pull_request": {"number": 42}, "repository": {"name": "repo", "owner": {"login": "org"}}}
        job_id = queue.enqueue("org/repo/pull/42", payload)
        assert job_id.startswith("job_")

        # Claim job
        claimed = queue.claim_next_job()
        assert claimed is not None
        assert claimed["job_id"] == job_id
        assert claimed["pr_identifier"] == "org/repo/pull/42"
        assert claimed["attempts"] == 1

        # Complete job
        queue.complete_job(job_id)

        # Confirm status in DB
        job_record = queue.get_job(job_id)
        assert job_record is not None
        assert job_record["status"] == "COMPLETED"

        # Claiming again should return None (no pending jobs)
        assert queue.claim_next_job() is None

    def test_crash_recovery_reclaims_orphan_processing_jobs(self, tmp_path):
        """
        Simulate server crashing mid-job while job is in PROCESSING status.
        Confirm that a new queue/worker instance recovers the orphan job rather than silently losing it.
        """
        db_file = tmp_path / "crash_test.db"

        # 1. First server process enqueues and claims job, then 'crashes' mid-execution
        queue_instance_1 = WebhookJobQueue(db_path=str(db_file))
        job_id = queue_instance_1.enqueue("org/repo/pull/99", {"action": "opened"})
        claimed_job = queue_instance_1.claim_next_job()
        assert claimed_job["job_id"] == job_id

        # Verify job is left in 'PROCESSING' state
        job_before_restart = queue_instance_1.get_job(job_id)
        assert job_before_restart["status"] == "PROCESSING"

        # 2. Server restarts (new queue instance created)
        queue_instance_2 = WebhookJobQueue(db_path=str(db_file))

        # Crash recovery method runs on startup
        recovered_count = queue_instance_2.recover_orphan_jobs()
        assert recovered_count == 1

        # Confirm job was reset to QUEUED with recovery note and not lost
        job_after_restart = queue_instance_2.get_job(job_id)
        assert job_after_restart["status"] == "QUEUED"
        assert "Recovered after ungraceful process restart" in job_after_restart["last_error"]

        # 3. New server process can re-claim and successfully process the recovered job
        reclaimed_job = queue_instance_2.claim_next_job()
        assert reclaimed_job is not None
        assert reclaimed_job["job_id"] == job_id
        queue_instance_2.complete_job(job_id)

    def test_retry_on_failure_up_to_max_retries(self, tmp_path):
        """Verify failed jobs retry until max_retries is reached, then transition to FAILED."""
        db_file = tmp_path / "retry_test.db"
        queue = WebhookJobQueue(db_path=str(db_file))

        job_id = queue.enqueue("org/repo/pull/1", {"pr": 1}, max_retries=2)

        # Attempt 1 -> Fails
        j1 = queue.claim_next_job()
        queue.fail_job(job_id, "Network timeout 1")
        assert queue.get_job(job_id)["status"] == "RETRYING"

        # Attempt 2 -> Fails (reaches max_retries)
        j2 = queue.claim_next_job()
        queue.fail_job(job_id, "Network timeout 2")
        assert queue.get_job(job_id)["status"] == "FAILED"

        # No more jobs available to claim
        assert queue.claim_next_job() is None
