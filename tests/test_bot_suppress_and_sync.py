"""
Tests for Bot Slash Commands (/review suppress, /unsuppress, /explain, /rerun)
and Bidirectional Comment Synchronization.
"""

import pytest
from unittest.mock import MagicMock, patch

from code_review_agent.bot.command_router import CommandRouter
from code_review_agent.suppression_store import SuppressionStore
from code_review_agent.models import InlineComment, SastFinding
from code_review_agent.comment_synchronizer import CommentSynchronizer


@pytest.fixture
def clean_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_bot_sync.db"
    monkeypatch.setenv("QUEUE_DB_PATH", str(test_db))
    return test_db


def test_bot_suppress_and_unsuppress_commands(clean_db):
    owner = "acme"
    repo = "app"
    fp = "abcd1234ef567890"

    # Test /review suppress
    res = CommandRouter.dispatch(
        f"/review suppress {fp} Test false positive in fixtures",
        pr_url=f"https://github.com/{owner}/{repo}/pull/10",
        auto_post=False
    )
    assert res.status == "SUCCESS"
    assert "Finding Suppressed" in res.response_markdown
    assert fp in res.response_markdown

    # Verify persisted in SuppressionStore
    store = SuppressionStore()
    assert store.is_suppressed(f"{owner}/{repo}", fp)

    # Test /review unsuppress
    res_unsuppress = CommandRouter.dispatch(
        f"/review unsuppress {fp}",
        pr_url=f"https://github.com/{owner}/{repo}/pull/10",
        auto_post=False
    )
    assert res_unsuppress.status == "SUCCESS"
    assert "Finding Unsuppressed" in res_unsuppress.response_markdown
    assert not store.is_suppressed(f"{owner}/{repo}", fp)


def test_bot_shorthand_suppress_and_unsuppress(clean_db):
    owner = "acme"
    repo = "app"
    fp = "12345678abcdef01"

    # Test /suppress shorthand
    res = CommandRouter.dispatch(
        f"/suppress {fp} Shorthand suppress test",
        pr_url=f"https://github.com/{owner}/{repo}/pull/15",
        auto_post=False
    )
    assert res.status == "SUCCESS"
    assert "Finding Suppressed" in res.response_markdown

    store = SuppressionStore()
    assert store.is_suppressed(f"{owner}/{repo}", fp)

    # Test /unsuppress shorthand
    res_un = CommandRouter.dispatch(
        f"/unsuppress {fp}",
        pr_url=f"https://github.com/{owner}/{repo}/pull/15",
        auto_post=False
    )
    assert res_un.status == "SUCCESS"
    assert "Finding Unsuppressed" in res_un.response_markdown
    assert not store.is_suppressed(f"{owner}/{repo}", fp)


def test_bot_explain_command(clean_db):
    sample_diff = """diff --git a/app/login.py b/app/login.py
--- a/app/login.py
+++ b/app/login.py
@@ -10,3 +10,4 @@
 def check_login(user, pwd):
+    cursor.execute(f"SELECT * FROM users WHERE user='{user}' AND pass='{pwd}'")
"""
    # First find the fingerprint generated on this diff
    from code_review_agent.tools.sast_scanner import SastEngine
    findings = SastEngine().scan_diff(sample_diff)
    assert len(findings) >= 1
    fp = findings[0].fingerprint

    # Test /review explain <fp>
    res = CommandRouter.dispatch(
        f"/review explain {fp}",
        raw_diff=sample_diff,
        auto_post=False
    )
    assert res.status == "SUCCESS"
    assert "Deep Finding Explanation" in res.response_markdown
    assert findings[0].rule_id in res.response_markdown


def test_comment_synchronizer_deduplication_and_resolution():
    mock_client = MagicMock()
    fp_existing = "aaaabbbbccccdddd"
    fp_new = "1111222233334444"
    fp_resolved = "9999888877776666"

    # Existing comments already on PR carry fp_existing and fp_resolved
    mock_client.list_pull_request_review_comments.return_value = [
        {"body": f"⚠️ **WARNING**: Old issue\n\n<!-- ai-code-review:fingerprint:{fp_existing} -->\n<sub>Fingerprint: `{fp_existing}`</sub>"},
        {"body": f"🚨 **CRITICAL**: Fixed issue\n\n<!-- ai-code-review:fingerprint:{fp_resolved} -->\n<sub>Fingerprint: `{fp_resolved}`</sub>"}
    ]

    c1 = InlineComment(
        path="app/auth.py",
        line=12,
        comment_body="Old issue",
        fingerprint=fp_existing
    )
    c2 = InlineComment(
        path="app/auth.py",
        line=25,
        comment_body="Brand new issue",
        fingerprint=fp_new
    )

    f1 = SastFinding(
        rule_id="RULE-1",
        description="Old issue",
        severity="MEDIUM",
        file_path="app/auth.py",
        line_number=12,
        fix_recommendation="Fix",
        fingerprint=fp_existing
    )
    f2 = SastFinding(
        rule_id="RULE-2",
        description="New issue",
        severity="HIGH",
        file_path="app/auth.py",
        line_number=25,
        fix_recommendation="Fix",
        fingerprint=fp_new
    )

    # Current revision has f1 and f2 (fp_resolved is no longer present!)
    deduped_comments, resolved_fps, _ = CommentSynchronizer.synchronize(
        client=mock_client,
        owner="test-org",
        repo="test-repo",
        pull_number=42,
        current_comments=[c1, c2],
        current_findings=[f1, f2],
        head_sha="commit_sha_123"
    )

    # Only c2 should be posted (c1 was already posted previously and deduplicated!)
    assert len(deduped_comments) == 1
    assert deduped_comments[0].fingerprint == fp_new

    # fp_resolved was detected as resolved
    assert fp_resolved in resolved_fps

    # Verify auto-resolution comment was posted to the issue discussion
    mock_client.post_issue_comment.assert_called_once()
    args, kwargs = mock_client.post_issue_comment.call_args
    assert "Verified Resolutions" in kwargs["body"]
    assert fp_resolved in kwargs["body"]


def test_api_suppressions_endpoints(clean_db):
    from fastapi.testclient import TestClient
    from code_review_agent.webhook_server import app

    client = TestClient(app)

    # 1. Initially empty
    res = client.get("/api/suppressions?repo=demo/app")
    assert res.status_code == 200
    assert res.json()["suppressions"] == []

    # 2. Add suppression
    post_res = client.post("/api/suppressions", json={
        "repo_id": "demo/app",
        "fingerprint": "feedbeef12345678",
        "reason": "Test endpoint suppression",
        "author": "admin"
    })
    assert post_res.status_code == 200
    assert post_res.json()["status"] == "success"

    # 3. Verify in list
    res2 = client.get("/api/suppressions?repo=demo/app")
    assert res2.status_code == 200
    supps = res2.json()["suppressions"]
    assert len(supps) == 1
    assert supps[0]["fingerprint"] == "feedbeef12345678"

    # 4. Delete suppression
    del_res = client.delete("/api/suppressions/feedbeef12345678?repo=demo/app")
    assert del_res.status_code == 200

    # 5. Verify deleted
    res3 = client.get("/api/suppressions?repo=demo/app")
    assert res3.json()["suppressions"] == []
