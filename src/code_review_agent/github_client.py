"""
GitHub API Client for Live Pull Request Ingestion and Inline Comment Submission.
Interacts with GitHub REST API v3 using httpx.
"""

import base64
import hashlib
import re
from typing import Dict, Any, List, Optional, Tuple
import httpx

from code_review_agent.config import get_github_token, logger
from code_review_agent.models import InlineComment


class GitHubClient:
    """Client for interacting with GitHub Pull Requests API."""

    def __init__(self, token: Optional[str] = None):
        tok = token or get_github_token()
        # Filter dummy/placeholder tokens
        if tok and any(p in tok.lower() for p in ["your_github", "your_token", "placeholder", "ghp_your"]):
            tok = None
        self.token = tok
        self.base_url = "https://api.github.com"

    def _get_headers(self, accept: str = "application/vnd.github.v3+json", include_auth: bool = True) -> Dict[str, str]:
        """Build standard GitHub authorization headers."""
        headers = {
            "Accept": accept,
            "User-Agent": "AI-Code-Review-Agent/2.0",
        }
        if self.token and include_auth:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    @staticmethod
    def parse_pr_identifier(pr_identifier: str) -> Tuple[str, str, int]:
        """
        Parse PR URLs or shorthand formats into (owner, repo, pull_number).
        Supports:
          - https://github.com/owner/repo/pull/42
          - https://github.com/owner/repo/pull/42?utm_source=...
          - owner/repo/pull/42
          - owner/repo/42
          - owner/repo#42
        """
        clean = pr_identifier.strip()
        # Strip query parameters or URL anchors (e.g. ?utm_source=... or #issuecomment-...)
        if "?" in clean:
            clean = clean.split("?")[0]
        if "#" in clean and not re.search(r"^[^/]+/[^/#]+#\d+$", clean):
            clean = clean.split("#")[0]

        match = re.search(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", clean)
        if match:
            return match.group(1), match.group(2), int(match.group(3))

        match = re.search(r"^([^/]+)/([^/#]+)(?:/pull/|/|#)(\d+)$", clean)
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
            # If 401 Unauthorized occurs due to an expired or bad token on a public repo, fallback to unauthenticated
            if resp.status_code == 401 and self.token:
                logger.warning(f"GitHub token rejected with 401 for {url}; retrying unauthenticated for public repository...")
                resp = client.get(url, headers=self._get_headers(include_auth=False))

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
        """Fetch raw unified diff for a pull request with multi-layer fallback for public repositories."""
        url = f"{self.base_url}/repos/{owner}/{repo}/pulls/{pull_number}"
        headers = self._get_headers(accept="application/vnd.github.v3.diff")
        with httpx.Client(headers=headers, timeout=45.0, follow_redirects=True) as client:
            resp = client.get(url)
            # Fallback 1: If 401 Unauthorized (e.g. invalid/expired token on public repo), retry unauthenticated
            if resp.status_code == 401 and self.token:
                logger.warning(f"GitHub token rejected with 401 for {url}; retrying unauthenticated API request...")
                resp = client.get(url, headers=self._get_headers(accept="application/vnd.github.v3.diff", include_auth=False))

            if resp.status_code == 200 and resp.text.strip():
                return resp.text

            # Fallback 2: Direct public web diff endpoint (works reliably for all public PRs without API token)
            direct_diff_url = f"https://github.com/{owner}/{repo}/pull/{pull_number}.diff"
            logger.info(f"Attempting direct public PR diff fetch from {direct_diff_url}...")
            resp_direct = client.get(
                direct_diff_url,
                headers={"User-Agent": "AI-Code-Review-Agent/2.0"},
                follow_redirects=True
            )
            if resp_direct.status_code == 200 and resp_direct.text.strip():
                return resp_direct.text

            raise RuntimeError(
                f"Failed to fetch PR diff from {url}: [{resp.status_code}] {resp.text}"
            )

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

    def set_commit_status(
        self,
        owner: str,
        repo: str,
        sha: str,
        state: str,  # 'pending', 'success', 'failure', 'error'
        description: str,
        context: str = "ai-code-review",
        target_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """Update commit status on a specific commit SHA using GitHub Statuses API."""
        url = f"{self.base_url}/repos/{owner}/{repo}/statuses/{sha}"
        payload = {
            "state": state,
            "description": description[:140],
            "context": context
        }
        if target_url:
            payload["target_url"] = target_url

        with httpx.Client(headers=self._get_headers(), timeout=30.0) as client:
            resp = client.post(url, json=payload)
            if resp.status_code not in (200, 201):
                logger.warning(f"Failed to set commit status on {url}: [{resp.status_code}] {resp.text}")
                return {"error": resp.text, "status_code": resp.status_code}
            return resp.json()

    def create_or_update_check_run(
        self,
        owner: str,
        repo: str,
        head_sha: str,
        name: str = "AI Code Review",
        status: str = "queued",  # 'queued', 'in_progress', 'completed'
        conclusion: Optional[str] = None,  # 'success', 'failure', 'neutral', 'action_required'
        title: Optional[str] = None,
        summary: Optional[str] = None,
        annotations: Optional[List[Dict[str, Any]]] = None,
        details_url: Optional[str] = None,
        check_run_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Create or update a GitHub Check Run.
        Falls back to Commit Status API if token lacks checks:write permissions (403/404).
        """
        if check_run_id:
            url = f"{self.base_url}/repos/{owner}/{repo}/check-runs/{check_run_id}"
            method = "PATCH"
        else:
            url = f"{self.base_url}/repos/{owner}/{repo}/check-runs"
            method = "POST"

        payload: Dict[str, Any] = {
            "name": name,
            "head_sha": head_sha,
            "status": status,
        }
        if status == "completed" and conclusion:
            payload["conclusion"] = conclusion

        if title or summary or annotations:
            payload["output"] = {
                "title": (title or name)[:255],
                "summary": summary or "Automated review evaluation completed.",
                "annotations": (annotations or [])[:50]
            }

        if details_url:
            payload["details_url"] = details_url

        headers = self._get_headers(accept="application/vnd.github.v3+json")
        with httpx.Client(headers=headers, timeout=30.0) as client:
            try:
                resp = client.request(method, url, json=payload)
                if resp.status_code in (200, 201):
                    return resp.json()

                # If 403 or 404, token lacks checks permission or app not installed; fall back to commit status
                if resp.status_code in (403, 404, 422):
                    logger.info(
                        f"GitHub Check Run API returned [{resp.status_code}]. "
                        "Falling back to GitHub Commit Status API..."
                    )
                    state = "pending" if status != "completed" else (
                        "success" if conclusion in ("success", "neutral") else "failure"
                    )
                    return self.set_commit_status(
                        owner=owner,
                        repo=repo,
                        sha=head_sha,
                        state=state,
                        description=(summary or title or name)[:140],
                        context=name.lower().replace(" ", "-"),
                        target_url=details_url
                    )
                return {"error": resp.text, "status_code": resp.status_code}
            except Exception as e:
                logger.warning(f"Error calling check run API: {e}; falling back to commit status...")
                state = "pending" if status != "completed" else (
                    "success" if conclusion in ("success", "neutral") else "failure"
                )
                return self.set_commit_status(
                    owner=owner,
                    repo=repo,
                    sha=head_sha,
                    state=state,
                    description=(summary or title or name)[:140],
                    context=name.lower().replace(" ", "-"),
                    target_url=details_url
                )

    def fetch_file_content(
        self,
        owner: str,
        repo: str,
        path: str,
        ref: Optional[str] = None
    ) -> str:
        """Fetch raw text content of a file from GitHub repository."""
        if not self.token:
            return ""
        url = f"{self.base_url}/repos/{owner}/{repo}/contents/{path}"
        params = {"ref": ref} if ref else {}
        headers = self._get_headers(accept="application/vnd.github.v3+json")
        with httpx.Client(headers=headers, timeout=30.0) as client:
            resp = client.get(url, params=params)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("encoding") == "base64" and "content" in data:
                    return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
                return data.get("content", "")
            return ""

    def commit_file_change(
        self,
        owner: str,
        repo: str,
        branch: str,
        path: str,
        content: str,
        commit_message: str
    ) -> Dict[str, Any]:
        """
        Commit an updated file directly to a branch on GitHub.
        Retrieves current file SHA (if existing), encodes content, and PUTs to GitHub Contents API.
        """
        if not self.token:
            simulated_sha = "simulated-" + hashlib.sha256(content.encode("utf-8")).hexdigest()[:10]
            return {
                "sha": simulated_sha,
                "html_url": f"https://github.com/{owner}/{repo}/commit/{simulated_sha}",
                "branch": branch,
                "path": path
            }

        url = f"{self.base_url}/repos/{owner}/{repo}/contents/{path}"
        headers = self._get_headers(accept="application/vnd.github.v3+json")

        with httpx.Client(headers=headers, timeout=30.0) as client:
            # 1. Check if file exists on target branch
            file_sha = None
            get_resp = client.get(url, params={"ref": branch})
            if get_resp.status_code == 200:
                file_sha = get_resp.json().get("sha")

            # 2. Commit file update
            encoded_content = base64.b64encode(content.encode("utf-8")).decode("ascii")
            payload: Dict[str, Any] = {
                "message": commit_message,
                "content": encoded_content,
                "branch": branch
            }
            if file_sha:
                payload["sha"] = file_sha

            put_resp = client.put(url, json=payload)
            if put_resp.status_code in (200, 201):
                data = put_resp.json()
                commit_info = data.get("commit", {})
                return {
                    "sha": commit_info.get("sha", ""),
                    "html_url": commit_info.get("html_url", ""),
                    "branch": branch,
                    "path": path
                }
            if put_resp.status_code in (401, 403):
                raise PermissionError(
                    f"GitHub token lacks write permissions on {owner}/{repo}:{branch}. "
                    f"Status [{put_resp.status_code}]: {put_resp.text}"
                )
            raise RuntimeError(f"Failed to commit file change: [{put_resp.status_code}] {put_resp.text}")

