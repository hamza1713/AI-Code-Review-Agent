"""
Repository Calibration Engine & Cried-Wolf Telemetry.
Aggregates historical review finding outcomes per repository,
calculating False-Positive Rates (FPR = FP / (TP + FP)) and Cried-Wolf indices
broken down by category and evidence grade (REPRODUCED vs HEURISTIC).
Generates interactive Markdown reports and honest review trust footers.
"""

import json
import time
from typing import Any

from pydantic import BaseModel, Field

from code_review_agent.calibration.outcome_store import (
    AUDIT_REASON_CORRECT,
    AUDIT_STATUS_INVALID,
    AUDIT_STATUS_VALID,
    CATEGORY_ARCHITECTURE,
    CATEGORY_GOVERNANCE,
    CATEGORY_QUALITY,
    CATEGORY_SECURITY,
    CONFIRMED_REAL,
    FALSE_POSITIVE,
    HEURISTIC,
    IGNORED_MERGED,
    INVALID_REPRODUCTION,
    PENDING,
    REPRODUCED,
    SOURCE_HUMAN_EXPLICIT,
    VALID_CATEGORIES,
    FindingOutcomeStore,
)


class MetricSlice(BaseModel):
    """Aggregate statistics for a slice of findings."""

    triaged: int = 0
    has_triaged_data: bool = False
    total: int = 0
    confirmed_real: int = 0  # TP
    false_positives: int = 0  # FP (underlying defect is false)
    invalid_reproductions: int = 0  # Test failed to prove reproduction
    ignored_merged: int = 0  # Ignored on PR merge
    pending: int = 0  # Untriaged / open
    false_positive_rate: float = 0.0  # FP / (TP + FP)
    cried_wolf_index: float = 0.0  # FP / (TP + FP)
    noise_burden_rate: float = (
        0.0  # (FP + INVALID_REPRODUCTION + IGNORED_MERGED) / total
    )

    # Human audit sampling metrics
    audited_total: int = 0  # Sample size (n) audited by humans
    audited_valid: int = 0  # Human confirmed valid reproduction
    audited_invalid: int = 0  # Human rejected reproduction
    validation_error_rate: float = 0.0  # audited_invalid / audited_total
    human_confirmed_fpr: float = 0.0  # human_fp / (human_tp + human_fp)
    audit_reasons_breakdown: dict[str, int] = Field(default_factory=dict)


class CalibrationMetrics(BaseModel):
    """Comprehensive repository calibration metrics across dimensions."""

    repo_id: str
    window_days: int = 30
    start_time: float
    end_time: float
    triaged: int = 0
    has_triaged_data: bool = False
    by_reproduced_category: dict[str, MetricSlice] = Field(default_factory=dict)
    total_findings: int = 0
    confirmed_real: int = 0
    false_positives: int = 0
    invalid_reproductions: int = 0
    ignored_merged: int = 0
    pending: int = 0
    false_positive_rate: float = 0.0
    cried_wolf_index: float = 0.0
    noise_burden_rate: float = 0.0

    # Top-level human audit totals
    audited_total: int = 0
    audited_valid: int = 0
    audited_invalid: int = 0
    validation_error_rate: float = 0.0
    audit_reasons_breakdown: dict[str, int] = Field(default_factory=dict)

    by_category: dict[str, MetricSlice] = Field(default_factory=dict)
    by_evidence_grade: dict[str, MetricSlice] = Field(default_factory=dict)
    by_severity: dict[str, MetricSlice] = Field(default_factory=dict)


