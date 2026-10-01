"""Operator calibration endpoints. Attribution is assigned by the server."""

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, model_validator

from code_review_agent.access_policy import validate_target

from .audit_sampler import AuditSampler
from .contracts import VALID_AUDIT_REASONS, VALID_OUTCOMES
from .engine import RepoCalibrationEngine
from .persistence import (
    AmbiguousFindingError,
    FindingNotFoundError,
    FindingOutcomeStore,
)

router = APIRouter(prefix="/api/calibration")


class FindingReference(BaseModel):
    finding_id: str | None = None
    repo_id: str = ""
    fingerprint: str = ""
    pr_id: str | None = None
    provider: Literal["github", "gitlab", "bitbucket"] = "github"
    host: str = "github.com"

    @model_validator(mode="after")
    def reference_required(self):
        if not self.finding_id and not (
            self.repo_id.strip() and self.fingerprint.strip()
        ):
            raise ValueError("Supply finding_id or repository and fingerprint.")
        if self.fingerprint:
            import re

            from .contracts import clean_fingerprint

            self.fingerprint = clean_fingerprint(self.fingerprint)
            if not self.fingerprint:
                raise ValueError("Invalid fingerprint: fingerprint cannot be empty")
            if not re.fullmatch(
                r"^[0-9a-fA-F]+$", self.fingerprint
            ) and self.fingerprint.lower() not in ("none", "null"):
                raise ValueError("Invalid fingerprint: must be a valid hex string")
        return self

    def scope(self):
        return {
            "repo_id": self.repo_id,
            "fingerprint": self.fingerprint,
            "finding_id": self.finding_id,
            "pr_id": self.pr_id,
            "provider": self.provider,
            "host": self.host,
        }


class FeedbackRequest(FindingReference):
    outcome: Literal[
        "CONFIRMED_REAL",
        "FALSE_POSITIVE",
        "IGNORED_MERGED",
        "PENDING",
        "INVALID_REPRODUCTION",
    ]
    reason: str | None = Field(None, max_length=4000)
    source: str | None = None  # Compatibility label; never trusted as attribution.


class AuditRequest(FindingReference):
    is_valid: bool
    reason_code: str | None = None
    notes: str | None = Field(None, max_length=4000)
    audited_by: str | None = (
        None  # Compatibility label; authenticated operator owns decision.
    )


def lookup(store, ref):
    try:
        row = store.require_finding(**ref.scope())
        validate_target(row["provider"], row["host"], row["repo_id"])
        return row
    except FindingNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except AmbiguousFindingError as exc:
        raise HTTPException(409, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


@router.get("/contracts")
def contracts():
    return {
        "outcomes": sorted(VALID_OUTCOMES),
        "audit_reasons": sorted(VALID_AUDIT_REASONS),
    }


@router.get("/metrics")
def metrics(
    repo_id: str = "",
    repo: str = "",
    window_days: int = Query(30, ge=1, le=365),
    start_time: float | None = Query(None, allow_inf_nan=False),
    end_time: float | None = Query(None, allow_inf_nan=False),
    provider: str = "github",
    host: str = "github.com",
):
    repository = (repo_id or repo).strip() or "default"
    if start_time is not None and end_time is not None and start_time > end_time:
        raise HTTPException(422, "Start time must not exceed end time.")
    try:
        validate_target(provider, host, repository)
        return (
            RepoCalibrationEngine()
            .get_metrics(
                repository,
                window_days,
                start_time,
                end_time,
                provider=provider,
                host=host,
            )
            .model_dump()
        )
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


@router.post("/feedback")
def feedback(req: FeedbackRequest):
    store = FindingOutcomeStore()
    lookup(store, req)
    row = store.update_outcome(
        **req.scope(),
        outcome=req.outcome,
        reason=req.reason,
        actor="operator",
        source="HUMAN_EXPLICIT",
    )
    result = RepoCalibrationEngine(store).get_metrics(
        row["repo_id"], provider=req.provider, host=req.host
    )
    return {
        "status": "success",
        "finding": row,
        "false_positive_rate": result.false_positive_rate,
        "cried_wolf_index": result.cried_wolf_index,
        "message": "Feedback recorded; suppression is a separate action.",
    }


@router.get("/audit/sample")
def sample(
    repo_id: str = "",
    repo: str = "",
    sample_size: int = Query(20, ge=1, le=100),
    days: int = Query(30, ge=1, le=365),
    unaudited_only: bool = True,
    provider: str = "github",
    host: str = "github.com",
):
    repository = (repo_id or repo).strip() or "default"
    try:
        validate_target(provider, host, repository)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    findings = AuditSampler().sample_for_audit(
        repository,
        sample_size,
        days,
        unaudited_only=unaudited_only,
        provider=provider,
        host=host,
    )
    return {
        "repo_id": repository,
        "sample_size": len(findings),
        "requested_size": sample_size,
        "days": days,
        "findings": findings,
    }


@router.post("/audit/submit")
def audit(req: AuditRequest):
    store = FindingOutcomeStore()
    lookup(store, req)
    try:
        row = store.record_human_audit(
            **req.scope(),
            is_valid=req.is_valid,
            reason_code=req.reason_code,
            notes=req.notes or "",
            audited_by="operator",
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    result = RepoCalibrationEngine(store).get_metrics(
        row["repo_id"], provider=req.provider, host=req.host
    )
    return {
        "status": "success",
        "finding": row,
        "audit_status": row["audit_status"],
        "validation_error_rate": result.validation_error_rate,
        "audited_total": result.audited_total,
        "audited_valid": result.audited_valid,
        "audited_invalid": result.audited_invalid,
    }


@router.get("/findings")
def findings(
    repo_id: str,
    provider: str = "github",
    host: str = "github.com",
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    validate_target(provider, host, repo_id)
    import time

    store = FindingOutcomeStore()
    rows = (
        store._get_connection()
        .execute(
            """SELECT * FROM finding_outcomes
        WHERE provider=? AND host=? AND repo_id=? AND publication_status='PUBLISHED'
        AND first_posted_at>=? ORDER BY first_posted_at DESC LIMIT ? OFFSET ?""",
            (provider, host, repo_id.lower(), time.time() - 30 * 86400, limit, offset),
        )
        .fetchall()
    )
    return {"findings": [dict(row) for row in rows], "limit": limit, "offset": offset}
