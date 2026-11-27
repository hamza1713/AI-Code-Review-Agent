"""Operator API for blind human labeling of the development evaluation split.

Only the development split is served. Held-out labels must stay outside tuning
workspaces (docs/EVALUATION_PROTOCOL_V2.md), so this router refuses them.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from code_review_agent.eval.adjudication import (
    COHORTS,
    LABELED,
    agreement_report,
    merge_labels,
    normalize_finding,
    progress,
    resolve_disagreement,
)

router = APIRouter(prefix="/api/eval", tags=["evaluation"])
SPLIT = "development"
_lock = threading.Lock()


def corpus_root() -> Path:
    default = Path(__file__).resolve().parents[3] / "samples" / "evaluation" / "public-pr-v2"
    root = Path(os.getenv("REVIEW_EVAL_CORPUS", str(default)))
    if not (root / "inputs.json").is_file():
        raise HTTPException(
            404,
            "The evaluation corpus is not on this server. Production images leave it out "
            "on purpose; run labeling from a source checkout with `uv run review-server`, "
            "or set REVIEW_EVAL_CORPUS.",
        )
    return root


def predictions_path() -> Path:
    default = Path(__file__).resolve().parents[3] / "evaluation-runs" / f"{SPLIT}-deterministic-predictions.json"
    return Path(os.getenv("REVIEW_EVAL_PREDICTIONS", str(default)))


def _labels_file(root: Path, who: str) -> Path:
    return root / "labeling" / f"{SPLIT}-{who}.json"


def _read(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise HTTPException(404, f"{path.name} was not found. Generate label templates first.")
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, data: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _split_cases(root: Path) -> list[dict[str, Any]]:
    manifest = json.loads((root / "inputs.json").read_text(encoding="utf-8"))
    return [c for c in manifest["cases"] if c.get("split") == SPLIT]


def _predictions() -> dict[str, list[dict[str, Any]]]:
    path = predictions_path()
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {c["id"]: c.get("findings", []) for c in data.get("cases", [])}


def _case_by_id(labels: dict[str, Any], case_id: str) -> dict[str, Any]:
    for case in labels["cases"]:
        if case["id"] == case_id:
            return case
    raise HTTPException(404, "Unknown evaluation case.")


class FindingIn(BaseModel):
    file_path: str
    line_number: int = Field(ge=1)
    rule_ids: list[str] = Field(default_factory=list)
    cwe: str | None = None
    severity: str = "MEDIUM"
    note: str = ""


class LabelIn(BaseModel):
    reviewer_name: str = Field(min_length=1, max_length=80)
    cohort: Literal["clean", "hard_negative", "defect"]
    rationale: str = Field(min_length=1, max_length=2000)
    findings: list[FindingIn] = Field(default_factory=list)


class AdjudicationIn(BaseModel):
    adjudicator: str = Field(min_length=1, max_length=80)
    cohort: Literal["clean", "hard_negative", "defect"]
    rationale: str = Field(min_length=1, max_length=2000)
    findings: list[FindingIn] = Field(default_factory=list)


def _status_for(labels: dict[str, Any] | None, case_id: str) -> str:
    if not labels:
        return "UNREVIEWED"
    for case in labels.get("cases", []):
        if case["id"] == case_id:
            return case.get("status", "UNREVIEWED")
    return "UNREVIEWED"


def _maybe(path: Path) -> dict[str, Any] | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


@router.get("/cases")
def list_cases():
    root = corpus_root()
    preds = _predictions()
    a = _maybe(_labels_file(root, "reviewer-a"))
    b = _maybe(_labels_file(root, "reviewer-b"))
    adj = _maybe(_labels_file(root, "adjudication"))
    cases = [
        {
            "id": c["id"],
            "predicted_findings": len(preds.get(c["id"], [])),
            "reviewer_a": _status_for(a, c["id"]),
            "reviewer_b": _status_for(b, c["id"]),
            "adjudication": _status_for(adj, c["id"]),
        }
        for c in _split_cases(root)
    ]
    return {
        "split": SPLIT,
        "cohorts": list(COHORTS),
        "reviewers": {
            "a": (a or {}).get("reviewer"),
            "b": (b or {}).get("reviewer"),
        },
        "progress": {
            "reviewer_a": progress(a),
            "reviewer_b": progress(b),
            "adjudication": progress(adj),
        },
        "predictions_available": bool(preds),
        "cases": cases,
    }


@router.get("/cases/{case_id}")
def get_case(case_id: str, reviewer: Literal["a", "b", "adjudication"] = "a"):
    """One case for one role. Reviewers only ever see their own label (blind)."""
    root = corpus_root()
    entry = next((c for c in _split_cases(root) if c["id"] == case_id), None)
    if entry is None:
        raise HTTPException(404, "Unknown development case.")
    from code_review_agent.path_validation import safe_diff_path

    diff = safe_diff_path(root, entry["file"]).read_text(encoding="utf-8")
    who = "adjudication" if reviewer == "adjudication" else f"reviewer-{reviewer}"
    label = _case_by_id(_read(_labels_file(root, who)), case_id)
    return {
        "id": case_id,
        "diff": diff,
        "predicted_findings": _predictions().get(case_id, []),
        "label": label,
    }


@router.put("/labels/{reviewer}/{case_id}")
def save_label(reviewer: Literal["a", "b"], case_id: str, body: LabelIn):
    root = corpus_root()
    other = "b" if reviewer == "a" else "a"
    name = body.reviewer_name.strip()
    try:
        findings = [normalize_finding(f.model_dump()) for f in body.findings]
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    if body.cohort == "defect" and not findings:
        raise HTTPException(422, "A defect case needs at least one finding.")
    with _lock:
        path = _labels_file(root, f"reviewer-{reviewer}")
        labels = _read(path)
        other_labels = _maybe(_labels_file(root, f"reviewer-{other}")) or {}
        other_name = (other_labels.get("reviewer") or "").strip()
        if other_name and other_name.lower() == name.lower():
            raise HTTPException(422, "Reviewer A and reviewer B must be different people.")
        current = (labels.get("reviewer") or "").strip()
        if current and current.lower() != name.lower():
            raise HTTPException(
                409, f"This label set belongs to {current}. Use the other reviewer slot."
            )
        labels["reviewer"] = name
        case = _case_by_id(labels, case_id)
        case.update(
            status=LABELED,
            reviewers=[name],
            cohort=body.cohort,
            rationale=body.rationale.strip(),
            findings=findings,
        )
        _write(path, labels)
    return {"saved": True, "case": case, "progress": progress(labels)}


@router.post("/merge")
def merge():
    root = corpus_root()
    with _lock:
        a = _read(_labels_file(root, "reviewer-a"))
        b = _read(_labels_file(root, "reviewer-b"))
        target = _labels_file(root, "adjudication")
        try:
            merged = merge_labels(a, b, _maybe(target))
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        _write(target, merged)
    return {"progress": progress(merged), "agreement": agreement_report(a, b)}


@router.put("/adjudication/{case_id}")
def adjudicate(case_id: str, body: AdjudicationIn):
    root = corpus_root()
    with _lock:
        path = _labels_file(root, "adjudication")
        labels = _read(path)
        case = _case_by_id(labels, case_id)
        try:
            resolved = resolve_disagreement(
                case,
                body.adjudicator,
                body.cohort,
                body.rationale,
                [f.model_dump() for f in body.findings],
            )
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        labels["cases"] = [resolved if c["id"] == case_id else c for c in labels["cases"]]
        _write(path, labels)
    return {"saved": True, "case": resolved, "progress": progress(labels)}


@router.get("/score")
def score():
    """Score predictions once every development case is adjudicated."""
    root = corpus_root()
    adj = _maybe(_labels_file(root, "adjudication"))
    a = _maybe(_labels_file(root, "reviewer-a")) or {"cases": []}
    b = _maybe(_labels_file(root, "reviewer-b")) or {"cases": []}
    state = progress(adj)
    agreement = agreement_report(a, b)
    if not adj or state["adjudicated"] != state["total"] or state["total"] == 0:
        return {"ready": False, "progress": state, "agreement": agreement}
    path = predictions_path()
    if not path.is_file():
        raise HTTPException(404, "Prediction capture for the development split was not found.")
    from code_review_agent.eval.corpus import score_predictions

    try:
        result = score_predictions(root, json.loads(path.read_text(encoding="utf-8")), adj)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    result.pop("details", None)
    return {"ready": True, "progress": state, "agreement": agreement, "score": result}

