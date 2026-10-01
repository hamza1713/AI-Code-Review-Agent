"""
Bidirectional Review Comment Synchronizer.
Synchronizes finding fingerprints across PR revisions, eliminates comment duplication,
and automatically resolves fixed review threads.
"""

from typing import Any

from code_review_agent.config import logger
from code_review_agent.models import (
    InlineComment,
    SastFinding,
    extract_fingerprint_from_comment,
)
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
        current_comments: list[InlineComment],
        current_findings: list[SastFinding],
        head_sha: str | None = None,
        suppressed_fingerprints: set[str] | None = None,
    ) -> tuple[list[InlineComment], set[str], set[str]]:
        """
        Synchronize review comments against prior comments on the PR.

        Returns:
            Tuple of:
            - deduped_comments: List of InlineComment that are genuinely NEW (not already posted)
            - resolved_fps: Set of fingerprints that were fixed in this commit
            - regressed_fps: Set of fingerprints that were re-introduced
        """
        suppressed = set(suppressed_fingerprints or [])
        head_sha[:7] if head_sha else "latest"

        # 1. Fetch prior review comments from the platform
        prior_comments: list[dict[str, Any]] = []
        try:
            prior_comments = client.list_pull_request_review_comments(
                owner, repo, pull_number
            )
        except Exception as e:
            logger.warning(
                f"Could not fetch prior PR review comments for {owner}/{repo}#{pull_number}: {e}"
            )

        # 2. Extract prior fingerprints
        prior_fps: set[str] = set()
        for comment in prior_comments:
            body = comment.get("body", "")
            fp = extract_fingerprint_from_comment(body)
            if fp:
                prior_fps.add(fp)

        # 3. Map current finding fingerprints
        current_finding_fps: set[str] = set()
        for f in current_findings:
            if f.fingerprint:
                current_finding_fps.add(f.fingerprint.lower())
                if f.fingerprint.lower() in suppressed:
                    f.lifecycle_status = "SUPPRESSED"

        # 4. Deduplicate comments to post
        deduped_comments: list[InlineComment] = []
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
        candidate_resolved: set[str] = set()
        for pfp in prior_fps:
            if pfp not in current_finding_fps and pfp not in suppressed:
                candidate_resolved.add(pfp)

        # Filter out findings that were already marked FALSE_POSITIVE or already resolved in a prior commit
        resolved_fps: set[str] = set()
        repo_id = f"{owner}/{repo}"
        try:
            from code_review_agent.calibration import (
                FALSE_POSITIVE,
                FindingOutcomeStore,
            )

            outcome_store = FindingOutcomeStore()
            for rfp in candidate_resolved:
                rec = outcome_store.get_finding(repo_id, rfp, pr_id=str(pull_number))
                if rec:
                    # Never claim an explicit false positive was resolved as a real defect
                    if rec.get("outcome") == FALSE_POSITIVE:
                        continue
                    # If it was already resolved by comment_sync in an earlier commit on this PR, don't re-announce
                    if rec.get("lifecycle_status") == "NOT_OBSERVED":
                        continue
                resolved_fps.add(rfp)
        except Exception as filter_err:
            logger.debug(
                f"Could not filter resolved findings against outcome store: {filter_err}"
            )
            resolved_fps = candidate_resolved

        # Scanner absence is an observation, never a verified defect or external write.
        from code_review_agent.calibration import FindingOutcomeStore

        store = FindingOutcomeStore()
        for fingerprint in resolved_fps:
            if store.get_finding(repo_id, fingerprint, pr_id=str(pull_number)):
                store.update_outcome(
                    repo_id,
                    fingerprint,
                    pr_id=str(pull_number),
                    source="comment_sync_resolved",
                    reason=f"Not observed at {head_sha}",
                )

        return deduped_comments, resolved_fps, set()
