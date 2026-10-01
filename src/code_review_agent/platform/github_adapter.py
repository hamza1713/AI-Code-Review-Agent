"""
GitHub Platform Adapter.
Implements GitPlatformClient for GitHub.com and GitHub Enterprise via GitHub REST API v3.
"""

from typing import Any

from code_review_agent.github_client import GitHubClient
from code_review_agent.models import InlineComment
from code_review_agent.platform.base import (
    GitPlatformClient,
    PlatformPRIdentifier,
    PlatformPRMetadata,
)


class GitHubPlatformClient(GitPlatformClient):
    """Platform client adapter for GitHub."""

    def __init__(self, token: str | None = None):
        self.client = GitHubClient() if token is None else GitHubClient(token=token)

    def get_repository_permission(self, owner, repo, actor):
        return self.client.get_repository_permission(owner, repo, actor)

    def list_pull_request_reviews(self, owner, repo, pull_number):
        return self.client.list_pull_request_reviews(owner, repo, pull_number)

    def list_issue_comments(self, owner, repo, issue_number):
        return self.client.list_issue_comments(owner, repo, issue_number)

    def parse_pr_identifier(self, pr_identifier: str) -> PlatformPRIdentifier:
        owner, repo, pull_num = self.client.parse_pr_identifier(pr_identifier)
        return PlatformPRIdentifier(
            platform="github",
            owner_or_project=owner,
            repo_or_slug=repo,
            pr_id=pull_num,
            raw_identifier=pr_identifier,
        )

    def fetch_pull_request_metadata(
        self, owner: str, repo: str, pull_number: int
    ) -> PlatformPRMetadata:
        data = self.client.fetch_pull_request_metadata(owner, repo, pull_number)
        return PlatformPRMetadata(
            title=data.get("title", ""),
            body=data.get("body", ""),
            author=data.get("author", ""),
            head_sha=data.get("head_sha", ""),
            head_repository=data.get("head_repository", ""),
            base_sha=data.get("base_sha", ""),
            head_ref=data.get("head_ref", ""),
            base_ref=data.get("base_ref", ""),
            state=data.get("state", "open"),
            html_url=data.get("html_url", ""),
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
        commit_id: str | None = None,
        comments: list[InlineComment] | None = None,
    ) -> dict[str, Any]:
        return self.client.post_pull_request_review(
            owner=owner,
            repo=repo,
            pull_number=pull_number,
            event=event,
            body=body,
            commit_id=commit_id,
            comments=comments,
        )

    def post_issue_comment(
        self, owner: str, repo: str, issue_number: int, body: str
    ) -> dict[str, Any]:
        return self.client.post_issue_comment(
            owner=owner, repo=repo, issue_number=issue_number, body=body
        )

    def update_pull_request_description(
        self,
        owner: str,
        repo: str,
        pull_number: int,
        body: str,
        title: str | None = None,
    ) -> dict[str, Any]:
        return self.client.update_pull_request_description(
            owner=owner, repo=repo, pull_number=pull_number, body=body, title=title
        )

    def fetch_issue(self, owner: str, repo: str, issue_number: int) -> dict[str, Any]:
        return self.client.fetch_issue(
            owner=owner, repo=repo, issue_number=issue_number
        )

    def list_pull_request_review_comments(
        self, owner: str, repo: str, pull_number: int
    ) -> list[dict[str, Any]]:
        return self.client.list_pull_request_review_comments(owner, repo, pull_number)

    def set_commit_status(
        self,
        owner: str,
        repo: str,
        sha: str,
        state: str,
        description: str,
        context: str = "ai-code-review",
        target_url: str | None = None,
    ) -> dict[str, Any]:
        return self.client.set_commit_status(
            owner=owner,
            repo=repo,
            sha=sha,
            state=state,
            description=description,
            context=context,
            target_url=target_url,
        )

    def create_or_update_check_run(
        self,
        owner: str,
        repo: str,
        head_sha: str,
        name: str = "AI Code Review",
        status: str = "queued",
        conclusion: str | None = None,
        title: str | None = None,
        summary: str | None = None,
        annotations: list[dict[str, Any]] | None = None,
        details_url: str | None = None,
        check_run_id: int | None = None,
    ) -> dict[str, Any]:
        return self.client.create_or_update_check_run(
            owner=owner,
            repo=repo,
            head_sha=head_sha,
            name=name,
            status=status,
            conclusion=conclusion,
            title=title,
            summary=summary,
            annotations=annotations,
            details_url=details_url,
            check_run_id=check_run_id,
        )

    def fetch_source_file(self, owner: str, repo: str, path: str, ref: str) -> bytes:
        return self.client.fetch_source_file(owner, repo, path, ref)

    def fetch_file_content(
        self, owner: str, repo: str, path: str, ref: str | None = None
    ) -> str:
        return self.client.fetch_file_content(
            owner=owner, repo=repo, path=path, ref=ref
        )

    def commit_file_change(
        self,
        owner: str,
        repo: str,
        branch: str,
        path: str,
        content: str,
        commit_message: str,
    ) -> dict[str, Any]:
        return self.client.commit_file_change(
            owner=owner,
            repo=repo,
            branch=branch,
            path=path,
            content=content,
            commit_message=commit_message,
        )
