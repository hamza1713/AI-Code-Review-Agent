"""
Tests for Human Audit Verification Layer and Outcomes.
Verifies separation between INVALID_REPRODUCTION and FALSE_POSITIVE,
evidence attribution sources, Validation Error Rate calculation with explicit sample size n,
bot commands (/audit, /audit-confirm, /audit-reject), and FastAPI audit endpoints.
"""

import pytest
from production_fixtures import recorded_finding
from starlette.testclient import TestClient

from code_review_agent.bot.command_router import CommandRouter
from code_review_agent.calibration.engine import RepoCalibrationEngine
from code_review_agent.calibration.outcome_store import (
    AUDIT_REASON_ASSERTION_INCORRECT,
    AUDIT_REASON_CORRECT,
    AUDIT_REASON_FINDING_FALSE,
    AUDIT_REASON_PROVES_SOMETHING_ELSE,
    AUDIT_STATUS_INVALID,
    AUDIT_STATUS_VALID,
    CONFIRMED_REAL,
    FALSE_POSITIVE,
    INVALID_REPRODUCTION,
    SOURCE_HUMAN_EXPLICIT,
    SOURCE_PR_RESOLUTION,
    FindingOutcomeStore,
)
from code_review_agent.webhook_server import app


@pytest.fixture
def clean_db(tmp_path, monkeypatch):
    """Provide an isolated SQLite database for human audit tests."""
    db_file = tmp_path / "test_human_audit.db"
    monkeypatch.setenv("QUEUE_DB_PATH", str(db_file))
    store = FindingOutcomeStore(db_path=str(db_file))
    return store


class TestHumanAuditOutcomesAndAttribution:
    """Verifies outcome store records audit decisions accurately."""

    def test_record_human_audit_valid_reproduction(self, clean_db):
        repo = "acme/repo"
        fp = "abcd1234ef56"
        recorded_finding(
            clean_db,
            repo,
            fp,
            category="SECURITY",
            evidence_grade="REPRODUCED",
            severity="HIGH",
            generated_test_code="def test_vuln(): assert False",
            test_status="PASSED",
            claim_title="SQL Injection",
        )

        record = clean_db.record_human_audit(
            repo_id=repo,
            fingerprint=fp,
            is_valid=True,
            reason_code=AUDIT_REASON_CORRECT,
            notes="Verified test triggers defect properly",
            audited_by="alice",
        )

        assert record is not None
        assert record["outcome"] == CONFIRMED_REAL
        assert record["audit_status"] == AUDIT_STATUS_VALID
        assert record["audit_reason_code"] == AUDIT_REASON_CORRECT
        assert record["audited_by"] == "alice"
        assert record["source"] == SOURCE_HUMAN_EXPLICIT
        assert record["generated_test_code"] == "def test_vuln(): assert False"
        assert record["claim_title"] == "SQL Injection"

    def test_record_human_audit_invalid_reproduction_distinct_from_false_positive(
        self, clean_db
    ):
        repo = "acme/repo"
        fp = "deadbeef1234"
        recorded_finding(
            clean_db,
            repo,
            fp,
            category="QUALITY",
            evidence_grade="REPRODUCED",
            severity="MEDIUM",
            claim_title="Resource Leak",
        )

        # Reject because test proves something else
        record = clean_db.record_human_audit(
            repo_id=repo,
            fingerprint=fp,
            is_valid=False,
            reason_code=AUDIT_REASON_PROVES_SOMETHING_ELSE,
            notes="Test asserts timeout instead of connection leak",
            audited_by="bob",
        )

        assert record is not None
        # MUST be INVALID_REPRODUCTION, NOT FALSE_POSITIVE
        assert record["outcome"] == INVALID_REPRODUCTION
        assert record["audit_status"] == AUDIT_STATUS_INVALID
        assert record["audit_reason_code"] == AUDIT_REASON_PROVES_SOMETHING_ELSE
        assert record["audited_by"] == "bob"
        assert record["source"] == SOURCE_HUMAN_EXPLICIT

    def test_record_human_audit_finding_false_sets_false_positive(self, clean_db):
        repo = "acme/repo"
        fp = "feedbeef5678"
        recorded_finding(
            clean_db,
            repo,
            fp,
            category="SECURITY",
            evidence_grade="REPRODUCED",
            severity="HIGH",
        )

        record = clean_db.record_human_audit(
            repo_id=repo,
            fingerprint=fp,
            is_valid=False,
            reason_code=AUDIT_REASON_FINDING_FALSE,
            notes="Total false alarm",
            audited_by="carol",
        )

        assert record is not None
        # When finding itself is false, outcome is FALSE_POSITIVE
        assert record["outcome"] == FALSE_POSITIVE
        assert record["audit_status"] == AUDIT_STATUS_INVALID
        assert record["audit_reason_code"] == AUDIT_REASON_FINDING_FALSE

    def test_human_explicit_precedence_over_automated_inference(self, clean_db):
        repo = "acme/repo"
        fp = "11223344aabb"
        recorded_finding(
            clean_db,
            repo,
            fp,
            category="SECURITY",
            evidence_grade="REPRODUCED",
            severity="HIGH",
        )

        # Human audits and marks as INVALID_REPRODUCTION
        clean_db.record_human_audit(
            repo, fp, is_valid=False, reason_code=AUDIT_REASON_ASSERTION_INCORRECT
        )

        # System attempts to auto-resolve via PR resolution
        clean_db.update_outcome(
            repo, fp, outcome=CONFIRMED_REAL, source=SOURCE_PR_RESOLUTION
        )

        # Explicit human audit MUST NOT be overwritten by system inference
        record = clean_db.get_finding(repo, fp)
        assert record["outcome"] == INVALID_REPRODUCTION
        assert record["audit_status"] == AUDIT_STATUS_INVALID


