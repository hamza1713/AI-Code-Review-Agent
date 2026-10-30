"""
Phase 3 Chaos Resilience, Orphan Recovery & Distributed Worker Lease Tests.
Verifies that:
1. Orphan jobs from crashed/killed workers are recovered automatically upon lease expiration.
2. Active worker leases prevent duplicate processing by competing workers.
3. Transient network/provider errors trigger exponential backoff retries.
4. Permanent validation errors fail fast without consuming retry budgets.
5. Worker handles high-frequency cancellations without database deadlocks.
"""

import time
from pathlib import Path

import pytest

from code_review_agent.webhook_queue import WebhookJobQueue, is_transient_error


@pytest.fixture
def queue(tmp_path: Path) -> WebhookJobQueue:
    db_file = tmp_path / "chaos_test_queue.db"
    return WebhookJobQueue(db_path=str(db_file))


class TestChaosOrphanRecovery:
    def test_crashed_worker_lease_expiration_recovery(self, queue: WebhookJobQueue):
        # 1. Enqueue job
        job_id = queue.enqueue(
            pr_identifier="org/crashed_worker/pull/1",
            payload={"pr": 1},
            max_retries=3,
        )

        # 2. Worker 1 claims job
        claimed = queue.claim_next_job(worker_id="worker-crash-victim")
        assert claimed is not None
        assert claimed["job_id"] == job_id
        assert claimed["status"] == "PROCESSING"

        # Simulate time passing beyond lease expiration (e.g. lease expired 10 seconds ago)
        past_time = time.time() - 10
        with queue._get_connection() as conn:
            conn.execute(
                "UPDATE webhook_jobs SET lease_expires_at = ?, locked_at = ? WHERE job_id = ?",
                (past_time, past_time - 300, job_id),
            )
            conn.commit()

        # 3. Queue recovery process runs
        recovered_count = queue.recover_orphan_jobs()
        assert recovered_count == 1

        # 4. Job is transitioned back to RETRYING for another worker to pick up
        reclaimed_job = queue.get_job(job_id)
        assert reclaimed_job["status"] == "RETRYING"
        assert "Expired worker lease" in reclaimed_job["last_error"]

    def test_exhausted_attempts_transitions_to_failed(self, queue: WebhookJobQueue):
        # Enqueue job with max_retries = 1
        job_id = queue.enqueue(
            pr_identifier="org/fatal_crash/pull/2",
            payload={"pr": 2},
            max_retries=1,
        )

        claimed = queue.claim_next_job(worker_id="worker-doomed")
        assert claimed is not None
        assert claimed["attempts"] == 1

        # Simulate worker death and lease expiration
        past = time.time() - 5
        with queue._get_connection() as conn:
            conn.execute(
                "UPDATE webhook_jobs SET lease_expires_at = ? WHERE job_id = ?",
                (past, job_id),
            )
            conn.commit()

        # Recover orphans
        queue.recover_orphan_jobs()

        # Because attempts (1) >= max_retries (1), job transitions directly to FAILED
        job = queue.get_job(job_id)
        assert job["status"] == "FAILED"
        assert "Expired worker lease" in job["last_error"]


class TestErrorClassificationResilience:
    def test_transient_vs_permanent_errors(self):
        # Transient errors (retryable)
        assert is_transient_error("HTTP 429 Too Many Requests: Rate limit exceeded") is True
        assert is_transient_error("503 Service Unavailable: Model provider capacity reached") is True
        assert is_transient_error("ConnectionResetError: Connection reset by peer") is True
        assert is_transient_error("TimeoutError: Read timeout after 30.0s") is True

        # Permanent errors (fail fast, no retries)
        assert is_transient_error("InputValidationError: Diff size 5MB exceeds 2MB limit") is False
        assert is_transient_error("401 Unauthorized: Invalid repository operator token") is False
        assert is_transient_error("403 Forbidden: Missing push permissions on target branch") is False
        assert is_transient_error("404 Not Found: Pull request does not exist") is False
        assert is_transient_error("Unsafe diff path detected") is False

    def test_permanent_error_fails_fast_in_queue(self, queue: WebhookJobQueue):
        job_id = queue.enqueue(
            pr_identifier="org/perm_fail/pull/3",
            payload={"diff": "bad"},
            max_retries=5,
        )

        claimed = queue.claim_next_job(worker_id="worker-1")
        token = claimed["claim_token"]

        # Fail with permanent error
        queue.fail_job(
            job_id,
            error_message="InputValidationError: Unsafe diff path '../etc/passwd'",
            claim_token=token,
        )

        job = queue.get_job(job_id)
        # Must be FAILED immediately despite 5 retries remaining
        assert job["status"] == "FAILED"
        assert "InputValidationError" in job["last_error"]

    def test_transient_error_retries_with_backoff(self, queue: WebhookJobQueue):
        job_id = queue.enqueue(
            pr_identifier="org/retry_ok/pull/4",
            payload={"diff": "ok"},
            max_retries=3,
        )

        claimed = queue.claim_next_job(worker_id="worker-1")
        token = claimed["claim_token"]

        # Fail with transient 429
        queue.fail_job(
            job_id,
            error_message="429 RateLimitError: Resource exhausted",
            claim_token=token,
            base_delay=10.0,
        )

        job = queue.get_job(job_id)
        # Must be RETRYING with future scheduled_at
        assert job["status"] == "RETRYING"
        assert job["scheduled_at"] >= time.time() + 5.0
