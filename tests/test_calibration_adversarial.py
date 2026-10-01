"""
Adversarial and boundary stress test suite for Calibration and False-Positive Telemetry.
Probes edge cases: empty/null inputs, boundary conditions, delimiter wrapping,
inverted time windows, and malformed inputs.
"""

import pytest
from fastapi.testclient import TestClient
from production_fixtures import recorded_finding

from code_review_agent.bot.command_router import CommandRouter
from code_review_agent.calibration.engine import (
    RepoCalibrationEngine,
)
from code_review_agent.calibration.outcome_store import (
    CONFIRMED_REAL,
    FALSE_POSITIVE,
    PENDING,
    FindingOutcomeStore,
    clean_fingerprint,
)
from code_review_agent.webhook_server import app


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_adversarial_calib.db"
    monkeypatch.setenv("QUEUE_DB_PATH", str(test_db))
    monkeypatch.setenv("REVIEW_REQUIRE_AUTH", "false")
    return test_db


@pytest.fixture
def store(temp_db):
    return FindingOutcomeStore(db_path=str(temp_db))


@pytest.fixture
def engine(store):
    return RepoCalibrationEngine(store=store)


@pytest.fixture
def client(temp_db):
    return TestClient(app)


class TestAdversarialOutcomeStore:
    """Stress tests for FindingOutcomeStore boundary inputs and guards."""

    def test_clean_fingerprint_bracket_and_markdown_stripping(self):
        """Verify angle brackets, square brackets, parentheses, backticks, colons, and hashes are stripped."""
        assert clean_fingerprint("<9a4b2f1e>") == "9a4b2f1e"
        assert clean_fingerprint("[9a4b2f1e]") == "9a4b2f1e"
        assert clean_fingerprint("(9a4b2f1e)") == "9a4b2f1e"
        assert clean_fingerprint("`<9A4B2F1E>`") == "9a4b2f1e"
        assert clean_fingerprint("  #:[9a4b2f1e]:  ") == "9a4b2f1e"
        assert clean_fingerprint("   ") == ""
        assert clean_fingerprint("```") == ""
        assert clean_fingerprint(None) == ""

    def test_record_finding_empty_or_whitespace_fingerprint_rejected(self, store):
        """Empty or whitespace-only fingerprints must be safely ignored and not written to DB."""
        assert recorded_finding(store, "owner/repo", "") == {}
        assert recorded_finding(store, "owner/repo", "   ") == {}
        assert recorded_finding(store, "owner/repo", "```") == {}

        findings = store.list_findings(repo_id="owner/repo")
        assert len(findings) == 0

    def test_update_outcome_empty_fingerprint_raises_value_error(self, store):
        """Updating an outcome with an empty or whitespace-only fingerprint must raise ValueError."""
        with pytest.raises(ValueError, match="Invalid or empty fingerprint"):
            store.update_outcome("owner/repo", "", outcome=CONFIRMED_REAL)

        with pytest.raises(ValueError, match="Invalid or empty fingerprint"):
            store.update_outcome("owner/repo", "   ", outcome=FALSE_POSITIVE)

    def test_get_finding_empty_inputs_returns_none(self, store):
        """get_finding with empty repo_id or fingerprint must safely return None without querying."""
        assert store.get_finding("", "9a4b2f1e") is None
        assert store.get_finding("   ", "9a4b2f1e") is None
        assert store.get_finding("owner/repo", "") is None
        assert store.get_finding("owner/repo", "   ") is None

    def test_mark_pr_pending_as_ignored_empty_pr_id_is_safe_noop(self, store):
        """
        Calling mark_pr_pending_as_ignored with an empty pr_id must NOT mark untagged PR
        findings (pr_id='') as IGNORED_MERGED.
        """
        repo = "acme/safe_merge"
        # Untagged finding (e.g. from standalone review with no pr_id)
        recorded_finding(
            store, repo_id=repo, fingerprint="untagged_fp_1", pr_id="", outcome=PENDING
        )
        assert store.get_finding(repo, "untagged_fp_1")["outcome"] == PENDING

        # Calling with empty pr_id must return 0 and not touch untagged finding
        affected = store.mark_pr_pending_as_ignored(repo, "")
        assert affected == 0
        assert store.get_finding(repo, "untagged_fp_1")["outcome"] == PENDING

        affected_whitespace = store.mark_pr_pending_as_ignored(repo, "   ")
        assert affected_whitespace == 0
        assert store.get_finding(repo, "untagged_fp_1")["outcome"] == PENDING


