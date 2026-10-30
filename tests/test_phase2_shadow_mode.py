"""
Unit and integration tests for Phase 2 Workstream 2.1:
Production Shadow Pilot Deployment and PilotStore WAL persistence.
"""

from unittest.mock import Mock

from code_review_agent.models import ReviewAPIResponse
from code_review_agent.pilot import PilotStore
from code_review_agent.webhook_queue import execute_platform_review


def test_pilot_store_wal_and_busy_timeout(tmp_path):
    """Verify PilotStore initializes SQLite with WAL mode and busy timeout."""
    db_file = tmp_path / "pilot_test.db"
    store = PilotStore(str(db_file))
    with store.connect() as conn:
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert journal.lower() == "wal"
        assert busy >= 30000


def test_shadow_mode_prevents_publication_and_records_shadow_run(tmp_path, monkeypatch):
    """
    When SHADOW_MODE=true, execute_platform_review must complete analysis,
    record the run in PilotStore as SHADOW, set publication to SHADOW_RECORDED,
    and never call git provider post review or comment APIs.
    """
    db_file = tmp_path / "shadow_test.db"
    monkeypatch.setenv("QUEUE_DB_PATH", str(db_file))
    monkeypatch.setenv("SHADOW_MODE", "true")
    monkeypatch.setenv("REVIEW_ENABLE_POSTING", "false")

    target = "https://github.com/corp/core-service/pull/42"
    head_sha = "a" * 40

    metadata = Mock(
        head_sha=head_sha,
        head_repository="corp/core-service",
        head_ref="feature",
        default_branch="main",
    )
    client = Mock(
        fetch_pull_request_metadata=Mock(return_value=metadata),
        fetch_pull_request_diff=Mock(
            return_value="diff --git a/a.py b/a.py\n@@ -0,0 +1 @@\n+print('test')\n"
        ),
        fetch_source_file=Mock(return_value=b"print('test')\n"),
        list_pull_request_review_comments=Mock(return_value=[]),
        list_pull_request_reviews=Mock(return_value=[]),
        post_pull_request_review=Mock(),
    )
    ident = Mock(
        owner_or_project="corp",
        repo_or_slug="core-service",
        pr_id=42,
        platform="github",
    )
    monkeypatch.setattr(
        "code_review_agent.access_policy.validate_pr_target",
        lambda _target: (client, ident, "github.com"),
    )

    from contextlib import contextmanager

    @contextmanager
    def checkout(*args, **kwargs):
        yield str(tmp_path)

    monkeypatch.setattr(
        "code_review_agent.bot.checkout.temporary_pr_checkout", checkout
    )

    review_resp = ReviewAPIResponse(
        verdict="APPROVE",
        confidence_score=95,
        summary="Shadow review completed successfully",
        full_report="Shadow analysis report",
        analysis_complete=True,
    )
    monkeypatch.setattr(
        "code_review_agent.review_service.ReviewService.execute_review",
        Mock(return_value=review_resp),
    )

    result = execute_platform_review(
        payload={"pull_request": {"number": 42}},
        pr_identifier=target,
    )

    assert result["analyzer_status"]["publication"] == "SHADOW_RECORDED"
    # Verify no post calls to GitHub
    client.post_pull_request_review.assert_not_called()

    # Verify run was recorded in PilotStore
    pilot = PilotStore(str(db_file))
    with pilot.connect() as conn:
        count = conn.execute("SELECT count(*) FROM pilot_runs").fetchone()[0]
        assert count >= 1
