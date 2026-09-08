"""
GitHub API Client for Live Pull Request Ingestion and Inline Comment Submission.
Interacts with GitHub REST API v3 using httpx.
"""

import os
import re
from typing import Dict, Any, List, Optional, Tuple
import httpx

from code_review_agent.config import get_github_token, logger
from code_review_agent.models import InlineComment


class GitHubClient:
    """Client for interacting with GitHub Pull Requests API."""

    def __init__(self, token: Optional[str] = None):
        self.token = token or get_github_token()
        self.base_url = "https://api.github.com"

    def _get_headers(self, accept: str = "application/vnd.github.v3+json") -> Dict[str, str]:
        """Build standard GitHub authorization headers."""
        headers = {
            "Accept": accept,
            "User-Agent": "AI-Code-Review-Agent/2.0",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    @staticmethod
    def parse_pr_identifier(pr_identifier: str) -> Tuple[str, str, int]:
        """
        Parse PR URLs or shorthand formats into (owner, repo, pull_number).
        Supports:
          - https://github.com/owner/repo/pull/42
          - owner/repo/pull/42
          - owner/repo/42
          - owner/repo#42
        """
        pr_identifier = pr_identifier.strip()
        match = re.search(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", pr_identifier)
        if match:
            return match.group(1), match.group(2), int(match.group(3))

        match = re.search(r"^([^/]+)/([^/#]+)(?:/pull/|/|#)(\d+)$", pr_identifier)
        if match:
            return match.group(1), match.group(2), int(match.group(3))

        raise ValueError(
            f"Invalid PR identifier '{pr_identifier}'. "
            "Expected format: 'owner/repo/pull/123' or 'https://github.com/owner/repo/pull/123'"
        )

    def fetch_pull_request_metadata(self, owner: str, repo: str, pull_number: int) -> Dict[str, Any]:
        """Fetch title, author, branch, and commit SHAs for a pull request."""
        url = f"{self.base_url}/repos/{owner}/{repo}/pulls/{pull_number}"
        with httpx.Client(headers=self._get_headers(), timeout=30.0) as client:
            resp = client.get(url)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Failed to fetch PR metadata from {url}: [{resp.status_code}] {resp.text}"
                )
            data = resp.json()
            return {
                "title": data.get("title", ""),
                "body": data.get("body") or "",
                "author": data.get("user", {}).get("login", ""),
                "base_sha": data.get("base", {}).get("sha", ""),
                "head_sha": data.get("head", {}).get("sha", ""),
                "head_ref": data.get("head", {}).get("ref", ""),
                "base_ref": data.get("base", {}).get("ref", ""),
                "state": data.get("state", ""),
                "html_url": data.get("html_url", ""),
            }

    def fetch_pull_request_diff(self, owner: str, repo: str, pull_number: int) -> str:
        """Fetch raw unified diff for a pull request."""
        url = f"{self.base_url}/repos/{owner}/{repo}/pulls/{pull_number}"
        headers = self._get_headers(accept="application/vnd.github.v3.diff")
        with httpx.Client(headers=headers, timeout=45.0) as client:
            resp = client.get(url)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Failed to fetch PR diff from {url}: [{resp.status_code}] {resp.text}"
                )
            return resp.text

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
        """
        Submit a full Pull Request Review with top-level summary and line-level inline comments.
        """
        url = f"{self.base_url}/repos/{owner}/{repo}/pulls/{pull_number}/reviews"
        
        review_comments = []
        if comments:
            for c in comments:
                review_comments.append({
                    "path": c.path,
                    "line": c.line,
                    "side": c.side,
                    "body": c.to_github_markdown()
                })

        payload: Dict[str, Any] = {
            "body": body,
            "event": event,
        }
        if commit_id:
            payload["commit_id"] = commit_id
        if review_comments:
            payload["comments"] = review_comments

        with httpx.Client(headers=self._get_headers(), timeout=45.0) as client:
            resp = client.post(url, json=payload)
            if resp.status_code not in [200, 201]:
                # Fallback: if batch review with comments fails due to line diff positions, submit top-level review
                logger.warning(
                    f"Batch PR review submission encountered status [{resp.status_code}]: {resp.text}. "
                    "Retrying with summary-only review..."
                )
                fallback_payload = {"body": body, "event": event}
                fallback_resp = client.post(url, json=fallback_payload)
                if fallback_resp.status_code not in [200, 201]:
                    raise RuntimeError(f"Failed to post GitHub review: {fallback_resp.text}")
                return fallback_resp.json()

            return resp.json()

    def post_issue_comment(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        body: str
    ) -> Dict[str, Any]:
        """Post a markdown comment to an issue or pull request discussion thread."""
        url = f"{self.base_url}/repos/{owner}/{repo}/issues/{issue_number}/comments"
        payload = {"body": body}
        with httpx.Client(headers=self._get_headers(), timeout=30.0) as client:
            resp = client.post(url, json=payload)
            if resp.status_code not in (200, 201):
                raise RuntimeError(
                    f"Failed to post comment to {url}: [{resp.status_code}] {resp.text}"
                )
            return resp.json()

    def update_pull_request_description(
        self,
        owner: str,
        repo: str,
        pull_number: int,
        body: str,
        title: Optional[str] = None
    ) -> Dict[str, Any]:
        """Update the PR description body and optionally title."""
        url = f"{self.base_url}/repos/{owner}/{repo}/pulls/{pull_number}"
        payload: Dict[str, Any] = {"body": body}
        if title:
            payload["title"] = title
        with httpx.Client(headers=self._get_headers(), timeout=30.0) as client:
            resp = client.patch(url, json=payload)
            if resp.status_code not in (200, 201):
                raise RuntimeError(
                    f"Failed to update PR description at {url}: [{resp.status_code}] {resp.text}"
                )
            return resp.json()

    def fetch_issue(self, owner: str, repo: str, issue_number: int) -> Dict[str, Any]:
        """Fetch title, body, state, and labels for an issue."""
        url = f"{self.base_url}/repos/{owner}/{repo}/issues/{issue_number}"
        with httpx.Client(headers=self._get_headers(), timeout=30.0) as client:
            resp = client.get(url)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Failed to fetch issue from {url}: [{resp.status_code}] {resp.text}"
                )
            return resp.json()

    def list_pull_request_review_comments(
        self, owner: str, repo: str, pull_number: int, per_page: int = 100
    ) -> List[Dict[str, Any]]:
        """
        List inline review comments on a PR — these are the comments that carry
        ```suggestion blocks. Used by the learning loop to find candidate suggestions
        whose acceptance is then verified against the merged diff.
        """
        url = f"{self.base_url}/repos/{owner}/{repo}/pulls/{pull_number}/comments"
        with httpx.Client(headers=self._get_headers(), timeout=30.0) as client:
            resp = client.get(url, params={"per_page": per_page})
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Failed to list review comments from {url}: [{resp.status_code}] {resp.text}"
                )
            return resp.json()

