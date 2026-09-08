"""
Abstract Base Platform Interface for Multi-Platform Git Providers.
Standardizes Pull Request, Merge Request, Diff, and Issue operations across
GitHub, GitLab, Bitbucket, and Local Git repositories.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field

from code_review_agent.models import InlineComment


class PlatformPRIdentifier(BaseModel):
    """Normalized Pull / Merge Request identifier across Git platforms."""
    platform: str = Field(..., description="'github', 'gitlab', 'bitbucket', or 'local'")
    owner_or_project: str = Field(..., description="Organization, group, or username")
    repo_or_slug: str = Field(..., description="Repository name or slug")
    pr_id: int = Field(default=0, description="Pull Request or Merge Request number/IID")
    raw_identifier: str = Field(default="", description="Original string or URL")


class PlatformPRMetadata(BaseModel):
    """Normalized PR/MR metadata across all platforms."""
    title: str = Field(default="", description="PR or MR title")
    body: str = Field(default="", description="Description or user story")
    author: str = Field(default="", description="Author username")
    head_sha: str = Field(default="", description="Commit SHA of head branch")
    base_sha: str = Field(default="", description="Commit SHA of target base branch")
    head_ref: str = Field(default="", description="Source branch name")
    base_ref: str = Field(default="", description="Target branch name")
    state: str = Field(default="open", description="'open', 'closed', or 'merged'")
    html_url: str = Field(default="", description="Web URL to view PR/MR")


class GitPlatformClient(ABC):
    """Abstract client interface for interacting with version control hosting platforms."""

    @abstractmethod
    def parse_pr_identifier(self, pr_identifier: str) -> PlatformPRIdentifier:
        """Parse raw PR URL or shorthand into a normalized PlatformPRIdentifier."""
        pass

    @abstractmethod
    def fetch_pull_request_metadata(self, owner: str, repo: str, pull_number: int) -> PlatformPRMetadata:
        """Fetch title, author, branch refs, and commit SHAs for a PR/MR."""
        pass

    @abstractmethod
    def fetch_pull_request_diff(self, owner: str, repo: str, pull_number: int) -> str:
        """Fetch raw unified diff string for a PR/MR."""
        pass

    @abstractmethod
    def post_pull_request_review(
        self,
        owner: str,
        repo: str,
        pull_number: int,
        event: str,  # 'APPROVE', 'REQUEST_CHANGES', or 'COMMENT'
        body: str,
        commit_id: Optional[str] = None,
        comments: Optional[List[InlineComment]] = None
    ) -> Dict[str, Any]:
        """Submit a full PR/MR review with top-level summary and line-level comments."""
        pass

    @abstractmethod
    def post_issue_comment(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        body: str
    ) -> Dict[str, Any]:
        """Post a markdown comment to an issue or PR discussion thread."""
        pass

    @abstractmethod
    def update_pull_request_description(
        self,
        owner: str,
        repo: str,
        pull_number: int,
        body: str,
        title: Optional[str] = None
    ) -> Dict[str, Any]:
        """Update the PR/MR description body and optionally title."""
        pass

    @abstractmethod
    def fetch_issue(
        self,
        owner: str,
        repo: str,
        issue_number: int
    ) -> Dict[str, Any]:
        """Fetch issue title, body, state, and labels."""
        pass

    def list_pull_request_review_comments(
        self,
        owner: str,
        repo: str,
        pull_number: int
    ) -> List[Dict[str, Any]]:
        """
        List inline review comments (which may carry ```suggestion blocks) for a PR/MR.

        Concrete (not abstract) so adapters that do not yet support it inherit a safe
        empty default — the learning loop then simply learns nothing on that platform
        rather than failing. GitHub overrides this.
        """
        return []