class TestEngineValidationErrorRateAndMetrics:
    """Verifies mathematical calculation of Validation Error Rate and n."""

    def test_validation_error_rate_calculation(self, clean_db):
        repo = "acme/calc"
        # Seed 10 REPRODUCED findings:
        # 8 valid reproductions
        # 2 invalid reproductions
        for i in range(8):
            recorded_finding(
                clean_db,
                repo,
                f"val_{i}",
                category="SECURITY",
                evidence_grade="REPRODUCED",
                severity="HIGH",
            )
            clean_db.record_human_audit(
                repo, f"val_{i}", is_valid=True, reason_code=AUDIT_REASON_CORRECT
            )

        for i in range(2):
            recorded_finding(
                clean_db,
                repo,
                f"inval_{i}",
                category="SECURITY",
                evidence_grade="REPRODUCED",
                severity="HIGH",
            )
            clean_db.record_human_audit(
                repo,
                f"inval_{i}",
                is_valid=False,
                reason_code=AUDIT_REASON_PROVES_SOMETHING_ELSE,
            )

        engine = RepoCalibrationEngine(clean_db)
        metrics = engine.get_metrics(repo, window_days=30)

        assert metrics.audited_total == 10
        assert metrics.audited_valid == 8
        assert metrics.audited_invalid == 2
        # VER = 2 / 10 = 20.0%
        assert pytest.approx(metrics.validation_error_rate, rel=1e-3) == 0.20
        assert metrics.invalid_reproductions == 2

    def test_markdown_report_includes_human_audit_section(self, clean_db):
        repo = "acme/report_repo"
        recorded_finding(
            clean_db,
            repo,
            "r1",
            category="SECURITY",
            evidence_grade="REPRODUCED",
            severity="HIGH",
        )
        clean_db.record_human_audit(
            repo, "r1", is_valid=True, reason_code=AUDIT_REASON_CORRECT
        )

        recorded_finding(
            clean_db,
            repo,
            "r2",
            category="SECURITY",
            evidence_grade="REPRODUCED",
            severity="HIGH",
        )
        clean_db.record_human_audit(
            repo, "r2", is_valid=False, reason_code=AUDIT_REASON_PROVES_SOMETHING_ELSE
        )

        engine = RepoCalibrationEngine(clean_db)
        metrics = engine.get_metrics(repo, window_days=30)
        report = engine.generate_markdown_report(metrics)

        assert "### 🔬 REPRODUCED Evidence Quality (Human-Audited Sampling)" in report
        assert "Validation Error Rate" in report
        assert "50.0%" in report
        assert "TEST_PROVES_SOMETHING_ELSE" in report

    def test_trust_footer_renders_sample_size_and_ver(self, clean_db):
        repo = "acme/trust"
        recorded_finding(
            clean_db,
            repo,
            "f1",
            category="SECURITY",
            evidence_grade="REPRODUCED",
            severity="HIGH",
        )
        clean_db.record_human_audit(repo, "f1", is_valid=True)

        engine = RepoCalibrationEngine(clean_db)
        metrics = engine.get_metrics(repo, window_days=30)
        footer = engine.generate_trust_footer(metrics)

        assert "REPRODUCED Validation Error Rate" in footer
        assert "n = 1 audited sample" in footer


