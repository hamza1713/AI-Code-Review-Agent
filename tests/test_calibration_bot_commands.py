"""
Programmatic pytest test suite verifying /fp, /confirm, and /calibration bot slash commands,
aliases (/cried-wolf, /fp-rate), auto-suppression, and markdown responses.
"""

import pytest
from production_fixtures import recorded_finding

from code_review_agent.bot.command_router import CommandRouter
from code_review_agent.calibration.outcome_store import (
    CONFIRMED_REAL,
    FALSE_POSITIVE,
    FindingOutcomeStore,
)
from code_review_agent.suppression_store import SuppressionStore


@pytest.fixture
def clean_db(tmp_path, monkeypatch):
    """Isolate database for command router tests."""
    test_db = tmp_path / "test_bot_calibration.db"
    monkeypatch.setenv("QUEUE_DB_PATH", str(test_db))
    return test_db


class TestBotSlashCommandsRouting:
    """Tests slash command detection, dispatching, and error handling."""

    def test_command_detection_and_parsing(self):
        assert CommandRouter.is_bot_command("/fp 9a4b2f1e") is True
        assert CommandRouter.is_bot_command("/confirm 9a4b2f1e") is True
        assert CommandRouter.is_bot_command("/calibration") is True
        assert CommandRouter.is_bot_command("/cried-wolf") is True
        assert CommandRouter.is_bot_command("/fp-rate") is True

        cmd, args = CommandRouter.parse_command("/fp 9a4b2f1e Unit test mock query")
        assert cmd == "fp"
        assert args == "9a4b2f1e Unit test mock query"

        cmd, args = CommandRouter.parse_command("/confirm abc12345")
        assert cmd == "confirm"
        assert args == "abc12345"

        cmd, args = CommandRouter.parse_command("/calibration 14")
        assert cmd == "calibration"
        assert args == "14"

    def test_fp_command_missing_args(self, clean_db):
        res = CommandRouter.dispatch("/fp", auto_post=False, trusted_operator=True)
        assert res.command == "fp"
        assert res.status == "ERROR"
        assert "Missing Fingerprint" in res.response_markdown

    def test_confirm_command_missing_args(self, clean_db):
        res = CommandRouter.dispatch("/confirm", auto_post=False, trusted_operator=True)
        assert res.command == "confirm"
        assert res.status == "ERROR"
        assert "Missing Fingerprint" in res.response_markdown

    def test_invalid_short_fingerprint_rejected(self, clean_db):
        res_fp = CommandRouter.dispatch(
            "/fp ab", auto_post=False, trusted_operator=True
        )
        assert res_fp.status == "ERROR"
        assert "invalid" in res_fp.response_markdown.lower()

        res_conf = CommandRouter.dispatch(
            "/confirm ab", auto_post=False, trusted_operator=True
        )
        assert res_conf.status == "ERROR"
        assert "invalid" in res_conf.response_markdown.lower()


