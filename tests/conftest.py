"""Pytest configuration and environment fixtures."""

import os
import sys
from pathlib import Path

# Ensure src directory is in sys.path for test runs
src_dir = Path(__file__).resolve().parent.parent / "src"
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

# Tests must never inherit real provider credentials from the developer's .env.

for credential in (
    "GITHUB_TOKEN",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GROQ_API_KEY",
    "OLLAMA_API_KEY",
    "GITLAB_TOKEN",
    "BITBUCKET_TOKEN",
):
    os.environ[credential] = ""

import pytest  # noqa: E402 -- credentials must be cleared before app imports

from code_review_agent.cache import (  # noqa: E402 -- credential scrubbing precedes app imports
    global_tool_cache,  # noqa: E402 -- credential scrubbing precedes app imports
)
from code_review_agent.context_engine.code_graph import (  # noqa: E402 -- credential scrubbing precedes app imports
    CodeGraphIndexer,  # noqa: E402 -- credential scrubbing precedes app imports
)
from code_review_agent.crews.code_review_crew.tool_registry import (  # noqa: E402 -- credential scrubbing precedes app imports
    ToolRegistry,  # noqa: E402 -- credential scrubbing precedes app imports
)
from code_review_agent.tools.bandit_runner import (  # noqa: E402 -- credential scrubbing precedes app imports
    BanditRunner,  # noqa: E402 -- credential scrubbing precedes app imports
)
from code_review_agent.tools.ruff_tool import (  # noqa: E402 -- credential scrubbing precedes app imports
    RuffRunner,  # noqa: E402 -- credential scrubbing precedes app imports
)


def _reset_all_caches():
    global_tool_cache.clear()
    CodeGraphIndexer.clear_cache()
    ToolRegistry.reset()
    # Process-lifetime tool-availability caches (see BanditRunner.is_available) —
    # reset so a test that mocks shutil.which isn't poisoned by an earlier test's result.
    BanditRunner._availability_cache = None
    RuffRunner._availability_cache = None


@pytest.fixture(autouse=True)
def reset_global_test_caches():
    """Reset global caches and registries between test runs to ensure complete isolation."""
    _reset_all_caches()
    yield
    _reset_all_caches()


@pytest.fixture(autouse=True)
def isolate_test_database(tmp_path_factory, monkeypatch):
    """Ensure every test runs against an isolated SQLite database rather than polluting workspace."""
    test_env = tmp_path_factory.mktemp("sqlite_test_env")
    test_db = test_env / "test_isolated_jobs.db"
    monkeypatch.setenv("QUEUE_DB_PATH", str(test_db))
    from code_review_agent.calibration import FindingOutcomeStore

    FindingOutcomeStore(str(test_db)).close()
    server = sys.modules.get("code_review_agent.webhook_server")
    if server is not None:
        from code_review_agent.webhook_queue import WebhookJobQueue, WebhookWorker

        queue = WebhookJobQueue(str(test_db))
        monkeypatch.setattr(server, "queue", queue)
        monkeypatch.setattr(server, "worker", WebhookWorker(queue, max_workers=2))
    yield test_db


@pytest.fixture(autouse=True)
def explicit_development_test_configuration(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("REVIEW_REQUIRE_AUTH", "false")
    monkeypatch.setenv("REVIEW_ALLOWED_REPOSITORIES", "*")
    monkeypatch.setenv("REVIEW_ENABLE_POSTING", "true")
    monkeypatch.setenv("REVIEW_ENABLE_REMEDIATION", "true")
    monkeypatch.setenv("BOT_GITHUB_LOGIN", "review-bot")


@pytest.fixture
def offline_repository_client(monkeypatch):
    from code_review_agent.platform.base import PlatformPRMetadata
    from code_review_agent.platform.github_adapter import GitHubPlatformClient

    monkeypatch.setattr(
        GitHubPlatformClient,
        "fetch_pull_request_diff",
        lambda *a, **kw: "diff --git a/a.py b/a.py\n+value=1\n",
    )
    monkeypatch.setattr(
        GitHubPlatformClient,
        "fetch_pull_request_metadata",
        lambda *a, **kw: PlatformPRMetadata(head_sha="a" * 40, head_ref="feature"),
    )
    monkeypatch.setattr(
        GitHubPlatformClient, "fetch_source_file", lambda *a, **kw: b"value=1\n"
    )
    monkeypatch.setattr(
        GitHubPlatformClient, "get_repository_permission", lambda *a, **kw: "maintain"
    )


@pytest.fixture
def queued_claim():
    """Create a real SQLite job lease for tests exercising provider writes."""

    def create(target="org/repo#1"):
        from code_review_agent.webhook_queue import WebhookJobQueue

        queue = WebhookJobQueue()
        job_id = queue.enqueue(target, {}, job_kind="command")
        claim = queue.claim_next_job()
        assert claim["job_id"] == job_id
        return {"job_id": job_id, "claim_token": claim["claim_token"]}

    return create
