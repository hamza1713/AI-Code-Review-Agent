"""
Comprehensive Unit & Integration Test Suite for Phase 5 Trust and Safety Release.

Covers:
1. Remediation Safety Engine (StructuredPatch, BranchGuardrails, Stale Head SHA, Rollback Manifest, Opt-In Policy)
2. Evidence Trust Grading & Self-Healing Integrity (4-Tier Grading, Assertion Integrity AST Verification)
3. External Side-Effect Idempotency & Queue Resilience (SQLite WAL Idempotency, Error Classification)
4. Production Security & RBAC (Maintainer-Only Slash Commands, Webhook Commenter Auth, Sandbox Security Defaults)
5. Suppression Governance (30-day default TTL, Severity Ceilings, Suppression Audit Trail)
"""

import ast
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_review_agent.bot.command_router import CommandRouter
from code_review_agent.governance.rules_engine import GovernanceConfigFile, RemediationPolicy, RulesEngine
from code_review_agent.models import (
    RollbackManifest,
    SastFinding,
    StructuredPatch,
    TestExecutionResult,
)
from code_review_agent.remediator import (
    AutoRemediator,
    BranchGuardrailViolation,
    PatchMismatchError,
    StaleHeadShaError,
)
from code_review_agent.sandbox.self_healer import (
    AssertionIntegrityError,
    EvidenceTrustGrade,
    TestSelfHealer,
)
from code_review_agent.sandbox.test_runner import SandboxTestRunner
from code_review_agent.suppression_store import SuppressionStore
from code_review_agent.webhook_queue import WebhookJobQueue, is_transient_error
from code_review_agent.webhook_server import (
    _bot_maintainer_associations,
    _is_authorized_commenter,
    _username_authorized,
)


@pytest.fixture
def test_db_path(tmp_path, monkeypatch):
    """Provide isolated SQLite WAL test database."""
    db_file = tmp_path / f"test_phase5_{uuid.uuid4().hex[:8]}.db"
    monkeypatch.setenv("QUEUE_DB_PATH", str(db_file))
    return str(db_file)


# =============================================================================
# 1. Remediation Safety Engine
# =============================================================================