def compute_slice_metrics(findings: list[dict[str, Any]]) -> MetricSlice:
    """Compute counts, FPR, Cried-Wolf indices, and human audit validation rates."""
    total = sum(f.get("n", 1) for f in findings)
    tp = sum(f.get("n", 1) for f in findings if f.get("outcome") == CONFIRMED_REAL)
    fp = sum(f.get("n", 1) for f in findings if f.get("outcome") == FALSE_POSITIVE)
    inv_rep = sum(
        f.get("n", 1) for f in findings if f.get("outcome") == INVALID_REPRODUCTION
    )
    ignored = sum(f.get("n", 1) for f in findings if f.get("outcome") == IGNORED_MERGED)
    pending = sum(f.get("n", 1) for f in findings if f.get("outcome") == PENDING)

    triaged = tp + fp
    fpr = round(fp / triaged, 4) if triaged > 0 else 0.0
    cwi = fpr  # Cried-Wolf Index among triaged alerts
    noise = round((fp + inv_rep + ignored) / total, 4) if total > 0 else 0.0

    # Human audit sampling metrics
    audited = [
        f
        for f in findings
        if f.get("audit_status") in (AUDIT_STATUS_VALID, AUDIT_STATUS_INVALID)
    ]
    n_audited = sum(f.get("n", 1) for f in audited)
    audited_valid = sum(
        f.get("n", 1) for f in audited if f.get("audit_status") == AUDIT_STATUS_VALID
    )
    audited_invalid = sum(
        f.get("n", 1) for f in audited if f.get("audit_status") == AUDIT_STATUS_INVALID
    )
    ver = round(audited_invalid / n_audited, 4) if n_audited > 0 else 0.0

    human_tp = sum(
        f.get("n", 1)
        for f in findings
        if f.get("outcome") == CONFIRMED_REAL
        and f.get("source") == SOURCE_HUMAN_EXPLICIT
    )
    human_fp = sum(
        f.get("n", 1)
        for f in findings
        if f.get("outcome") == FALSE_POSITIVE
        and f.get("source") == SOURCE_HUMAN_EXPLICIT
    )
    human_triaged = human_tp + human_fp
    human_fpr = round(human_fp / human_triaged, 4) if human_triaged > 0 else 0.0

    reasons: dict[str, int] = {}
    for f in audited:
        code = f.get("audit_reason_code")
        if code and code != AUDIT_REASON_CORRECT:
            reasons[code] = reasons.get(code, 0) + f.get("n", 1)

    return MetricSlice(
        total=total,
        triaged=triaged,
        has_triaged_data=triaged > 0,
        confirmed_real=tp,
        false_positives=fp,
        invalid_reproductions=inv_rep,
        ignored_merged=ignored,
        pending=pending,
        false_positive_rate=fpr,
        cried_wolf_index=cwi,
        noise_burden_rate=noise,
        audited_total=n_audited,
        audited_valid=audited_valid,
        audited_invalid=audited_invalid,
        validation_error_rate=ver,
        human_confirmed_fpr=human_fpr,
        audit_reasons_breakdown=reasons,
    )