class TestFalsePositiveAndConfirmLifecycle:
    """Tests state persistence, auto-suppression, and response generation."""

    def test_fp_command_records_outcome_and_suppresses(self, clean_db):
        owner, repo = "acme", "payment-service"
        fp = "9a4b2f1e8c7d6e5f"
        pr_url = f"https://github.com/{owner}/{repo}/pull/42"

        recorded_finding(FindingOutcomeStore(), f"{owner}/{repo}", fp, pr_id="42")
        # Dispatch /fp command
        res = CommandRouter.dispatch(
            f"/fp {fp} Not a real vulnerability in mock tests",
            pr_url=pr_url,
            auto_post=False,
            trusted_operator=True,
        )

        assert res.status == "SUCCESS"
        assert res.action_taken == "POST_COMMENT"
        assert "Finding Marked as False Positive" in res.response_markdown
        assert fp in res.response_markdown
        assert "auto-suppressed" not in res.response_markdown

        # Verify recorded in FindingOutcomeStore
        store = FindingOutcomeStore()
        finding = store.get_finding(f"{owner}/{repo}", fp)
        assert finding is not None
        assert finding["outcome"] == FALSE_POSITIVE
        assert "Not a real vulnerability" in finding["reason"]
        assert finding["source"] == "HUMAN_EXPLICIT"

        # Verify auto-suppressed in SuppressionStore
        supp_store = SuppressionStore()
        assert not supp_store.is_suppressed(f"{owner}/{repo}", fp)

    def test_confirm_command_records_true_positive(self, clean_db):
        owner, repo = "acme", "auth-service"
        fp = "b1c2d3e4f5a6b7c8"
        pr_url = f"https://github.com/{owner}/{repo}/pull/12"

        recorded_finding(FindingOutcomeStore(), f"{owner}/{repo}", fp, pr_id="12")
        # Dispatch /confirm command
        res = CommandRouter.dispatch(
            f"/confirm {fp} Verified SQL injection in production path",
            pr_url=pr_url,
            auto_post=False,
            trusted_operator=True,
        )

        assert res.status == "SUCCESS"
        assert res.action_taken == "POST_COMMENT"
        assert "Finding Confirmed Real" in res.response_markdown
        assert fp in res.response_markdown

        # Verify recorded in FindingOutcomeStore
        store = FindingOutcomeStore()
        finding = store.get_finding(f"{owner}/{repo}", fp)
        assert finding is not None
        assert finding["outcome"] == CONFIRMED_REAL
        assert finding["source"] == "HUMAN_EXPLICIT"

    def test_review_subcommand_routing(self, clean_db):
        """Verify /review fp and /review confirm subcommands route correctly."""
        owner, repo = "acme", "core"
        fp1 = "a1b2c3d4e5f60001"
        fp2 = "a1b2c3d4e5f60002"
        pr_url = f"https://github.com/{owner}/{repo}/pull/5"

        recorded_finding(FindingOutcomeStore(), f"{owner}/{repo}", fp1, pr_id="5")
        recorded_finding(FindingOutcomeStore(), f"{owner}/{repo}", fp2, pr_id="5")
        # /review fp
        res1 = CommandRouter.dispatch(
            f"/review fp {fp1} Subcommand false alarm",
            pr_url=pr_url,
            auto_post=False,
            trusted_operator=True,
        )
        assert res1.status == "SUCCESS"
        assert "Finding Marked as False Positive" in res1.response_markdown

        # /review confirm
        res2 = CommandRouter.dispatch(
            f"/review confirm {fp2}",
            pr_url=pr_url,
            auto_post=False,
            trusted_operator=True,
        )
        assert res2.status == "SUCCESS"
        assert "Finding Confirmed Real" in res2.response_markdown

    def test_slash_command_with_markdown_backticks_and_formatting(self, clean_db):
        """Verify backticks, colons, or quotes are cleanly stripped."""
        owner, repo = "acme", "clean-repo"
        pr_url = f"https://github.com/{owner}/{repo}/pull/8"

        recorded_finding(
            FindingOutcomeStore(), f"{owner}/{repo}", "9a4b2f1e", pr_id="8"
        )
        recorded_finding(
            FindingOutcomeStore(), f"{owner}/{repo}", "b1c2d3e4", pr_id="8"
        )
        # /fp `9a4b2f1e`
        res_fp = CommandRouter.dispatch(
            "/fp `9a4b2f1e` Markdown formatted",
            pr_url=pr_url,
            auto_post=False,
            trusted_operator=True,
        )
        assert res_fp.status == "SUCCESS"
        assert res_fp.metadata["fingerprint"] == "9a4b2f1e"

        # Verify SuppressionStore suppressed clean hex
        supp_store = SuppressionStore()
        assert not supp_store.is_suppressed(f"{owner}/{repo}", "9a4b2f1e")

        # Verify FindingOutcomeStore recorded clean hex
        store = FindingOutcomeStore()
        assert (
            store.get_finding(f"{owner}/{repo}", "9a4b2f1e")["outcome"]
            == FALSE_POSITIVE
        )

        # /confirm `b1c2d3e4:`
        res_conf = CommandRouter.dispatch(
            "/confirm `b1c2d3e4:`",
            pr_url=pr_url,
            auto_post=False,
            trusted_operator=True,
        )
        assert res_conf.status == "SUCCESS"
        assert res_conf.metadata["fingerprint"] == "b1c2d3e4"
        assert (
            store.get_finding(f"{owner}/{repo}", "b1c2d3e4")["outcome"]
            == CONFIRMED_REAL
        )

    def test_slash_command_rejects_non_hex_fingerprints(self, clean_db):
        """Verify non-hex strings are rejected with clean error message."""
        res_fp = CommandRouter.dispatch(
            "/fp not_a_valid_hex Reason", auto_post=False, trusted_operator=True
        )
        assert res_fp.status == "ERROR"
        assert res_fp.metadata["error"] == "invalid_fingerprint"

        res_conf = CommandRouter.dispatch(
            "/confirm !!!", auto_post=False, trusted_operator=True
        )
        assert res_conf.status == "ERROR"
        assert res_conf.metadata["error"] == "invalid_fingerprint"

    def test_review_subcommand_boundary_routing(self, clean_db):
        """Ensure /review fptest is not erroneously parsed as /review fp."""
        res = CommandRouter.dispatch(
            "/review fptest", auto_post=False, trusted_operator=True
        )
        assert res.command == "review"
        assert "no diff content" in res.response_markdown.lower()


