"""
Tests for Persistent SuppressionStore and finding lifecycle suppression integration.
"""

import pytest
from pathlib import Path

from code_review_agent.suppression_store import SuppressionStore
from code_review_agent.models import SastFinding, compute_finding_lifecycle


@pytest.fixture
def temp_store(tmp_path):
    db_file = tmp_path / "test_suppressions.db"
    return SuppressionStore(db_path=str(db_file))


def test_suppression_lifecycle(temp_store):
    repo = "owner/my-app"
    fp1 = "9a4b2f1e00112233"
    fp2 = "8877665544332211"

    # Initially empty
    assert not temp_store.is_suppressed(repo, fp1)
    assert temp_store.get_suppressed_fingerprints(repo) == set()

    # Suppress fp1
    success = temp_store.suppress(
        repo_id=repo,
        fingerprint=fp1,
        reason="Mock test dummy password",
        author="alice",
        pr_id="42"
    )
    assert success is True
    assert temp_store.is_suppressed(repo, fp1)
    assert not temp_store.is_suppressed(repo, fp2)
    assert temp_store.get_suppressed_fingerprints(repo) == {fp1}

    # Suppress fp2
    temp_store.suppress(repo, fp2, reason="Legacy code", author="bob")
    assert temp_store.get_suppressed_fingerprints(repo) == {fp1, fp2}

    # Listing suppressions
    items = temp_store.list_suppressions(repo)
    assert len(items) == 2
    fps = [i["fingerprint"] for i in items]
    assert fp1 in fps and fp2 in fps

    # Unsuppress fp1
    deleted = temp_store.unsuppress(repo, fp1)
    assert deleted is True
    assert not temp_store.is_suppressed(repo, fp1)
    assert temp_store.is_suppressed(repo, fp2)
    assert temp_store.get_suppressed_fingerprints(repo) == {fp2}

    # Unsuppress non-existent fp
    assert temp_store.unsuppress(repo, "non_existent_fp") is False


def test_repo_normalization(temp_store):
    fp = "abcdef1234567890"
    # Suppress via full URL
    temp_store.suppress(
        repo_id="https://github.com/my-org/my-project/pull/123",
        fingerprint=fp,
        reason="Test URL normalization"
    )

    # Lookup via clean owner/repo
    assert temp_store.is_suppressed("my-org/my-project", fp)
    assert temp_store.is_suppressed("MY-ORG/MY-PROJECT", fp)
    assert temp_store.is_suppressed("my-org/my-project/pull/456", fp)


def test_suppressed_finding_lifecycle_integration(temp_store):
    repo = "test-org/test-repo"
    fp_active = "1111222233334444"
    fp_suppressed = "5555666677778888"

    # Suppress fp_suppressed in store
    temp_store.suppress(repo, fp_suppressed, reason="Accepted risk")
    suppressed_set = temp_store.get_suppressed_fingerprints(repo)

    f1 = SastFinding(
        rule_id="SAST-SQLI-001",
        name="SQL Injection",
        description="Vulnerable SQL query",
        severity="CRITICAL",
        file_path="app/db.py",
        line_number=10,
        snippet="cursor.execute(f'SELECT * FROM users WHERE id = {user_id}')",
        fix_recommendation="Use parameterized queries",
        fingerprint=fp_active
    )

    f2 = SastFinding(
        rule_id="SAST-HARDCODED-SECRET-001",
        name="Hardcoded Secret",
        description="Found test secret",
        severity="HIGH",
        file_path="tests/mock_secrets.py",
        line_number=5,
        snippet="TEST_TOKEN = 'secret_12345'",
        fix_recommendation="Move to env var",
        fingerprint=fp_suppressed
    )

    lifecycle = compute_finding_lifecycle(
        current_findings=[f1, f2],
        previous_findings=[],
        suppressed_fingerprints=suppressed_set
    )

    # f1 is NEW, f2 is SUPPRESSED
    assert len(lifecycle.new) == 1
    assert lifecycle.new[0].fingerprint == fp_active
    assert len(lifecycle.suppressed) == 1
    assert lifecycle.suppressed[0].fingerprint == fp_suppressed
    assert lifecycle.suppressed[0].lifecycle_status == "SUPPRESSED"


def test_execute_platform_review_with_suppressed_blocking_finding(tmp_path, monkeypatch):
    from unittest.mock import MagicMock, patch
    from contextlib import contextmanager
    from code_review_agent.webhook_queue import execute_platform_review
    from code_review_agent.platform.base import PlatformPRIdentifier, PlatformPRMetadata

    test_db = tmp_path / "test_exec_suppress.db"
    monkeypatch.setenv("QUEUE_DB_PATH", str(test_db))

    store = SuppressionStore(db_path=str(test_db))
    fp_blocked = "beefbeef12345678"
    store.suppress("test-org/test-repo", fp_blocked, reason="Accepted risk in demo")

    fake_client = MagicMock()
    fake_client.fetch_pull_request_diff.return_value = "diff --git a/x b/x\n+ok\n"
    fake_client.fetch_pull_request_metadata.return_value = PlatformPRMetadata(head_sha="sha1", head_ref="feat")
    fake_client.list_pull_request_review_comments.return_value = []

    ident = PlatformPRIdentifier(
        platform="github", owner_or_project="test-org", repo_or_slug="test-repo", pr_id=1,
        raw_identifier="test-org/test-repo/pull/1",
    )

    finding = SastFinding(
        rule_id="SAST-SQLI-001",
        name="SQL Injection",
        description="SQL injection vulnerability",
        severity="CRITICAL",
        file_path="app/db.py",
        line_number=10,
        snippet="execute(query)",
        fix_recommendation="Use parameterized query",
        fingerprint=fp_blocked
    )

    fake_response = MagicMock()
    fake_response.verdict = "REQUEST CHANGES"
    fake_response.findings = [finding]
    fake_response.inline_comments = []
    fake_response.full_report = "## report"
    fake_response.model_dump.return_value = {"verdict": "APPROVE"}

    @contextmanager
    def fake_checkout(*args, **kwargs):
        yield str(tmp_path)

    with patch("code_review_agent.platform.factory.get_platform_client", return_value=(fake_client, ident)), \
         patch("code_review_agent.bot.checkout.temporary_pr_checkout", fake_checkout), \
         patch("code_review_agent.review_service.ReviewService.execute_review", return_value=fake_response):
        res = execute_platform_review({}, "test-org/test-repo/pull/1")

    # The verdict was overridden to APPROVE because the single CRITICAL finding was suppressed!
    fake_client.post_pull_request_review.assert_called_once()
    assert fake_client.post_pull_request_review.call_args.kwargs["event"] == "APPROVE"
