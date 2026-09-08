"""
Unit tests for enterprise Git platform adapters:
- Platform factory resolution
- GitHub platform adapter
- GitLab platform adapter
- Bitbucket platform adapter
- Local air-gapped Git adapter
"""

import pytest
from unittest.mock import MagicMock, patch

from code_review_agent.platform import (
    get_platform_client,
    GitHubPlatformClient,
    GitLabPlatformClient,
    BitbucketPlatformClient,
    LocalGitPlatformClient,
    PlatformPRMetadata,
)
from code_review_agent.models import InlineComment


class TestPlatformFactory:
    """Test resolution of platform clients and PR identifiers from URLs."""

    def test_resolve_github_url(self):
        client, ident = get_platform_client("https://github.com/octocat/Hello-World/pull/42", token="gh_dummy")
        assert isinstance(client, GitHubPlatformClient)
        assert ident.platform == "github"
        assert ident.owner_or_project == "octocat"
        assert ident.repo_or_slug == "Hello-World"
        assert ident.pr_id == 42

    def test_resolve_github_shorthand(self):
        client, ident = get_platform_client("octocat/Hello-World#42", token="gh_dummy")
        assert isinstance(client, GitHubPlatformClient)
        assert ident.platform == "github"
        assert ident.owner_or_project == "octocat"
        assert ident.repo_or_slug == "Hello-World"
        assert ident.pr_id == 42

    def test_resolve_gitlab_url(self):
        client, ident = get_platform_client("https://gitlab.com/my-org/backend-team/api-service/-/merge_requests/15", token="gl_dummy")
        assert isinstance(client, GitLabPlatformClient)
        assert ident.platform == "gitlab"
        assert ident.owner_or_project == "my-org/backend-team"
        assert ident.repo_or_slug == "api-service"
        assert ident.pr_id == 15

    def test_resolve_bitbucket_url(self):
        client, ident = get_platform_client("https://bitbucket.org/corp-workspace/payment-gateway/pull-requests/7", token="bb_dummy")
        assert isinstance(client, BitbucketPlatformClient)
        assert ident.platform == "bitbucket"
        assert ident.owner_or_project == "corp-workspace"
        assert ident.repo_or_slug == "payment-gateway"
        assert ident.pr_id == 7

    def test_resolve_local_git_url(self, tmp_path):
        client, ident = get_platform_client(f"local://{tmp_path}")
        assert isinstance(client, LocalGitPlatformClient)
        assert ident.platform == "local"

    def test_invalid_url_raises_value_error(self):
        with pytest.raises(ValueError, match="Unsupported or unparseable platform URL"):
            get_platform_client("https://unknown-vcs.com/repo/123")

    def test_resolve_queue_shorthand_identifiers(self):
        """The scheme-less identifiers the durable queue enqueues resolve to the right host."""
        c1, i1 = get_platform_client("my-org/sub-group/api-service/merge_requests/15")
        assert isinstance(c1, GitLabPlatformClient) and i1.platform == "gitlab"
        assert i1.owner_or_project == "my-org/sub-group" and i1.repo_or_slug == "api-service" and i1.pr_id == 15

        c2, i2 = get_platform_client("corp/payments/pull-requests/7")
        assert isinstance(c2, BitbucketPlatformClient) and i2.platform == "bitbucket"
        assert i2.pr_id == 7

        c3, i3 = get_platform_client("octocat/Hello-World/pull/42")
        assert isinstance(c3, GitHubPlatformClient) and i3.platform == "github"
        assert i3.pr_id == 42


class TestGitHubPlatformAdapter:
    """Test GitHub adapter delegation to underlying GitHubClient."""

    @patch("code_review_agent.platform.github_adapter.GitHubClient")
    def test_fetch_metadata_and_diff(self, mock_gh_cls):
        mock_instance = MagicMock()
        mock_gh_cls.return_value = mock_instance
        mock_instance.fetch_pull_request_metadata.return_value = {
            "title": "Add Auth Handler",
            "body": "Implements JWT verification",
            "author": "dev_user",
            "head_sha": "abc1234",
            "base_sha": "def5678",
            "head_ref": "feature/auth",
            "base_ref": "main",
            "state": "open",
            "html_url": "https://github.com/owner/repo/pull/1",
        }
        mock_instance.fetch_pull_request_diff.return_value = "diff --git a/auth.py b/auth.py\n..."

        adapter = GitHubPlatformClient(token="mock_token")
        meta = adapter.fetch_pull_request_metadata("owner", "repo", 1)

        assert isinstance(meta, PlatformPRMetadata)
        assert meta.title == "Add Auth Handler"
        assert meta.author == "dev_user"
        assert meta.head_sha == "abc1234"

        diff = adapter.fetch_pull_request_diff("owner", "repo", 1)
        assert "diff --git" in diff

    @patch("code_review_agent.platform.github_adapter.GitHubClient")
    def test_post_review_and_comments(self, mock_gh_cls):
        mock_instance = MagicMock()
        mock_gh_cls.return_value = mock_instance
        mock_instance.post_pull_request_review.return_value = {"id": 1001, "state": "CHANGES_REQUESTED"}

        adapter = GitHubPlatformClient(token="mock_token")
        inline = InlineComment(
            path="src/main.py",
            line=10,
            comment_body="Fix security flaw",
            severity="CRITICAL"
        )
        res = adapter.post_pull_request_review(
            "owner", "repo", 1, event="REQUEST_CHANGES", body="Please fix critical issue", comments=[inline]
        )
        assert res["id"] == 1001
        mock_instance.post_pull_request_review.assert_called_once()