class TestCalibrationReportingCommand:
    """Tests /calibration, /cried-wolf, /fp-rate command outputs."""

    def test_calibration_command_generates_markdown_table(self, clean_db):
        owner, repo = "acme", "analytics"
        pr_url = f"https://github.com/{owner}/{repo}/pull/7"
        repo_id = f"{owner}/{repo}"

        store = FindingOutcomeStore()
        # Seed 3 true positives and 1 false positive
        recorded_finding(
            store, repo_id, "fp1", category="SECURITY", outcome=CONFIRMED_REAL
        )
        recorded_finding(
            store, repo_id, "fp2", category="SECURITY", outcome=CONFIRMED_REAL
        )
        recorded_finding(
            store, repo_id, "fp3", category="QUALITY", outcome=CONFIRMED_REAL
        )
        recorded_finding(
            store, repo_id, "fp4", category="QUALITY", outcome=FALSE_POSITIVE
        )

        # 1. /calibration
        res = CommandRouter.dispatch(
            "/calibration", pr_url=pr_url, auto_post=False, trusted_operator=True
        )
        assert res.status == "SUCCESS"
        assert res.action_taken == "POST_COMMENT"
        assert (
            "## 🐺 Repository Calibration & Cried-Wolf Telemetry"
            in res.response_markdown
        )
        assert "| Category | Total |" in res.response_markdown
        assert "False-Positive Rate (FPR)" in res.response_markdown
        # 1 FP / (3 TP + 1 FP) = 25.0%
        assert "25.0%" in res.response_markdown

        # 2. Alias /cried-wolf
        res_alias1 = CommandRouter.dispatch(
            "/cried-wolf", pr_url=pr_url, auto_post=False, trusted_operator=True
        )
        assert res_alias1.status == "SUCCESS"
        assert "25.0%" in res_alias1.response_markdown

        # 3. Alias /fp-rate
        res_alias2 = CommandRouter.dispatch(
            "/fp-rate", pr_url=pr_url, auto_post=False, trusted_operator=True
        )
        assert res_alias2.status == "SUCCESS"
        assert "25.0%" in res_alias2.response_markdown

    def test_help_command_includes_calibration_and_signals(self, clean_db):
        res = CommandRouter.dispatch("/help", auto_post=False, trusted_operator=True)
        assert res.status == "SUCCESS"
        assert "/fp" in res.response_markdown
        assert "/confirm" in res.response_markdown
        assert "/calibration" in res.response_markdown


pytestmark = pytest.mark.usefixtures("offline_repository_client")
