"""Human label merging, inter-rater agreement and interval estimates.

Follows docs/EVALUATION_PROTOCOL_V2.md: two independent reviewers label each
case blind; agreeing cases are adjudicated automatically, and disagreements
need a third, distinct adjudicator. Nothing here invents reviewer identities.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from typing import Any

COHORTS = ("clean", "hard_negative", "defect")
LABELED = "LABELED"
ADJUDICATED = "ADJUDICATED"
NEEDS_ADJUDICATION = "NEEDS_ADJUDICATION"
UNREVIEWED = "UNREVIEWED"


def normalize_finding(finding: dict[str, Any]) -> dict[str, Any]:
    """Validate and canonicalize one gold finding (same contract as eval.matching)."""
    path = str(finding.get("file_path") or "").strip()
    try:
        line = int(finding.get("line_number") or 0)
    except (TypeError, ValueError):
        line = 0
    rule_ids = sorted(
        {str(r).strip() for r in finding.get("rule_ids") or [] if str(r).strip()}
    )
    cwe = str(finding.get("cwe") or "").strip().upper() or None
    if not path or line < 1:
        raise ValueError("Each finding needs a file path and a line number of 1 or more.")
    if not rule_ids and not cwe:
        raise ValueError("Each finding needs at least one rule ID or a CWE.")
    severity = str(finding.get("severity") or "MEDIUM").strip().upper()
    note = str(finding.get("note") or "").strip()[:500]
    return {
        "file_path": path,
        "line_number": line,
        "rule_ids": rule_ids,
        "cwe": cwe,
        "severity": severity,
        "note": note,
    }


def finding_key(finding: dict[str, Any]) -> tuple:
    return (
        finding["file_path"],
        finding["line_number"],
        tuple(finding.get("rule_ids") or ()),
        finding.get("cwe"),
    )


def _label_digest(case: dict[str, Any]) -> str:
    body = {
        "cohort": case.get("cohort"),
        "findings": sorted(finding_key(f) for f in case.get("findings", [])),
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def cases_agree(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return a.get("cohort") == b.get("cohort") and {
        finding_key(f) for f in a.get("findings", [])
    } == {finding_key(f) for f in b.get("findings", [])}


def merge_labels(
    reviewer_a: dict[str, Any],
    reviewer_b: dict[str, Any],
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the adjudication file from two independent reviewer files.

    Agreeing cases become ADJUDICATED. Disagreements become NEEDS_ADJUDICATION
    unless ``previous`` already holds a third-party resolution made against the
    same two labels, which is then kept.
    """
    name_a = (reviewer_a.get("reviewer") or "").strip()
    name_b = (reviewer_b.get("reviewer") or "").strip()
    if not name_a or not name_b:
        raise ValueError("Both reviewer files need a reviewer name.")
    if name_a.lower() == name_b.lower():
        raise ValueError("Reviewer A and reviewer B must be different people.")
    for key in ("corpus_manifest_sha256", "corpus_version"):
        if reviewer_a.get(key) != reviewer_b.get(key):
            raise ValueError("Reviewer files come from different corpus manifests.")

    b_cases = {c["id"]: c for c in reviewer_b["cases"]}
    prev_cases = {c["id"]: c for c in (previous or {}).get("cases", [])}
    merged_cases = []
    for a in reviewer_a["cases"]:
        b = b_cases.get(a["id"])
        if b is None:
            raise ValueError(f"Case {a['id']} is missing from reviewer B.")
        base = {
            "id": a["id"],
            "sha256": a["sha256"],
            "input_snapshot_hash": a.get("input_snapshot_hash"),
        }
        if a.get("status") != LABELED or b.get("status") != LABELED:
            merged_cases.append(
                {
                    **base,
                    "status": UNREVIEWED,
                    "reviewers": [],
                    "cohort": None,
                    "rationale": "",
                    "disagreement": False,
                    "adjudicator": None,
                    "findings": [],
                }
            )
            continue
        digest = hashlib.sha256((_label_digest(a) + _label_digest(b)).encode()).hexdigest()
        if cases_agree(a, b):
            merged_cases.append(
                {
                    **base,
                    "status": ADJUDICATED,
                    "reviewers": [name_a, name_b],
                    "cohort": a["cohort"],
                    "rationale": f"A: {a.get('rationale', '').strip()} | B: {b.get('rationale', '').strip()}",
                    "disagreement": False,
                    "adjudicator": None,
                    "findings": a.get("findings", []),
                    "labels_digest": digest,
                }
            )
            continue
        prior = prev_cases.get(a["id"])
        if (
            prior
            and prior.get("status") == ADJUDICATED
            and prior.get("disagreement")
            and prior.get("labels_digest") == digest
            and prior.get("adjudicator")
        ):
            merged_cases.append(prior)
            continue
        merged_cases.append(
            {
                **base,
                "status": NEEDS_ADJUDICATION,
                "reviewers": [name_a, name_b],
                "cohort": None,
                "rationale": "",
                "disagreement": True,
                "adjudicator": None,
                "findings": [],
                "candidates": {
                    "a": {"cohort": a.get("cohort"), "findings": a.get("findings", []), "rationale": a.get("rationale", "")},
                    "b": {"cohort": b.get("cohort"), "findings": b.get("findings", []), "rationale": b.get("rationale", "")},
                },
                "labels_digest": digest,
            }
        )
    return {
        "corpus_manifest_sha256": reviewer_a.get("corpus_manifest_sha256"),
        "schema_version": reviewer_a.get("schema_version"),
        "corpus_version": reviewer_a.get("corpus_version"),
        "cases": merged_cases,
    }


