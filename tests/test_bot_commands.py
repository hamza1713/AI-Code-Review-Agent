"""
Unit tests for Interactive PR Bot CommandRouter and slash command endpoints.
"""

import os
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

from code_review_agent.bot.command_router import CommandRouter
from code_review_agent.webhook_server import app


SAMPLE_SQLI_DIFF = """diff --git a/app/db.py b/app/db.py
--- a/app/db.py
+++ b/app/db.py
@@ -10,2 +10,4 @@ def get_user(user_id):
-    return db.query("SELECT * FROM users WHERE id = %s", (user_id,))
+    query = f"SELECT * FROM users WHERE id = '{user_id}'"
+    return db.execute(query)
"""

SAMPLE_CLEAN_DIFF = """diff --git a/app/calc.py b/app/calc.py
--- a/app/calc.py
+++ b/app/calc.py
@@ -1,2 +1,2 @@
-def add(a, b): return a+b
+def add(a: int, b: int) -> int: return a + b
"""


class TestCommandRouter:
    """Test command parsing, detection, and dispatching."""

    def test_is_bot_command(self):
        assert CommandRouter.is_bot_command("/describe") is True
        assert CommandRouter.is_bot_command("  /ask Why is this needed?  ") is True
        assert CommandRouter.is_bot_command("/compliance") is True
        assert CommandRouter.is_bot_command("LGTM, good work!") is False
        assert CommandRouter.is_bot_command("Fix line 10 /describe") is False
        assert CommandRouter.is_bot_command("") is False

    def test_parse_command(self):
        cmd, args = CommandRouter.parse_command("/describe")
        assert cmd == "describe"
        assert args == ""

        cmd, args = CommandRouter.parse_command("/ask Why is this query vulnerable?")
        assert cmd == "ask"
        assert args == "Why is this query vulnerable?"

        cmd, args = CommandRouter.parse_command("/compliance --strict")
        assert cmd == "compliance"
        assert args == "--strict"

        cmd, args = CommandRouter.parse_command("not a command")
        assert cmd == ""
        assert args == ""

    def test_help_command(self):
        result = CommandRouter.dispatch("/help", auto_post=False)
        assert result.command == "help"
        assert result.status == "SUCCESS"
        assert "/describe" in result.response_markdown
        assert "/ask" in result.response_markdown
        assert "/compliance" in result.response_markdown

    def test_compliance_command_detects_violations(self):
        result = CommandRouter.dispatch("/compliance", raw_diff=SAMPLE_SQLI_DIFF, auto_post=False)
        assert result.command == "compliance"
        assert result.status == "SUCCESS"
        # Should flag raw SQL interpolation rule
        assert "NON-COMPLIANT" in result.response_markdown or "gov-no-raw-sql" in result.response_markdown or "Violations" in result.response_markdown

    def test_compliance_command_clean_code(self):
        result = CommandRouter.dispatch("/compliance", raw_diff=SAMPLE_CLEAN_DIFF, auto_post=False)
        assert result.command == "compliance"
        assert result.status == "SUCCESS"
        assert "COMPLIANT" in result.response_markdown

    @patch("code_review_agent.llm_factory.LLMFactory.create_llm")
    def test_describe_command(self, mock_llm_factory):
        mock_llm = MagicMock()
        mock_llm.call.return_value = "### Summary\nRefactored user lookup to dynamic SQL.\n```mermaid\ngraph LR\nA-->B\n```"
        mock_llm_factory.return_value = mock_llm

        result = CommandRouter.dispatch("/describe", raw_diff=SAMPLE_SQLI_DIFF, auto_post=False)
        assert result.command == "describe"
        assert result.status == "SUCCESS"
        assert "PR Description" in result.response_markdown
        assert "mermaid" in result.response_markdown

    @patch("code_review_agent.llm_factory.LLMFactory.create_llm")
    def test_ask_command(self, mock_llm_factory):
        mock_llm = MagicMock()
        mock_llm.call.return_value = "This query is vulnerable to SQL Injection (CWE-89) because of f-string interpolation."
        mock_llm_factory.return_value = mock_llm

        result = CommandRouter.dispatch("/ask Why is this vulnerable?", raw_diff=SAMPLE_SQLI_DIFF, auto_post=False)
        assert result.command == "ask"
        assert result.status == "SUCCESS"
        assert "Response to: \"Why is this vulnerable?\"" in result.response_markdown
        assert "SQL Injection" in result.response_markdown

    def test_unknown_command(self):
        result = CommandRouter.dispatch("/foobar", auto_post=False)
        assert result.command == "foobar"
        assert result.status == "ERROR"
        assert "Unknown command" in result.response_markdown