class TestRemediationSafetyEngine:
    """Test structured patches, branch guardrails, stale head sha, and rollback manifests."""

    def test_structured_patch_application_success(self):
        original_code = (
            "def authenticate(user, password):\n"
            "    query = f\"SELECT * FROM users WHERE name = '{user}'\"\n"
            "    return db.execute(query)\n"
        )
        patch = StructuredPatch(
            file_path="auth.py",
            target_line=2,
            expected_old_text="query = f\"SELECT * FROM users WHERE name = '{user}'\"",
            replacement_text="query = \"SELECT * FROM users WHERE name = :user\"",
            rationale="Fix SQL injection with parameterized query"
        )

        patched = AutoRemediator.apply_structured_patch(original_code, patch)
        assert "SELECT * FROM users WHERE name = :user" in patched
        assert "query = f\"SELECT" not in patched

    def test_structured_patch_mismatch_raises_error(self):
        original_code = "def authenticate(user, password):\n    return False\n"
        patch = StructuredPatch(
            file_path="auth.py",
            target_line=2,
            expected_old_text="SELECT * FROM users WHERE name = '{user}'",
            replacement_text="SELECT * FROM users WHERE name = :user"
        )

        with pytest.raises(PatchMismatchError) as exc_info:
            AutoRemediator.apply_structured_patch(original_code, patch)
        assert "Structured patch mismatch" in str(exc_info.value)

    @pytest.mark.parametrize("protected_branch", [
        "main",
        "master",
        "release",
        "release/v1.0",
        "release/2026.09",
        "production",
        "production/us-east-1",
        "prod",
        "prod/eu-west-1",
    ])
    def test_is_branch_protected_blocks_critical_branches(self, protected_branch):
        assert AutoRemediator.is_branch_protected(protected_branch) is True

    @pytest.mark.parametrize("allowed_branch", [
        "feature/add-oauth",
        "fix/sql-injection",
        "remediation-9a4b2f1e",
        "dependabot/npm_and_yarn/lodash-4.17.21",
        "dev",
        "staging-test",
    ])
    def test_is_branch_protected_allows_feature_branches(self, allowed_branch):
        assert AutoRemediator.is_branch_protected(allowed_branch) is False

    def test_custom_blocked_branches_in_guardrails(self):
        assert AutoRemediator.is_branch_protected("staging-deploy", custom_blocked=["staging*"]) is True
        assert AutoRemediator.is_branch_protected("dev", custom_blocked=["staging*"]) is False

    def test_apply_remediation_blocks_protected_branch(self, test_db_path):
        mock_client = MagicMock()
        mock_pr_meta = MagicMock()
        mock_pr_meta.head_ref = "main"
        mock_pr_meta.head_sha = "abc12345"
        mock_client.fetch_pull_request_metadata.return_value = mock_pr_meta

        result = AutoRemediator.apply_remediation(
            pr_identifier="org/repo#10",
            fingerprint="9a4b2f1e00112233",
            client=mock_client,
            bypass_policy=True
        )

        assert result["status"] == "error"
        assert "Strict branch guardrail violation" in result["message"]
        assert result["branch"] == "main"

    def test_apply_remediation_stale_head_sha_check(self, test_db_path):
        mock_client = MagicMock()
        mock_pr_meta = MagicMock()
        mock_pr_meta.head_ref = "feature/security-fix"
        mock_pr_meta.head_sha = "current_head_new_commit_456"
        mock_client.fetch_pull_request_metadata.return_value = mock_pr_meta

        result = AutoRemediator.apply_remediation(
            pr_identifier="org/repo#10",
            fingerprint="9a4b2f1e00112233",
            client=mock_client,
            reviewed_head="old_reviewed_commit_123",
            bypass_policy=True
        )

        assert result["status"] == "error"
        assert "Stale Head SHA check failed" in result["message"]

    def test_remediation_policy_opt_in_governance(self, tmp_path):
        # Default policy: allow_direct_commits is False
        policy = RemediationPolicy()
        assert policy.allow_direct_commits is False

        # Config without remediation section defaults to blocked
        config = GovernanceConfigFile()
        engine = RulesEngine(config=config)
        allowed, msg = engine.is_remediation_commit_allowed("feature/my-branch")
        assert allowed is False
        assert "opt-in required" in msg or "false" in msg.lower()

        # Config with allow_direct_commits=True permits feature branch
        active_config = GovernanceConfigFile(
            remediation=RemediationPolicy(allow_direct_commits=True)
        )
        active_engine = RulesEngine(config=active_config)
        allowed, msg = active_engine.is_remediation_commit_allowed("feature/my-branch")
        assert allowed is True

        # Config with allow_direct_commits=True but branch is main
        allowed, msg = active_engine.is_remediation_commit_allowed("main")
        assert allowed is False
        assert "protected" in msg.lower()

    def test_remediation_audit_trail_and_rollback(self, test_db_path, tmp_path):
        rem_id = f"rem-{uuid.uuid4().hex[:8]}"
        repo = "acme/backend"
        branch = "feature/payment-fix"
        file_path = "services/payment.py"
        old_text = "token = request.args.get('token')"
        new_text = "token = request.headers.get('X-Token')"

        manifest = RollbackManifest(
            remediation_id=rem_id,
            repo_id=repo,
            pr_id="42",
            branch=branch,
            file_path=file_path,
            blob_sha_before="blob_before_111",
            commit_sha_after="commit_after_222",
            actor="alice@example.com",
            timestamp=time.time(),
            expected_old_text=old_text,
            replacement_text=new_text,
            status="APPLIED"
        )

        # 1. Record audit manifest
        AutoRemediator.record_remediation_audit(manifest)

        # 2. Retrieve manifest
        stored = AutoRemediator.get_remediation_manifest(rem_id)
        assert stored is not None
        assert stored["remediation_id"] == rem_id
        assert stored["repo_id"] == repo
        assert stored["status"] == "APPLIED"

        # 3. List remediations
        history = AutoRemediator.list_remediations(repo)
        assert len(history) >= 1
        assert history[0]["remediation_id"] == rem_id

        # 4. Rollback
        mock_client = MagicMock()
        # Mock fetch_file_content returning new_text
        mock_client.fetch_file_content.return_value = f"# Header\n{new_text}\n# Footer"
        mock_client.commit_file_change.return_value = {"sha": "revert-commit-333"}

        rollback_res = AutoRemediator.rollback_remediation(
            remediation_id=rem_id,
            client=mock_client
        )
        assert rollback_res["status"] == "success"
        assert rollback_res["rollback_commit_sha"] == "revert-commit-333"

        # Verify commit call restored old_text
        mock_client.commit_file_change.assert_called_once()
        commit_kwargs = mock_client.commit_file_change.call_args.kwargs
        assert old_text in commit_kwargs["content"]
        assert new_text not in commit_kwargs["content"]

        # Verify audit trail status updated to ROLLED_BACK
        updated_manifest = AutoRemediator.get_remediation_manifest(rem_id)
        assert updated_manifest["status"] == "ROLLED_BACK"

        # 5. Subsequent rollback on already rolled-back remediation is rejected
        second_rollback = AutoRemediator.rollback_remediation(rem_id, client=mock_client)
        assert second_rollback["status"] == "error"
        assert "already been rolled back" in second_rollback["message"]