def resolve_disagreement(
    case: dict[str, Any],
    adjudicator: str,
    cohort: str,
    rationale: str,
    findings: list[dict[str, Any]],
) -> dict[str, Any]:
    """Apply a third-party resolution to a NEEDS_ADJUDICATION case."""
    if case.get("status") not in (NEEDS_ADJUDICATION, ADJUDICATED) or not case.get("disagreement"):
        raise ValueError("Only cases where the reviewers disagreed need an adjudicator.")
    name = (adjudicator or "").strip()
    if not name:
        raise ValueError("Enter the adjudicator's name.")
    if name.lower() in {r.strip().lower() for r in case.get("reviewers", [])}:
        raise ValueError("The adjudicator must be someone other than reviewers A and B.")
    if cohort not in COHORTS:
        raise ValueError("Choose a cohort: clean, hard_negative or defect.")
    if not rationale.strip():
        raise ValueError("Write a short rationale for the decision.")
    return {
        **case,
        "status": ADJUDICATED,
        "adjudicator": name,
        "cohort": cohort,
        "rationale": rationale.strip(),
        "findings": [normalize_finding(f) for f in findings],
    }


def cohens_kappa(pairs: list[tuple[str, str]]) -> float | None:
    """Cohen's kappa for two raters over categorical labels; None if undefined."""
    n = len(pairs)
    if n == 0:
        return None
    observed = sum(a == b for a, b in pairs) / n
    count_a = Counter(a for a, _ in pairs)
    count_b = Counter(b for _, b in pairs)
    expected = sum(count_a[k] * count_b[k] for k in set(count_a) | set(count_b)) / (n * n)
    if expected >= 1.0:
        return 1.0 if observed == 1.0 else None
    return round((observed - expected) / (1 - expected), 4)


def agreement_report(reviewer_a: dict[str, Any], reviewer_b: dict[str, Any]) -> dict[str, Any]:
    """Cohort kappa and finding-level agreement over cases both reviewers labeled."""
    b_cases = {c["id"]: c for c in reviewer_b.get("cases", [])}
    pairs, shared, union = [], 0, 0
    for a in reviewer_a.get("cases", []):
        b = b_cases.get(a["id"])
        if not b or a.get("status") != LABELED or b.get("status") != LABELED:
            continue
        pairs.append((a.get("cohort"), b.get("cohort")))
        keys_a = {finding_key(f) for f in a.get("findings", [])}
        keys_b = {finding_key(f) for f in b.get("findings", [])}
        shared += len(keys_a & keys_b)
        union += len(keys_a | keys_b)
    return {
        "cases_compared": len(pairs),
        "cohort_agreement": round(sum(a == b for a, b in pairs) / len(pairs), 4) if pairs else None,
        "cohort_kappa": cohens_kappa(pairs),
        "finding_agreement": round(shared / union, 4) if union else None,
    }


def wilson_interval(successes: int, total: int, z: float = 1.96) -> list[float] | None:
    """95% Wilson score interval for a proportion; None when total is 0."""
    if total <= 0:
        return None
    p = successes / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def progress(labels: dict[str, Any] | None) -> dict[str, int]:
    counts = Counter(c.get("status", UNREVIEWED) for c in (labels or {}).get("cases", []))
    return {
        "total": sum(counts.values()),
        "labeled": counts.get(LABELED, 0),
        "adjudicated": counts.get(ADJUDICATED, 0),
        "needs_adjudication": counts.get(NEEDS_ADJUDICATION, 0),
        "unreviewed": counts.get(UNREVIEWED, 0),
    }
