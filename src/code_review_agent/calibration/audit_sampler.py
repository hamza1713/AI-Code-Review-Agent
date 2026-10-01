"""
Stratified Audit Sampler for REPRODUCED Findings.
Selects stratified observed samples of findings from real PRs
stratified across Category x Severity dimensions for human verification.
"""

import math
import random
import time
from typing import Any

from code_review_agent.calibration.outcome_store import (
    AUDIT_STATUS_UNAUDITED,
    REPRODUCED,
    FindingOutcomeStore,
    normalize_category,
)


class AuditSampler:
    """
    Selects stratified random samples of findings across Category x Severity strata.
    Guarantees coverage across rare/critical strata while maintaining representative proportional balance.
    """

    def __init__(self, store: FindingOutcomeStore | None = None):
        self.store = store or FindingOutcomeStore()

    def sample_for_audit(
        self,
        repo_id: str,
        sample_size: int = 20,
        days: int = 30,
        window_days: int | None = None,
        unaudited_only: bool = True,
        evidence_grade: str = REPRODUCED,
        status_filter: str | None = None,
        seed: int | None = None,
        start_time: float | None = None,
        end_time: float | None = None,
        provider: str = "github",
        host: str = "github.com",
    ) -> list[dict[str, Any]]:
        """
        Extract an stratified observed sample of findings for human audit.
        Returns a list of sampled finding dictionaries.
        """
        result = self.sample_for_audit_full(
            repo_id=repo_id,
            sample_size=sample_size,
            days=days,
            window_days=window_days,
            unaudited_only=unaudited_only,
            evidence_grade=evidence_grade,
            status_filter=status_filter,
            seed=seed,
            start_time=start_time,
            end_time=end_time,
            provider=provider,
            host=host,
        )
        return result.get("sample", [])

    def sample_for_audit_full(
        self,
        repo_id: str,
        sample_size: int = 20,
        days: int = 30,
        window_days: int | None = None,
        unaudited_only: bool = True,
        evidence_grade: str = REPRODUCED,
        status_filter: str | None = None,
        seed: int | None = None,
        start_time: float | None = None,
        end_time: float | None = None,
        provider: str = "github",
        host: str = "github.com",
    ) -> dict[str, Any]:
        """
        Extract a stratified sample of findings for human audit with full metadata.

        Args:
            repo_id: Target repository identifier (e.g. 'owner/repo')
            sample_size: Target number of findings to sample (default 20)
            days: Lookback window in days (default 30)
            window_days: Alias for days
            unaudited_only: Sample only unaudited findings (default True)
            evidence_grade: Finding evidence grade (defaults to 'REPRODUCED')
            status_filter: Explicit audit status filter (overrides unaudited_only if provided)
            seed: Optional integer seed for deterministic, reproducible sampling
            start_time: Explicit start timestamp
            end_time: Explicit end timestamp

        Returns:
            Dict containing metadata, strata counts, and the sampled findings list.
        """
        clean_repo = (repo_id or "").strip() or "default"
        now = time.time()
        eff_days = window_days if window_days is not None else days
        eff_end = end_time if end_time is not None else now
        eff_start = (
            start_time
            if start_time is not None
            else (eff_end - (max(1, eff_days) * 86400))
        )
        target_n = max(1, sample_size)

        if status_filter is not None:
            eff_status = status_filter
        elif unaudited_only:
            eff_status = AUDIT_STATUS_UNAUDITED
        else:
            eff_status = None

        # 1. Fetch candidate findings from store
        candidates = self.store.list_findings(
            repo_id=clean_repo,
            start_time=eff_start,
            end_time=eff_end,
            evidence_grade=evidence_grade,
            audit_status=eff_status,
            provider=provider,
            host=host,
        )

        total_eligible = len(candidates)
        if total_eligible == 0:
            return {
                "repo_id": clean_repo,
                "window_days": eff_days,
                "total_eligible": 0,
                "sample_size": 0,
                "strata_summary": {},
                "sample": [],
            }

        # If eligible pool is smaller or equal to target, return all
        if total_eligible <= target_n:
            return {
                "repo_id": clean_repo,
                "window_days": eff_days,
                "total_eligible": total_eligible,
                "sample_size": total_eligible,
                "strata_summary": self._build_strata_summary(candidates, candidates),
                "sample": candidates,
            }

        # 2. Partition candidates into strata: (category, severity)
        strata: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in candidates:
            cat = normalize_category(item.get("category"))
            sev = (item.get("severity") or "MEDIUM").upper()
            key = (cat, sev)
            if key not in strata:
                strata[key] = []
            strata[key].append(item)

        # 3. Calculate stratified quotas
        # Rule: Every non-empty stratum receives at least 1 slot if sample_size >= number of strata.
        # Remaining slots are distributed proportionally according to stratum size.
        num_strata = len(strata)
        quotas: dict[tuple[str, str], int] = {}
        rng = random.Random(seed)

        if target_n >= num_strata:
            # Base allocation: 1 per stratum
            remaining = target_n - num_strata
            for key in strata:
                quotas[key] = 1

            # Proportional distribution for remaining slots
            weights = {key: len(items) - 1 for key, items in strata.items()}
            weight_sum = sum(weights.values())

            initial_remaining = remaining
            if weight_sum > 0:
                for key, w in weights.items():
                    add_n = int(math.floor(initial_remaining * (w / weight_sum)))
                    # Cap by available items in stratum
                    max_additional = len(strata[key]) - quotas[key]
                    assigned = min(add_n, max_additional)
                    quotas[key] += assigned
                    remaining -= assigned

            # Distribute any fractional remainder to largest under-allocated strata
            if remaining > 0:
                sorted_strata = sorted(
                    strata.keys(),
                    key=lambda k: len(strata[k]) - quotas[k],
                    reverse=True,
                )
                for key in sorted_strata:
                    if remaining <= 0:
                        break
                    if quotas[key] < len(strata[key]):
                        quotas[key] += 1
                        remaining -= 1
        else:
            # Target N is less than number of strata: allocate 1 to largest N strata
            sorted_strata = sorted(
                strata.keys(), key=lambda k: len(strata[k]), reverse=True
            )
            for key in sorted_strata[:target_n]:
                quotas[key] = 1
            for key in sorted_strata[target_n:]:
                quotas[key] = 0

        # 4. Sample items within each stratum without replacement
        sampled: list[dict[str, Any]] = []
        for key, quota in quotas.items():
            pool = list(strata[key])
            if quota >= len(pool):
                sampled.extend(pool)
            elif quota > 0:
                sampled.extend(rng.sample(pool, quota))

        # Sort sampled findings deterministically by created_at DESC
        sampled.sort(key=lambda x: x.get("created_at", 0), reverse=True)

        return {
            "repo_id": clean_repo,
            "window_days": eff_days,
            "total_eligible": total_eligible,
            "sample_size": len(sampled),
            "strata_summary": self._build_strata_summary(candidates, sampled),
            "sample": sampled,
        }

    @staticmethod
    def _build_strata_summary(
        candidates: list[dict[str, Any]],
        sampled: list[dict[str, Any]],
    ) -> dict[str, dict[str, int]]:
        """Compute candidate vs sampled counts per stratum."""
        summary: dict[str, dict[str, int]] = {}
        for c in candidates:
            cat = normalize_category(c.get("category"))
            sev = (c.get("severity") or "MEDIUM").upper()
            key = f"{cat}:{sev}"
            if key not in summary:
                summary[key] = {"eligible": 0, "sampled": 0}
            summary[key]["eligible"] += 1

        for s in sampled:
            cat = normalize_category(s.get("category"))
            sev = (s.get("severity") or "MEDIUM").upper()
            key = f"{cat}:{sev}"
            if key in summary:
                summary[key]["sampled"] += 1

        return summary
