"""
Temporary PR repository checkout for the interactive bot.

The RAG / AST / governance engines index a real filesystem `repo_root`. For a
locally-invoked review that root is the developer's working tree, but for a live
GitHub webhook (a PR opened on the server) there is no checkout — so, without this,
the engines would index the *server's own working directory*, producing irrelevant
context and leaking the server's code into prompts.

`temporary_pr_checkout` shallow-clones the PR's repository into an isolated temp
directory and best-effort checks out the PR head, then cleans up. It ALWAYS yields
an isolated directory (an empty one on any failure) and NEVER falls back to the
process working directory, so the bot can never accidentally index the server tree.
"""

import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from typing import Iterator, Optional

from code_review_agent.config import get_github_token, logger


def _run_git(args: list, timeout: float = 120.0) -> bool:
    """Run a git command, returning True on success. Never raises."""
    try:
        subprocess.run(
            ["git", *args],
            check=True,
            capture_output=True,
            timeout=timeout,
        )
        return True
    except Exception as e:
        logger.debug(f"git {' '.join(args[:2])}… failed: {e}")
        return False


# Per-platform clone host and credential-user prefix for token auth in the clone URL.
_PLATFORM_HOSTS = {"github": "github.com", "gitlab": "gitlab.com", "bitbucket": "bitbucket.org"}
_PLATFORM_AUTH_USER = {"github": "x-access-token", "gitlab": "oauth2", "bitbucket": "x-token-auth"}
_PLATFORM_TOKEN_ENV = {"github": "GITHUB_TOKEN", "gitlab": "GITLAB_TOKEN", "bitbucket": "BITBUCKET_TOKEN"}


@contextmanager
def temporary_pr_checkout(
    owner: str,
    repo: str,
    head_sha: Optional[str] = None,
    head_ref: Optional[str] = None,
    token: Optional[str] = None,
    platform: str = "github",
    host: Optional[str] = None,
) -> Iterator[str]:
    """
    Yield a path to an isolated checkout of the PR/MR's repository, cleaned up on exit.

    Works across GitHub, GitLab, and Bitbucket by selecting the right host and token
    auth scheme (`host` overrides the default, e.g. for self-hosted GitLab). Always
    yields an isolated temp directory. On any clone failure (or when cloning is disabled
    via BOT_CLONE_REPO=false) it yields an *empty* temp directory, which the indexers
    treat as a repo with no symbols — safe, if contextless — rather than the server's
    own working directory.
    """
    platform = (platform or "github").strip().lower()
    tmp = tempfile.mkdtemp(prefix="cr_pr_")
    try:
        if os.getenv("BOT_CLONE_REPO", "true").strip().lower() in ("0", "false", "no"):
            logger.info("BOT_CLONE_REPO disabled; using empty isolated context for this command.")
            yield tmp
            return

        if token is None:
            token = os.getenv(_PLATFORM_TOKEN_ENV.get(platform, "GITHUB_TOKEN"), "") or (
                get_github_token() if platform == "github" else ""
            )
        clone_host = host or _PLATFORM_HOSTS.get(platform, "github.com")
        auth_user = _PLATFORM_AUTH_USER.get(platform, "x-access-token")
        # Token is embedded in the clone URL only; it is never logged.
        auth = f"{auth_user}:{token}@" if token else ""
        clone_url = f"https://{auth}{clone_host}/{owner}/{repo}.git"

        if not _run_git(["clone", "--depth", "1", "--no-single-branch", clone_url, tmp]):
            logger.warning(
                f"Could not clone {owner}/{repo}; the command will run with empty repo context."
            )
            yield tmp
            return

        # Best-effort: move the working tree to the exact PR head. If this fails
        # (e.g. a fork's ref is not on origin), the default-branch clone is still a
        # valid, relevant checkout for surrounding-code context.
        ref = head_sha or head_ref
        if ref:
            if _run_git(["-C", tmp, "fetch", "--depth", "1", "origin", ref]):
                _run_git(["-C", tmp, "checkout", "--force", "FETCH_HEAD"])
            elif head_ref and head_ref != ref:
                if _run_git(["-C", tmp, "fetch", "--depth", "1", "origin", head_ref]):
                    _run_git(["-C", tmp, "checkout", "--force", "FETCH_HEAD"])

        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
