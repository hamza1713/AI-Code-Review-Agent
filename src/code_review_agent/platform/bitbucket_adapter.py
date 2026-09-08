"""
Bitbucket Platform Adapter.
Implements GitPlatformClient for Bitbucket Cloud via Bitbucket REST API v2.0.
"""

import os
import re
from typing import Dict, Any, List, Optional
import httpx

from code_review_agent.models import InlineComment
from code_review_agent.platform.base import GitPlatformClient, PlatformPRIdentifier, PlatformPRMetadata


class BitbucketPlatformClient(GitPlatformClient):
    """Platform client adapter for Bitbucket Cloud Pull Requests."""

    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        token: Optional[str] = None,
        base_url: Optional[str] = None
    ):
        self.username = username or os.environ.get("BITBUCKET_USERNAME", "")
        self.password = password or os.environ.get("BITBUCKET_APP_PASSWORD", "")
        self.token = token or os.environ.get("BITBUCKET_TOKEN", "")
        self.base_url = (base_url or "https://api.bitbucket.org/2.0").rstrip("/")

    def _get_client(self, timeout: float = 30.0) -> httpx.Client:
        headers = {
            "Accept": "application/json",
            "User-Agent": "AI-Code-Review-Agent/2.0",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
            return httpx.Client(headers=headers, timeout=timeout)
        elif self.username and self.password:
            return httpx.Client(headers=headers, auth=(self.username, self.password), timeout=timeout)
        return httpx.Client(headers=headers, timeout=timeout)

    def parse_pr_identifier(self, pr_identifier: str) -> PlatformPRIdentifier:
        """
        Parse Bitbucket PR URLs or shorthand into normalized PlatformPRIdentifier.
        Supports:
          - https://bitbucket.org/workspace/repo/pull-requests/42
          - bitbucket:workspace/repo/pull-requests/42
          - workspace/repo/pull-requests/42
        """
        clean = pr_identifier.strip()
        match = re.search(r"bitbucket\.org/([^/]+)/([^/]+)/pull-requests/(\d+)", clean)
        if match:
            return PlatformPRIdentifier(
                platform="bitbucket",
                owner_or_project=match.group(1),
                repo_or_slug=match.group(2),
                pr_id=int(match.group(3)),
                raw_identifier=pr_identifier
            )

        match = re.search(r"^([^/]+)/([^/]+)/pull-requests/(\d+)$", clean)
        if match:
            return PlatformPRIdentifier(
                platform="bitbucket",
                owner_or_project=match.group(1),
                repo_or_slug=match.group(2),
                pr_id=int(match.group(3)),
                raw_identifier=pr_identifier
            )

        raise ValueError(
            f"Invalid Bitbucket PR identifier '{pr_identifier}'. "
            "Expected format: 'https://bitbucket.org/workspace/repo/pull-requests/123'"
        )

    def fetch_pull_request_metadata(self, owner: str, repo: str, pull_number: int) -> PlatformPRMetadata:
        url = f"{self.base_url}/repositories/{owner}/{repo}/pullrequests/{pull_number}"
        with self._get_client() as client:
            resp = client.get(url)
            if resp.status_code != 200:
                raise RuntimeError(f"Failed to fetch Bitbucket PR from {url}: [{resp.status_code}] {resp.text}")
            data = resp.json()
            return PlatformPRMetadata(
                title=data.get("title", ""),
                body=data.get("description") or "",
                author=data.get("author", {}).get("display_name") or data.get("author", {}).get("nickname", ""),
                head_sha=data.get("source", {}).get("commit", {}).get("hash", ""),
                base_sha=data.get("destination", {}).get("commit", {}).get("hash", ""),
                head_ref=data.get("source", {}).get("branch", {}).get("name", ""),
                base_ref=data.get("destination", {}).get("branch", {}).get("name", ""),
                state=data.get("state", "OPEN").lower(),
                html_url=data.get("links", {}).get("html", {}).get("href", "")
            )

    def fetch_pull_request_diff(self, owner: str, repo: str, pull_number: int) -> str:
        url = f"{self.base_url}/repositories/{owner}/{repo}/pullrequests/{pull_number}/diff"
        with self._get_client(timeout=45.0) as client:
            resp = client.get(url, headers={"Accept": "text/plain"})
            if resp.status_code != 200:
                raise RuntimeError(f"Failed to fetch Bitbucket PR diff: [{resp.status_code}] {resp.text}")
            return resp.text

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
        """Post top-level comment and inline feedback to Bitbucket PR."""
        url = f"{self.base_url}/repositories/{owner}/{repo}/pullrequests/{pull_number}/comments"
        full_comment = f"## 🤖 AI Code Review Verdict: **{event}**\n\n{body}"
        if comments:
            full_comment += "\n\n### 📝 Line-Level Findings\n"
            for c in comments:
                full_comment += f"\n- **`{c.path}:L{c.line}`** ({c.severity}): {c.comment_body}\n"
                if c.suggestion_code:
                    full_comment += f"```suggestion\n{c.suggestion_code.strip()}\n```\n"

        payload = {"content": {"raw": full_comment}}
        with self._get_client() as client:
            resp = client.post(url, json=payload)
            if resp.status_code not in (200, 201):
                raise RuntimeError(f"Failed to post Bitbucket review: [{resp.status_code}] {resp.text}")
            return resp.json()

    def post_issue_comment(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        body: str
    ) -> Dict[str, Any]:
        """Post comment to PR or Issue in Bitbucket."""
        url = f"{self.base_url}/repositories/{owner}/{repo}/pullrequests/{issue_number}/comments"
        payload = {"content": {"raw": body}}
        with self._get_client() as client:
            resp = client.post(url, json=payload)
            if resp.status_code not in (200, 201):
                # Fallback to issue comment
                issue_url = f"{self.base_url}/repositories/{owner}/{repo}/issues/{issue_number}/comments"
                resp = client.post(issue_url, json=payload)
                if resp.status_code not in (200, 201):
                    raise RuntimeError(f"Failed to post Bitbucket comment: [{resp.status_code}] {resp.text}")
            return resp.json()

    def update_pull_request_description(
        self,
        owner: str,
        repo: str,
        pull_number: int,
        body: str,
        title: Optional[str] = None
    ) -> Dict[str, Any]:
        url = f"{self.base_url}/repositories/{owner}/{repo}/pullrequests/{pull_number}"
        payload: Dict[str, Any] = {"description": body}
        if title:
            payload["title"] = title

        with self._get_client() as client:
            resp = client.put(url, json=payload)
            if resp.status_code != 200:
                raise RuntimeError(f"Failed to update Bitbucket PR: [{resp.status_code}] {resp.text}")
            return resp.json()

    def fetch_issue(
        self,
        owner: str,
        repo: str,
        issue_number: int
    ) -> Dict[str, Any]:
        url = f"{self.base_url}/repositories/{owner}/{repo}/issues/{issue_number}"
        with self._get_client() as client:
            resp = client.get(url)
            if resp.status_code != 200:
                raise RuntimeError(f"Failed to fetch Bitbucket issue: [{resp.status_code}] {resp.text}")
            data = resp.json()
            return {
                "title": data.get("title", ""),
                "body": data.get("content", {}).get("raw") or "",
                "state": data.get("state", "new"),
                "html_url": data.get("links", {}).get("html", {}).get("href", "")
            }

    def list_pull_request_review_comments(
        self,
        owner: str,
        repo: str,
        pull_number: int,
        max_pages: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        List PR comments, normalized to the learning loop's shape
        ({'body', 'user': {'login'}}). Deleted comments (no content) are skipped, and
        pagination follows Bitbucket's `next` links up to `max_pages`.
        """
        url = f"{self.base_url}/repositories/{owner}/{repo}/pullrequests/{pull_number}/comments?pagelen=100"
        comments: List[Dict[str, Any]] = []
        with self._get_client() as client:
            pages = 0
            while url and pages < max_pages:
                resp = client.get(url)
                if resp.status_code != 200:
                    break
                data = resp.json()
                for c in data.get("values", []):
                    raw = (c.get("content") or {}).get("raw")
                    if not raw:
                        continue
                    user = c.get("user") or {}
                    login = user.get("nickname") or user.get("display_name", "")
                    comments.append({"body": raw, "user": {"login": login}})
                url = data.get("next")
                pages += 1
        return comments