class TestAuditBotSlashCommands:
    """Verifies /audit, /audit-confirm, /audit-reject slash commands."""

    def test_audit_sample_command(self, clean_db):
        repo_id = "test_owner/test_repo"
        recorded_finding(
            clean_db,
            repo_id,
            "9a4b2f1e8c7d6e5f",
            category="SECURITY",
            evidence_grade="REPRODUCED",
            severity="HIGH",
            claim_title="SQLi in user query",
            file_path="src/query.py",
            line_number=42,
            test_status="PASSED",
            pr_id="101",
        )

        res = CommandRouter.dispatch(
            "/audit 5",
            auto_post=False,
            repo_id=repo_id,
            owner="test_owner",
            repo="test_repo",
            trusted_operator=True,
        )
        assert res.status == "SUCCESS"
        assert "Stratified Audit Sample" in res.response_markdown
        assert "9a4b2f1e8c7d6e5f" in res.response_markdown
        assert "SQLi in user query" in res.response_markdown
        assert "src/query.py:42" in res.response_markdown
        assert "/audit-confirm" in res.response_markdown
        assert "/audit-reject" in res.response_markdown

    def test_audit_confirm_command(self, clean_db):
        repo_id = "test_owner/test_repo"
        fp = "9a4b2f1e8c7d6e5f"
        recorded_finding(
            clean_db,
            repo_id,
            fp,
            category="SECURITY",
            evidence_grade="REPRODUCED",
            severity="HIGH",
        )

        res = CommandRouter.dispatch(
            f"/audit-confirm {fp} Accurately reproduces vulnerability",
            auto_post=False,
            repo_id=repo_id,
            owner="test_owner",
            repo="test_repo",
            pr_metadata={"author": "reviewer1"},
            trusted_operator=True,
        )
        assert res.status == "SUCCESS"
        assert "Verified as Valid" in res.response_markdown
        assert "VALID_REPRODUCTION" in res.response_markdown
        assert "*n* = 1" in res.response_markdown or "n = 1" in res.response_markdown

        # Verify in store
        record = clean_db.get_finding(repo_id, fp)
        assert record["outcome"] == CONFIRMED_REAL
        assert record["audit_status"] == AUDIT_STATUS_VALID
        assert record["audited_by"] == "reviewer1"

    def test_audit_reject_command_invalid_reproduction(self, clean_db):
        repo_id = "test_owner/test_repo"
        fp = "9a4b2f1e8c7d6e5f"
        recorded_finding(
            clean_db,
            repo_id,
            fp,
            category="SECURITY",
            evidence_grade="REPRODUCED",
            severity="HIGH",
        )

        res = CommandRouter.dispatch(
            f"/audit-reject {fp} PROVES_SOMETHING_ELSE Tests wrong route",
            auto_post=False,
            repo_id=repo_id,
            owner="test_owner",
            repo="test_repo",
            pr_metadata={"author": "reviewer2"},
            trusted_operator=True,
        )
        assert res.status == "SUCCESS"
        assert "INVALID_REPRODUCTION" in res.response_markdown
        assert "TEST_PROVES_SOMETHING_ELSE" in res.response_markdown

        record = clean_db.get_finding(repo_id, fp)
        assert record["outcome"] == INVALID_REPRODUCTION
        assert record["audit_status"] == AUDIT_STATUS_INVALID

    def test_audit_reject_command_finding_false_triggers_suppression(self, clean_db):
        repo_id = "test_owner/test_repo"
        fp = "9a4b2f1e8c7d6e5f"
        recorded_finding(
            clean_db,
            repo_id,
            fp,
            category="SECURITY",
            evidence_grade="REPRODUCED",
            severity="HIGH",
        )

        res = CommandRouter.dispatch(
            f"/audit-reject {fp} FINDING_FALSE Flawed analysis, not a bug",
            auto_post=False,
            repo_id=repo_id,
            owner="test_owner",
            repo="test_repo",
            pr_metadata={"author": "reviewer3"},
            trusted_operator=True,
        )
        assert res.status == "SUCCESS"
        assert "FALSE_POSITIVE" in res.response_markdown

        record = clean_db.get_finding(repo_id, fp)
        assert record["outcome"] == FALSE_POSITIVE

        # Check suppression
        from code_review_agent.suppression_store import SuppressionStore

        supp_store = SuppressionStore()
        assert not supp_store.is_suppressed(repo_id, fp)