class RepoCalibrationEngine:
    """
    Computes per-repository calibration statistics, historical false-positive rates,
    and formats human-readable Markdown telemetry summaries.
    """

    def __init__(self, store: FindingOutcomeStore | None = None):
        self.store = store or FindingOutcomeStore()

    def get_metrics(
        self,
        repo_id: str,
        window_days: int = 30,
        start_time: float | None = None,
        end_time: float | None = None,
        *,
        provider: str = "github",
        host: str = "github.com",
    ) -> CalibrationMetrics:
        """
        Aggregate repository finding outcomes for rolling 30-day or custom time window.
        """
        clean_repo = (repo_id or "").strip() or "default"
        effective_days = window_days if window_days >= 1 else 30
        now = time.time()
        effective_end = end_time if end_time is not None else now
        effective_start = (
            start_time
            if start_time is not None
            else (effective_end - (effective_days * 86400))
        )

        computed_window_days = effective_days
        if start_time is not None and end_time is not None and end_time >= start_time:
            computed_window_days = max(1, int(round((end_time - start_time) / 86400)))

        # Query all findings for repo within window
        findings = self.store.aggregate(
            clean_repo, effective_start, effective_end, provider, host
        )

        overall = compute_slice_metrics(findings)

        # Break down by category
        by_category: dict[str, MetricSlice] = {}
        for cat in sorted(VALID_CATEGORIES):
            cat_findings = [f for f in findings if f.get("category") == cat]
            by_category[cat] = compute_slice_metrics(cat_findings)

        # Break down by evidence grade
        by_grade: dict[str, MetricSlice] = {}
        for grade in (REPRODUCED, HEURISTIC):
            grade_findings = [f for f in findings if f.get("evidence_grade") == grade]
            by_grade[grade] = compute_slice_metrics(grade_findings)

        # Break down by severity
        severities = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO", "WARNING"]
        by_sev: dict[str, MetricSlice] = {}
        for sev in severities:
            sev_findings = [
                f for f in findings if (f.get("severity") or "").upper() == sev
            ]
            if sev_findings:
                by_sev[sev] = compute_slice_metrics(sev_findings)

        return CalibrationMetrics(
            repo_id=clean_repo,
            window_days=computed_window_days,
            start_time=effective_start,
            end_time=effective_end,
            triaged=overall.triaged,
            has_triaged_data=overall.has_triaged_data,
            by_reproduced_category={
                cat: compute_slice_metrics(
                    [
                        f
                        for f in findings
                        if f["category"] == cat and f["evidence_grade"] == REPRODUCED
                    ]
                )
                for cat in VALID_CATEGORIES
            },
            total_findings=overall.total,
            confirmed_real=overall.confirmed_real,
            false_positives=overall.false_positives,
            invalid_reproductions=overall.invalid_reproductions,
            ignored_merged=overall.ignored_merged,
            pending=overall.pending,
            false_positive_rate=overall.false_positive_rate,
            cried_wolf_index=overall.cried_wolf_index,
            noise_burden_rate=overall.noise_burden_rate,
            audited_total=overall.audited_total,
            audited_valid=overall.audited_valid,
            audited_invalid=overall.audited_invalid,
            validation_error_rate=overall.validation_error_rate,
            audit_reasons_breakdown=overall.audit_reasons_breakdown,
            by_category=by_category,
            by_evidence_grade=by_grade,
            by_severity=by_sev,
        )

    def generate_markdown_report(self, metrics: CalibrationMetrics) -> str:
        """
        Format comprehensive calibration and cried-wolf telemetry into GitHub-flavored Markdown.
        """
        triaged = metrics.confirmed_real + metrics.false_positives
        fpr_pct = (
            f"{metrics.false_positive_rate * 100:.1f}%"
            if triaged > 0
            else "N/A (0 triaged)"
        )
        cwi_pct = f"{metrics.cried_wolf_index * 100:.1f}%" if triaged > 0 else "N/A"
        audited_n = metrics.audited_total
        ver_pct = (
            f"{metrics.validation_error_rate * 100:.1f}%"
            if audited_n > 0
            else "N/A (0 audited)"
        )

        md_lines = [
            "## 🐺 Repository Calibration & Cried-Wolf Telemetry",
            f"**Repository**: `{metrics.repo_id}` · **Window**: Rolling {metrics.window_days} Days\n",
            "| Metric | Value | Meaning |",
            "| :--- | :---: | :--- |",
            f"| **False-Positive Rate (FPR)** | **{fpr_pct}** | $\\frac{{FP}}{{TP + FP}}$ (proportion of triaged findings that were false alarms) |",
            f"| **Cried-Wolf Index** | **{cwi_pct}** | Historical false alarm frequency |",
            f"| **REPRODUCED Validation Error Rate** | **{ver_pct}** | Proportion of human-audited generated tests that failed verification ($n={audited_n}$) |",
            f"| **Total Findings Posted** | **{metrics.total_findings}** | Total findings across PRs in time window |",
            f"| **Confirmed Real (TP)** | **{metrics.confirmed_real}** | Verified defects (via `/confirm`, `/apply`, code fix, or human audit) |",
            f"| **False Positives (FP)** | **{metrics.false_positives}** | Flagged false alarms (via `/fp` or 👎 reaction) |",
            f"| **Invalid Reproductions** | **{metrics.invalid_reproductions}** | Generated tests that failed to prove the claimed defect |",
            f"| **Ignored on Merge** | **{metrics.ignored_merged}** | Merged without resolution or confirmation |",
            f"| **Pending Triage** | **{metrics.pending}** | Open findings awaiting action |",
            "",
            "### 📂 Breakdown by Category",
            "| Category | Total | Confirmed (TP) | False Pos (FP) | Invalid Reps | Ignored | Pending | FP Rate (%) | Cried-Wolf Index |",
            "| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |",
        ]

        for cat in (
            CATEGORY_SECURITY,
            CATEGORY_QUALITY,
            CATEGORY_GOVERNANCE,
            CATEGORY_ARCHITECTURE,
        ):
            slice_m = metrics.by_category.get(cat, MetricSlice())
            cat_triaged = slice_m.confirmed_real + slice_m.false_positives
            cat_fpr = (
                f"{slice_m.false_positive_rate * 100:.1f}%" if cat_triaged > 0 else "-"
            )
            cat_cwi = (
                f"{slice_m.cried_wolf_index * 100:.1f}%" if cat_triaged > 0 else "-"
            )
            md_lines.append(
                f"| `{cat}` | {slice_m.total} | {slice_m.confirmed_real} | {slice_m.false_positives} | "
                f"{slice_m.invalid_reproductions} | {slice_m.ignored_merged} | {slice_m.pending} | **{cat_fpr}** | {cat_cwi} |"
            )

        md_lines.extend(
            [
                "",
                "### 🔬 REPRODUCED Evidence Quality (Human-Audited Sampling)",
                "| Category | Total REPRODUCED | Audited (n) | Valid Reproduction | Invalid Reproduction | Validation Error Rate (%) |",
                "| :--- | :---: | :---: | :---: | :---: | :---: |",
            ]
        )

        for cat in (
            CATEGORY_SECURITY,
            CATEGORY_QUALITY,
            CATEGORY_GOVERNANCE,
            CATEGORY_ARCHITECTURE,
        ):
            slice_m = metrics.by_reproduced_category.get(cat, MetricSlice())
            cat_n = slice_m.audited_total
            cat_ver = (
                f"{slice_m.validation_error_rate * 100:.1f}%" if cat_n > 0 else "-"
            )
            md_lines.append(
                f"| `{cat}` | {slice_m.total} | **{cat_n}** | {slice_m.audited_valid} | {slice_m.audited_invalid} | **{cat_ver}** |"
            )

        if metrics.audit_reasons_breakdown:
            md_lines.extend(["", "> ⚠️ **Reproduction Rejection Root Causes**:"])
            for reason, cnt in metrics.audit_reasons_breakdown.items():
                label = reason.replace("_", " ").title()
                md_lines.append(f"> - **{label}** (`{reason}`): {cnt}")

        md_lines.extend(
            [
                "",
                "### 🧪 Breakdown by Evidence Grade",
                "| Evidence Grade | Description | Total | Confirmed (TP) | False Pos (FP) | Invalid Reps | FP Rate (%) |",
                "| :--- | :--- | :---: | :---: | :---: | :---: | :---: |",
            ]
        )

        rep_m = metrics.by_evidence_grade.get(REPRODUCED, MetricSlice())
        rep_triaged = rep_m.confirmed_real + rep_m.false_positives
        rep_fpr = f"{rep_m.false_positive_rate * 100:.1f}%" if rep_triaged > 0 else "-"
        md_lines.append(
            f"| `REPRODUCED` | Empirical sandbox test reproduced defect | {rep_m.total} | {rep_m.confirmed_real} | {rep_m.false_positives} | {rep_m.invalid_reproductions} | **{rep_fpr}** |"
        )

        heur_m = metrics.by_evidence_grade.get(HEURISTIC, MetricSlice())
        heur_triaged = heur_m.confirmed_real + heur_m.false_positives
        heur_fpr = (
            f"{heur_m.false_positive_rate * 100:.1f}%" if heur_triaged > 0 else "-"
        )
        md_lines.append(
            f"| `HEURISTIC` | Static regex pattern or AST rule check | {heur_m.total} | {heur_m.confirmed_real} | {heur_m.false_positives} | {heur_m.invalid_reproductions} | **{heur_fpr}** |"
        )

        md_lines.extend(
            [
                "",
                "> 💡 **How to Calibrate**: Help our review models learn your codebase!",
                "> - Audit reproduction: `/audit next` or `/audit-confirm <fingerprint>` / `/audit-reject <fingerprint> <reason>`",
                "> - Flag a false positive: `/fp <fingerprint> [reason]` (suppression is a separate maintainer action)",
                "> - Confirm a genuine defect: `/confirm <fingerprint>`",
            ]
        )

        return "\n".join(md_lines)

    def generate_trust_footer(self, repo_id: Any, window_days: int = 30) -> str:
        """
        Generate a concise, honest trust footer for PR review summaries with sample size n.
        """
        if isinstance(repo_id, CalibrationMetrics):
            metrics = repo_id
            clean_repo = metrics.repo_id
            window_days = metrics.window_days
        else:
            metrics = self.get_metrics(repo_id, window_days=window_days)
            clean_repo = str(repo_id).strip()

        triaged = metrics.confirmed_real + metrics.false_positives

        if triaged == 0 and metrics.audited_total == 0:
            return (
                f"\n\n---\n"
                f"> 📊 **Historical Calibration (`{clean_repo}`)**: No triaged review findings recorded yet. "
                f"Calibrate with `/fp <fingerprint>` or audit with `/audit next`."
            )

        overall_pct = f"{metrics.false_positive_rate * 100:.1f}%"
        rep_m = metrics.by_evidence_grade.get(REPRODUCED, MetricSlice())
        rep_triaged = rep_m.confirmed_real + rep_m.false_positives
        rep_pct = (
            f"{rep_m.false_positive_rate * 100:.1f}%"
            if rep_triaged > 0
            else "N/A (0 triaged)"
        )

        heur_m = metrics.by_evidence_grade.get(HEURISTIC, MetricSlice())
        heur_triaged = heur_m.confirmed_real + heur_m.false_positives
        heur_pct = (
            f"{heur_m.false_positive_rate * 100:.1f}%"
            if heur_triaged > 0
            else "N/A (0 triaged)"
        )

        if rep_m.audited_total > 0:
            ver_pct = f"{rep_m.validation_error_rate * 100:.1f}%"
            rep_detail = f"Reproduced: **{rep_pct}** FP [REPRODUCED Validation Error Rate: **{ver_pct}**, n = {rep_m.audited_total} audited sample]"
        else:
            rep_detail = f"Reproduced: **{rep_pct}** FP (n={rep_triaged} triaged)"

        return (
            f"\n\n---\n"
            f"> 📊 **Repository Trust & Calibration (`{clean_repo}`)**: "
            f"Historical False-Positive Rate: **{overall_pct}** "
            f"({rep_detail}, Heuristic: **{heur_pct}** over rolling {window_days} days). "
            f"Audit reproductions with `/audit next` or flag false positives with `/fp <fingerprint>`."
        )


    def evaluate_tricorder_circuit_breaker(
        self,
        repo_id: str,
        window_days: int = 30,
        min_sample: int = 10,
        max_fpr: float = 0.10,
        *,
        provider: str = "github",
        host: str = "github.com",
        suppression_store: Any | None = None,
    ) -> list[dict[str, Any]]:
        """
        Evaluates Google Tricorder-style circuit breaker across rules for the repository.
        Any rule exceeding max_fpr (default 10%) across at least min_sample (default 10)
        triaged evaluations is automatically placed on probation in SuppressionStore.
        """
        clean_repo = (repo_id or "").strip() or "default"
        effective_days = max(1, window_days)
        now = time.time()
        start_time = now - (effective_days * 86400)

        from code_review_agent.suppression_store import SuppressionStore

        supp_store = suppression_store or SuppressionStore(
            db_path=getattr(self.store, "db_path", None)
        )

        conn_getter = getattr(self.store, "_get_connection", None)
        if conn_getter is None:
            return []

        rows = conn_getter().execute(
            """
            SELECT category, metadata_json, claim_title, outcome, audit_status
            FROM finding_outcomes
            WHERE provider = ? AND host = ? AND repo_id = ?
              AND first_posted_at >= ? AND first_posted_at <= ?
            """,
            (provider, host, clean_repo.lower(), start_time, now),
        ).fetchall()

        rule_findings: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            data = dict(row)
            meta = {}
            try:
                meta = json.loads(data.get("metadata_json") or "{}")
            except Exception:
                pass
            rule_id = (
                meta.get("rule_id")
                or meta.get("rule")
                or data.get("claim_title")
                or data.get("category")
                or "UNKNOWN"
            ).strip()

            rule_findings.setdefault(rule_id, []).append(data)
            cat = (data.get("category") or "").strip()
            if cat and cat != rule_id:
                cat_key = f"category:{cat}"
                rule_findings.setdefault(cat_key, []).append(data)

        results: list[dict[str, Any]] = []
        for target_id, items in sorted(rule_findings.items()):
            triaged = 0
            positives = 0
            negatives = 0
            for item in items:
                outcome = item.get("outcome")
                audit_status = item.get("audit_status")

                is_triaged = (
                    outcome
                    in (
                        CONFIRMED_REAL,
                        FALSE_POSITIVE,
                        INVALID_REPRODUCTION,
                        IGNORED_MERGED,
                    )
                    or audit_status in (AUDIT_STATUS_VALID, AUDIT_STATUS_INVALID)
                )
                if not is_triaged:
                    continue

                triaged += 1
                if (
                    outcome in (FALSE_POSITIVE, INVALID_REPRODUCTION)
                    or audit_status == AUDIT_STATUS_INVALID
                ):
                    negatives += 1
                elif (
                    outcome == CONFIRMED_REAL
                    or audit_status == AUDIT_STATUS_VALID
                ):
                    positives += 1

            if positives + negatives > 0:
                fpr = negatives / (positives + negatives)
            elif triaged > 0:
                fpr = negatives / triaged
            else:
                fpr = 0.0

            if triaged >= min_sample and fpr > max_fpr:
                reason = (
                    f"Tricorder FPR {fpr * 100:.1f}% exceeds threshold "
                    f"{max_fpr * 100:.1f}% (n={triaged}, neg={negatives})"
                )
                supp_store.put_rule_on_probation(
                    repo_id=clean_repo,
                    rule_id=target_id,
                    fpr=fpr,
                    total=triaged,
                    negative_signals=negatives,
                    reason=reason,
                )
                results.append(
                    {
                        "rule_id": target_id,
                        "status": "PROBATION",
                        "action": "PROBATION_APPLIED",
                        "false_positive_rate": fpr,
                        "total_evaluations": triaged,
                        "negative_signals": negatives,
                        "reason": reason,
                    }
                )
            elif triaged >= min_sample and fpr <= max_fpr:
                was_on_probation = supp_store.is_rule_on_probation(clean_repo, target_id)
                if was_on_probation:
                    supp_store.remove_rule_from_probation(clean_repo, target_id)
                    action = "PROBATION_LIFTED"
                else:
                    action = "NONE"
                results.append(
                    {
                        "rule_id": target_id,
                        "status": "HEALTHY",
                        "action": action,
                        "false_positive_rate": fpr,
                        "total_evaluations": triaged,
                        "negative_signals": negatives,
                        "reason": f"Tricorder FPR {fpr * 100:.1f}% within threshold (n={triaged})",
                    }
                )
            else:
                results.append(
                    {
                        "rule_id": target_id,
                        "status": "INSUFFICIENT_DATA",
                        "action": "NONE",
                        "false_positive_rate": fpr,
                        "total_evaluations": triaged,
                        "negative_signals": negatives,
                        "reason": f"Sample size {triaged} below minimum {min_sample}",
                    }
                )

        return results
