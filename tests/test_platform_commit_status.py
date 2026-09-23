"""
Tests for Platform Commit Statuses and GitHub Check Runs across
GitHub, GitLab, Bitbucket, and Local Git adapters.
"""

import pytest
import httpx
from unittest.mock import patch, MagicMock

from code_review_agent.platform.github_adapter import GitHubPlatformClient
from code_review_agent.platform.gitlab_adapter import GitLabPlatformClient
from code_review_agent.platform.bitbucket_adapter import BitbucketPlatformClient
from code_review_agent.platform.local_git_adapter import LocalGitPlatformClient


def test_github_commit_status():
    client = GitHubPlatformClient(token="ghp_test_token_12345678901234567890")
    with patch("httpx.Client.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=201, json=lambda: {"id": 101, "state": "success"})
        res = client.set_commit_status(
            owner="octocat",
            repo="hello-world",
            sha="6dcb09b5b57875f334f61aebed695e2e4193db5e",
            state="success",
            description="AI Code Review passed",
            context="ai-code-review",
            target_url="https://example.com/review"
        )
        assert res.get("id") == 101
        assert res.get("state") == "success"
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert "octocat/hello-world/statuses/6dcb09b5" in args[0]
        assert kwargs["json"]["state"] == "success"
        assert kwargs["json"]["context"] == "ai-code-review"


def test_github_check_run_success():
    client = GitHubPlatformClient(token="ghp_test_token_12345678901234567890")
    with patch("httpx.Client.request") as mock_req:
        mock_req.return_value = MagicMock(status_code=201, json=lambda: {"id": 202, "status": "completed", "conclusion": "success"})
        res = client.create_or_update_check_run(
            owner="octocat",
            repo="hello-world",
            head_sha="6dcb09b5b57875f334f61aebed695e2e4193db5e",
            name="AI Code Review",
            status="completed",
            conclusion="success",
            title="Clean Review",
            summary="No defects detected.",
            annotations=[]
        )
        assert res.get("id") == 202
        assert res.get("status") == "completed"
        assert res.get("conclusion") == "success"


def test_github_check_run_fallback_on_403():
    client = GitHubPlatformClient(token="ghp_test_token_12345678901234567890")
    with patch("httpx.Client.request") as mock_req, patch.object(client.client, "set_commit_status") as mock_set_status:
        # Check-run returns 403 Forbidden (token lacks checks:write)
        mock_req.return_value = MagicMock(status_code=403, text="Resource not accessible by personal access token")
        mock_set_status.return_value = {"state": "success", "description": "Fallback commit status"}

        res = client.create_or_update_check_run(
            owner="octocat",
            repo="hello-world",
            head_sha="6dcb09b5b57875f334f61aebed695e2e4193db5e",
            name="AI Code Review",
            status="completed",
            conclusion="success",
            summary="Clean PR"
        )
        mock_set_status.assert_called_once_with(
            owner="octocat",
            repo="hello-world",
            sha="6dcb09b5b57875f334f61aebed695e2e4193db5e",
            state="success",
            description="Clean PR",
            context="ai-code-review",
            target_url=None
        )
        assert res.get("state") == "success"


def test_gitlab_commit_status():
    client = GitLabPlatformClient(token="glpat_test_token", base_url="https://gitlab.example.com/api/v4")
    with patch("httpx.Client.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=201, json=lambda: {"id": 303, "status": "success"})
        res = client.set_commit_status(
            owner="group",
            repo="project",
            sha="abcdef1234567890",
            state="success",
            description="Review passed",
            context="ai-code-review"
        )
        assert res.get("id") == 303
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert "projects/group%2Fproject/statuses/abcdef1234567890" in args[0]
        assert kwargs["json"]["state"] == "success"


def test_bitbucket_commit_status():
    client = BitbucketPlatformClient(token="bb_token_123", base_url="https://api.bitbucket.org/2.0")
    with patch("httpx.Client.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"key": "ai-code-review", "state": "SUCCESSFUL"})
        res = client.set_commit_status(
            owner="workspace",
            repo="repo-slug",
            sha="1234567890abcdef",
            state="success",
            description="Review passed",
            context="ai-code-review"
        )
        assert res.get("state") == "SUCCESSFUL"
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert "repositories/workspace/repo-slug/commit/1234567890abcdef/statuses/build" in args[0]
        assert kwargs["json"]["state"] == "SUCCESSFUL"


def test_local_git_commit_status(tmp_path):
    client = LocalGitPlatformClient(repo_dir=str(tmp_path))
    # Create .git directory in tmp_path
    (tmp_path / ".git").mkdir()
    res = client.set_commit_status(
        owner="local",
        repo=tmp_path.name,
        sha="local_sha_123",
        state="success",
        description="Local review passed"
    )
    assert res["state"] == "success"
    assert res["sha"] == "local_sha_123"
    status_file = tmp_path / ".git" / "ai_review_status.json"
    assert status_file.exists()
