"""
GitHub API Client for Live Pull Request Ingestion and Inline Comment Submission.
Interacts with GitHub REST API v3 using httpx.
"""

import base64
import hashlib
import re
from typing import Any

import httpx

from code_review_agent.config import get_github_token, logger
from code_review_agent.models import InlineComment


class GitHubClient:
    """Client for interacting with GitHub Pull Requests API."""

    _DEFAULT_TOKEN = object()

    def __init__(self, token: Any = _DEFAULT_TOKEN):
        tok: str | None = (
            get_github_token() if token is self._DEFAULT_TOKEN else token
        )
        # Filter dummy/placeholder tokens
        if tok and any(
            p in tok.lower()
            for p in ["your_github", "your_token", "placeholder", "ghp_your"]
        ):
            tok = None
        self.token = tok
        self.base_url = "https://api.github.com"

    def _get_headers(
        self, accept: str = "application/vnd.github.v3+json", include_auth: bool = True
    ) -> dict[str, str]:
        """Build standard GitHub authorization headers."""
        headers = {
            "Accept": accept,
            "User-Agent": "AI-Code-Review-Agent/2.0",
        }
        if self.token and include_auth:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    @staticmethod
    def parse_pr_identifier(pr_identifier: str) -> tuple[str, str, int]:
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

    def fetch_pull_request_metadata(
        self, owner: str, repo: str, pull_number: int
    ) -> dict[str, Any]:
        """Fetch title, author, branch, and commit SHAs for a pull request."""
        url = f"{self.base_url}/repos/{owner}/{repo}/pulls/{pull_number}"
        with httpx.Client(headers=self._get_headers(), timeout=30.0) as client:
            resp = client.get(url)
            # If 401 Unauthorized occurs due to an expired or bad token on a public repo, fallback to unauthenticated
            if resp.status_code == 401 and self.token:
                logger.warning(
                    f"GitHub token rejected with 401 for {url}; retrying unauthenticated for public repository..."
                )
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
                "head_repository": (data.get("head", {}).get("repo") or {}).get(
                    "full_name", ""
                ),
                "base_ref": data.get("base", {}).get("ref", ""),
                "state": data.get("state", ""),
                "html_url": data.get("html_url", ""),
            }

    def fetch_pull_request_diff(self, owner: str, repo: str, pull_number: int) -> str:
        """Fetch raw unified diff for a pull request with multi-layer fallback for public repositories."""
        url = f"{self.base_url}/repos/{owner}/{repo}/pulls/{pull_number}"
        headers = self._get_headers(accept="application/vnd.github.v3.diff")
        with httpx.Client(
            headers=headers, timeout=45.0, follow_redirects=True
        ) as client:
            resp = client.get(url)
            # Fallback 1: If 401 Unauthorized (e.g. invalid/expired token on public repo), retry unauthenticated
            if resp.status_code == 401 and self.token:
                logger.warning(
                    f"GitHub token rejected with 401 for {url}; retrying unauthenticated API request..."
                )
                resp = client.get(
                    url,
                    headers=self._get_headers(
                        accept="application/vnd.github.v3.diff", include_auth=False
                    ),
                )

            if resp.status_code == 200 and resp.text.strip():
                return resp.text

            # Fallback 2: Direct public web diff endpoint (works reliably for all public PRs without API token)
            direct_diff_url = (
                f"https://github.com/{owner}/{repo}/pull/{pull_number}.diff"
            )
            logger.info(
                f"Attempting direct public PR diff fetch from {direct_diff_url}..."
            )
            resp_direct = client.get(
                direct_diff_url,
                headers={"User-Agent": "AI-Code-Review-Agent/2.0"},
                follow_redirects=True,
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
        commit_id: str | None = None,
        comments: list[InlineComment] | None = None,
    ) -> dict[str, Any]:
        """
        Submit a full Pull Request Review with top-level summary and line-level inline comments.
        """
        url = f"{self.base_url}/repos/{owner}/{repo}/pulls/{pull_number}/reviews"

        review_comments = []
        if comments:
            for c in comments:
                review_comments.append(
                    {
                        "path": c.path,
                        "line": c.line,
                        "side": c.side,
                        "body": c.to_github_markdown(),
                    }
                )

        payload: dict[str, Any] = {
            "body": body,
            "event": event,
        }
        if commit_id:
            payload["commit_id"] = commit_id
        if review_comments:
            payload["comments"] = review_comments

        with httpx.Client(headers=self._get_headers(), timeout=45.0) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()

            return resp.json()

    def post_issue_comment(
        self, owner: str, repo: str, issue_number: int, body: str
    ) -> dict[str, Any]:
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
        title: str | None = None,
    ) -> dict[str, Any]:
        """Update the PR description body and optionally title."""
        url = f"{self.base_url}/repos/{owner}/{repo}/pulls/{pull_number}"
        payload: dict[str, Any] = {"body": body}
        if title:
            payload["title"] = title
        with httpx.Client(headers=self._get_headers(), timeout=30.0) as client:
            resp = client.patch(url, json=payload)
            if resp.status_code not in (200, 201):
                raise RuntimeError(
                    f"Failed to update PR description at {url}: [{resp.status_code}] {resp.text}"
                )
            return resp.json()

    def fetch_issue(self, owner: str, repo: str, issue_number: int) -> dict[str, Any]:
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
    ) -> list[dict[str, Any]]:
        """
        List inline review comments on a PR — these are the comments that carry
        ```suggestion blocks. Used by the learning loop to find candidate suggestions
        whose acceptance is then verified against the merged diff.
        """
        return self._paginate(f"/repos/{owner}/{repo}/pulls/{pull_number}/comments")

    def _paginate(self, path):
        """Follow every page; never return partial results as a complete snapshot."""
        from urllib.parse import urlsplit

        url = self.base_url + path
        items = []
        seen = set()
        with httpx.Client(headers=self._get_headers(), timeout=30) as client:
            while url:
                if (
                    url in seen
                    or urlsplit(url).netloc != urlsplit(self.base_url).netloc
                ):
                    raise RuntimeError("Invalid provider pagination link")
                seen.add(url)
                response = client.get(
                    url, params={"per_page": 100} if not items else None
                )
                response.raise_for_status()
                items.extend(response.json())
                url = response.links.get("next", {}).get("url")
        return items

    def list_pull_request_reviews(self, owner, repo, pull_number):
        return self._paginate(f"/repos/{owner}/{repo}/pulls/{pull_number}/reviews")

    def list_issue_comments(self, owner, repo, issue_number):
        return self._paginate(f"/repos/{owner}/{repo}/issues/{issue_number}/comments")

    def get_repository_permission(self, owner, repo, actor):
        from urllib.parse import quote

        with httpx.Client(headers=self._get_headers(), timeout=15) as client:
            response = client.get(
                f"{self.base_url}/repos/{owner}/{repo}/collaborators/{quote(actor, safe='')}/permission"
            )
            response.raise_for_status()
            data = response.json()
            return data.get("role_name") or data.get("permission", "none")

    def get_authenticated_login(self):
        with httpx.Client(headers=self._get_headers(), timeout=15) as client:
            response = client.get(f"{self.base_url}/user")
            response.raise_for_status()
            return response.json()["login"]

    def set_commit_status(
        self,
        owner: str,
        repo: str,
        sha: str,
        state: str,  # 'pending', 'success', 'failure', 'error'
        description: str,
        context: str = "ai-code-review",
        target_url: str | None = None,
    ) -> dict[str, Any]:
        """Update commit status on a specific commit SHA using GitHub Statuses API."""
        url = f"{self.base_url}/repos/{owner}/{repo}/statuses/{sha}"
        payload = {"state": state, "description": description[:140], "context": context}
        if target_url:
            payload["target_url"] = target_url

        with httpx.Client(headers=self._get_headers(), timeout=30.0) as client:
            resp = client.post(url, json=payload)
            if resp.status_code not in (200, 201):
                logger.warning(
                    f"Failed to set commit status on {url}: [{resp.status_code}] {resp.text}"
                )
                return {"error": resp.text, "status_code": resp.status_code}
            return resp.json()

    def create_or_update_check_run(
        self,
        owner: str,
        repo: str,
        head_sha: str,
        name: str = "AI Code Review",
        status: str = "queued",  # 'queued', 'in_progress', 'completed'
        conclusion: str
        | None = None,  # 'success', 'failure', 'neutral', 'action_required'
        title: str | None = None,
        summary: str | None = None,
        annotations: list[dict[str, Any]] | None = None,
        details_url: str | None = None,
        check_run_id: int | None = None,
    ) -> dict[str, Any]:
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

        payload: dict[str, Any] = {
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
                "annotations": (annotations or [])[:50],
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
                    state = (
                        "pending"
                        if status != "completed"
                        else (
                            "success"
                            if conclusion in ("success", "neutral")
                            else "failure"
                        )
                    )
                    return self.set_commit_status(
                        owner=owner,
                        repo=repo,
                        sha=head_sha,
                        state=state,
                        description=(summary or title or name)[:140],
                        context=name.lower().replace(" ", "-"),
                        target_url=details_url,
                    )
                return {"error": resp.text, "status_code": resp.status_code}
            except Exception as e:
                logger.warning(
                    f"Error calling check run API: {e}; falling back to commit status..."
                )
                state = (
                    "pending"
                    if status != "completed"
                    else (
                        "success" if conclusion in ("success", "neutral") else "failure"
                    )
                )
                return self.set_commit_status(
                    owner=owner,
                    repo=repo,
                    sha=head_sha,
                    state=state,
                    description=(summary or title or name)[:140],
                    context=name.lower().replace(" ", "-"),
                    target_url=details_url,
                )

    def fetch_source_file(self, owner: str, repo: str, path: str, ref: str) -> bytes:
        """Verify regular-file mode and blob bytes against the exact commit tree."""
        import hashlib
        import json
        import os
        import re
        import time
        from urllib.parse import quote

        from code_review_agent.path_validation import validate_diff_path

        if not re.fullmatch(r"[0-9a-f]{40}", ref):
            raise ValueError("An immutable source commit is required")
        if any(
            not re.fullmatch(r"[A-Za-z0-9_.-]+", value) or value in {".", ".."}
            for value in (owner, repo)
        ):
            raise ValueError("Invalid source repository")
        path = validate_diff_path(path)
        remaining = float(os.getenv("REVIEW_WORKER_DEADLINE", "inf")) - time.time()
        if remaining <= 0:
            raise TimeoutError("Source acquisition deadline expired")
        with httpx.Client(
            headers=self._get_headers(),
            timeout=min(20, remaining),
            follow_redirects=False,
        ) as client:

            def get(endpoint, params=None):
                remaining = (
                    float(os.getenv("REVIEW_WORKER_DEADLINE", "inf")) - time.time()
                )
                if remaining <= 0:
                    raise TimeoutError("Source acquisition deadline expired")
                client.timeout = httpx.Timeout(min(20, remaining))
                with client.stream(
                    "GET", self.base_url + endpoint, params=params
                ) as response:
                    if response.status_code != 200:
                        raise RuntimeError(
                            f"Source acquisition returned HTTP {response.status_code}"
                        )
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data) > 4 * 1024 * 1024:
                            raise OverflowError("Source metadata exceeds limit")
                        if time.time() >= float(
                            os.getenv("REVIEW_WORKER_DEADLINE", "inf")
                        ):
                            raise TimeoutError("Source acquisition deadline expired")
                    return json.loads(data)

            prefix = f"/repos/{owner}/{repo}"
            cache = getattr(self, "_source_trees", {})
            identity = (owner, repo, ref)
            if identity not in cache:
                commit = get(prefix + "/git/commits/" + ref)
                if commit.get("sha") != ref:
                    raise ValueError("Provider commit identity mismatch")
                tree = get(
                    prefix + "/git/trees/" + commit["tree"]["sha"], {"recursive": "1"}
                )
                if (
                    tree.get("sha") != commit["tree"]["sha"]
                    or tree.get("truncated")
                    or not isinstance(tree.get("tree"), list)
                ):
                    raise ValueError("Source tree coverage unavailable")
                if len({entry["path"] for entry in tree["tree"]}) != len(tree["tree"]):
                    raise ValueError("Source tree contains duplicate paths")
                cache[identity] = {entry["path"]: entry for entry in tree["tree"]}
                self._source_trees = cache
            entry = cache[identity].get(path)
            if (
                not entry
                or entry.get("type") != "blob"
                or entry.get("mode") not in {"100644", "100755"}
            ):
                raise ValueError("Source is unavailable or not a regular file")
            if entry.get("size", 0) > 2 * 1024 * 1024:
                raise OverflowError("Source byte limit")
            data = get(prefix + "/contents/" + quote(path, safe="/"), {"ref": ref})
            if (
                not isinstance(data, dict)
                or data.get("type") != "file"
                or data.get("encoding") != "base64"
                or data.get("size", 0) > 2 * 1024 * 1024
            ):
                raise ValueError(
                    "Source is unavailable, oversized, or not a regular file"
                )
            raw = base64.b64decode("".join(data["content"].split()), validate=True)
            if len(raw) > 2 * 1024 * 1024:
                raise OverflowError("Source byte limit")
            blob = hashlib.sha1(
                b"blob " + str(len(raw)).encode() + b"\0" + raw
            ).hexdigest()
            if (
                len(raw) != data.get("size")
                or blob != data.get("sha")
                or blob != entry.get("sha")
            ):
                raise ValueError("Provider source blob hash mismatch")
            return raw

    def fetch_file_content(
        self, owner: str, repo: str, path: str, ref: str | None = None
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
                    return base64.b64decode(data["content"]).decode(
                        "utf-8", errors="replace"
                    )
                return data.get("content", "")
            return ""

    def commit_file_change(
        self,
        owner: str,
        repo: str,
        branch: str,
        path: str,
        content: str,
        commit_message: str,
    ) -> dict[str, Any]:
        """
        Commit an updated file directly to a branch on GitHub.
        Retrieves current file SHA (if existing), encodes content, and PUTs to GitHub Contents API.
        """
        if not self.token:
            simulated_sha = (
                "simulated-" + hashlib.sha256(content.encode("utf-8")).hexdigest()[:10]
            )
            return {
                "sha": simulated_sha,
                "html_url": f"https://github.com/{owner}/{repo}/commit/{simulated_sha}",
                "branch": branch,
                "path": path,
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
            payload: dict[str, Any] = {
                "message": commit_message,
                "content": encoded_content,
                "branch": branch,
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
                    "path": path,
                }
            if put_resp.status_code in (401, 403):
                raise PermissionError(
                    f"GitHub token lacks write permissions on {owner}/{repo}:{branch}. "
                    f"Status [{put_resp.status_code}]: {put_resp.text}"
                )
            raise RuntimeError(
                f"Failed to commit file change: [{put_resp.status_code}] {put_resp.text}"
            )
