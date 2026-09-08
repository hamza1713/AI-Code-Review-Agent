"""
Unit tests for Team Memory Store, Suggestion Tracker, and /memory, /learn slash commands.
"""

import tempfile
from unittest.mock import MagicMock, patch

import pytest

from code_review_agent.learning.team_memory import TeamMemoryStore, BestPractice
from code_review_agent.learning.suggestion_tracker import SuggestionTracker
from code_review_agent.models import InlineComment
from code_review_agent.bot.command_router import CommandRouter


class TestTeamMemoryStore:
    """Test storage, retrieval, update, and prompt formatting in TeamMemoryStore."""

    def test_record_and_retrieve_practice(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TeamMemoryStore(cache_dir=tmpdir)
            practice = BestPractice(
                id="use-httpx",
                category="idiom",
                title="Use httpx over requests",
                description="Our async services standardize on httpx client.",
                good_pattern="import httpx\nasync with httpx.AsyncClient() as client: ...",
                bad_pattern="import requests\nresp = requests.get(...)",
                times_accepted=1
            )
            store.record_practice("owner/repo", practice)

            practices = store.get_practices("owner/repo")
            assert len(practices) == 1
            assert practices[0].id == "use-httpx"
            assert practices[0].title == "Use httpx over requests"
            assert practices[0].times_accepted == 1

    def test_reinforce_existing_practice(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TeamMemoryStore(cache_dir=tmpdir)
            p1 = BestPractice(
                id="use-httpx",
                title="Use httpx",
                description="First rule description",
                times_accepted=1
            )
            store.record_practice("owner/repo", p1)

            # Re-recording increments counter
            p2 = BestPractice(
                id="use-httpx",
                title="Use httpx updated",
                description="Updated rule description",
                times_accepted=1
            )
            updated = store.record_practice("owner/repo", p2)
            assert updated.times_accepted == 2

            practices = store.get_practices("owner/repo")
            assert len(practices) == 1
            assert practices[0].times_accepted == 2
            assert practices[0].description == "Updated rule description"

    def test_record_accepted_suggestion(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TeamMemoryStore(cache_dir=tmpdir)
            p = store.record_accepted_suggestion(
                repo_id="test/repo",
                title="Use Parameterized SQL Queries",
                description="Avoid string formatting inside queries.",
                good_code="db.execute('SELECT * FROM users WHERE id = %s', (uid,))",
                bad_code="db.execute(f'SELECT * FROM users WHERE id = {uid}')",
                category="security",
                source_pr="test/repo#42"
            )
            assert "parameterized" in p.id
            assert p.category == "security"
            assert p.source_pr == "test/repo#42"

            practices = store.get_practices("test/repo")
            assert len(practices) == 1
            assert practices[0].times_accepted == 1

    def test_format_team_memory_prompt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TeamMemoryStore(cache_dir=tmpdir)
            store.record_accepted_suggestion(
                repo_id="my/repo",
                title="Standardize on Pydantic v2",
                description="Use model_dump() instead of dict().",
                good_code="payload = model.model_dump()",
                bad_code="payload = model.dict()"
            )

            prompt = store.format_team_memory_prompt("my/repo")
            assert "Standardize on Pydantic v2" in prompt
            assert "Preferred Idiom" in prompt
            assert "model.model_dump()" in prompt
            assert "Avoid" in prompt

    def test_clear_memory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TeamMemoryStore(cache_dir=tmpdir)
            store.record_accepted_suggestion(
                repo_id="my/repo",
                title="Sample rule",
                description="Sample desc",
                good_code="print('ok')"
            )
            assert len(store.get_practices("my/repo")) == 1
            assert store.clear_memory("my/repo") is True
            assert len(store.get_practices("my/repo")) == 0


class TestSuggestionTracker:
    """Test learning loop from merged PRs."""

    def test_process_merged_pr_with_inline_comments(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TeamMemoryStore(cache_dir=tmpdir)
            tracker = SuggestionTracker(memory_store=store)

            comments = [
                InlineComment(
                    path="app/auth.py",
                    line=15,
                    severity="CRITICAL",
                    comment_body="SQL Injection: parameterized query required",
                    why="User input is concatenated into query string",
                    suggestion_code="return db.query('SELECT * FROM users WHERE id = %s', (user_id,))"
                ),
                InlineComment(
                    path="app/utils.py",
                    line=20,
                    severity="WARNING",
                    comment_body="Prefer list comprehension over map",
                    suggestion_code="return [x * 2 for x in items]"
                )
            ]

            learned = tracker.process_merged_pr(
                owner="acme",
                repo="backend",
                pr_number=99,
                applied_suggestions=comments
            )
            assert len(learned) == 2
            assert store.get_practices("acme/backend")[0].times_accepted == 1

    def test_process_merged_pr_from_github_comment_payload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TeamMemoryStore(cache_dir=tmpdir)
            tracker = SuggestionTracker(memory_store=store)

            raw_comments = [
                {
                    "body": "🚨 **CRITICAL**: Use safe hash\n```suggestion\nhashlib.sha256(data).hexdigest()\n```"
                }
            ]

            learned = tracker.process_merged_pr(
                owner="acme",
                repo="core",
                pr_number=101,
                review_comments=raw_comments
            )
            assert len(learned) == 1
            practices = store.get_practices("acme/core")
            assert len(practices) == 1
            assert "Use safe hash" in practices[0].title

    def test_merge_learns_only_applied_suggestions(self):
        """When the tracker fetches candidates itself, only suggestions that actually
        landed in the merged diff are learned — not every suggestion on the PR."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TeamMemoryStore(cache_dir=tmpdir)
            tracker = SuggestionTracker(memory_store=store)

            comments = [
                {"body": "💡 Use sha256\n```suggestion\nreturn hashlib.sha256(data).hexdigest()\n```",
                 "user": {"login": "review-bot"}},
                {"body": "💡 Ignored idea\n```suggestion\nreturn never_applied_helper_xyz(data)\n```",
                 "user": {"login": "review-bot"}},
            ]
            merged_diff = (
                "diff --git a/h.py b/h.py\n--- a/h.py\n+++ b/h.py\n"
                "@@ -1,1 +1,1 @@\n+    return hashlib.sha256(data).hexdigest()\n"
            )
            # Simulate the webhook path (no comments passed → tracker fetches + verifies).
            with patch.object(tracker, "_fetch_candidates", return_value=(comments, merged_diff, True)):
                learned = tracker.process_merged_pr(owner="acme", repo="core", pr_number=7)

            assert len(learned) == 1
            assert "sha256" in (learned[0].good_pattern or "")

    def test_merge_learns_nothing_without_a_diff_to_verify(self):
        """Conservative: if the merged diff can't be fetched, nothing is learned."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TeamMemoryStore(cache_dir=tmpdir)
            tracker = SuggestionTracker(memory_store=store)
            comments = [{"body": "💡 x\n```suggestion\nreturn foo_bar_baz()\n```", "user": {"login": "b"}}]
            with patch.object(tracker, "_fetch_candidates", return_value=(comments, "", True)):
                learned = tracker.process_merged_pr(owner="acme", repo="core", pr_number=8)
            assert learned == []

    def test_suggestion_applied_helper(self):
        added = "return hashlib.sha256(data).hexdigest()"
        assert SuggestionTracker._suggestion_applied("hashlib.sha256(data).hexdigest()", added) is True
        assert SuggestionTracker._suggestion_applied("totally_absent_call()", added) is False
        assert SuggestionTracker._suggestion_applied("anything", "") is False

    def test_merge_routes_to_platform_client(self):
        """On merge, the tracker resolves the client for the PR's platform (e.g. GitLab)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TeamMemoryStore(cache_dir=tmpdir)
            tracker = SuggestionTracker(memory_store=store)

            fake_client = MagicMock()
            fake_client.list_pull_request_review_comments.return_value = []
            fake_client.fetch_pull_request_diff.return_value = ""
            with patch(
                "code_review_agent.platform.factory.get_client_for_platform",
                return_value=fake_client,
            ) as get_client:
                learned = tracker.process_merged_pr("grp", "proj", 5, platform="gitlab")

            get_client.assert_called_once_with("gitlab")
            assert learned == []  # GitLab adapter lists no review comments yet → learns nothing safely


class TestBotMemoryCommands:
    """Test /memory and /learn slash commands."""

    def test_learn_command(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with pytest.MonkeyPatch.context() as mp:
                mp.setenv("TEAM_MEMORY_CACHE_DIR", tmpdir)
                res = CommandRouter.dispatch(
                    "/learn Always use pytest fixtures for mock databases",
                    pr_url="https://github.com/myorg/myapp/pull/12",
                    auto_post=False
                )
                assert res.command == "learn"
                assert res.status == "SUCCESS"
                assert "Team Memory Updated" in res.response_markdown

                # Check /memory recalls it
                mem_res = CommandRouter.dispatch(
                    "/memory",
                    pr_url="https://github.com/myorg/myapp/pull/12",
                    auto_post=False
                )
                assert mem_res.command == "memory"
                assert mem_res.status == "SUCCESS"
                assert "Always use pytest fixtures" in mem_res.response_markdown

    def test_learn_command_validation(self):
        res = CommandRouter.dispatch("/learn hi", auto_post=False)
        assert res.status == "ERROR"
        assert "Please provide a convention description" in res.response_markdown