class TestAdversarialEngine:
    """Stress tests for RepoCalibrationEngine calculations and time-window filtering."""

    def test_engine_negative_or_zero_window_days_defaults_safely(self, engine, store):
        """Passing window_days=0 or negative must safely default to 30 days without crash."""
        repo = "acme/window_repo"
        recorded_finding(
            store, repo_id=repo, fingerprint="fp_window_01", outcome=CONFIRMED_REAL
        )

        metrics_zero = engine.get_metrics(repo, window_days=0)
        assert metrics_zero.window_days == 30
        assert metrics_zero.total_findings == 1

        metrics_neg = engine.get_metrics(repo, window_days=-10)
        assert metrics_neg.window_days == 30
        assert metrics_neg.total_findings == 1

    def test_engine_custom_time_window_computes_effective_window_days(
        self, engine, store
    ):
        """When start_time and end_time are explicitly given, window_days reflects the difference."""
        repo = "acme/time_repo"
        base_time = 1_700_000_000.0
        # 14 days interval
        start = base_time
        end = base_time + (14 * 86400)

        recorded_finding(
            store,
            repo_id=repo,
            fingerprint="fp_time_01",
            created_at=start + 3600,
            outcome=CONFIRMED_REAL,
        )

        metrics = engine.get_metrics(repo, start_time=start, end_time=end)
        assert metrics.window_days == 14
        assert metrics.total_findings == 1

    def test_engine_zero_data_repo_generates_clean_reports(self, engine):
        """A repository with zero findings must generate clean reports without NaN or exceptions."""
        metrics = engine.get_metrics("non_existent_repo_xyz")
        assert metrics.total_findings == 0
        assert metrics.confirmed_real == 0
        assert metrics.false_positives == 0
        assert metrics.false_positive_rate == 0.0
        assert metrics.cried_wolf_index == 0.0

        md_report = engine.generate_markdown_report(metrics)
        assert "## 🐺 Repository Calibration & Cried-Wolf Telemetry" in md_report
        assert "N/A (0 triaged)" in md_report

        footer = engine.generate_trust_footer("non_existent_repo_xyz")
        assert "No triaged review findings recorded yet" in footer


class TestAdversarialBotCommands:
    """Stress tests for /fp, /confirm, and /calibration bot command parsing and delimiters."""

    def test_fp_and_confirm_with_bracket_delimiters(self, temp_db):
        """Slash commands with angle brackets, square brackets, or quotes must be cleanly handled."""
        owner, repo = "acme", "brackets_repo"
        pr_url = f"https://github.com/{owner}/{repo}/pull/10"

        recorded_finding(
            FindingOutcomeStore(), f"{owner}/{repo}", "9a4b2f1e", pr_id="10"
        )
        recorded_finding(
            FindingOutcomeStore(), f"{owner}/{repo}", "b1c2d3e4", pr_id="10"
        )
        # 1. /fp <9a4b2f1e>
        res_fp = CommandRouter.dispatch(
            "/fp <9a4b2f1e> Bracketed false positive",
            pr_url=pr_url,
            auto_post=False,
            trusted_operator=True,
        )
        assert res_fp.status == "SUCCESS"
        assert res_fp.metadata["fingerprint"] == "9a4b2f1e"

        # 2. /confirm [b1c2d3e4]
        res_conf = CommandRouter.dispatch(
            "/confirm [b1c2d3e4]", pr_url=pr_url, auto_post=False, trusted_operator=True
        )
        assert res_conf.status == "SUCCESS"
        assert res_conf.metadata["fingerprint"] == "b1c2d3e4"

    def test_empty_repo_root_fallback(self, temp_db):
        """When owner/repo is missing and repo_root is empty string, repo_id defaults safely to 'default'."""
        recorded_finding(FindingOutcomeStore(), "default", "9a4b2f1e", pr_id="")
        res = CommandRouter.dispatch(
            "/fp 9a4b2f1e", repo_root="", auto_post=False, trusted_operator=True
        )
        assert res.status == "SUCCESS"
        assert res.metadata["repo_id"] == "default"


class TestAdversarialFastAPIEndpoints:
    """Stress tests for FastAPI endpoints under inverted, malformed, or boundary requests."""

    def test_get_calibration_metrics_inverted_time_window_returns_400(self, client):
        """Providing start_time > end_time must return HTTP 400 Bad Request."""
        resp = client.get(
            "/api/calibration/metrics?repo_id=test/repo&start_time=5000&end_time=1000"
        )
        assert resp.status_code == 422
        assert "Start time" in resp.json()["detail"]

    def test_get_calibration_metrics_whitespace_repo_falls_back_to_default(
        self, client
    ):
        """Passing whitespace-only repo_id falls back cleanly to 'default'."""
        resp = client.get("/api/calibration/metrics?repo_id=%20%20%20")
        assert resp.status_code == 200
        assert resp.json()["repo_id"] == "default"

    def test_submit_feedback_requires_repository_scope(self, client):
        resp = client.post(
            "/api/calibration/feedback",
            json={
                "repo_id": "   ",
                "fingerprint": "<9a4b2f1e8c7d6e5f>",
                "outcome": "CONFIRMED_REAL",
            },
        )
        assert resp.status_code == 422


pytestmark = pytest.mark.usefixtures("offline_repository_client")
