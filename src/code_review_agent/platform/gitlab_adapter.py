"""
GitLab Platform Adapter.
Implements GitPlatformClient for GitLab.com and self-hosted GitLab CE/EE instances via GitLab REST API v4.
"""

import os
import re
import urllib.parse
from typing import Dict, Any, List, Optional
import httpx

from code_review_agent.models import InlineComment
from code_review_agent.platform.base import GitPlatformClient, PlatformPRIdentifier, PlatformPRMetadata


class GitLabPlatformClient(GitPlatformClient):
    """Platform client adapter for GitLab Merge Requests and Issues."""

    def __init__(self, token: Optional[str] = None, base_url: Optional[str] = None):
        self.token = token or os.environ.get("GITLAB_TOKEN", "")
        self.base_url = (base_url or os.environ.get("GITLAB_BASE_URL", "https://gitlab.com/api/v4")).rstrip("/")

    def _get_headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "AI-Code-Review-Agent/2.0",
        }
        if self.token:
            headers["PRIVATE-TOKEN"] = self.token
        return headers

    def _encode_project(self, project: str) -> str:
        """URL-encode project path e.g. 'group/project' -> 'group%2Fproject'."""
        return urllib.parse.quote(project.strip("/"), safe="")

    def parse_pr_identifier(self, pr_identifier: str) -> PlatformPRIdentifier:
        """
        Parse GitLab MR URLs or shorthand into normalized PlatformPRIdentifier.
        Supports:
          - https://gitlab.com/group/project/-/merge_requests/42
          - gitlab:group/project/merge_requests/42
          - group/project/merge_requests/42
        """
        clean = pr_identifier.strip()
        match = re.search(r"gitlab(?:\.[^/]+)?/(.+?)(?:/-)?/merge_requests/(\d+)", clean)
        if match:
            project_path = match.group(1).strip("/")
            mr_id = int(match.group(2))
            parts = project_path.split("/")
            owner = "/".join(parts[:-1]) if len(parts) > 1 else parts[0]
            repo = parts[-1]
            return PlatformPRIdentifier(
                platform="gitlab",
                owner_or_project=owner,
                repo_or_slug=repo,
                pr_id=mr_id,
                raw_identifier=pr_identifier
            )

        # Scheme-less shorthand, incl. nested groups: 'group/sub/project/merge_requests/5'.
        match = re.search(r"^(.+?)/merge_requests/(\d+)$", clean)
        if match:
            project_path = match.group(1).strip("/")
            parts = project_path.split("/")
            owner = "/".join(parts[:-1]) if len(parts) > 1 else parts[0]
            repo = parts[-1]
            return PlatformPRIdentifier(
                platform="gitlab",
                owner_or_project=owner,
                repo_or_slug=repo,
                pr_id=int(match.group(2)),
                raw_identifier=pr_identifier
            )

        raise ValueError(
            f"Invalid GitLab MR identifier '{pr_identifier}'. "
            "Expected format: 'https://gitlab.com/group/repo/-/merge_requests/123' or 'group/repo/merge_requests/123'"
        )

    def fetch_pull_request_metadata(self, owner: str, repo: str, pull_number: int) -> PlatformPRMetadata:
        project_str = f"{owner}/{repo}"
        enc = self._encode_project(project_str)
        url = f"{self.base_url}/projects/{enc}/merge_requests/{pull_number}"

        with httpx.Client(headers=self._get_headers(), timeout=30.0) as client:
            resp = client.get(url)
            if resp.status_code != 200:
                raise RuntimeError(f"Failed to fetch GitLab MR from {url}: [{resp.status_code}] {resp.text}")
            data = resp.json()
            return PlatformPRMetadata(
                title=data.get("title", ""),
                body=data.get("description") or "",
                author=data.get("author", {}).get("username", ""),
                head_sha=data.get("sha", ""),
                base_sha=data.get("diff_refs", {}).get("base_sha", ""),
                head_ref=data.get("source_branch", ""),
                base_ref=data.get("target_branch", ""),
                state=data.get("state", "opened"),
                html_url=data.get("web_url", "")
            )

    def fetch_pull_request_diff(self, owner: str, repo: str, pull_number: int) -> str:
        project_str = f"{owner}/{repo}"
        enc = self._encode_project(project_str)
        # Attempt raw diff endpoint
        url = f"{self.base_url}/projects/{enc}/merge_requests/{pull_number}.diff"
        headers = self._get_headers()
        headers["Accept"] = "text/plain"

        with httpx.Client(headers=headers, timeout=45.0) as client:
            resp = client.get(url)
            if resp.status_code == 200:
                return resp.text

            # Fallback to diffs JSON API and reconstruct unified diff
            diffs_url = f"{self.base_url}/projects/{enc}/merge_requests/{pull_number}/diffs"
            resp2 = client.get(diffs_url, headers=self._get_headers())
            if resp2.status_code != 200:
                raise RuntimeError(f"Failed to fetch GitLab MR diff: [{resp.status_code}] {resp.text}")
            diff_chunks = []
            for d in resp2.json():
                old_path = d.get("old_path", "unknown")
                new_path = d.get("new_path", old_path)
                diff_chunks.append(f"diff --git a/{old_path} b/{new_path}\n{d.get('diff', '')}")
            return "\n\n".join(diff_chunks)

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
        """Post top-level review note and inline comments to GitLab MR."""
        project_str = f"{owner}/{repo}"
        enc = self._encode_project(project_str)
        url = f"{self.base_url}/projects/{enc}/merge_requests/{pull_number}/notes"

        # Format complete markdown note including verdict and findings
        full_note = f"## 🤖 AI Code Review Verdict: **{event}**\n\n{body}"
        if comments:
            full_note += "\n\n### 📝 Line-Level Findings\n"
            for c in comments:
                full_note += f"\n- **`{c.path}:L{c.line}`** ({c.severity}): {c.comment_body}\n"
                if c.suggestion_code:
                    full_note += f"```suggestion\n{c.suggestion_code.strip()}\n```\n"

        with httpx.Client(headers=self._get_headers(), timeout=30.0) as client:
            resp = client.post(url, json={"body": full_note})
            if resp.status_code not in (200, 201):
                raise RuntimeError(f"Failed to post GitLab review note: [{resp.status_code}] {resp.text}")
            return resp.json()

    def post_issue_comment(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        body: str
    ) -> Dict[str, Any]:
        """Post comment to GitLab MR or Issue discussion thread."""
        project_str = f"{owner}/{repo}"
        enc = self._encode_project(project_str)
        url = f"{self.base_url}/projects/{enc}/merge_requests/{issue_number}/notes"

        with httpx.Client(headers=self._get_headers(), timeout=30.0) as client:
            resp = client.post(url, json={"body": body})
            if resp.status_code not in (200, 201):
                # Fallback to issue endpoint if not an MR
                issue_url = f"{self.base_url}/projects/{enc}/issues/{issue_number}/notes"
                resp = client.post(issue_url, json={"body": body})
                if resp.status_code not in (200, 201):
                    raise RuntimeError(f"Failed to post GitLab comment: [{resp.status_code}] {resp.text}")
            return resp.json()

    def update_pull_request_description(
        self,
        owner: str,
        repo: str,
        pull_number: int,
        body: str,
        title: Optional[str] = None
    ) -> Dict[str, Any]:
        project_str = f"{owner}/{repo}"
        enc = self._encode_project(project_str)
        url = f"{self.base_url}/projects/{enc}/merge_requests/{pull_number}"
        payload: Dict[str, Any] = {"description": body}
        if title:
            payload["title"] = title

        with httpx.Client(headers=self._get_headers(), timeout=30.0) as client:
            resp = client.put(url, json=payload)
            if resp.status_code != 200:
                raise RuntimeError(f"Failed to update GitLab MR: [{resp.status_code}] {resp.text}")
            return resp.json()

    def fetch_issue(
        self,
        owner: str,
        repo: str,
        issue_number: int
    ) -> Dict[str, Any]:
        project_str = f"{owner}/{repo}"
        enc = self._encode_project(project_str)
        url = f"{self.base_url}/projects/{enc}/issues/{issue_number}"

        with httpx.Client(headers=self._get_headers(), timeout=30.0) as client:
            resp = client.get(url)
            if resp.status_code != 200:
                raise RuntimeError(f"Failed to fetch GitLab issue: [{resp.status_code}] {resp.text}")
            data = resp.json()
            return {
                "title": data.get("title", ""),
                "body": data.get("description") or "",
                "state": data.get("state", "opened"),
                "html_url": data.get("web_url", "")
            }

    def list_pull_request_review_comments(
        self,
        owner: str,
        repo: str,
        pull_number: int,
        max_pages: int = 10,
        per_page: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        List MR discussion notes, normalized to the learning loop's shape
        ({'body', 'user': {'login'}}). System notes are skipped. Pagination is bounded
        by `max_pages` so a very active MR cannot make this unbounded.
        """
        enc = self._encode_project(f"{owner}/{repo}")
        comments: List[Dict[str, Any]] = []
        with httpx.Client(headers=self._get_headers(), timeout=30.0) as client:
            for page in range(1, max_pages + 1):
                url = f"{self.base_url}/projects/{enc}/merge_requests/{pull_number}/notes"
                resp = client.get(url, params={"per_page": per_page, "page": page})
                if resp.status_code != 200:
                    break
                notes = resp.json()
                if not notes:
                    break
                for n in notes:
                    if n.get("system"):
                        continue
                    comments.append({
                        "body": n.get("body", ""),
                        "user": {"login": (n.get("author") or {}).get("username", "")},
                    })
                if len(notes) < per_page:
                    break
        return comments