# =============================================================================
# 2. Evidence Trust Grading & Self-Healing Integrity
# =============================================================================


class TestEvidenceTrustGradingAndSelfHealing:
    """Test 4-tier evidence grading, test suite telemetry persistence, and assertion integrity."""

    def test_trust_grade_original_pass(self):
        valid_test = (
            "def test_addition():\n"
            "    assert 1 + 1 == 2\n"
        )
        mock_runner = MagicMock()
        mock_runner.return_value = TestExecutionResult(
            executed=True, status="PASSED", tests_passed=1
        )

        res, healed = TestSelfHealer.heal_and_run(mock_runner, valid_test)
        assert res.trust_grade == EvidenceTrustGrade.EMPIRICAL_ORIGINAL_PASSED.value
        assert res.original_test_suite == valid_test
        assert res.healed_test_suite == valid_test.strip()
        assert res.mock_stub_warning is None

    def test_trust_grade_syntax_healed(self):
        markdown_wrapped = (
            "```python\n"
            "def test_multiplication():\n"
            "    assert 3 * 3 == 9\n"
            "```"
        )
        # Runner passes on cleaned syntax
        def runner(code, pr_content=None):
            ast.parse(code)
            return TestExecutionResult(executed=True, status="PASSED", tests_passed=1)

        res, healed = TestSelfHealer.heal_and_run(runner, markdown_wrapped)
        assert res.trust_grade == EvidenceTrustGrade.HEALED_SYNTAX_REPAIRED.value
        assert res.original_test_suite == markdown_wrapped
        assert "```" not in res.healed_test_suite
        assert res.mock_stub_warning is None

    def test_trust_grade_mock_stubbed(self):
        test_with_missing_pkg = (
            "import obscure_database_driver\n"
            "def test_connection():\n"
            "    client = obscure_database_driver.connect()\n"
            "    assert client is not None\n"
        )
        call_count = 0
        def runner(code, pr_content=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return TestExecutionResult(
                    executed=True,
                    status="ERROR",
                    stderr="ModuleNotFoundError: No module named 'obscure_database_driver'"
                )
            return TestExecutionResult(executed=True, status="PASSED", tests_passed=1)

        res, healed = TestSelfHealer.heal_and_run(runner, test_with_missing_pkg, max_attempts=2)
        assert res.trust_grade == EvidenceTrustGrade.HEALED_MOCK_STUBBED.value
        assert res.original_test_suite == test_with_missing_pkg
        assert "obscure_database_driver" in res.healed_test_suite
        assert res.mock_stub_warning is not None
        assert "in-memory" in res.mock_stub_warning.lower()
        assert "magicmock" in res.mock_stub_warning.lower()

    def test_assertion_integrity_verification_accepts_preserved_assertions(self):
        original = (
            "def test_calc():\n"
            "    x = 10\n"
            "    assert x > 5\n"
            "    assert x == 10\n"
        )
        candidate = (
            "# Auto-healed import header\n"
            "import pytest\n"
            "def test_calc():\n"
            "    x = 10\n"
            "    assert x > 5\n"
            "    assert x == 10\n"
        )
        assert TestSelfHealer.verify_assertion_integrity(original, candidate) is True

    def test_assertion_integrity_verification_rejects_deleted_assertion(self):
        original = (
            "def test_calc():\n"
            "    x = 10\n"
            "    assert x > 5\n"
            "    assert x == 10\n"
        )
        weakened = (
            "def test_calc():\n"
            "    x = 10\n"
            "    assert x > 5\n"
            "    # assert x == 10 removed\n"
        )
        assert TestSelfHealer.verify_assertion_integrity(original, weakened) is False

    def test_assertion_integrity_verification_rejects_modified_assertion(self):
        original = (
            "def test_auth():\n"
            "    assert is_admin('guest') is False\n"
        )
        tampered = (
            "def test_auth():\n"
            "    assert is_admin('guest') is True\n"  # Flipped logic
        )
        assert TestSelfHealer.verify_assertion_integrity(original, tampered) is False

    def test_heal_and_run_assertion_integrity_error_on_tampering(self):
        original_test = (
            "def test_security():\n"
            "    assert is_safe() is True\n"
        )
        # Runner returns missing module error so healer attempts repairs
        def tampering_runner(code, pr_content=None):
            return TestExecutionResult(
                executed=True,
                status="ERROR",
                stderr="ModuleNotFoundError: No module named 'auth_helper'"
            )

        # Mock heal_code to simulate an aggressive heuristic rewriting asserts
        with patch.object(TestSelfHealer, "heal_code", return_value="def test_security(): pass"):
            with pytest.raises(AssertionIntegrityError) as exc_info:
                TestSelfHealer.heal_and_run(tampering_runner, original_test)
            assert "Assertion integrity violation" in str(exc_info.value)


# =============================================================================
# 3. External Side-Effect Idempotency & Queue Resilience
# =============================================================================


class TestExternalIdempotencyAndQueueResilience:
    """Test external_operations_log table, deduplication, and error classification."""

    def test_external_operations_log_deduplication(self, test_db_path):
        q = WebhookJobQueue()
        op_key = f"github:issue_comment:{uuid.uuid4().hex[:8]}"

        # 1. First record -> success
        first = q.record_external_operation(
            operation_key=op_key,
            operation_type="post_comment",
            target_id="repo/123#1",
            payload={"body": "Hello PR"},
            status="SUCCESS",
            result={"comment_id": 9999}
        )
        assert first is True
        assert q.is_operation_executed(op_key) is True

        # 2. Duplicate record -> rejected by SQLite unique constraint
        duplicate = q.record_external_operation(
            operation_key=op_key,
            operation_type="post_comment",
            target_id="repo/123#1",
            payload={"body": "Hello PR Duplicate"},
            status="SUCCESS"
        )
        assert duplicate is False

        # 3. Retrieve stored operation
        record = q.get_external_operation(op_key)
        assert record is not None
        assert record["operation_key"] == op_key
        assert record["operation_type"] == "post_comment"
        assert record["target_id"] == "repo/123#1"
        assert "9999" in record["result_json"]

    @pytest.mark.parametrize("transient_err", [
        "HTTP 429 Too Many Requests: Rate limit exceeded",
        "500 Internal Server Error",
        "502 Bad Gateway",
        "503 Service Unavailable",
        "504 Gateway Timeout",
        "Connection reset by peer",
        "Connection refused to host",
        "RemoteDisconnected: connection closed prematurely",
        "ReadTimeout: request timed out after 30 seconds",
    ])
    def test_is_transient_error_detects_retryable_errors(self, transient_err):
        assert is_transient_error(Exception(transient_err)) is True

    @pytest.mark.parametrize("permanent_err", [
        "400 Bad Request: Malformed JSON payload",
        "401 Unauthorized: Invalid GitHub API token",
        "403 Forbidden: Missing write permissions on repository",
        "404 Not Found: Pull request does not exist",
        "422 Unprocessable Entity: Validation failed",
        "InputValidationError: Invalid parameter format",
        "Strict branch guardrail violation: commits to main are forbidden",
        "Structured patch mismatch for file.py",
        "Stale Head SHA check failed",
    ])
    def test_is_transient_error_detects_permanent_fail_fast_errors(self, permanent_err):
        assert is_transient_error(Exception(permanent_err)) is False

    def test_queue_fail_job_fail_fast_on_permanent_error(self, test_db_path):
        q = WebhookJobQueue()
        job_id = q.enqueue(pr_identifier="acme/app#1", payload={"test": True})

        # Claim the job
        claimed = q.claim_next_job(worker_id="worker-1", lease_seconds=30)
        assert claimed is not None
        assert claimed["job_id"] == job_id

        # Fail with permanent 401 Unauthorized error
        q.fail_job(job_id=job_id, error="401 Unauthorized: GitHub token expired")

        # Job must be marked FAILED immediately, without RETRYING state
        job = q.get_job(job_id)
        assert job["status"] == "FAILED"
        assert "401 Unauthorized" in job["last_error"]

    def test_queue_fail_job_retries_transient_error(self, test_db_path):
        q = WebhookJobQueue()
        job_id = q.enqueue(pr_identifier="acme/app#2", payload={"test": True})

        claimed = q.claim_next_job(worker_id="worker-1", lease_seconds=30)
        assert claimed is not None

        # Fail with transient 503 error
        q.fail_job(job_id=job_id, error="503 Service Unavailable")

        job = q.get_job(job_id)
        assert job["status"] == "RETRYING"
        assert job["attempts"] == 1
        assert "503 Service Unavailable" in job["last_error"]


# =============================================================================
# 4. Production Security & RBAC
# =============================================================================


class TestProductionSecurityAndRBAC:
    """Test maintainer permissions on slash commands and sandbox security defaults."""

    def test_maintainer_only_slash_commands_rejected_for_non_maintainers(self, test_db_path):
        # Non-maintainer trying /apply
        res_apply = CommandRouter.dispatch(
            "/apply 9a4b2f1e00112233",
            pr_url="https://github.com/acme/app/pull/1",
            author_association="CONTRIBUTOR",
            auto_post=False
        )
        assert res_apply.status == "ERROR"
        assert "Permission Denied" in res_apply.response_markdown
        assert "restricted to repository maintainers" in res_apply.response_markdown

        # Non-maintainer trying /suppress
        res_suppress = CommandRouter.dispatch(
            "/suppress 9a4b2f1e00112233 Accepted risk",
            pr_url="https://github.com/acme/app/pull/1",
            author_association="COLLABORATOR",
            auto_post=False
        )
        assert res_suppress.status == "ERROR"
        assert "Permission Denied" in res_suppress.response_markdown

        # Non-maintainer trying /unsuppress
        res_unsuppress = CommandRouter.dispatch(
            "/unsuppress 9a4b2f1e00112233",
            pr_url="https://github.com/acme/app/pull/1",
            author_association="NONE",
            auto_post=False
        )
        assert res_unsuppress.status == "ERROR"
        assert "Permission Denied" in res_unsuppress.response_markdown

        # Non-maintainer trying /review apply subcommand
        res_rev_apply = CommandRouter.dispatch(
            "/review apply 9a4b2f1e00112233",
            pr_url="https://github.com/acme/app/pull/1",
            author_association="FIRST_TIME_CONTRIBUTOR",
            auto_post=False
        )
        assert res_rev_apply.status == "ERROR"
        assert "Permission Denied" in res_rev_apply.response_markdown

    def test_maintainer_only_slash_commands_permitted_for_maintainers(self, test_db_path):
        fp = "abcd1234ef567890"
        # OWNER running /review suppress
        res_owner = CommandRouter.dispatch(
            f"/review suppress {fp} Test valid maintainer suppression",
            pr_url="https://github.com/acme/app/pull/5",
            author_association="OWNER",
            auto_post=False
        )
        assert res_owner.status == "SUCCESS"
        assert "Finding Suppressed" in res_owner.response_markdown

        # MEMBER running /review unsuppress
        res_member = CommandRouter.dispatch(
            f"/review unsuppress {fp}",
            pr_url="https://github.com/acme/app/pull/5",
            author_association="MEMBER",
            auto_post=False
        )
        assert res_member.status == "SUCCESS"
        assert "Finding Unsuppressed" in res_member.response_markdown

    def test_webhook_authorized_commenter_maintainer_restriction(self):
        # /help is open to anyone
        assert _is_authorized_commenter({"author_association": "NONE"}, "help") is True

        # Non-mutating command allowed for collaborator
        assert _is_authorized_commenter({"author_association": "COLLABORATOR"}, "describe") is True
        assert _is_authorized_commenter({"author_association": "COLLABORATOR"}, "ask") is True

        # Mutating commands restricted to OWNER and MEMBER
        assert _is_authorized_commenter({"author_association": "COLLABORATOR"}, "apply") is False
        assert _is_authorized_commenter({"author_association": "COLLABORATOR"}, "suppress") is False
        assert _is_authorized_commenter({"author_association": "COLLABORATOR"}, "unsuppress") is False

        assert _is_authorized_commenter({"author_association": "OWNER"}, "apply") is True
        assert _is_authorized_commenter({"author_association": "MEMBER"}, "suppress") is True

        # Subcommands via /review
        assert _is_authorized_commenter({"author_association": "COLLABORATOR"}, "review", subcommand="apply") is False
        assert _is_authorized_commenter({"author_association": "COLLABORATOR"}, "review", subcommand="suppress") is False
        assert _is_authorized_commenter({"author_association": "MEMBER"}, "review", subcommand="apply") is True

    def test_sandbox_local_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("REVIEW_ALLOW_LOCAL_SANDBOX", raising=False)
        monkeypatch.delenv("REVIEW_SANDBOX_IMAGE", raising=False)

        res = SandboxTestRunner.run_tests(
            test_code="def test_foo(): assert True"
        )
        assert res.executed is False
        assert res.trust_grade == "UNVERIFIED"
        assert "Execution disabled: configure a reviewed sandbox image" in res.summary_message


# =============================================================================
# 5. Suppression Governance
# =============================================================================


class TestSuppressionGovernance:
    """Test 30-day default TTL, severity ceilings, and audit trail in SuppressionStore."""

    def test_suppression_default_30_day_ttl(self, test_db_path):
        store = SuppressionStore(db_path=test_db_path)
        repo = "acme/backend"
        fp = "1122334455667788"

        before = time.time()
        store.suppress(repo_id=repo, fingerprint=fp, reason="Temporary exemption")
        after = time.time()

        items = store.list_suppressions(repo)
        assert len(items) == 1
        item = items[0]

        # Verify expires_at is approximately 30 days in the future
        expected_ttl_seconds = 30 * 86400
        assert item["expires_at"] >= before + expected_ttl_seconds - 5
        assert item["expires_at"] <= after + expected_ttl_seconds + 5

    def test_expired_suppressions_are_filtered(self, test_db_path):
        store = SuppressionStore(db_path=test_db_path)
        repo = "acme/backend"
        fp_active = "active_fp_123456"
        fp_expired = "expired_fp_789012"

        # Active suppression (30 days TTL)
        store.suppress(repo_id=repo, fingerprint=fp_active, reason="Active", ttl_seconds=86400)

        # Expired suppression (already expired 10 seconds ago)
        store.suppress(repo_id=repo, fingerprint=fp_expired, reason="Expired", ttl_seconds=-10)

        # Query active status
        assert store.is_suppressed(repo, fp_active) is True
        assert store.is_suppressed(repo, fp_expired) is False

        # List suppressions only includes unexpired
        active_items = store.list_suppressions(repo)
        fps = [i["fingerprint"] for i in active_items]
        assert fp_active in fps
        assert fp_expired not in fps

    def test_severity_ceiling_blocks_critical_without_override(self, test_db_path):
        store = SuppressionStore(db_path=test_db_path)
        repo = "acme/security"
        fp_regular = "regular_suppressed_1"
        fp_override = "override_suppressed_2"

        # Regular suppression without override keyword
        store.suppress(repo_id=repo, fingerprint=fp_regular, reason="False positive in demo")

        # Explicit override suppression
        store.suppress(repo_id=repo, fingerprint=fp_override, reason="Accepted risk: signed off by security architect")

        assert store.has_critical_override(repo, fp_regular) is False
        assert store.has_critical_override(repo, fp_override) is True

    def test_suppression_audit_log_records_events(self, test_db_path):
        store = SuppressionStore(db_path=test_db_path)
        repo = "acme/compliance"
        fp = "audit_test_fp_999"

        # 1. Record suppression
        store.suppress(
            repo_id=repo,
            fingerprint=fp,
            reason="Mock database credential",
            author="security-lead",
            pr_id="101"
        )

        # 2. Record unsuppress
        store.unsuppress(repo_id=repo, fingerprint=fp)

        # 3. Inspect audit trail
        logs = store.get_audit_log(repo_id=repo)
        assert len(logs) == 2

        # Most recent first
        unsuppress_entry, suppress_entry = logs[0], logs[1]

        assert suppress_entry["action"] == "SUPPRESS"
        assert suppress_entry["fingerprint"] == fp
        assert suppress_entry["author"] == "security-lead"
        assert suppress_entry["pr_id"] == "101"
        assert "Mock database credential" in suppress_entry["reason"]

        assert unsuppress_entry["action"] == "UNSUPPRESS"
        assert unsuppress_entry["fingerprint"] == fp


# =============================================================================
# 6. Deep Edge Cases, Side-Effect Idempotency & Production Security
# =============================================================================


class TestPhase5DeepEdgeCasesAndRegressions:
    """Rigorous verification of edge cases, idempotency, heartbeat resilience, and operational endpoints."""

    @pytest.mark.parametrize("protected_name", [
        "default",
        "trunk",
        "",
        "   ",
        None
    ])
    def test_default_and_empty_branch_guardrail(self, protected_name):
        assert AutoRemediator.is_branch_protected(protected_name) is True

    def test_apply_remediation_blocks_pr_base_ref(self, test_db_path):
        mock_client = MagicMock()
        mock_pr_meta = MagicMock()
        mock_pr_meta.head_ref = "develop"
        mock_pr_meta.base_ref = "develop"  # Target branch is same
        mock_pr_meta.head_sha = "abc12345"
        mock_client.fetch_pull_request_metadata.return_value = mock_pr_meta

        result = AutoRemediator.apply_remediation(
            pr_identifier="org/repo#12",
            fingerprint="9a4b2f1e00112233",
            client=mock_client,
            bypass_policy=True
        )
        assert result["status"] == "error"
        assert "Strict branch guardrail violation" in result["message"]

    def test_rollback_remediation_noop_failure(self, test_db_path):
        rem_id = f"rem_{uuid.uuid4().hex[:8]}"
        manifest = RollbackManifest(
            remediation_id=rem_id,
            repo_id="acme/service",
            pr_id="1",
            branch="feat",
            file_path="app.py",
            blob_sha_before="sha_before",
            commit_sha_after="sha_after",
            actor="tester",
            timestamp=time.time(),
            expected_old_text="old_code()",
            replacement_text="new_code()",
            status="COMMITTED"
        )
        AutoRemediator.record_remediation_audit(manifest)

        mock_client = MagicMock()
        # Mock file content where replacement_text is absent
        mock_client.fetch_file_content.return_value = "def unrelated():\n    pass\n"

        res = AutoRemediator.rollback_remediation(rem_id, client=mock_client)
        assert res["status"] == "error"
        assert "replacement text was not found" in res["message"]

    def test_apply_remediation_idempotent_skips_duplicate_commit(self, test_db_path, tmp_path):
        mock_client = MagicMock()
        mock_pr_meta = MagicMock()
        mock_pr_meta.head_ref = "feature/idempotent-fix"
        mock_pr_meta.base_ref = "main"
        mock_pr_meta.head_sha = "sha_head_100"
        mock_client.fetch_pull_request_metadata.return_value = mock_pr_meta
        mock_client.fetch_pull_request_diff.return_value = (
            "diff --git a/app.py b/app.py\n"
            "@@ -1,2 +1,2 @@\n"
            "-query = f\"SELECT * FROM users WHERE id = {uid}\"\n"
            "+query = \"SELECT * FROM users WHERE id = :uid\"\n"
        )
        mock_client.fetch_file_content.return_value = "query = f\"SELECT * FROM users WHERE id = {uid}\"\n"
        mock_client.commit_file_change.return_value = {"sha": "commit_first_run"}

        fp = "idem12345678"
        # Create a mock finding with matching fingerprint
        finding = SastFinding(
            rule_id="SQLI-01",
            name="SQL Injection",
            description="Potential SQL injection vulnerability",
            severity="HIGH",
            file_path="app.py",
            line_number=1,
            snippet="query = f\"SELECT * FROM users WHERE id = {uid}\"",
            fix_recommendation="Use parameterized query: `query = \"SELECT * FROM users WHERE id = :uid\"`",
            fingerprint=fp
        )

        with patch.object(AutoRemediator, "find_target_finding", return_value=finding):
            # First execution -> commits change
            res1 = AutoRemediator.apply_remediation(
                pr_identifier="org/repo#20",
                fingerprint=fp,
                client=mock_client,
                bypass_policy=True
            )
            assert res1["status"] == "success"
            assert res1["commit_sha"] == "commit_first_run"
            assert mock_client.commit_file_change.call_count == 1

            # Second execution -> detects existing operation and returns idempotent success without committing
            res2 = AutoRemediator.apply_remediation(
                pr_identifier="org/repo#20",
                fingerprint=fp,
                client=mock_client,
                bypass_policy=True
            )
            assert res2["status"] == "success"
            assert "idempotent" in res2["message"].lower()
            # Commit was NOT called a second time
            assert mock_client.commit_file_change.call_count == 1

    def test_assertion_integrity_multiset_detects_duplicate_deleted_assertion(self):
        orig = (
            "def test_multi_assert():\n"
            "    assert check() is True\n"
            "    do_work()\n"
            "    assert check() is True\n"
        )
        # Cand deleted the second assert
        cand = (
            "def test_multi_assert():\n"
            "    assert check() is True\n"
            "    do_work()\n"
        )
        assert TestSelfHealer.verify_assertion_integrity(orig, cand) is False

    def test_assertion_integrity_syntax_error_in_original_not_falsely_rejected(self):
        orig_with_syntax_err = (
            "def test_broken_syntax():\n"
            "    assert calculate(5) == 25\n"
            "    val = (unclosed_call(\n"
        )
        cand_healed = (
            "def test_broken_syntax():\n"
            "    assert calculate(5) == 25\n"
            "    val = (unclosed_call())\n"
        )
        # Should not crash or falsely reject intact assertion
        assert TestSelfHealer.verify_assertion_integrity(orig_with_syntax_err, cand_healed) is True

    def test_mock_stub_warning_in_reconciled_test_evidence(self):
        from code_review_agent.synthesis.reconciler import SynthesisReconciler

        te = TestExecutionResult(
            executed=True,
            status="PASSED",
            evidence_badge="PASSING",
            trust_grade="HEALED_MOCK_STUBBED",
            mock_stub_warning="Dependencies were stubbed in-memory with MagicMock."
        )
        rec = SynthesisReconciler.reconcile(
            sast_findings=[],
            rule_violations=[],
            test_execution=te,
            generated_tests="def test_sample(): assert True",
            pr_content="+def foo(): pass"
        )
        assert rec.test_evidence is not None
        assert "Mock Stub Warning" in rec.test_evidence.proves
        assert "in-memory" in rec.test_evidence.proves.lower()

    def test_execute_platform_review_idempotent_side_effects(self, test_db_path, tmp_path):
        from contextlib import contextmanager
        from code_review_agent.webhook_queue import execute_platform_review
        from code_review_agent.platform.base import PlatformPRIdentifier, PlatformPRMetadata

        fake_client = MagicMock()
        fake_client.fetch_pull_request_diff.return_value = "diff --git a/x b/x\n+ok\n"
        fake_client.fetch_pull_request_metadata.return_value = PlatformPRMetadata(head_sha="sha_idem_99", head_ref="feat")
        fake_client.list_pull_request_review_comments.return_value = []

        ident = PlatformPRIdentifier(
            platform="github", owner_or_project="idem-org", repo_or_slug="idem-repo", pr_id=5,
            raw_identifier="idem-org/idem-repo/pull/5",
        )

        fake_response = MagicMock()
        fake_response.verdict = "APPROVE"
        fake_response.findings = []
        fake_response.inline_comments = []
        fake_response.full_report = "## report"
        fake_response.model_dump.return_value = {"verdict": "APPROVE"}

        @contextmanager
        def fake_checkout(*args, **kwargs):
            yield str(tmp_path)

        with patch("code_review_agent.platform.factory.get_platform_client", return_value=(fake_client, ident)), \
             patch("code_review_agent.bot.checkout.temporary_pr_checkout", fake_checkout), \
             patch("code_review_agent.review_service.ReviewService.execute_review", return_value=fake_response):

            # First run: posts review and check-run
            execute_platform_review({}, "idem-org/idem-repo/pull/5")
            assert fake_client.post_pull_request_review.call_count == 1
            assert fake_client.create_or_update_check_run.call_count >= 1

            # Second run on same PR and head_sha (e.g. queue retry): skips duplicate side effects
            execute_platform_review({}, "idem-org/idem-repo/pull/5")
            assert fake_client.post_pull_request_review.call_count == 1  # Still 1! Not duplicated!

    def test_worker_heartbeat_prevents_stale_job_theft(self, test_db_path):
        q = WebhookJobQueue()
        job_id = q.enqueue(pr_identifier="org/repo#99", payload={"test": True})

        # Claim with short initial lease
        claimed = q.claim_next_job(worker_id="worker-A", lease_duration=1.0)
        assert claimed is not None
        assert claimed["job_id"] == job_id

        # Worker-A issues heartbeat extending lease to 60s
        pulse = q.heartbeat_job(job_id=job_id, worker_id="worker-A", lease_duration=60.0)
        assert pulse is True

        # Worker-B attempts to claim next job with a 0s lock timeout threshold
        # Because Worker-A's lease_expires_at is in the future, Worker-B must NOT steal it
        steal_attempt = q.claim_next_job(worker_id="worker-B", lock_timeout_seconds=0.001)
        assert steal_attempt is None

    def test_operational_endpoints_fail_closed_without_token(self, monkeypatch):
        from fastapi.testclient import TestClient
        from code_review_agent.webhook_server import app

        monkeypatch.setenv("REVIEW_REQUIRE_AUTH", "true")
        monkeypatch.delenv("REVIEW_API_TOKEN", raising=False)
        client = TestClient(app)

        # Operational endpoints fail closed (503 Service Unavailable when token unconfigured)
        assert client.get("/jobs").status_code == 503
        assert client.get("/api/cache/stats").status_code == 503
        assert client.get("/api/suppressions").status_code == 503
        assert client.post("/api/remediation/apply", json={}).status_code == 503

        # With token configured, invalid bearer returns 401 Unauthorized
        monkeypatch.setenv("REVIEW_API_TOKEN", "a" * 32)
        assert client.get("/jobs", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.get("/api/cache/stats", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.get("/api/suppressions", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.post("/api/remediation/apply", json={}, headers={"Authorization": "Bearer wrong"}).status_code == 401

    def test_slash_command_apply_enforces_stale_head_sha(self, test_db_path):
        mock_client = MagicMock()
        mock_pr_meta = MagicMock()
        mock_pr_meta.head_ref = "feature/security-fix"
        mock_pr_meta.head_sha = "new_head_pushed_commit_999"
        mock_client.fetch_pull_request_metadata.return_value = mock_pr_meta

        with patch("code_review_agent.bot.command_router.get_platform_client", return_value=(mock_client, MagicMock(owner_or_project="org", repo_or_slug="repo", pr_id=1, platform="github"))):
            res = CommandRouter.dispatch(
                "/apply 9a4b2f1e00112233",
                pr_url="https://github.com/org/repo/pull/1",
                author_association="OWNER",
                pr_metadata={"head_sha": "stale_old_reviewed_sha_111", "author": "alice"},
                client=mock_client,
                auto_post=False
            )
            assert res.status == "ERROR"
            assert "Stale Head SHA check failed" in res.response_markdown
