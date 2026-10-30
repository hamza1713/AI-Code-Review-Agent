"""
Unit and integration tests for Phase 2 Workstream 2.2:
Google Tricorder Feedback Calibration, Automatic Probation Circuit Breaker,
SuppressionStore Integration, and FastAPI Endpoints.
"""

import time

import pytest
from fastapi.testclient import TestClient
from production_fixtures import recorded_finding

from code_review_agent.calibration.api import router
from code_review_agent.calibration.engine import RepoCalibrationEngine
from code_review_agent.calibration.outcome_store import (
    CONFIRMED_REAL,
    FALSE_POSITIVE,
    FindingOutcomeStore,
)
from code_review_agent.models import ReviewAPIResponse, SastFinding
from code_review_agent.review_policy import apply_finding_policy
from code_review_agent.suppression_store import SuppressionStore


@pytest.fixture
def temp_db(tmp_path):
    return str(tmp_path / "tricorder_test.db")


@pytest.fixture
def outcome_store(temp_db):
    return FindingOutcomeStore(db_path=temp_db)


@pytest.fixture
def supp_store(temp_db):
    return SuppressionStore(db_path=temp_db)


@pytest.fixture
def engine(outcome_store):
    return RepoCalibrationEngine(store=outcome_store)


def test_circuit_breaker_insufficient_data(engine, supp_store):
    """Rules with fewer than min_sample evaluations should not trigger circuit breaker."""
    repo = "corp/payments"
    now = time.time()
    for i in range(5):
        recorded_finding(
            engine.store,
            repo,
            f"fp_{i}",
            category="SECURITY",
            outcome=FALSE_POSITIVE,
            created_at=now,
            metadata={"rule_id": "SEC-INSUFFICIENT"},
        )

    results = engine.evaluate_tricorder_circuit_breaker(
        repo_id=repo,
        min_sample=10,
        max_fpr=0.10,
        suppression_store=supp_store,
    )
    assert any(
        r["rule_id"] == "SEC-INSUFFICIENT" and r["status"] == "INSUFFICIENT_DATA"
        for r in results
    )
    assert not supp_store.is_rule_on_probation(repo, "SEC-INSUFFICIENT")


def test_circuit_breaker_probation_triggered_when_fpr_exceeds_threshold(
    engine, supp_store
):
    """When a rule exceeds 10% FPR across >= 10 samples, it must be put on probation."""
    repo = "corp/payments"
    now = time.time()

    # Create 8 False Positives and 2 True Positives -> 80% FPR (well above 10%)
    for i in range(8):
        recorded_finding(
            engine.store,
            repo,
            f"noisy_fp_{i}",
            category="SECURITY",
            outcome=FALSE_POSITIVE,
            created_at=now,
            metadata={"rule_id": "SEC-NOISY-RULE"},
        )
    for i in range(2):
        recorded_finding(
            engine.store,
            repo,
            f"noisy_tp_{i}",
            category="SECURITY",
            outcome=CONFIRMED_REAL,
            created_at=now,
            metadata={"rule_id": "SEC-NOISY-RULE"},
        )

    results = engine.evaluate_tricorder_circuit_breaker(
        repo_id=repo,
        min_sample=10,
        max_fpr=0.10,
        suppression_store=supp_store,
    )
    match = next((r for r in results if r["rule_id"] == "SEC-NOISY-RULE"), None)
    assert match is not None
    assert match["status"] == "PROBATION"
    assert match["action"] == "PROBATION_APPLIED"
    assert match["false_positive_rate"] == 0.80
    assert match["total_evaluations"] == 10
    assert supp_store.is_rule_on_probation(repo, "SEC-NOISY-RULE")