class TestBotAPIEndpoints:
    """Test REST API endpoint /api/bot/command and webhook integration."""

    @pytest.fixture
    def client(self):
        return TestClient(app)

    def test_api_bot_command_help(self, client):
        resp = client.post("/api/bot/command", json={"command": "/help"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["command"] == "help"
        assert data["status"] == "SUCCESS"
        assert "/describe" in data["response_markdown"]

    def test_api_bot_command_compliance(self, client):
        resp = client.post("/api/bot/command", json={
            "command": "/compliance",
            "raw_diff": SAMPLE_CLEAN_DIFF
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["command"] == "compliance"
        assert data["status"] == "SUCCESS"

    @patch("code_review_agent.webhook_server.verify_github_signature", return_value=True)
    @patch("code_review_agent.bot.command_router.CommandRouter.dispatch")
    def test_webhook_issue_comment_schedules_command(self, mock_dispatch, mock_sig, client):
        """A slash command is accepted immediately and dispatched in the background."""
        payload = {
            "action": "created",
            "issue": {
                "number": 99,
                "pull_request": {"html_url": "https://github.com/org/repo/pull/99"},
            },
            "comment": {
                "body": "/help",
                "user": {"login": "developer", "type": "User"},
                "author_association": "NONE",
            },
        }
        resp = client.post(
            "/webhook/github",
            headers={"X-GitHub-Event": "issue_comment"},
            json=payload,
        )
        assert resp.status_code == 200
        data = resp.json()
        # /help is exempt from authz and is accepted for background processing.
        assert data["status"] == "accepted"
        assert data["command"] == "help"
        mock_dispatch.assert_called_once()

    @patch("code_review_agent.webhook_server.verify_github_signature", return_value=True)
    @patch("code_review_agent.bot.command_router.CommandRouter.dispatch")
    def test_webhook_rejects_unauthorized_command(self, mock_dispatch, mock_sig, client):
        """A cost-bearing command from an untrusted commenter is refused and never dispatched."""
        payload = {
            "action": "created",
            "issue": {
                "number": 99,
                "pull_request": {"html_url": "https://github.com/org/repo/pull/99"},
            },
            "comment": {
                "body": "/review",
                "user": {"login": "random-drive-by", "type": "User"},
                "author_association": "NONE",
            },
        }
        resp = client.post(
            "/webhook/github",
            headers={"X-GitHub-Event": "issue_comment"},
            json=payload,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"
        mock_dispatch.assert_not_called()

    @patch("code_review_agent.webhook_server.verify_github_signature", return_value=True)
    @patch("code_review_agent.bot.command_router.CommandRouter.dispatch")
    def test_webhook_allows_authorized_command(self, mock_dispatch, mock_sig, client):
        """A collaborator may run /review; it is accepted and dispatched."""
        payload = {
            "action": "created",
            "issue": {
                "number": 99,
                "pull_request": {"html_url": "https://github.com/org/repo/pull/99"},
            },
            "comment": {
                "body": "/review",
                "user": {"login": "maintainer", "type": "User"},
                "author_association": "COLLABORATOR",
            },
        }
        resp = client.post(
            "/webhook/github",
            headers={"X-GitHub-Event": "issue_comment"},
            json=payload,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "accepted"
        mock_dispatch.assert_called_once()


class TestDescribeMerge:
    """The /describe update must preserve the author's original PR body."""

    def test_merge_appends_when_no_marker(self):
        merged = CommandRouter._merge_description("Original author text.", "AI SECTION")
        assert "Original author text." in merged
        assert "AI SECTION" in merged
        assert merged.count("AI SECTION") == 1

    def test_merge_is_idempotent(self):
        first = CommandRouter._merge_description("Original.", "AI ONE")
        second = CommandRouter._merge_description(first, "AI TWO")
        # The author's text survives; the bot block is replaced, not stacked.
        assert "Original." in second
        assert "AI TWO" in second
        assert "AI ONE" not in second

    def test_merge_handles_empty_original(self):
        merged = CommandRouter._merge_description("", "AI SECTION")
        assert "AI SECTION" in merged


class TestPlatformAgnosticDispatch:
    """dispatch must resolve non-GitHub PRs through the platform adapter factory."""

    def test_dispatch_uses_resolved_platform_client(self):
        from code_review_agent.platform.base import PlatformPRIdentifier, PlatformPRMetadata

        fake = MagicMock()
        fake.fetch_pull_request_diff.return_value = "diff --git a/x b/x\n+ok\n"
        fake.fetch_pull_request_metadata.return_value = PlatformPRMetadata(title="t", body="b", head_ref="feat")
        ident = PlatformPRIdentifier(
            platform="gitlab", owner_or_project="grp", repo_or_slug="proj", pr_id=5,
            raw_identifier="https://gitlab.com/grp/proj/-/merge_requests/5",
        )

        with patch("code_review_agent.bot.command_router.get_platform_client", return_value=(fake, ident)):
            result = CommandRouter.dispatch(
                "/help",
                pr_url="https://gitlab.com/grp/proj/-/merge_requests/5",
                auto_post=True,
            )

        assert result.command == "help"
        # The report was posted back through the GitLab client with GitLab identifiers.
        fake.post_issue_comment.assert_called_once()
        _, kwargs = fake.post_issue_comment.call_args
        assert kwargs.get("issue_number") == 5
        assert kwargs.get("owner") == "grp"


class TestMultiPlatformWebhookAuth:
    """GitLab/Bitbucket comment commands must be gated like the GitHub path."""

    @pytest.fixture
    def client(self):
        return TestClient(app)

    def _gitlab_note_payload(self, note, username):
        return {
            "object_kind": "note",
            "user": {"username": username},
            "object_attributes": {"note": note},
            "merge_request": {"url": "https://gitlab.com/g/p/-/merge_requests/3"},
            "project": {"web_url": "https://gitlab.com/g/p"},
        }

    @patch("code_review_agent.bot.command_router.CommandRouter.dispatch")
    def test_gitlab_rejects_unlisted_user_fail_closed(self, mock_dispatch, client):
        """With no BOT_ALLOWED_USERS configured, a cost-bearing command is refused."""
        env = {"GITLAB_WEBHOOK_SECRET": "", "GITLAB_TOKEN": "", "BOT_ALLOWED_USERS": ""}
        with patch.dict(os.environ, env, clear=False):
            resp = client.post(
                "/webhook/gitlab",
                headers={"X-Gitlab-Event": "Note Hook"},
                json=self._gitlab_note_payload("/review", "random-user"),
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"
        mock_dispatch.assert_not_called()

    @patch("code_review_agent.bot.command_router.CommandRouter.dispatch")
    def test_gitlab_allows_listed_user(self, mock_dispatch, client):
        env = {"GITLAB_WEBHOOK_SECRET": "", "GITLAB_TOKEN": "", "BOT_ALLOWED_USERS": "maintainer"}
        with patch.dict(os.environ, env, clear=False):
            resp = client.post(
                "/webhook/gitlab",
                headers={"X-Gitlab-Event": "Note Hook"},
                json=self._gitlab_note_payload("/review", "maintainer"),
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "accepted"
        mock_dispatch.assert_called_once()

    def test_bitbucket_rejects_wrong_token(self, client):
        with patch.dict(os.environ, {"BITBUCKET_WEBHOOK_SECRET": "s3cret"}, clear=False):
            resp = client.post(
                "/webhook/bitbucket",
                headers={"X-Event-Key": "pullrequest:comment_created"},
                json={"comment": {"content": {"raw": "/review"}}, "actor": {"nickname": "x"}},
            )
        assert resp.status_code == 401

    @patch("code_review_agent.bot.command_router.CommandRouter.dispatch")
    def test_bitbucket_rejects_unlisted_user(self, mock_dispatch, client):
        env = {"BITBUCKET_WEBHOOK_SECRET": "s3cret", "BOT_ALLOWED_USERS": "alice"}
        with patch.dict(os.environ, env, clear=False):
            resp = client.post(
                "/webhook/bitbucket?token=s3cret",
                headers={"X-Event-Key": "pullrequest:comment_created"},
                json={
                    "comment": {"content": {"raw": "/ask what is this"}},
                    "actor": {"nickname": "drive-by"},
                    "pullrequest": {"links": {"html": {"href": "https://bitbucket.org/w/r/pull-requests/2"}}},
                },
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"
        mock_dispatch.assert_not_called()
