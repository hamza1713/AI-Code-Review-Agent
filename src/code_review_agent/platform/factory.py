"""
Universal Git Platform Client Factory.
Inspects repository URLs and identifiers and instantiates the matching platform adapter.
"""

from pathlib import Path
from typing import Optional, Tuple

from code_review_agent.platform.base import GitPlatformClient, PlatformPRIdentifier
from code_review_agent.platform.github_adapter import GitHubPlatformClient
from code_review_agent.platform.gitlab_adapter import GitLabPlatformClient
from code_review_agent.platform.bitbucket_adapter import BitbucketPlatformClient
from code_review_agent.platform.local_git_adapter import LocalGitPlatformClient


def get_platform_client(
    target_identifier: str,
    token: Optional[str] = None
) -> Tuple[GitPlatformClient, PlatformPRIdentifier]:
    """
    Factory resolving the appropriate GitPlatformClient and parsed PlatformPRIdentifier.

    Detection rules (applied to both full URLs and the shorthand identifiers the
    webhook queue enqueues, e.g. 'group/proj/merge_requests/5'):
    - 'local:'/'file://' or an existing directory on disk -> LocalGitPlatformClient
    - 'gitlab' host or '/merge_requests/' segment          -> GitLabPlatformClient
    - 'bitbucket' host or '/pull-requests/' segment        -> BitbucketPlatformClient
    - 'github.com' or 'owner/repo/pull/123' / 'owner/repo#123' -> GitHubPlatformClient
    - Unknown or malformed inputs raise ValueError
    """
    clean = target_identifier.strip().lower()
    is_url = clean.startswith("http://") or clean.startswith("https://")

    # Local paths take precedence (they may otherwise contain misleading substrings).
    if (
        clean.startswith("local:")
        or clean.startswith("file://")
        or (Path(target_identifier).exists() and Path(target_identifier).is_dir())
    ):
        repo_dir = target_identifier.replace("local://", "").replace("local:", "").replace("file://", "").strip()
        client: GitPlatformClient = LocalGitPlatformClient(repo_dir=repo_dir or None)
    # Platform detection works on URLs AND on scheme-less shorthand identifiers, so the
    # durable-queue identifiers ('group/proj/merge_requests/5') resolve to the right host.
    elif "gitlab" in clean or "/merge_requests/" in clean:
        client = GitLabPlatformClient(token=token)
    elif "bitbucket" in clean or "/pull-requests/" in clean:
        client = BitbucketPlatformClient(token=token)
    elif is_url:
        # A full URL to an unrecognized host is rejected rather than guessed.
        if "github.com" in clean:
            client = GitHubPlatformClient(token=token)
        else:
            raise ValueError(f"Unsupported or unparseable platform URL: {target_identifier}")
    elif "#" in clean or "/" in clean:
        # Scheme-less owner/repo shorthand (e.g. 'owner/repo/pull/5' or 'owner/repo#5').
        client = GitHubPlatformClient(token=token)
    else:
        raise ValueError(f"Unsupported or unparseable platform identifier: {target_identifier}")

    ident = client.parse_pr_identifier(target_identifier)
    return client, ident


def get_client_for_platform(platform: str, token: Optional[str] = None) -> GitPlatformClient:
    """
    Build a platform client from a platform name ('github'|'gitlab'|'bitbucket'|'local').

    Used where only the platform is known (e.g. a merge webhook that already parsed the
    project), not a full URL. Unknown names default to GitHub.
    """
    name = (platform or "github").strip().lower()
    if name == "gitlab":
        return GitLabPlatformClient(token=token)
    if name == "bitbucket":
        return BitbucketPlatformClient(token=token)
    if name == "local":
        return LocalGitPlatformClient()
    return GitHubPlatformClient(token=token)

