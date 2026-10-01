"""Run an isolated, credential-free SQLite saturation/crash/restore drill."""

import argparse
import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from code_review_agent.operations import backup, migrate, restore
from code_review_agent.publication import ReconciliationRequired, publish
from code_review_agent.webhook_queue import WebhookJobQueue


def drill(directory: Path) -> dict:
    directory.mkdir(parents=True, exist_ok=False)
    database = directory / "review.db"
    queue = WebhookJobQueue(str(database))
    migration = migrate(database)
    first = [queue.enqueue(f"org/repo#{i}", {}, job_kind="drill") for i in range(2)]
    barrier = threading.Barrier(2)

    def claim(worker):
        barrier.wait(timeout=10)
        result = queue.claim_next_job(worker_id=worker)
        queue._get_connection().close()
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, ["drill-one", "drill-two"]))
    assert {c["job_id"] for c in claims} == set(first)
    for i in range(100):
        queue.enqueue(f"org/repo#{i + 2}", {}, job_kind="drill")
    try:
        queue.enqueue("org/repo#overflow", {}, job_kind="drill")
    except ValueError:
        pass
    else:
        raise AssertionError("Queue admitted more than 100 waiting jobs")
    saturation = queue.get_queue_metrics()
    with queue._get_connection() as conn:
        conn.execute(
            "UPDATE webhook_jobs SET lease_expires_at=0 WHERE status='PROCESSING'"
        )
    assert queue.recover_orphan_jobs() == 2
    for old in claims:
        assert not queue.complete_job(old["job_id"], claim_token=old["claim_token"])

    def drain(worker):
        finished = []
        while job := queue.claim_next_job(worker_id=worker):
            assert queue.complete_job(
                job["job_id"],
                claim_token=job["claim_token"],
                result_json=json.dumps({"drill": True}),
            )
            finished.append(job["job_id"])
        queue._get_connection().close()
        queue._local.conn = None
        return finished

    with ThreadPoolExecutor(max_workers=2) as pool:
        finished = [
            item
            for group in pool.map(drain, ["restarted-one", "restarted-two"])
            for item in group
        ]
    assert len(finished) == len(set(finished)) == 102
    # An accepted provider write with a lost response is reconciled, never resent.
    jid = queue.enqueue("org/repo#timeout", {}, job_kind="drill")
    active = queue.claim_next_job()
    receipts, sends = [], []

    def accepted(marker):
        sends.append(marker)
        receipts.append({"id": 71, "body": marker, "user": {"login": "review-bot"}})
        raise TimeoutError("Provider accepted but acknowledgement was lost")

    args = {
        "job_id": jid,
        "claim_token": active["claim_token"],
        "key": "drill:publication",
        "target": "drill",
        "payload": {},
        "send": accepted,
        "search": lambda: receipts,
    }
    try:
        publish(queue, **args)
    except ReconciliationRequired:
        pass
    assert publish(queue, **args)["id"] == 71 and len(sends) == 1
    assert queue.complete_job(jid, claim_token=active["claim_token"])
    saved = Path(backup(database, directory / "backup.db"))
    restored = directory / "isolated-restore.db"
    restore(restored, saved)
    with sqlite3.connect(restored) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM webhook_jobs WHERE status='COMPLETED'"
            ).fetchone()[0]
            == 103
        )
        assert (
            conn.execute(
                "SELECT status FROM external_operations_log WHERE operation_key='drill:publication'"
            ).fetchone()[0]
            == "SUCCESS"
        )
    queue._get_connection().close()
    return {
        "status": "PASS",
        "commit": __import__("os").getenv("GITHUB_SHA")
        or __import__("os").getenv("REVIEW_SOURCE_COMMIT")
        or "unreleased-working-tree",
        "scope": "isolated SQLite and simulated provider; no production writes",
        "migration": migration,
        "saturation": saturation,
        "recovered": 2,
        "completed": 103,
        "provider_sends": len(sends),
        "backup_sha256": hashlib.sha256(saved.read_bytes()).hexdigest(),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # The drill uses only these controlled local settings, never provider credentials.
    import os

    os.environ["BOT_GITHUB_LOGIN"] = "review-bot"
    os.environ["MAX_QUEUE_CAPACITY"] = "100"
    report = drill(args.directory)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))
