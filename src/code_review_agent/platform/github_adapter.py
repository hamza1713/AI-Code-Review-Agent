"""
GitHub Platform Adapter.
Implements GitPlatformClient for GitHub.com and GitHub Enterprise via GitHub REST API v3.
"""

from typing import Dict, Any, List, Optional
from code_review_agent.github_client import GitHubClient
from code_review_agent.models import InlineComment
from code_review_agent.platform.base import GitPlatformClient, PlatformPRIdentifier, PlatformPRMetadata


class GitHubPlatformClient(GitPlatformClient):
    """Platform client adapter for GitHub."""

    def __init__(self, token: Optional[str] = None):
        self.client = GitHubClient(token=token)

    def parse_pr_identifier(self, pr_identifier: str) -> PlatformPRIdentifier:
        owner, repo, pull_num = self.client.parse_pr_identifier(pr_identifier)
        return PlatformPRIdentifier(
            platform="github",
            owner_or_project=owner,
            repo_or_slug=repo,
            pr_id=pull_num,
            raw_identifier=pr_identifier
        )

    def fetch_pull_request_metadata(self, owner: str, repo: str, pull_number: int) -> PlatformPRMetadata:
        data = self.client.fetch_pull_request_metadata(owner, repo, pull_number)
        return PlatformPRMetadata(
            title=data.get("title", ""),
            body=data.get("body", ""),
            author=data.get("author", ""),
            head_sha=data.get("head_sha", ""),
            base_sha=data.get("base_sha", ""),
            head_ref=data.get("head_ref", ""),
            base_ref=data.get("base_ref", ""),
            state=data.get("state", "open"),
            html_url=data.get("html_url", "")
        )

    def fetch_pull_request_diff(self, owner: str, repo: str, pull_number: int) -> str:
        return self.client.fetch_pull_request_diff(owner, repo, pull_number)

    def post_pull_request_review(
        self,
        owner: str,
        repo: str,
        pull_number: int,
        event: str,
        body: str,
        commit_id: Optional[str] = None,
        comments: Optional[List[InlineComment]] = None
    ) -> Dict[str, Any]:
        return self.client.post_pull_request_review(
            owner=owner,
            repo=repo,
            pull_number=pull_number,
            event=event,
            body=body,
            commit_id=commit_id,
            comments=comments
        )

    def post_issue_comment(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        body: str
    ) -> Dict[str, Any]:
        return self.client.post_issue_comment(owner=owner, repo=repo, issue_number=issue_number, body=body)

    def update_pull_request_description(
        self,
        owner: str,
        repo: str,
        pull_number: int,
        body: str,
        title: Optional[str] = None
    ) -> Dict[str, Any]:
        return self.client.update_pull_request_description(owner=owner, repo=repo, pull_number=pull_number, body=body, title=title)

    def fetch_issue(
        self,
        owner: str,
        repo: str,
        issue_number: int
    ) -> Dict[str, Any]:
        return self.client.fetch_issue(owner=owner, repo=repo, issue_number=issue_number)

    def list_pull_request_review_comments(
        self,
        owner: str,
        repo: str,
        pull_number: int
    ) -> List[Dict[str, Any]]:
        return self.client.list_pull_request_review_comments(owner, repo, pull_number)
