"""
Multi-Platform Git Provider Integration Package.
Provides unified clients for GitHub, GitLab, Bitbucket, and Local Git repositories.
"""

from code_review_agent.platform.base import (
    GitPlatformClient,
    PlatformPRIdentifier,
    PlatformPRMetadata
)
from code_review_agent.platform.github_adapter import GitHubPlatformClient
from code_review_agent.platform.gitlab_adapter import GitLabPlatformClient
from code_review_agent.platform.bitbucket_adapter import BitbucketPlatformClient
from code_review_agent.platform.local_git_adapter import LocalGitPlatformClient
from code_review_agent.platform.factory import get_platform_client

__all__ = [
    "GitPlatformClient",
    "PlatformPRIdentifier",
    "PlatformPRMetadata",
    "GitHubPlatformClient",
    "GitLabPlatformClient",
    "BitbucketPlatformClient",
    "LocalGitPlatformClient",
    "get_platform_client",
]
