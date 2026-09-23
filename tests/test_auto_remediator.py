"""
Unit and integration tests for AutoRemediator, 1-click branch commits,
bot slash commands (/review apply), and REST API ingress.
"""

import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from code_review_agent.bot.command_router import CommandRouter
from code_review_agent.models import SastFinding
from code_review_agent.platform.base import PlatformPRMetadata
from code_review_agent.platform.github_adapter import GitHubPlatformClient
from code_review_agent.platform.gitlab_adapter import GitLabPlatformClient
from code_review_agent.platform.bitbucket_adapter import BitbucketPlatformClient
from code_review_agent.platform.local_git_adapter import LocalGitPlatformClient
from code_review_agent.remediator import AutoRemediator
from code_review_agent.webhook_server import app


def test_extract_remediation_code():
    # Markdown block
    rec1 = "Consider parameterizing query:\n```python\ncursor.execute('SELECT * FROM u WHERE id = %s', (uid,))\n```"
    code1 = AutoRemediator.extract_remediation_code(rec1)
    assert code1 == "cursor.execute('SELECT * FROM u WHERE id = %s', (uid,))"

    # Suggestion block
    rec2 = "```suggestion\nuser = get_safe_user(id)\n```"
    code2 = AutoRemediator.extract_remediation_code(rec2)
    assert code2 == "user = get_safe_user(id)"

    # Inline code
    rec3 = "Replace with: `safe_execute(query)`"
    code3 = AutoRemediator.extract_remediation_code(rec3)
    assert code3 == "safe_execute(query)"


def test_apply_code_patch_indentation_preservation():
    orig = "def my_func():\n    # Vulnerable call\n    query = f'SELECT * FROM users WHERE id = {user_id}'\n    return query\n"
    snippet = "query = f'SELECT * FROM users WHERE id = {user_id}'"
    replacement = "query = 'SELECT * FROM users WHERE id = %s'\nparams = (user_id,)"

    patched = AutoRemediator.apply_code_patch(orig, snippet, replacement, target_line=3)

    assert "    query = 'SELECT * FROM users WHERE id = %s'" in patched
    assert "    params = (user_id,)" in patched
    assert "def my_func():" in patched


def test_local_git_adapter_commit_file_change():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)
        import subprocess
        subprocess.run(["git", "init", "-b", "main"], cwd=repo_path, check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "config", "user.name", "Test Reviewer"], cwd=repo_path, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_path, check=True)

        test_file = repo_path / "app.py"
        test_file.write_text("value = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "app.py"], cwd=repo_path, check=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_path, check=True)

        adapter = LocalGitPlatformClient(repo_dir=str(repo_path))

        # Test fetch_file_content
        content = adapter.fetch_file_content(owner="local", repo="repo", path="app.py")
        assert content == "value = 1\n"

        # Test commit_file_change
        result = adapter.commit_file_change(
            owner="local",
            repo="repo",
            branch="main",
            path="app.py",
            content="value = 2\n",
            commit_message="fix(quality): update value"
        )

        assert result["sha"] != "local-working-tree"
        assert test_file.read_text(encoding="utf-8") == "value = 2\n"


def test_github_commit_file_change_offline():
    client = GitHubPlatformClient(token=None)
    result = client.commit_file_change(
        owner="test-org",
        repo="test-repo",
        branch="patch-1",
        path="service.py",
        content="safe_code = True\n",
        commit_message="fix(security): sanitize"
    )
    assert result["branch"] == "patch-1"
    assert "simulated" in result["sha"]


def test_gitlab_commit_file_change_offline():
    client = GitLabPlatformClient(token=None)
    result = client.commit_file_change(
        owner="test-org",
        repo="test-repo",
        branch="patch-1",
        path="service.py",
        content="safe_code = True\n",
        commit_message="fix(security): sanitize"
    )
    assert result["branch"] == "patch-1"
    assert "simulated" in result["sha"]


def test_bitbucket_commit_file_change_offline():
    client = BitbucketPlatformClient(username=None, app_password=None)
    result = client.commit_file_change(
        owner="test-org",
        repo="test-repo",
        branch="patch-1",
        path="service.py",
        content="safe_code = True\n",
        commit_message="fix(security): sanitize"
    )
    assert result["branch"] == "patch-1"
    assert "simulated" in result["sha"]


def test_command_router_apply_slash_command():
    fake_diff = """diff --git a/app.py b/app.py
new file mode 100644
--- /dev/null
+++ b/app.py
@@ -0,0 +1,4 @@
+import sqlite3
+def get_user(db, uid):
+    return db.execute(f"SELECT * FROM users WHERE id = '{uid}'")
+"""
    # 1. Run explain to find the fingerprint
    from code_review_agent.tools.sast_scanner import SastEngine
    findings = SastEngine().scan_diff(fake_diff)
    assert len(findings) > 0
    target_fp = findings[0].fingerprint

    # 2. Mock AutoRemediator.apply_remediation
    with patch.object(AutoRemediator, "apply_remediation") as mock_apply:
        mock_apply.return_value = {
            "status": "success",
            "commit_sha": "a1b2c3d4e5f6",
            "commit_url": "https://github.com/org/repo/commit/a1b2c3d4e5f6",
            "file_path": "app.py",
            "fingerprint": target_fp,
            "rule_id": findings[0].rule_id,
            "branch": "fix-sqli"
        }

        # Dispatch /review apply <fp>
        res = CommandRouter.dispatch(
            f"/review apply {target_fp}",
            pr_url="https://github.com/org/repo/pull/12",
            raw_diff=fake_diff,
            auto_post=False
        )

        assert res.status == "SUCCESS"
        assert "1-Click Auto-Remediation Applied" in res.response_markdown
        assert "a1b2c3d4" in res.response_markdown

        # Dispatch shorthand /apply <fp>
        res2 = CommandRouter.dispatch(
            f"/apply {target_fp}",
            pr_url="https://github.com/org/repo/pull/12",
            raw_diff=fake_diff,
            auto_post=False
        )
        assert res2.status == "SUCCESS"


def test_webhook_api_remediation_apply():
    test_client = TestClient(app)
    with patch.object(AutoRemediator, "apply_remediation") as mock_apply:
        mock_apply.return_value = {
            "status": "success",
            "commit_sha": "123456789abc",
            "commit_url": "https://github.com/org/repo/commit/123456789abc",
            "file_path": "auth.py",
            "fingerprint": "abc123def4567890",
            "rule_id": "SAST-SQLI-001",
            "branch": "patch-branch"
        }

        response = test_client.post(
            "/api/remediation/apply",
            json={
                "pr_identifier": "https://github.com/org/repo/pull/10",
                "fingerprint": "abc123def4567890",
                "custom_message": "fix: resolve sqli"
            }
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["commit_sha"] == "123456789abc"
