"""
Local Air-Gapped Git Platform Adapter.
Enables running the AI Code Review Agent on local repositories without internet connectivity,
cloud tokens, or hosting platform dependencies.
"""

import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional

from code_review_agent.config import logger
from code_review_agent.models import InlineComment
from code_review_agent.platform.base import GitPlatformClient, PlatformPRIdentifier, PlatformPRMetadata


class LocalGitPlatformClient(GitPlatformClient):
    """
    Platform adapter for local, air-gapped Git repositories.
    Operates directly on the local working tree using git CLI.
    """

    def __init__(self, repo_dir: Optional[str] = None):
        self.repo_dir = Path(repo_dir).resolve() if repo_dir else Path.cwd()

    def _run_git(self, args: List[str]) -> str:
        """Run a git command in the repository directory."""
        cmd = ["git"] + args
        res = subprocess.run(
            cmd,
            cwd=str(self.repo_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace"
        )
        if res.returncode != 0:
            raise RuntimeError(f"Git command failed ({' '.join(cmd)}): {res.stderr.strip()}")
        return res.stdout

    def parse_pr_identifier(self, pr_identifier: str) -> PlatformPRIdentifier:
        """
        Parse local git identifiers e.g. 'local:HEAD~1', 'local://path/to/repo', or directory path.
        """
        clean = pr_identifier.strip()
        repo_name = self.repo_dir.name
        return PlatformPRIdentifier(
            platform="local",
            owner_or_project="local",
            repo_or_slug=repo_name,
            pr_id=1,
            raw_identifier=clean
        )

    def fetch_pull_request_metadata(self, owner: str, repo: str, pull_number: int) -> PlatformPRMetadata:
        """Derive metadata from local git branch and commit log."""
        try:
            head_sha = self._run_git(["rev-parse", "HEAD"]).strip()
            branch = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"]).strip()
            subject = self._run_git(["log", "-1", "--format=%s"]).strip()
            body = self._run_git(["log", "-1", "--format=%b"]).strip()
            author = self._run_git(["log", "-1", "--format=%an"]).strip()
        except Exception as e:
            logger.warning(f"Could not extract full git metadata: {e}")
            head_sha, branch, subject, body, author = "local", "HEAD", "Local Changes", "", "Developer"

        return PlatformPRMetadata(
            title=subject or "Local Changes",
            body=body or "Review of local uncommitted or staged changes.",
            author=author,
            head_sha=head_sha,
            base_sha="HEAD~1",
            head_ref=branch,
            base_ref="main",
            state="open",
            html_url=str(self.repo_dir)
        )

    def fetch_pull_request_diff(self, owner: str, repo: str, pull_number: int) -> str:
        """
        Extract diff from local repository.
        Attempts unstaged + staged diff first; if empty, uses HEAD~1 diff.
        """
        try:
            # 1. Check working directory changes (staged + unstaged)
            diff = self._run_git(["diff", "HEAD"])
            if diff.strip():
                return diff

            # 2. Check staged changes
            diff = self._run_git(["diff", "--cached"])
            if diff.strip():
                return diff

            # 3. Check latest commit against parent
            diff = self._run_git(["diff", "HEAD~1", "HEAD"])
            if diff.strip():
                return diff
        except Exception as e:
            logger.warning(f"Local git diff extraction notice: {e}")

        return ""

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
        """Save review report locally to REVIEW.md in the repository root."""
        out_file = self.repo_dir / "REVIEW.md"
        content = f"# 🤖 Local Code Review Verdict: **{event}**\n\n{body}\n"
        if comments:
            content += "\n## 📝 Line-Level Comments\n\n"
            for c in comments:
                content += f"### `{c.path}:L{c.line}` ({c.severity})\n{c.comment_body}\n"
                if c.suggestion_code:
                    content += f"```suggestion\n{c.suggestion_code.strip()}\n```\n"

        out_file.write_text(content, encoding="utf-8")
        logger.info(f"📄 Local review report saved to {out_file}")
        return {"status": "saved", "path": str(out_file), "verdict": event}

    def post_issue_comment(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        body: str
    ) -> Dict[str, Any]:
        """Append comment to REVIEW_NOTES.md in the local repository."""
        out_file = self.repo_dir / "REVIEW_NOTES.md"
        with open(out_file, "a", encoding="utf-8") as f:
            f.write(f"\n---\n{body}\n")
        return {"status": "appended", "path": str(out_file)}

    def update_pull_request_description(
        self,
        owner: str,
        repo: str,
        pull_number: int,
        body: str,
        title: Optional[str] = None
    ) -> Dict[str, Any]:
        out_file = self.repo_dir / "PR_DESCRIPTION.md"
        content = f"# {title or 'PR Description'}\n\n{body}\n"
        out_file.write_text(content, encoding="utf-8")
        return {"status": "updated", "path": str(out_file)}

    def fetch_issue(
        self,
        owner: str,
        repo: str,
        issue_number: int
    ) -> Dict[str, Any]:
        """Look for local issue description in .issues/{issue_number}.md if present."""
        issue_file = self.repo_dir / ".issues" / f"{issue_number}.md"
        if issue_file.exists():
            text = issue_file.read_text(encoding="utf-8")
            return {"title": f"Local Issue #{issue_number}", "body": text, "state": "open"}
        return {"title": f"Issue #{issue_number}", "body": "", "state": "open"}
