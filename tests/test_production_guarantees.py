"""Release regressions use real contracts, SQLite files, and provider HTTP fixtures."""

import sqlite3
import time
from contextlib import closing, contextmanager
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from code_review_agent.access_policy import validate_configuration
from code_review_agent.calibration import FindingOutcomeStore, RepoCalibrationEngine
from code_review_agent.calibration.persistence import (
    AmbiguousFindingError,
    FindingNotFoundError,
)
from code_review_agent.calibration.reaction_poller import ReactionPoller
from code_review_agent.models import (
    CrossFileImpactSummary,
    ReviewAPIResponse,
    SastFinding,
)
from code_review_agent.operations import backup, migrate, restore
from code_review_agent.publication import ReconciliationRequired, publish
from code_review_agent.webhook_queue import WebhookJobQueue


@pytest.fixture
def production(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(
        "REVIEW_API_TOKEN", "test-operator-token-with-more-than-32-characters"
    )
    monkeypatch.setenv("REVIEW_ALLOWED_REPOSITORIES", "github:github.com/org/repo")
    monkeypatch.setenv("REVIEW_ENABLE_POSTING", "true")
    monkeypatch.setenv("BOT_GITHUB_LOGIN", "review-bot")
    monkeypatch.delenv("REVIEW_ENABLE_REMEDIATION", raising=False)


def occurrence(store, pr="1", fingerprint="aabbccdd", **kwargs):
    return store.record_finding(
        "org/repo",
        fingerprint,
        pr_id=pr,
        provider_review_id="91",
        head_sha="a" * 40,
        **kwargs,
    )


def test_occurrences_revisions_and_immutable_feedback(production):
    store = FindingOutcomeStore()
    first = occurrence(store)
    other = occurrence(store, pr="2")
    repeated = occurrence(store)
    assert first["finding_id"] == repeated["finding_id"] != other["finding_id"]
    assert first["first_posted_at"] == repeated["first_posted_at"]
    with pytest.raises(AmbiguousFindingError):
        store.update_outcome("org/repo", "aabbccdd", outcome="FALSE_POSITIVE")
    with pytest.raises(FindingNotFoundError):
        store.update_outcome("org/repo", "unknown", outcome="CONFIRMED_REAL")
    store.update_outcome(
        finding_id=first["finding_id"], outcome="FALSE_POSITIVE", actor="maintainer"
    )
    store.update_outcome(
        finding_id=first["finding_id"],
        outcome="CONFIRMED_REAL",
        source="comment_sync_resolved",
    )
    assert (
        store.require_finding(finding_id=first["finding_id"])["outcome"]
        == "FALSE_POSITIVE"
    )
    with pytest.raises(sqlite3.IntegrityError):
        store._get_connection().execute("UPDATE feedback_events SET actor='forged'")


def test_evidence_must_identify_finding_and_commit(production):
    store = FindingOutcomeStore()
    assert (
        occurrence(store, evidence_grade="REPRODUCED")["evidence_grade"] == "HEURISTIC"
    )
    evidence = {
        "finding_fingerprint": "aabbccdd",
        "commit_sha": "a" * 40,
        "executed": True,
        "test_id": "test_sql",
        "status": "REPRODUCED",
    }
    assert (
        occurrence(store, evidence_grade="REPRODUCED", evidence_provenance=evidence)[
            "evidence_grade"
        ]
        == "REPRODUCED"
    )
    evidence["commit_sha"] = "different"
    assert (
        occurrence(store, evidence_grade="REPRODUCED", evidence_provenance=evidence)[
            "evidence_grade"
        ]
        == "HEURISTIC"
    )


def test_reaction_conflict_deletion_and_human_precedence(production):
    store = FindingOutcomeStore()
    fid = occurrence(store)["finding_id"]
    positive = {"id": "1", "actor": "alice", "content": "+1"}
    negative = {"id": "2", "actor": "bob", "content": "-1"}
    store.reconcile_reactions(fid, [positive, negative])
    assert store.require_finding(finding_id=fid)["outcome"] == "PENDING"
    store.reconcile_reactions(fid, [negative])
    assert store.require_finding(finding_id=fid)["outcome"] == "FALSE_POSITIVE"
    store.update_outcome(finding_id=fid, outcome="CONFIRMED_REAL", actor="operator")
    store.reconcile_reactions(fid, [])
    assert store.require_finding(finding_id=fid)["outcome"] == "CONFIRMED_REAL"
    from code_review_agent.suppression_store import SuppressionStore

    assert not SuppressionStore().list_suppressions()


def test_rolling_windows_use_first_publication_and_denominators(production):
    store = FindingOutcomeStore()
    now = time.time()
    occurrence(store, created_at=now - 31 * 86400, outcome="FALSE_POSITIVE")
    occurrence(store)  # A new revision cannot move the publication window.
    metrics = RepoCalibrationEngine(store).get_metrics("org/repo", 30)
    assert metrics.total_findings == 0 and not metrics.has_triaged_data
    occurrence(store, pr="2", outcome="CONFIRMED_REAL")
    metrics = RepoCalibrationEngine(store).get_metrics("org/repo", 30)
    assert (
        metrics.triaged == 1
        and metrics.has_triaged_data
        and metrics.false_positive_rate == 0
    )


def test_leases_cancellation_retry_exhaustion_and_saturation(tmp_path, monkeypatch):
    queue = WebhookJobQueue(str(tmp_path / "queue.db"))
    jid = queue.enqueue("org/repo#1", {}, max_retries=2)
    first = queue.claim_next_job(worker_id="one")
    assert queue.recover_orphan_jobs() == 0
    assert not queue.complete_job(jid, claim_token="forged")
    with queue._get_connection() as conn:
        conn.execute(
            "UPDATE webhook_jobs SET lease_expires_at=0 WHERE job_id=?", (jid,)
        )
    second = queue.claim_next_job(worker_id="two")
    assert second["attempts"] == 2 and first["claim_token"] != second["claim_token"]
    assert not queue.heartbeat_job(jid, claim_token=first["claim_token"])
    assert not queue.fail_job(jid, "timeout", claim_token=first["claim_token"])
    with queue._get_connection() as conn:
        conn.execute(
            "UPDATE webhook_jobs SET lease_expires_at=0 WHERE job_id=?", (jid,)
        )
    assert queue.claim_next_job() is None
    assert queue.get_job(jid)["status"] == "FAILED"
    for index in range(2):
        queue.enqueue(f"org/repo#{index}", {})
        queue.claim_next_job()
    for index in range(100):
        queue.enqueue(f"org/repo#{index}", {})
    with pytest.raises(ValueError, match="capacity"):
        queue.enqueue("org/repo#101", {})
    queued = queue.list_jobs(status="QUEUED")[0]["job_id"]
    assert queue.cancel_job(queued)
    active = queue.list_jobs(status="PROCESSING")[0]["job_id"]
    assert queue.cancel_job(active)
    assert not queue.complete_job(active)
    assert queue.get_job(active)["status"] == "CANCELLED"


def test_uncertain_publication_reconciles_without_second_write(tmp_path, production):
    queue = WebhookJobQueue(str(tmp_path / "publication.db"))
    jid = queue.enqueue("org/repo#1", {})
    claim = queue.claim_next_job()
    receipts = []
    calls = []

    def accepted_then_timeout(marker):
        calls.append(marker)
        receipts.append({"id": 71, "body": marker, "user": {"login": "review-bot"}})
        raise httpx.ReadTimeout("Provider accepted but response was lost")

    args = {
        "job_id": jid,
        "claim_token": claim["claim_token"],
        "key": "stable",
        "target": "org/repo#1",
        "payload": {"body": "review"},
        "send": accepted_then_timeout,
        "search": lambda: receipts,
    }
    with pytest.raises(ReconciliationRequired):
        publish(queue, **args)
    assert publish(queue, **args)["id"] == 71
    assert publish(queue, **args)["id"] == 71
    assert len(calls) == 1
    queue.cancel_job(jid)
    with pytest.raises(PermissionError):
        publish(queue, **args)


def test_unreconciled_absence_does_not_repost(tmp_path, production):
    queue = WebhookJobQueue(str(tmp_path / "unknown.db"))
    jid = queue.enqueue("org/repo#1", {})
    claim = queue.claim_next_job()
    sender = Mock(side_effect=httpx.ReadTimeout("unknown"))
    args = {
        "job_id": jid,
        "claim_token": claim["claim_token"],
        "key": "unknown",
        "target": "pr",
        "payload": {},
        "send": sender,
        "search": list,
    }
    for _ in range(2):
        with pytest.raises(ReconciliationRequired):
            publish(queue, **args)
    assert sender.call_count == 1


def test_legacy_migration_backup_restore_and_rerun(tmp_path):
    database = tmp_path / "legacy.db"
    with closing(sqlite3.connect(database)) as conn:
        conn.execute(
            "CREATE TABLE finding_outcomes(repo_id TEXT,fingerprint TEXT,pr_id TEXT,created_at REAL,updated_at REAL)"
        )
        conn.execute(
            "INSERT INTO finding_outcomes VALUES ('org/repo','deadbeef','12',10,20)"
        )
        conn.commit()
    report = migrate(database)
    assert report["unresolved"] == 1 and report["backup"]
    assert migrate(database)["unresolved"] == 1
    with closing(sqlite3.connect(database)) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM finding_outcomes_legacy_v1").fetchone()[
                0
            ]
            == 1
        )
        assert (
            conn.execute("SELECT first_posted_at FROM finding_outcomes").fetchone()[0]
            is None
        )
    restore(database, report["backup"])
    with closing(sqlite3.connect(database)) as conn:
        assert "finding_id" not in {
            r[1] for r in conn.execute("PRAGMA table_info(finding_outcomes)")
        }
    assert migrate(database)["unresolved"] == 1
    saved = backup(database, tmp_path / "daily.db")
    restored = tmp_path / "restored.db"
    restore(restored, saved)
    with closing(sqlite3.connect(restored)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_api_identity_errors_attribution_and_no_automatic_suppression(production):
    from code_review_agent.suppression_store import SuppressionStore
    from code_review_agent.webhook_server import app

    client = TestClient(
        app,
        headers={
            "Authorization": "Bearer test-operator-token-with-more-than-32-characters"
        },
    )
    store = FindingOutcomeStore()
    first = occurrence(store)
    occurrence(store, pr="2")
    assert (
        client.post(
            "/api/calibration/feedback",
            json={
                "repo_id": "org/repo",
                "fingerprint": "none",
                "outcome": "FALSE_POSITIVE",
            },
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/calibration/feedback",
            json={
                "repo_id": "org/repo",
                "fingerprint": "aabbccdd",
                "outcome": "FALSE_POSITIVE",
            },
        ).status_code
        == 409
    )
    response = client.post(
        "/api/calibration/feedback",
        json={
            "finding_id": first["finding_id"],
            "outcome": "FALSE_POSITIVE",
            "source": "forged",
        },
    )
    assert response.status_code == 200
    event = (
        store._get_connection()
        .execute("SELECT actor,source FROM feedback_events WHERE kind='FEEDBACK'")
        .fetchone()
    )
    assert tuple(event) == ("operator", "HUMAN_EXPLICIT")
    assert not SuppressionStore().list_suppressions()
    for path in ("/docs", "/redoc", "/openapi.json", "/metrics", "/jobs"):
        assert (
            client.get(path, headers={"Authorization": "Bearer wrong"}).status_code
            == 401
        )


def test_production_startup_rejects_missing_short_token(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("REVIEW_REQUIRE_AUTH", "false")
    for token in ("", "short"):
        monkeypatch.setenv("REVIEW_API_TOKEN", token)
        with pytest.raises(RuntimeError):
            validate_configuration()
    monkeypatch.setenv("APP_ENV", "development")
    validate_configuration()


def test_reaction_partial_fetch_never_erases_and_budget_is_bounded(production):
    store = FindingOutcomeStore()
    row = occurrence(store, provider_comment_id="10")
    store.reconcile_reactions(
        row["finding_id"], [{"id": "1", "actor": "alice", "content": "-1"}]
    )

    def provider(request):
        path = request.url.path
        if path.endswith("/pulls/1"):
            return httpx.Response(200, json={"state": "open"})
        if path.endswith("/comments/10"):
            return httpx.Response(200, json={"user": {"login": "review-bot"}})
        if request.url.params.get("page") == "2":
            return httpx.Response(500)
        return httpx.Response(
            200,
            json=[],
            headers={"link": '<https://api.github.com/reactions?page=2>; rel="next"'},
        )

    poller = ReactionPoller(store, budget=4)
    poller.cycle(httpx.MockTransport(provider))
    assert poller.state("requests_used") == 4
    assert (
        store.require_finding(finding_id=row["finding_id"])["outcome"]
        == "FALSE_POSITIVE"
    )
    assert (
        store._get_connection()
        .execute("SELECT active FROM reaction_observations")
        .fetchone()[0]
        == 1
    )


def test_real_response_contract_reaches_delivery_and_calibration(
    production, tmp_path, monkeypatch
):
    from code_review_agent.platform.base import PlatformPRIdentifier, PlatformPRMetadata
    from code_review_agent.review_service import ReviewService
    from code_review_agent.webhook_queue import execute_platform_review

    diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -0,0 +1,1 @@\n+query(user_input)\n"
    finding = SastFinding(
        rule_id="SQL",
        severity="HIGH",
        description="Untrusted input enters an unparameterized SQL query.",
        file_path="a.py",
        line_number=1,
        snippet="query(user_input)",
        fix_recommendation="Bind user input through SQL query parameters.",
    )
    response = ReviewAPIResponse(
        verdict="REQUEST CHANGES",
        confidence_score=85,
        summary="Review",
        full_report="Full review",
        findings=[finding],
        reviewed_diff=diff,
        cross_file_impact=CrossFileImpactSummary(
            available=False, is_python=True, message="Unavailable"
        ),
    )
    from production_fixtures import versioned_response

    response = versioned_response(response)
    client = Mock()
    client.fetch_pull_request_metadata.return_value = PlatformPRMetadata(
        head_sha="a" * 40, head_ref="feature"
    )
    client.fetch_pull_request_diff.return_value = diff
    client.post_pull_request_review.return_value = {"id": 91}
    client.create_or_update_check_run.return_value = {"id": 93}
    client.list_pull_request_review_comments.return_value = []
    client.set_commit_status.return_value = {"id": 92}
    ident = PlatformPRIdentifier(
        platform="github",
        owner_or_project="org",
        repo_or_slug="repo",
        pr_id=1,
        raw_identifier="org/repo#1",
    )
    monkeypatch.setattr(
        "code_review_agent.platform.factory.get_platform_client",
        lambda _: (client, ident),
    )

    @contextmanager
    def checkout(*args, **kwargs):
        yield str(tmp_path)

    monkeypatch.setattr(
        "code_review_agent.bot.checkout.temporary_pr_checkout", checkout
    )
    monkeypatch.setattr(ReviewService, "execute_review", lambda **kw: response)
    # This contract test isolates publication from the separately tested pilot gate.
    monkeypatch.setattr(
        "code_review_agent.pilot.PilotStore.require_advisory", lambda *args: None
    )
    queue = WebhookJobQueue()
    jid = queue.enqueue("org/repo#1", {})
    claim = queue.claim_next_job()
    result = execute_platform_review({}, "org/repo#1", jid, claim["claim_token"])
    assert result["findings"][0]["finding_id"]
    assert result["findings"][0]["publication_status"] == "PUBLISHED"
    assert result["full_report"] == "Full review" and result["confidence_score"] == 85
    assert (
        FindingOutcomeStore().require_finding(
            finding_id=result["findings"][0]["finding_id"]
        )["provider_review_id"]
        == "91"
    )
    assert client.post_pull_request_review.call_args.kwargs["commit_id"] == "a" * 40


def test_operator_publication_reconciliation_endpoint(production, tmp_path):
    from code_review_agent.webhook_server import app

    client = TestClient(
        app,
        headers={
            "Authorization": "Bearer test-operator-token-with-more-than-32-characters"
        },
    )
    queue = WebhookJobQueue()
    jid = queue.enqueue("org/repo#1", {})
    claim = queue.claim_next_job()
    queue.fail_job(
        jid,
        "RECONCILIATION_REQUIRED: uncertain write",
        claim_token=claim["claim_token"],
        state="RECONCILIATION_REQUIRED",
    )
    assert queue.get_job(jid)["status"] == "RECONCILIATION_REQUIRED"

    # Insert a pending external operation
    with queue._get_connection() as conn:
        conn.execute(
            "INSERT INTO external_operations_log (operation_key, operation_type, target_id, payload_hash, status, result_json, created_at, updated_at) "
            "VALUES (?, 'publication', 'org/repo#1', 'hash123', 'PENDING', '{}', ?, ?)",
            (f"op_{jid}", time.time(), time.time()),
        )
        conn.execute(
            "INSERT INTO external_operation_jobs VALUES (?,?)", (f"op_{jid}", jid)
        )

    # Reconcile 404 for nonexistent job
    assert client.post("/jobs/unknown_job/reconcile").status_code == 404

    # Reconcile valid job
    assert (
        client.post(f"/jobs/{jid}/reconcile", json={"notes": "No receipt"}).status_code
        == 422
    )
    res = client.post(
        f"/jobs/{jid}/reconcile",
        json={
            "notes": "Confirmed delivered on GitHub manually",
            "operation_key": f"op_{jid}",
            "receipt": {"id": 71},
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "RECONCILED"
    assert data["job_id"] == jid
    assert data["job_status"] == "RETRYING"

    # Verify queue and operation log states
    assert queue.get_job(jid)["status"] == "RETRYING"
    op = queue.get_external_operation(f"op_{jid}")
    assert op["status"] == "SUCCESS"
    resumed = queue.claim_next_job()
    queue.complete_job(jid, claim_token=resumed["claim_token"])

    # Test action='retry' transitions job to RETRYING
    jid2 = queue.enqueue("org/repo#2", {})
    claim2 = queue.claim_next_job()
    queue.fail_job(
        jid2,
        "RECONCILIATION_REQUIRED",
        claim_token=claim2["claim_token"],
        state="RECONCILIATION_REQUIRED",
    )
    res_retry = client.post(
        f"/jobs/{jid2}/reconcile",
        json={"action": "retry", "notes": "Operator requested retry"},
    )
    assert res_retry.status_code == 200
    assert res_retry.json()["job_status"] == "RETRYING"
    assert queue.get_job(jid2)["status"] == "RETRYING"
    resumed2 = queue.claim_next_job()
    queue.complete_job(jid2, claim_token=resumed2["claim_token"])

    # Test action='cancel' transitions job to CANCELLED
    jid3 = queue.enqueue("org/repo#3", {})
    claim3 = queue.claim_next_job()
    queue.fail_job(
        jid3,
        "RECONCILIATION_REQUIRED",
        claim_token=claim3["claim_token"],
        state="RECONCILIATION_REQUIRED",
    )
    res_cancel = client.post(
        f"/jobs/{jid3}/reconcile",
        json={"action": "cancel", "notes": "Operator cancelled"},
    )
    assert res_cancel.status_code == 200
    assert res_cancel.json()["job_status"] == "CANCELLED"
    assert queue.get_job(jid3)["status"] == "CANCELLED"
