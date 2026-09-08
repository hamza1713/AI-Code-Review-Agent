"""
Team Memory Store and Best Practices Wiki.
Persists accepted code conventions, idioms, and review lessons learned per repository.
Feeds them back dynamically as few-shot demonstrations to SeniorDeveloper.
"""

import json
import os
import time
from pathlib import Path
from typing import List, Optional
from pydantic import BaseModel, Field

from code_review_agent.config import logger


class BestPractice(BaseModel):
    """Represents a learned team coding standard, convention, or accepted pattern."""
    id: str = Field(..., description="Unique slug for the practice, e.g., 'use-httpx-over-requests'")
    category: str = Field(default="idiom", description="'idiom', 'security', 'style', 'architecture', 'testing'")
    title: str = Field(..., description="Human-readable title")
    description: str = Field(..., description="Explanation of why this is preferred by the team")
    bad_pattern: Optional[str] = Field(default=None, description="Disapproved snippet or anti-pattern")
    good_pattern: Optional[str] = Field(default=None, description="Approved or idiomatic snippet")
    times_accepted: int = Field(default=1, description="Number of times this pattern was accepted/reinforced")
    source_pr: Optional[str] = Field(default=None, description="PR identifier or URL where this was first learned")
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class TeamMemoryStore:
    """
    Manages persistent repository-scoped memory for coding standards and accepted suggestions.
    Stores records in .cache/team_memory/{safe_repo_id}.json.
    """

    def __init__(self, cache_dir: Optional[str] = None):
        if cache_dir:
            self.cache_dir = Path(cache_dir)
        else:
            self.cache_dir = Path(os.environ.get("TEAM_MEMORY_CACHE_DIR", ".cache/team_memory"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _safe_filename(self, repo_id: str) -> Path:
        """Convert 'owner/repo' into a safe filesystem filename."""
        safe_name = repo_id.strip().replace("/", "_").replace("\\", "_").replace(":", "_")
        return self.cache_dir / f"{safe_name}.json"

    def get_practices(self, repo_id: str, category: Optional[str] = None) -> List[BestPractice]:
        """Retrieve all active practices learned for a repository."""
        file_path = self._safe_filename(repo_id)
        if not file_path.exists():
            return []

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            practices = [BestPractice(**p) for p in data.get("practices", [])]
            if category:
                practices = [p for p in practices if p.category.lower() == category.lower()]
            return sorted(practices, key=lambda p: p.times_accepted, reverse=True)
        except Exception as e:
            logger.warning(f"Failed to read team memory for {repo_id}: {e}")
            return []

    def record_practice(self, repo_id: str, practice: BestPractice) -> BestPractice:
        """Store or update a learned best practice."""
        existing = {p.id: p for p in self.get_practices(repo_id)}

        if practice.id in existing:
            # Reinforce existing practice
            current = existing[practice.id]
            current.times_accepted += 1
            current.updated_at = time.time()
            if practice.good_pattern:
                current.good_pattern = practice.good_pattern
            if practice.bad_pattern:
                current.bad_pattern = practice.bad_pattern
            if practice.description:
                current.description = practice.description
            practice = current
        else:
            practice.updated_at = time.time()
            existing[practice.id] = practice

        self._save(repo_id, list(existing.values()))
        logger.info(f"🧠 TeamMemory updated for {repo_id}: '{practice.title}' (accepted {practice.times_accepted}x)")
        return practice

    def record_accepted_suggestion(
        self,
        repo_id: str,
        title: str,
        description: str,
        good_code: str,
        bad_code: Optional[str] = None,
        category: str = "idiom",
        source_pr: Optional[str] = None
    ) -> BestPractice:
        """Create or reinforce a practice from an accepted review suggestion."""
        # Slugify title
        slug = "-".join(title.lower().replace("_", " ").split()[:6])
        slug = "".join(c for c in slug if c.isalnum() or c == "-") or "learned-convention"

        practice = BestPractice(
            id=slug,
            category=category,
            title=title,
            description=description,
            good_pattern=good_code.strip() if good_code else None,
            bad_pattern=bad_code.strip() if bad_code else None,
            source_pr=source_pr,
            times_accepted=1
        )
        return self.record_practice(repo_id, practice)

    def format_team_memory_prompt(self, repo_id: str, max_items: int = 5) -> str:
        """
        Render learned best practices into prompt-ready markdown for injection into agent context.
        """
        practices = self.get_practices(repo_id)[:max_items]
        if not practices:
            return "No repository-specific team conventions recorded yet. Rely on standard language idioms."

        lines = [
            "### 🧠 Learned Team Conventions & Best Practices for this Repository",
            "The development team has previously accepted and enforced the following coding standards in this repo. "
            "Prioritize these conventions during your review:\n"
        ]

        for i, p in enumerate(practices, start=1):
            lines.append(f"**{i}. {p.title}** (`{p.category}`, accepted {p.times_accepted}x)")
            lines.append(f"   - **Rule**: {p.description}")
            if p.bad_pattern:
                lines.append("   - **Avoid**:")
                lines.append(f"     ```\n     {p.bad_pattern.strip()}\n     ```")
            if p.good_pattern:
                lines.append("   - **Preferred Idiom**:")
                lines.append(f"     ```\n     {p.good_pattern.strip()}\n     ```")
            lines.append("")

        return "\n".join(lines).strip()

    def clear_memory(self, repo_id: str) -> bool:
        """Clear memory cache for a repository."""
        file_path = self._safe_filename(repo_id)
        if file_path.exists():
            file_path.unlink()
            return True
        return False

    def _save(self, repo_id: str, practices: List[BestPractice]):
        """Persist practices list to JSON file."""
        file_path = self._safe_filename(repo_id)
        payload = {
            "repo_id": repo_id,
            "updated_at": time.time(),
            "count": len(practices),
            "practices": [p.model_dump() for p in practices]
        }
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
