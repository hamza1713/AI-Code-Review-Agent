"""
Suggestion Tracker for PR Merges and Accepted Review Comments.

Learns team conventions from suggestions that were *actually accepted* — not merely
present on a PR that happened to merge. On merge it fetches the PR's inline review
comments and verifies that each ```suggestion block's code actually landed in the
merged diff before promoting it into repository Team Memory. This avoids poisoning
the memory (which is injected as few-shot context into every future review) with
suggestions the team saw but ignored.
"""

import os
import re
from typing import Dict, Any, List, Optional

from code_review_agent.config import logger
from code_review_agent.learning.team_memory import TeamMemoryStore, BestPractice
from code_review_agent.models import InlineComment

_SUGGESTION_BLOCK = re.compile(r"```suggestion\s*\n(.*?)\n```", re.DOTALL)


class SuggestionTracker:
    """Tracks suggestions posted on PRs and learns conventions when PRs are merged."""

    def __init__(self, memory_store: Optional[TeamMemoryStore] = None):
        self.memory_store = memory_store or TeamMemoryStore()

    def process_merged_pr(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        review_comments: Optional[List[Dict[str, Any]]] = None,
        applied_suggestions: Optional[List[InlineComment]] = None,
        final_diff: Optional[str] = None,
        client: Optional[Any] = None,
        platform: str = "github",
    ) -> List[BestPractice]:
        """
        Learn accepted conventions from a merged PR.

        Sources, in order:
          1. `applied_suggestions` (InlineComment): treated as already-known accepted —
             the caller asserts these were applied.
          2. `review_comments` (raw GitHub review-comment dicts): candidates. When the
             tracker itself fetched them (because the caller passed none), each is kept
             only if its suggested code is verified present in the merged diff.

        When no comments are supplied, the tracker fetches the PR's review comments and
        the merged diff from GitHub and verifies acceptance before learning anything.
        """
        repo_id = f"{owner}/{repo}"
        learned: List[BestPractice] = []

        # 1. Known-accepted InlineComment suggestions (caller-asserted).
        if applied_suggestions:
            for s in applied_suggestions:
                if not s.suggestion_code:
                    continue
                title = s.comment_body.split("\n")[0][:60].strip()
                category = "security" if s.severity == "CRITICAL" else "idiom"
                learned.append(self.memory_store.record_accepted_suggestion(
                    repo_id=repo_id,
                    title=title,
                    description=s.why or s.comment_body,
                    good_code=s.suggestion_code,
                    category=category,
                    source_pr=f"{repo_id}#{pr_number}",
                ))

        # 2. Review comments. If the caller supplied them, trust them; if we fetch them
        #    ourselves, verify each suggestion actually landed in the merged diff.
        must_verify = False
        if review_comments is None and not applied_suggestions:
            review_comments, final_diff, must_verify = self._fetch_candidates(
                owner, repo, pr_number, client, final_diff, platform
            )

        if review_comments:
            added_text = self._added_lines_text(final_diff) if final_diff else ""
            # Bot identities across platforms (GitHub login + any extra bot usernames),
            # so acceptance learning attributes only the bot's own suggestions when set.
            bot_ids = {
                u.strip().lower()
                for u in f"{os.getenv('BOT_GITHUB_LOGIN', '')},{os.getenv('BOT_BOT_USERNAMES', '')}".split(",")
                if u.strip()
            }

            for comment in review_comments:
                body = comment.get("body", "")
                match = _SUGGESTION_BLOCK.search(body)
                if not match:
                    continue
                suggested_code = match.group(1)

                # When we fetched candidates, only learn suggestions that were applied,
                # and (if configured) only the bot's own suggestions.
                if must_verify:
                    if bot_ids:
                        author = (comment.get("user", {}) or {}).get("login", "").lower()
                        if author and author not in bot_ids:
                            continue
                    if not self._suggestion_applied(suggested_code, added_text):
                        continue

                first_line = body.split("\n")[0]
                cleaned_title = re.sub(r"^[🚨⚠️💡\*]+\s*(\*\*[A-Z]+\*\*:?)?\s*", "", first_line).strip()[:60]
                if not cleaned_title:
                    cleaned_title = f"Convention from PR #{pr_number}"
                category = "security" if "CRITICAL" in body else "idiom"
                learned.append(self.memory_store.record_accepted_suggestion(
                    repo_id=repo_id,
                    title=cleaned_title,
                    description=cleaned_title,
                    good_code=suggested_code,
                    category=category,
                    source_pr=f"{repo_id}#{pr_number}",
                ))

        logger.info(
            f"🎓 Learned {len(learned)} accepted pattern(s) from merged PR #{pr_number} in {repo_id}"
        )
        return learned

    def _fetch_candidates(self, owner, repo, pr_number, client, final_diff, platform="github"):
        """
        Fetch review comments and the merged diff via the correct platform client for
        acceptance verification. Returns (review_comments, final_diff, must_verify).
        On any failure — or on a platform whose adapter does not implement review-comment
        listing yet (returns []) — nothing unverified is ever learned.
        """
        try:
            if client is None:
                from code_review_agent.platform.factory import get_client_for_platform
                client = get_client_for_platform(platform)
            comments = client.list_pull_request_review_comments(owner, repo, pr_number)
            if final_diff is None:
                try:
                    final_diff = client.fetch_pull_request_diff(owner, repo, pr_number)
                except Exception as e:
                    logger.warning(f"Could not fetch merged diff for acceptance check: {e}")
                    final_diff = ""
            return comments, final_diff, True
        except Exception as e:
            logger.warning(f"Could not fetch review comments for PR #{pr_number}: {e}")
            return [], final_diff or "", True

    @staticmethod
    def _added_lines_text(diff: str) -> str:
        """Concatenate the stripped added ('+') lines of a unified diff for membership tests."""
        return "\n".join(
            line[1:].strip()
            for line in (diff or "").splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )

    @staticmethod
    def _suggestion_applied(suggested_code: str, added_text: str) -> bool:
        """
        True if a majority of the suggestion's meaningful lines appear among the merged
        diff's added lines — evidence the developer actually applied the suggestion.
        Conservative: with no diff to check against, returns False (learn nothing).
        """
        if not added_text:
            return False
        lines = [ln.strip() for ln in suggested_code.splitlines() if len(ln.strip()) > 3]
        if not lines:
            return False
        present = sum(1 for ln in lines if ln in added_text)
        return present >= max(1, (len(lines) + 1) // 2)
