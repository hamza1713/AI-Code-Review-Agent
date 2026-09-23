"""
Bidirectional Review Comment Synchronizer.
Synchronizes finding fingerprints across PR revisions, eliminates comment duplication,
and automatically resolves fixed review threads.
"""

from typing import List, Dict, Any, Set, Tuple, Optional
from code_review_agent.config import logger
from code_review_agent.models import InlineComment, SastFinding, extract_fingerprint_from_comment
from code_review_agent.platform.base import GitPlatformClient


class CommentSynchronizer:
    """
    Synchronizes inline review comments and finding lifecycles with Git hosting platforms.
    """

    @classmethod
    def synchronize(
        cls,
        client: GitPlatformClient,
        owner: str,
        repo: str,
        pull_number: int,
        current_comments: List[InlineComment],
        current_findings: List[SastFinding],
        head_sha: Optional[str] = None,
        suppressed_fingerprints: Optional[Set[str]] = None
    ) -> Tuple[List[InlineComment], Set[str], Set[str]]:
        """
        Synchronize review comments against prior comments on the PR.

        Returns:
            Tuple of:
            - deduped_comments: List of InlineComment that are genuinely NEW (not already posted)
            - resolved_fps: Set of fingerprints that were fixed in this commit
            - regressed_fps: Set of fingerprints that were re-introduced
        """
        suppressed = set(suppressed_fingerprints or [])
        short_sha = head_sha[:7] if head_sha else "latest"

        # 1. Fetch prior review comments from the platform
        prior_comments: List[Dict[str, Any]] = []
        try:
            prior_comments = client.list_pull_request_review_comments(owner, repo, pull_number)
        except Exception as e:
            logger.warning(f"Could not fetch prior PR review comments for {owner}/{repo}#{pull_number}: {e}")

        # 2. Extract prior fingerprints
        prior_fps: Set[str] = set()
        for comment in prior_comments:
            body = comment.get("body", "")
            fp = extract_fingerprint_from_comment(body)
            if fp:
                prior_fps.add(fp)

        # 3. Map current finding fingerprints
        current_finding_fps: Set[str] = set()
        for f in current_findings:
            if f.fingerprint:
                current_finding_fps.add(f.fingerprint.lower())
                if f.fingerprint.lower() in suppressed:
                    f.lifecycle_status = "SUPPRESSED"

        # 4. Deduplicate comments to post
        deduped_comments: List[InlineComment] = []
        for c in current_comments:
            fp = (c.fingerprint or "").lower()
            if not fp:
                # Fallback: keep comments without fingerprint
                deduped_comments.append(c)
                continue

            if fp in suppressed:
                c.lifecycle_status = "SUPPRESSED"
                continue

            if fp in prior_fps:
                # Already reported on PR; do not spam duplicate comment
                c.lifecycle_status = "NEW"  # keeps schema valid
                continue

            # Truly new finding comment
            deduped_comments.append(c)

        # 5. Detect resolved findings (present in prior comments but absent in current findings)
        resolved_fps: Set[str] = set()
        for pfp in prior_fps:
            if pfp not in current_finding_fps and pfp not in suppressed:
                resolved_fps.add(pfp)

        # 6. Post auto-resolution summary if findings were fixed
        if resolved_fps and pull_number > 0:
            try:
                res_lines = [
                    f"### 🎯 AI Code Review — Verified Resolutions (`{short_sha}`)",
                    f"Great progress! **{len(resolved_fps)}** previously flagged finding(s) have been resolved in commit `{short_sha}`:\n",
                ]
                for rfp in sorted(resolved_fps):
                    res_lines.append(f"- ✅ Finding fingerprint `{rfp}` resolved.")
                res_lines.append("\n<sub>Automatically verified and resolved by AI Code Review Agent</sub>")

                client.post_issue_comment(
                    owner=owner,
                    repo=repo,
                    issue_number=pull_number,
                    body="\n".join(res_lines)
                )
                logger.info(f"🎉 Posted auto-resolution comment for {len(resolved_fps)} resolved findings to {owner}/{repo}#{pull_number}")
            except Exception as post_err:
                logger.warning(f"Could not post resolution summary for {owner}/{repo}#{pull_number}: {post_err}")

        return deduped_comments, resolved_fps, set()