class TestFastAPIAuditEndpoints:
    """Verifies REST endpoints for audit sampling and decision submission."""

    def test_api_audit_sample_and_submit_lifecycle(self, clean_db):
        client = TestClient(app)
        repo = "api_owner/api_repo"
        fp = "cafe1234babe5678"

        recorded_finding(
            clean_db,
            repo,
            fp,
            category="QUALITY",
            evidence_grade="REPRODUCED",
            severity="HIGH",
            claim_title="Memory Leak in parser",
            file_path="src/parser.py",
            line_number=99,
        )

        # 1. GET /api/calibration/audit/sample
        res_sample = client.get(
            f"/api/calibration/audit/sample?repo={repo}&sample_size=10"
        )
        assert res_sample.status_code == 200
        sample_data = res_sample.json()
        assert sample_data["sample_size"] == 1
        assert sample_data["findings"][0]["fingerprint"] == fp
        assert sample_data["findings"][0]["claim_title"] == "Memory Leak in parser"

        # 2. POST /api/calibration/audit/submit (Valid)
        payload = {
            "repo_id": repo,
            "fingerprint": fp,
            "is_valid": True,
            "reason_code": "REPRODUCTION_CORRECT",
            "notes": "Verified leak repro in valgrind",
            "audited_by": "qa_lead",
        }
        res_submit = client.post("/api/calibration/audit/submit", json=payload)
        assert res_submit.status_code == 200
        submit_data = res_submit.json()
        assert submit_data["status"] == "success"
        assert submit_data["audit_status"] == AUDIT_STATUS_VALID
        assert submit_data["audited_total"] == 1
        assert submit_data["audited_valid"] == 1
        assert submit_data["validation_error_rate"] == 0.0

        # Verify sample now returns 0 unaudited findings
        res_sample_after = client.get(
            f"/api/calibration/audit/sample?repo={repo}&sample_size=10&unaudited_only=true"
        )
        assert res_sample_after.status_code == 200
        assert res_sample_after.json()["sample_size"] == 0


pytestmark = pytest.mark.usefixtures("offline_repository_client")
