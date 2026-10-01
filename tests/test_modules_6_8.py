"""Release and rollout gates: real SQLite evidence, independent labels, and fail-closed controls."""

import json
import logging
import sqlite3
from copy import deepcopy

import pytest
from release_fixture import profile, retained_release

from code_review_agent.data_policy import repository_scope, require_model_approval
from code_review_agent.models import ReviewAPIResponse, SastFinding
from code_review_agent.observability.alerts import evaluate_alerts
from code_review_agent.observability.telemetry import TelemetryTracker
from code_review_agent.pilot import PilotStore
from code_review_agent.release import validate_release
from code_review_agent.structured_logging import RedactedJSONFormatter


def response(head="a" * 40, findings=True):
    reviewer = profile()
    return ReviewAPIResponse(
        verdict="REQUEST CHANGES" if findings else "APPROVE",
        confidence_score=80,
        summary="Synthetic gate fixture",
        full_report="Not field evidence",
        analyzed_commit=head,
        reviewer_profile=reviewer,
        reviewer_profile_id=reviewer.profile_id,
        input_snapshot_hash="d" * 64,
        findings=[
            SastFinding(
                rule_id="SQL",
                severity="HIGH",
                file_path="a.py",
                line_number=1,
                snippet="query(user_input)",
                description="Untrusted input enters a SQL query without parameters.",
                fix_recommendation="Bind user input through SQL query parameters.",
                fingerprint="a" * 16,
            )
        ]
        if findings
        else [],
    )


@pytest.fixture
def release(tmp_path, monkeypatch):
    root = tmp_path / "retained-evidence"
    data = retained_release(root)
    monkeypatch.setenv("REVIEW_RELEASE_EVIDENCE_DIR", str(root))
    return data


def test_empty_pilot_is_not_promotable(tmp_path, release):
    store = PilotStore(str(tmp_path / "pilot.db"))
    report = store.report("org/repo")
    assert not report["shadow_gate"] and not report["advisory_gate"]
    assert report["shadow"]["actionable_rate"] is None
    with pytest.raises(ValueError, match="active reviewer profile"):
        store.control(
            "org/repo", "ADVISORY", "operator", "Ready to pilot", deepcopy(release)
        )
    with pytest.raises(PermissionError):
        store.require_advisory("org/repo")


def test_independent_labels_conflicts_and_retry_do_not_inflate_sample(tmp_path):
    store = PilotStore(str(tmp_path / "pilot.db"))
    review = response()
    run = store.record("org/repo", "1", review, "SHADOW")
    assert store.record("org/repo", "1", review, "SHADOW") == run
    store.label(
        run,
        "Alice",
        "DEFECT",
        {"a" * 16: "ACTIONABLE"},
        "Confirmed from source and test",
    )
    assert store.report("org/repo")["shadow"]["prs"] == 0
    with pytest.raises(sqlite3.IntegrityError):
        store.label(run, "ALICE", "DEFECT", {"a" * 16: "ACTIONABLE"}, "Same reviewer")
    store.label(
        run,
        "Bob",
        "DEFECT",
        {"a" * 16: "NOT_ACTIONABLE"},
        "Different independent result",
    )
    assert store.report("org/repo")["unresolved_runs"] == 1
    with pytest.raises(ValueError, match="third reviewer"):
        store.adjudicate(
            run, "alice", "DEFECT", {"a" * 16: "ACTIONABLE"}, "Resolve conflict"
        )
    store.adjudicate(
        run,
        "charlie",
        "DEFECT",
        {"a" * 16: "ACTIONABLE"},
        "Verified from source and independent test",
    )
    assert store.report("org/repo")["shadow"]["prs"] == 1
    assert store.weekly("org/repo")["runs"] == 1
    with pytest.raises(ValueError, match="every"):
        store.label(run, "Charlie", "DEFECT", {}, "Missing finding label")