def test_circuit_breaker_lifts_probation_when_healthy(engine, supp_store):
    """When a probationed rule recovers and meets <= 10% FPR, probation is lifted."""
    repo = "corp/payments"
    supp_store.put_rule_on_probation(
        repo, "SEC-RECOVERING", fpr=0.25, total=12, negative_signals=3
    )
    assert supp_store.is_rule_on_probation(repo, "SEC-RECOVERING")

    now = time.time()
    # 10 evaluations: 1 FP, 9 TP -> 10% FPR
    recorded_finding(
        engine.store,
        repo,
        "rec_fp_0",
        category="SECURITY",
        outcome=FALSE_POSITIVE,
        created_at=now,
        metadata={"rule_id": "SEC-RECOVERING"},
    )
    for i in range(9):
        recorded_finding(
            engine.store,
            repo,
            f"rec_tp_{i}",
            category="SECURITY",
            outcome=CONFIRMED_REAL,
            created_at=now,
            metadata={"rule_id": "SEC-RECOVERING"},
        )

    results = engine.evaluate_tricorder_circuit_breaker(
        repo_id=repo,
        min_sample=10,
        max_fpr=0.10,
        suppression_store=supp_store,
    )
    match = next((r for r in results if r["rule_id"] == "SEC-RECOVERING"), None)
    assert match is not None
    assert match["status"] == "HEALTHY"
    assert match["action"] == "PROBATION_LIFTED"
    assert not supp_store.is_rule_on_probation(repo, "SEC-RECOVERING")


def test_apply_finding_policy_with_probation_suppression():
    """Verify apply_finding_policy suppresses findings belonging to probationed rules."""
    finding_normal = SastFinding(
        severity="MEDIUM",
        rule_id="RULE-HEALTHY",
        category="SECURITY",
        description="Normal security check finding.",
        fix_recommendation="Ensure input validation is applied to parameters.",
        file_path="src/main.py",
        line_number=5,
        snippet="+ input_data = request.args.get('val')",
        fingerprint="0123456789abcdef",
    )
    finding_probation = SastFinding(
        severity="LOW",
        rule_id="RULE-PROBATION",
        category="QUALITY",
        description="Overly pedantic styling check finding.",
        fix_recommendation="Ensure variable naming conforms to snake_case style.",
        file_path="src/main.py",
        line_number=6,
        snippet="+ myVar = 1",
        fingerprint="fedcba9876543210",
    )

    diff = (
        "diff --git a/src/main.py b/src/main.py\n"
        "@@ -4,0 +5,2 @@\n"
        "+ input_data = request.args.get('val')\n"
        "+ myVar = 1\n"
    )

    response = ReviewAPIResponse(
        verdict="COMMENT",
        confidence_score=90,
        summary="Test summary",
        reviewed_diff=diff,
        analyzed_commit="sha-commit-1",
        analysis_complete=True,
        findings=[finding_normal, finding_probation],
    )

    apply_finding_policy(
        response,
        probation_rules={"RULE-PROBATION"},
    )

    # Healthy rule finding remains eligible
    assert "PROBATION_SUPPRESSED" not in finding_normal.publication_reasons
    assert finding_normal.publication_status == "ELIGIBLE"

    # Probationed rule finding is suppressed and withheld
    assert "PROBATION_SUPPRESSED" in finding_probation.publication_reasons
    assert finding_probation.publication_status == "WITHHELD"

    # Only healthy finding is in inline comments
    fingerprints = [c.fingerprint for c in response.inline_comments]
    assert "0123456789abcdef" in fingerprints
    assert "fedcba9876543210" not in fingerprints


def test_circuit_breaker_api_endpoints(temp_db, monkeypatch):
    """Test /circuit-breaker/evaluate and /circuit-breaker/status endpoints."""
    from fastapi import FastAPI

    monkeypatch.setenv("QUEUE_DB_PATH", temp_db)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    # Call evaluate
    eval_resp = client.post(
        "/api/calibration/circuit-breaker/evaluate",
        json={
            "repo_id": "corp/gateway",
            "window_days": 30,
            "min_sample": 5,
            "max_fpr": 0.10,
        },
    )
    assert eval_resp.status_code == 200
    data = eval_resp.json()
    assert data["status"] == "success"
    assert data["repo_id"] == "corp/gateway"

    # Call status
    status_resp = client.get(
        "/api/calibration/circuit-breaker/status",
        params={"repo_id": "corp/gateway"},
    )
    assert status_resp.status_code == 200
    status_data = status_resp.json()
    assert status_data["status"] == "success"
    assert "probations" in status_data