class TestGitLabPlatformAdapter:
    """Test GitLab REST v4 API adapter."""

    @patch("httpx.Client")
    def test_fetch_metadata(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "title": "Migrate DB Schema",
            "description": "Adds index to users table",
            "author": {"username": "gitlab_guru"},
            "sha": "fedcba9876",
            "diff_refs": {"base_sha": "12345678"},
            "source_branch": "fix/schema",
            "target_branch": "main",
            "state": "opened",
            "web_url": "https://gitlab.com/org/proj/-/merge_requests/10",
        }
        mock_client.get.return_value = mock_resp

        adapter = GitLabPlatformClient(token="glpat-dummy")
        meta = adapter.fetch_pull_request_metadata("org", "proj", 10)

        assert meta.title == "Migrate DB Schema"
        assert meta.author == "gitlab_guru"
        assert meta.head_sha == "fedcba9876"
        assert meta.head_ref == "fix/schema"
        assert meta.state == "opened"

    @patch("httpx.Client")
    def test_fetch_diff(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1,3 +1,4 @@\n import os\n+import sys\n"
        mock_client.get.return_value = mock_resp

        adapter = GitLabPlatformClient(token="glpat-dummy")
        diff_text = adapter.fetch_pull_request_diff("org", "proj", 10)

        assert "--- a/app.py" in diff_text
        assert "+++ b/app.py" in diff_text
        assert "+import sys" in diff_text

    @patch("httpx.Client")
    def test_post_review_and_discussions(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {"id": 999, "body": "Summary comment"}
        mock_client.post.return_value = mock_resp

        adapter = GitLabPlatformClient(token="glpat-dummy")
        inline = InlineComment(
            path="app.py",
            line=5,
            comment_body="Check null pointer exception",
            severity="HIGH"
        )
        res = adapter.post_pull_request_review(
            "org", "proj", 10,
            event="REQUEST_CHANGES",
            body="Code review feedback",
            commit_id="abc1234",
            comments=[inline]
        )
        assert res["id"] == 999
        assert mock_client.post.call_count == 1

    @patch("httpx.Client")
    def test_list_review_comments_normalized(self, mock_client_cls):
        """MR notes are normalized to {'body','user':{'login'}} and system notes dropped."""
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = [
            {"body": "use bcrypt", "author": {"username": "alice"}, "system": False},
            {"body": "merged by bot", "author": {"username": "gitbot"}, "system": True},
        ]
        mock_client.get.return_value = resp

        adapter = GitLabPlatformClient(token="t")
        comments = adapter.list_pull_request_review_comments("org", "proj", 10)
        assert comments == [{"body": "use bcrypt", "user": {"login": "alice"}}]


class TestBitbucketPlatformAdapter:
    """Test Bitbucket Cloud v2 REST API adapter."""

    @patch("httpx.Client")
    def test_fetch_metadata(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "title": "Payment Flow Refactoring",
            "description": "Clean up webhook handlers",
            "author": {"display_name": "Bitbucket Dev"},
            "source": {"commit": {"hash": "bbcommit01"}, "branch": {"name": "refactor/payments"}},
            "destination": {"commit": {"hash": "bbbase01"}, "branch": {"name": "master"}},
            "state": "OPEN",
            "links": {"html": {"href": "https://bitbucket.org/corp/payments/pull-requests/4"}},
        }
        mock_client.get.return_value = mock_resp

        adapter = BitbucketPlatformClient(token="bb_token")
        meta = adapter.fetch_pull_request_metadata("corp", "payments", 4)

        assert meta.title == "Payment Flow Refactoring"
        assert meta.author == "Bitbucket Dev"
        assert meta.head_sha == "bbcommit01"
        assert meta.head_ref == "refactor/payments"

    @patch("httpx.Client")
    def test_fetch_diff(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "diff --git a/service.py b/service.py\n+def pay(): pass"
        mock_client.get.return_value = mock_resp

        adapter = BitbucketPlatformClient(token="bb_token")
        diff_text = adapter.fetch_pull_request_diff("corp", "payments", 4)
        assert "def pay(): pass" in diff_text

    @patch("httpx.Client")
    def test_post_review_and_inline_comments(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {"id": 555}
        mock_client.post.return_value = mock_resp

        adapter = BitbucketPlatformClient(token="bb_token")
        inline = InlineComment(
            path="service.py",
            line=20,
            comment_body="Add logging statement",
            severity="LOW"
        )
        res = adapter.post_pull_request_review(
            "corp", "payments", 4,
            event="APPROVE",
            body="Looks great overall!",
            comments=[inline]
        )
        assert res["id"] == 555
        assert mock_client.post.call_count == 1

    @patch("httpx.Client")
    def test_list_review_comments_normalized(self, mock_client_cls):
        """PR comments are normalized; deleted (no-content) comments are skipped."""
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "values": [
                {"content": {"raw": "looks good"}, "user": {"nickname": "bob"}},
                {"content": {}, "user": {"nickname": "ghost"}},
            ],
            "next": None,
        }
        mock_client.get.return_value = resp

        adapter = BitbucketPlatformClient(token="t")
        comments = adapter.list_pull_request_review_comments("corp", "payments", 4)
        assert comments == [{"body": "looks good", "user": {"login": "bob"}}]


class TestLocalGitPlatformAdapter:
    """Test Local Air-Gapped Git adapter."""

    def test_local_git_review_writes_markdown(self, tmp_path):
        adapter = LocalGitPlatformClient(repo_dir=str(tmp_path))

        # Mock _run_git commands for metadata and diff
        def mock_run_git(args):
            if "rev-parse" in args:
                return "local_sha123\n"
            if "log" in args:
                return "Local commit title\n"
            if "diff" in args:
                return "diff --git a/foo.py b/foo.py\n+x = 1"
            return ""

        adapter._run_git = mock_run_git

        meta = adapter.fetch_pull_request_metadata("local", "test", 1)
        assert meta.title == "Local commit title"
        assert meta.head_sha == "local_sha123"

        diff = adapter.fetch_pull_request_diff("local", "test", 1)
        assert "+x = 1" in diff

        # Post review with inline comment
        inline = InlineComment(
            path="foo.py",
            line=1,
            comment_body="Initialize variable properly",
            severity="MEDIUM",
            suggestion_code="x: int = 1"
        )
        res = adapter.post_pull_request_review(
            "local", "test", 1,
            event="REQUEST_CHANGES",
            body="Found a type annotation issue.",
            comments=[inline]
        )
        assert res["status"] == "saved"
        review_file = tmp_path / "REVIEW.md"
        assert review_file.exists()
        content = review_file.read_text(encoding="utf-8")
        assert "Local Code Review Verdict: **REQUEST_CHANGES**" in content
        assert "foo.py:L1" in content
        assert "x: int = 1" in content

    def test_local_git_notes_and_description(self, tmp_path):
        adapter = LocalGitPlatformClient(repo_dir=str(tmp_path))
        adapter.post_issue_comment("local", "test", 1, "Discussing edge case")
        notes_file = tmp_path / "REVIEW_NOTES.md"
        assert notes_file.exists()
        assert "Discussing edge case" in notes_file.read_text(encoding="utf-8")

        adapter.update_pull_request_description("local", "test", 1, "New PR description text", title="Updated PR")
        desc_file = tmp_path / "PR_DESCRIPTION.md"
        assert desc_file.exists()
        assert "Updated PR" in desc_file.read_text(encoding="utf-8")


class TestPlatformAgnosticWorker:
    """The durable-queue default handler reviews and posts across platforms."""

    def test_default_review_resolves_platform_and_posts(self):
        from contextlib import contextmanager
        from code_review_agent.webhook_queue import WebhookWorker
        from code_review_agent.platform.base import PlatformPRIdentifier, PlatformPRMetadata

        fake_client = MagicMock()
        fake_client.fetch_pull_request_diff.return_value = "diff --git a/x b/x\n+ok\n"
        fake_client.fetch_pull_request_metadata.return_value = PlatformPRMetadata(head_sha="sha1", head_ref="feat")
        ident = PlatformPRIdentifier(
            platform="gitlab", owner_or_project="grp", repo_or_slug="proj", pr_id=5,
            raw_identifier="grp/proj/merge_requests/5",
        )

        fake_response = MagicMock()
        fake_response.verdict = "REQUEST CHANGES"
        fake_response.full_report = "## report"
        fake_response.inline_comments = []
        fake_response.model_dump.return_value = {"verdict": "REQUEST CHANGES"}

        @contextmanager
        def fake_checkout(*args, **kwargs):
            yield "/tmp/fake-checkout"

        with patch("code_review_agent.platform.factory.get_platform_client", return_value=(fake_client, ident)), \
             patch("code_review_agent.bot.checkout.temporary_pr_checkout", fake_checkout), \
             patch("code_review_agent.review_service.ReviewService.execute_review", return_value=fake_response) as mock_exec:
            worker = WebhookWorker(queue=MagicMock())
            result = worker._default_execute_review({}, "grp/proj/merge_requests/5")

        # Reviewed the fetched diff without per-IP throttling or size cap.
        assert mock_exec.call_args.kwargs.get("bypass_limits") is True
        # Verdict mapped and posted back through the resolved (GitLab) client.
        fake_client.post_pull_request_review.assert_called_once()
        assert fake_client.post_pull_request_review.call_args.kwargs["event"] == "REQUEST_CHANGES"
        assert result["verdict"] == "REQUEST CHANGES"