def test_promotion_thresholds_pause_and_publication_identity(
    tmp_path, monkeypatch, release
):
    store = PilotStore(str(tmp_path / "pilot.db"))
    # These 100 generated fixtures test the gate. They never count as a real pilot.
    for pr in range(100):
        review = response(findings=pr >= 25)
        run = store.record("org/repo", str(pr), review, "SHADOW")
        outcomes = {"a" * 16: "ACTIONABLE"} if pr >= 25 else {}
        for reviewer in ("alice", "bob"):
            store.label(
                run,
                reviewer,
                "DEFECT" if pr >= 25 else "CLEAN",
                outcomes,
                "Synthetic gate fixture",
            )
    assert store.report("org/repo")["shadow_gate"]
    invalid = deepcopy(release)
    invalid["checks"]["image-smoke"] = "UNAVAILABLE"
    with pytest.raises(ValueError):
        store.control("org/repo", "ADVISORY", "operator", "Synthetic fixture", invalid)
    store.control(
        "org/repo", "ADVISORY", "operator", "Synthetic fixture", deepcopy(release)
    )
    store.require_advisory("org/repo")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("REVIEW_DEPLOYED_COMMIT", raising=False)
    monkeypatch.delenv("REVIEW_IMAGE_DIGEST", raising=False)
    with pytest.raises(PermissionError, match="deployed release"):
        store.require_advisory("org/repo")
    monkeypatch.setenv("REVIEW_DEPLOYED_COMMIT", "a" * 40)
    monkeypatch.setenv("REVIEW_IMAGE_DIGEST", "sha256:" + "b" * 64)
    store.require_advisory("org/repo")
    run = store.record("org/repo", "101", response(), "ADVISORY")
    with pytest.raises(ValueError, match="Wrong-commit"):
        store.publication(run, ["a" * 16], {"id": 71}, "wrong")
    store.publication(run, ["a" * 16], {"id": 71}, "a" * 40)
    store.publication(run, ["a" * 16], {"id": 71}, "a" * 40)
    store.publication(
        run, ["a" * 16], {"id": 71, "body": "Provider lookup adds metadata"}, "a" * 40
    )
    with pytest.raises(ValueError, match="advisory"):
        store.publication(run, ["a" * 16], {"id": 71, "event": "APPROVE"}, "a" * 40)
    with pytest.raises(ValueError, match="different receipt"):
        store.publication(run, ["a" * 16], {"id": 72}, "a" * 40)
    for reviewer in ("alice", "bob"):
        store.label(
            run, reviewer, "DEFECT", {"a" * 16: "ACTIONABLE"}, "Synthetic outcome"
        )
    assert store.report("org/repo")["advisory"]["labeled_findings"] == 1
    assert not store.report("org/repo")["advisory_gate"]
    store.control("org/repo", "PAUSED", "operator", "Incident response")
    with pytest.raises(PermissionError):
        store.require_advisory("org/repo")


def test_provider_consent_exact_model_repository_and_expiry(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    policy = tmp_path / "policy.json"
    monkeypatch.setenv("REVIEW_DATA_POLICY", str(policy))
    with pytest.raises(PermissionError):
        require_model_approval("gemini/test-model")
    data = {
        "approved_by": "operator",
        "approval_reference": "test only",
        "expires_at": "2099-01-01T00:00:00Z",
        "models": ["gemini/test-model"],
        "repositories": ["github:github.com/org/repo"],
        "provider_terms_reference": "approved terms",
        "retention_policy": "30 days",
    }
    policy.write_text(json.dumps(data))
    with repository_scope("github:github.com/org/repo"):
        assert require_model_approval("gemini/test-model")["approved_by"] == "operator"
        with pytest.raises(PermissionError):
            require_model_approval("gemini/other-model")
    with pytest.raises(PermissionError):
        require_model_approval("gemini/test-model")
    data["expires_at"] = "2000-01-01T00:00:00Z"
    policy.write_text(json.dumps(data))
    with repository_scope("github:github.com/org/repo"), pytest.raises(PermissionError):
        require_model_approval("gemini/test-model")


def test_usage_and_unknown_prices_remain_honest(monkeypatch):
    monkeypatch.delenv("REVIEW_MODEL_PRICING_JSON", raising=False)
    tracker = TelemetryTracker("unknown-model")
    tracker.start()
    metrics = tracker.stop(
        {"prompt_tokens": 100, "completion_tokens": 50, "source": "ESTIMATED"}
    )
    assert (
        metrics.estimated_cost_usd is None and metrics.token_usage_source == "ESTIMATED"
    )
    assert "Unavailable" in tracker.format_summary_table(metrics)
    monkeypatch.setenv(
        "REVIEW_MODEL_PRICING_JSON",
        json.dumps(
            {"unknown-model": {"prompt": 1, "completion": 2, "as_of": "2026-10-01"}}
        ),
    )
    measured = tracker.stop(
        {"prompt_tokens": 100, "completion_tokens": 50, "source": "PROVIDER_REPORTED"}
    )
    assert measured.estimated_cost_usd == 0.0002
    assert (
        measured.cost_source == "ESTIMATED"
        and measured.token_usage_source == "PROVIDER_REPORTED"
    )
    assert tracker.stop().estimated_cost_usd is None


def test_release_missing_checks_and_secret_redaction(monkeypatch, release):
    validate_release(deepcopy(release))
    for field in ("commit", "image_digest", "checks", "approved_by", "backup_sha256"):
        invalid = deepcopy(release)
        del invalid[field]
        with pytest.raises(ValueError):
            validate_release(invalid)
    monkeypatch.setenv("GITHUB_TOKEN", "secret-canary-token-123")
    record = logging.LogRecord(
        "review",
        logging.INFO,
        "",
        0,
        "job=opaque Bearer abc.def secret-canary-token-123",
        (),
        None,
    )
    formatted = RedactedJSONFormatter().format(record)
    assert "secret-canary-token-123" not in formatted and "abc.def" not in formatted
    codes = {
        alert["code"]
        for alert in evaluate_alerts(
            {
                "worker_running": True,
                "database_accessible": True,
                "publication_reconciliation_required": 1,
                "queue_oldest_age_seconds": 301,
            }
        )
    }
    assert codes == {"PUBLICATION_UNCERTAIN", "QUEUE_LATENCY"}
