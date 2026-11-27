"""Read-only metrics endpoints: per-rule calibration, review latency, pilot weekly."""

import time

import pytest
from fastapi.testclient import TestClient
from production_fixtures import recorded_finding

from code_review_agent.calibration.engine import score_rule_outcomes
from code_review_agent.calibration.outcome_store import (
    CONFIRMED_REAL,
    FALSE_POSITIVE,
    HEURISTIC,
    PENDING,
    FindingOutcomeStore,
)
from code_review_agent.models import TelemetryMetrics
from code_review_agent.observability.telemetry import TelemetryTracker, latency_summary
from code_review_agent.suppression_store import SuppressionStore
from code_review_agent.webhook_server import app


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    db = tmp_path / "metrics.db"
    monkeypatch.setenv("QUEUE_DB_PATH", str(db))
    monkeypatch.setenv("REVIEW_REQUIRE_AUTH", "false")
    return db


def _record(store, repo, fingerprint, category, outcome):
    recorded_finding(
        store,
        repo,
        fingerprint,
        category=category,
        evidence_grade=HEURISTIC,
        outcome=outcome,
        created_at=time.time(),
    )


def test_score_rule_outcomes_counts_triage_only():
    items = [
        {"outcome": CONFIRMED_REAL},
        {"outcome": FALSE_POSITIVE},
        {"outcome": FALSE_POSITIVE},
        {"outcome": PENDING},
    ]
    assert score_rule_outcomes(items) == (3, 1, 2, pytest.approx(2 / 3))


def test_rules_endpoint_is_read_only_and_reports_rates():
    repo = "acme/rules"
    store = FindingOutcomeStore()
    _record(store, repo, "r1", "SECURITY", CONFIRMED_REAL)
    _record(store, repo, "r2", "SECURITY", FALSE_POSITIVE)
    _record(store, repo, "r3", "QUALITY", FALSE_POSITIVE)

    with TestClient(app) as client:
        before = SuppressionStore().get_probation_rules(repo)
        resp = client.get(f"/api/calibration/rules?repo_id={repo}")
        assert resp.status_code == 200
        rules = {r["rule_id"]: r for r in resp.json()["rules"]}
        assert SuppressionStore().get_probation_rules(repo) == before

        metrics = client.get(f"/api/calibration/metrics?repo_id={repo}").json()

    assert rules, "expected at least one rule row"
    assert all("on_probation" in r and "acceptance_rate" in r for r in rules.values())
    assert sum(r["triaged"] for r in rules.values() if not r["rule_id"].startswith("category:")) == 3
    assert metrics["acceptance_rate"] == pytest.approx(1 / 3, abs=1e-3)


def test_latency_summary_and_endpoint(isolated_db):
    assert latency_summary(isolated_db)["runs"] == 0
    for seconds in (1.0, 2.0, 3.0, 10.0):
        TelemetryTracker.record_to_db(
            TelemetryMetrics(duration_seconds=seconds, final_verdict="COMMENT"),
            repo_id="acme/api",
            db_path=isolated_db,
        )
    summary = latency_summary(isolated_db, repo_id="acme/api")
    assert summary["runs"] == 4
    assert summary["latency_p50_seconds"] == 3.0
    assert summary["latency_p95_seconds"] == 10.0

    with TestClient(app) as client:
        data = client.get("/api/telemetry/latency?repo_id=acme/api").json()
    assert data["runs"] == 4


def test_pilot_weekly_endpoint_validates_window():
    with TestClient(app) as client:
        ok = client.get("/api/pilot/weekly?repository=acme/api&days=7")
        bad = client.get("/api/pilot/weekly?repository=acme/api&days=0")
    assert ok.status_code == 200
    assert ok.json()["runs"] == 0
    assert bad.status_code == 422
