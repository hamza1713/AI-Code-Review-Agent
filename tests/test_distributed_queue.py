"""
Tests for Phase 2: Distributed Queue, Worker Leasing, Heartbeats, Exponential Backoff, and Observability.
"""

import time
import threading
import pytest
from code_review_agent.webhook_queue import WebhookJobQueue, WebhookWorker


def test_priority_ordering(tmp_path):
    """Verify jobs are claimed by priority DESC, then created_at ASC."""
    db_file = tmp_path / "priority_test.db"
    queue = WebhookJobQueue(db_path=str(db_file))

    # Enqueue low-priority job first
    low_id = queue.enqueue("low/pr/1", {"pr": 1}, priority=10)
    # Enqueue high-priority job second
    high_id = queue.enqueue("high/pr/2", {"pr": 2}, priority=100)
    # Enqueue normal-priority job third
    med_id = queue.enqueue("med/pr/3", {"pr": 3}, priority=50)

    # Claim 1: must be high priority
    c1 = queue.claim_next_job(worker_id="w1")
    assert c1 is not None
    assert c1["job_id"] == high_id
    assert c1["priority"] == 100

    # Claim 2: must be normal priority
    c2 = queue.claim_next_job(worker_id="w1")
    assert c2 is not None
    assert c2["job_id"] == med_id
    assert c2["priority"] == 50

    # Claim 3: must be low priority
    c3 = queue.claim_next_job(worker_id="w1")
    assert c3 is not None
    assert c3["job_id"] == low_id
    assert c3["priority"] == 10


def test_worker_leasing_and_heartbeats(tmp_path):
    """Verify worker_id tracking, lease expiration, and heartbeat extensions."""
    db_file = tmp_path / "lease_test.db"
    queue = WebhookJobQueue(db_path=str(db_file))

    job_id = queue.enqueue("org/repo/pull/10", {"action": "sync"})

    # Worker 1 claims job with 2-second lease
    claimed = queue.claim_next_job(worker_id="worker_alpha", lease_duration=2.0)
    assert claimed is not None
    assert claimed["worker_id"] == "worker_alpha"

    # Pulse heartbeat with stage updates
    success = queue.heartbeat_job(
        job_id,
        worker_id="worker_alpha",
        current_stage="SECURITY_SCAN",
        stage_progress=0.45,
        lease_duration=2.0,
    )
    assert success is True

    # Inspect job state
    job_record = queue.get_job(job_id)
    assert job_record["worker_id"] == "worker_alpha"
    assert job_record["current_stage"] == "SECURITY_SCAN"
    assert job_record["stage_progress"] == 0.45


def test_stale_worker_eviction(tmp_path):
    """Verify a job with an expired lease or abandoned heartbeat is reclaimed by another worker."""
    db_file = tmp_path / "eviction_test.db"
    queue = WebhookJobQueue(db_path=str(db_file))

    job_id = queue.enqueue("org/repo/pull/20", {"action": "sync"})

    # Worker 1 claims with very short lease (0.1 second)
    claimed_1 = queue.claim_next_job(worker_id="worker_dying", lease_duration=0.1)
    assert claimed_1["job_id"] == job_id
    assert claimed_1["worker_id"] == "worker_dying"

    # Wait for lease to expire
    time.sleep(0.2)

    # Worker 2 attempts claim; must successfully evict dead worker and take over
    claimed_2 = queue.claim_next_job(worker_id="worker_alive", lease_duration=10.0)
    assert claimed_2 is not None
    assert claimed_2["job_id"] == job_id
    assert claimed_2["worker_id"] == "worker_alive"
    assert claimed_2["attempts"] == 2


def test_exponential_backoff_scheduling(tmp_path):
    """Verify failed jobs respect scheduled_at retry delays."""
    db_file = tmp_path / "backoff_test.db"
    queue = WebhookJobQueue(db_path=str(db_file))

    job_id = queue.enqueue("org/repo/pull/30", {"action": "test"}, max_retries=3)

    # Claim attempt 1
    c1 = queue.claim_next_job(worker_id="w1")
    assert c1["job_id"] == job_id

    # Fail attempt 1 with explicit base delay of 2.0s
    queue.fail_job(job_id, "Rate limited by upstream API", base_delay=2.0)

    # Verify status is RETRYING and scheduled_at is in the future
    job_record = queue.get_job(job_id)
    assert job_record["status"] == "RETRYING"
    assert job_record["scheduled_at"] > time.time()

    # Immediate claim attempt must return None (job is still in backoff window)
    immediate_claim = queue.claim_next_job(worker_id="w1")
    assert immediate_claim is None


def test_job_cancellation(tmp_path):
    """Verify queued and processing jobs can be cancelled."""
    db_file = tmp_path / "cancel_test.db"
    queue = WebhookJobQueue(db_path=str(db_file))

    job_id = queue.enqueue("org/repo/pull/40", {"action": "test"})
    assert queue.get_job(job_id)["status"] == "QUEUED"

    # Cancel job
    cancelled = queue.cancel_job(job_id, reason="User clicked Cancel")
    assert cancelled is True

    record = queue.get_job(job_id)
    assert record["status"] == "CANCELLED"
    assert record["last_error"] == "User clicked Cancel"

    # Cancelled job cannot be claimed
    assert queue.claim_next_job() is None


def test_queue_metrics_aggregation(tmp_path):
    """Verify get_queue_metrics correctly calculates counts, active workers, and average duration."""
    db_file = tmp_path / "metrics_test.db"
    queue = WebhookJobQueue(db_path=str(db_file))

    # 1. Enqueue two jobs
    j1 = queue.enqueue("org/repo/pull/1", {})
    j2 = queue.enqueue("org/repo/pull/2", {})

    # Claim j1 -> PROCESSING
    queue.claim_next_job(worker_id="worker_active")

    metrics_1 = queue.get_queue_metrics()
    assert metrics_1["queued"] == 1
    assert metrics_1["processing"] == 1
    assert metrics_1["active_workers"] == 1

    # Complete j1
    queue.complete_job(j1)

    # Cancel j2
    queue.cancel_job(j2)

    metrics_2 = queue.get_queue_metrics()
    assert metrics_2["completed"] == 1
    assert metrics_2["cancelled"] == 1
    assert metrics_2["queued"] == 0
    assert metrics_2["processing"] == 0
