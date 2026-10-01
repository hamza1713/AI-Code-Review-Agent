"""
FastAPI Webhook Gateway for GitHub Pull Request Ingestion.
Validates HMAC SHA-256 signatures, enqueues review jobs into a persistent task queue,
and executes asynchronous multi-agent reviews with retries and crash recovery.
"""

import hashlib
import hmac
import json
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import uvicorn
from fastapi import (
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from code_review_agent.bot import CommandRouter
from code_review_agent.calibration.api import router as calibration_router
from code_review_agent.config import get_webhook_secret, logger
from code_review_agent.gateway_security import GatewaySecurityMiddleware
from code_review_agent.models import ReviewAPIResponse
from code_review_agent.review_executor import (
    ReviewBusyError,
    ReviewDeadlineError,
    ReviewWorkerError,
    encode_inputs,
    run_job_async,
)
from code_review_agent.review_service import RateLimitExceeded, rate_limiter
from code_review_agent.webhook_queue import WebhookJobQueue, WebhookWorker

# Global persistent queue and worker instances
queue = WebhookJobQueue()
worker = WebhookWorker(
    queue=queue,
    poll_interval=1.0,
    max_workers=int(os.getenv("MAX_CONCURRENT_REVIEWS", "2")),
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage lifecycle of background queue worker and crash recovery."""
    from code_review_agent.access_policy import validate_configuration
    from code_review_agent.calibration.reaction_poller import ReactionPoller
    from code_review_agent.operations import file_lock, migrate

    validate_configuration()
    with file_lock(str(queue.db_path) + ".application.lock"):
        report = migrate(queue.db_path)
        logger.info("Schema migration: %s", report)
        worker.start()
        poller = ReactionPoller()
        app.state.reaction_poller = poller
        poller.start()
        try:
            yield
        finally:
            poller.stop()
            worker.stop(timeout=5.0)


app = FastAPI(
    title="AI Code Review Agent Webhook Gateway",
    description="Automated multi-agent code review service with durable job persistence for GitHub Pull Requests",
    version="2.0.0",
    lifespan=lifespan,
)


app.add_middleware(GatewaySecurityMiddleware)


@app.exception_handler(PermissionError)
async def denied(request, error):
    return JSONResponse({"detail": str(error)}, status_code=403)


@app.exception_handler(sqlite3.Error)
async def database_unavailable(request, error):
    app.state.database_errors = getattr(app.state, "database_errors", 0) + 1
    logger.error("Database operation failed: %s", type(error).__name__)
    return JSONResponse({"detail": "Database temporarily unavailable"}, status_code=503)


@app.exception_handler(ValueError)
async def invalid(request, error):
    status = 503 if "capacity" in str(error) else 422
    return JSONResponse({"detail": str(error)}, status_code=status)


def verify_github_signature(payload_body: bytes, signature_header: str | None) -> bool:
    """Verify HMAC SHA-256 signature from GitHub webhook request."""
    secret = get_webhook_secret()
    if not secret:
        logger.error("GITHUB_WEBHOOK_SECRET is not configured; webhook rejected.")
        return False

    if not signature_header or not signature_header.startswith("sha256="):
        return False

    expected_signature = (
        "sha256="
        + hmac.new(
            key=secret.encode("utf-8"), msg=payload_body, digestmod=hashlib.sha256
        ).hexdigest()
    )

    return hmac.compare_digest(expected_signature, signature_header)


def checked_target(identifier):
    from code_review_agent.access_policy import validate_pr_target

    try:
        return validate_pr_target(identifier)
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    except (ValueError, TypeError) as error:
        raise HTTPException(422, "Invalid repository target") from error


def enqueue_command(
    *,
    command_text,
    pr_url,
    auto_post=True,
    actor="",
    delivery_id=None,
    trusted_operator=False,
    **metadata,
):
    from code_review_agent.access_policy import authorize_actor, effective_command

    client, ident, host = checked_target(pr_url)
    cmd, args = CommandRouter.parse_command(command_text)
    if not trusted_operator:
        try:
            allowed = authorize_actor(
                client,
                ident.owner_or_project,
                ident.repo_or_slug,
                actor,
                effective_command(cmd, args),
                ident.platform,
            )
        except Exception:
            allowed = False
        if not allowed:
            raise HTTPException(403, "Repository permission denied")
    payload = dict(
        command_text=command_text,
        pr_url=pr_url,
        auto_post=auto_post,
        actor=actor,
        trusted_operator=trusted_operator,
        **metadata,
    )
    key = (
        f"command:{ident.platform}:{host}:{ident.owner_or_project}/{ident.repo_or_slug}:{ident.pr_id}:{delivery_id}"
        if delivery_id
        else None
    )
    job_id = queue.enqueue(pr_url, payload, job_kind="command", idempotency_key=key)
    return {"status": "accepted", "job_id": job_id}


def enqueue_merge(*, owner, repo, pr_number, platform="github"):
    from code_review_agent.access_policy import validate_target

    host = {
        "github": "github.com",
        "gitlab": "gitlab.com",
        "bitbucket": "bitbucket.org",
    }[platform]
    validate_target(platform, host, f"{owner}/{repo}")
    payload = {
        "owner": owner,
        "repo": repo,
        "pr_number": pr_number,
        "platform": platform,
    }
    return queue.enqueue(
        f"{owner}/{repo}#{pr_number}",
        payload,
        job_kind="merge",
        idempotency_key=f"merge:{platform}:{host}:{owner}/{repo}:{pr_number}",
    )


def _bot_identities() -> set:
    """Usernames whose comments are the bot's own — skipped to avoid feedback loops."""
    raw = f"{os.getenv('BOT_GITHUB_LOGIN', '')},{os.getenv('BOT_BOT_USERNAMES', '')}"
    return {u.strip().lower() for u in raw.split(",") if u.strip()}


@app.get("/health")
@app.get("/health/live")
def health_check():
    return {"status": "alive"}


@app.get("/health/ready")
def readiness():
    import time

    from code_review_agent.calibration import FindingOutcomeStore

    try:
        conn = queue._get_connection()
        version = conn.execute(
            "SELECT version FROM schema_versions WHERE component='calibration'"
        ).fetchone()
        if (
            not version
            or version[0] != FindingOutcomeStore.VERSION
            or not worker._running
            or not worker._thread.is_alive()
            or time.monotonic() - worker.last_loop_at > 30
        ):
            raise RuntimeError("Not ready")
        conn.execute("SELECT 1 FROM webhook_jobs LIMIT 1").fetchone()
        return {"status": "ready"}
    except Exception:
        return JSONResponse({"status": "not_ready"}, status_code=503)


@app.get("/jobs")
def get_jobs(
    status: str | None = Query(
        None,
        description="Filter by job status: QUEUED, PROCESSING, COMPLETED, FAILED, RETRYING, CANCELLED",
    ),
    limit: int = Query(50, ge=1, le=200, description="Max jobs to return"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
):
    """List persistent webhook review jobs with pagination."""
    return {"jobs": queue.list_jobs(status=status, limit=limit, offset=offset)}


@app.get("/jobs/metrics")
def get_jobs_metrics():
    """Retrieve real-time queue depth, active worker count, and processing telemetry."""
    return queue.get_queue_metrics()


@app.get("/metrics")
def operational_metrics():
    import time

    conn = queue._get_connection()
    result = queue.get_queue_metrics()
    oldest = conn.execute(
        "SELECT MIN(created_at) FROM webhook_jobs WHERE status IN ('QUEUED','RETRYING')"
    ).fetchone()[0]
    result["queue_oldest_age_seconds"] = max(0, time.time() - oldest) if oldest else 0
    result["publication_reconciliation_required"] = conn.execute(
        "SELECT COUNT(*) FROM external_operations_log WHERE status<>'SUCCESS'"
    ).fetchone()[0]
    result["database_accessible"] = True
    result["database_errors"] = worker.database_errors + getattr(
        app.state, "database_errors", 0
    )
    result["worker_running"] = (
        worker._running and worker._thread is not None and worker._thread.is_alive()
    )
    poller = getattr(app.state, "reaction_poller", None)
    result["reaction_poll_last_cycle"] = poller.state("last_cycle") if poller else None
    row = conn.execute("""SELECT MIN(CAST(s.value AS REAL)) FROM finding_outcomes f LEFT JOIN reaction_poll_state s
        ON s.key='success:'||f.finding_id WHERE provider_comment_id<>'' AND publication_status='PUBLISHED'""").fetchone()
    result["reaction_poll_lag_seconds"] = (
        max(0, time.time() - row[0]) if row and row[0] else None
    )
    from code_review_agent.observability.alerts import evaluate_alerts

    result["alerts"] = evaluate_alerts(
        result, float(os.getenv("REVIEW_QUEUE_SLA_SECONDS", "300"))
    )
    return result


class PilotControlRequest(BaseModel):
    mode: Literal["SHADOW", "PAUSED", "ADVISORY"]
    reason: str = Field(min_length=10)
    release: dict[str, Any] | None = None


@app.get("/api/pilot")
def pilot_report(repository: str, reviewer_profile_id: str | None = None):
    from code_review_agent.pilot import PilotStore

    return PilotStore(str(queue.db_path)).report(repository, reviewer_profile_id)


@app.post("/api/pilot/control")
def pilot_control(repository: str, request: PilotControlRequest):
    from code_review_agent.pilot import PilotStore

    store = PilotStore(str(queue.db_path))
    store.control(repository, request.mode, "operator", request.reason, request.release)
    return store.report(repository)


@app.get("/jobs/{job_id}")
def get_job_detail(job_id: str):
    """Retrieve details for a specific webhook job."""
    job = queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return job


@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    """Cancel an active or queued review job."""
    success = queue.cancel_job(job_id, reason="Cancelled by user/operator via API")
    if not success:
        job = queue.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
        raise HTTPException(
            status_code=400,
            detail=f"Job '{job_id}' is in status '{job.get('status')}' and cannot be cancelled.",
        )
    return {
        "status": "CANCELLED",
        "job_id": job_id,
        "message": "Job successfully cancelled.",
    }


class ReconcileJobRequest(BaseModel):
    action: Literal["reconcile", "mark_resolved", "retry", "cancel"] = "reconcile"
    operation_key: str | None = None
    notes: str | None = None
    receipt: dict[str, Any] | None = None


@app.post("/jobs/{job_id}/reconcile")
def reconcile_job(job_id: str, req: ReconcileJobRequest | None = None):
    """
    Record an operator-supplied provider receipt for one linked write intent.
    Retry and cancellation preserve uncertain intents and never imply delivery.
    """
    import time

    with queue._get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        job_row = conn.execute(
            "SELECT status, pr_identifier FROM webhook_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if not job_row:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

        current_status = job_row["status"]
        if current_status not in ("RECONCILIATION_REQUIRED", "FAILED", "CANCELLED"):
            raise HTTPException(
                409, "Only failed jobs awaiting reconciliation can be changed"
            )
        now = time.time()
        action = req.action if req else "reconcile"
        if current_status == "CANCELLED" and action == "retry":
            raise HTTPException(
                409, "Cancelled jobs cannot be resumed through reconciliation"
            )
        if action in ("reconcile", "mark_resolved"):
            if not req or not req.operation_key or not req.receipt:
                raise HTTPException(
                    422, "Provide an operation_key and the actual provider receipt"
                )
            row = conn.execute(
                """SELECT o.result_json FROM external_operations_log o
                JOIN external_operation_jobs j USING(operation_key)
                WHERE j.job_id=? AND o.operation_key=? AND o.status<>'SUCCESS' """,
                (job_id, req.operation_key),
            ).fetchone()
            if not row:
                raise HTTPException(404, "Pending operation is not linked to this job")
            record = json.loads(row["result_json"] or "{}")
            receipt_key = record.get("receipt_key", "id")
            from code_review_agent.publication import valid_receipt

            if not valid_receipt(req.receipt, receipt_key):
                raise HTTPException(422, f"Provider receipt must contain {receipt_key}")
            record.update(
                receipt=req.receipt,
                reconciled_by="operator",
                reconciliation_notes=req.notes,
            )
            conn.execute(
                "UPDATE external_operations_log SET status = 'SUCCESS', result_json = ?, updated_at = ? WHERE operation_key = ?",
                (json.dumps(record), now, req.operation_key),
            )
        if action != "cancel" and current_status != "CANCELLED":
            conn.execute(
                "UPDATE webhook_jobs SET status = 'RETRYING', attempts = 0, worker_id = NULL, locked_at = NULL, lease_expires_at = NULL, claim_token = NULL, scheduled_at = ?, current_stage = 'QUEUED', stage_progress = 0.0, updated_at = ? WHERE job_id = ?",
                (now, now, job_id),
            )
            job_status = "RETRYING"
        else:
            conn.execute(
                "UPDATE webhook_jobs SET status = 'CANCELLED', worker_id = NULL, locked_at = NULL, lease_expires_at = NULL, claim_token = NULL, updated_at = ? WHERE job_id = ?",
                (now, job_id),
            )
            job_status = "CANCELLED"

    return {
        "status": "RECONCILED",
        "job_id": job_id,
        "job_status": job_status,
        "message": f"Job '{job_id}' publication state successfully reconciled.",
    }


@app.get("/jobs/{job_id}/result")
def get_job_result(job_id: str):
    """Retrieve completed review result for a webhook job."""
    job = queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    if job.get("status") != "COMPLETED":
        return JSONResponse(
            status_code=202,
            content={
                "status": job.get("status"),
                "message": f"Job is currently {job.get('status')}",
            },
        )
    return job.get("result") or {"message": "No result payload stored for this job"}


class SuppressionCreateRequest(BaseModel):
    repo_id: str
    fingerprint: str
    reason: str = Field(min_length=1, max_length=4000)
    critical_override: bool = False
    ttl_days: int = Field(default=30, ge=1, le=365)
    author: str = "operator"
    pr_id: str = ""


@app.get("/api/suppressions")
def list_suppressions(
    repo: str | None = Query(None, description="Repository identifier (owner/repo)"),
):
    """List active finding suppressions."""
    from code_review_agent.suppression_store import SuppressionStore

    store = SuppressionStore()
    return {"suppressions": store.list_suppressions(repo_id=repo)}


@app.post("/api/suppressions")
def create_suppression(req: SuppressionCreateRequest):
    """Suppress a finding fingerprint for a repository."""
    from code_review_agent.suppression_store import SuppressionStore

    store = SuppressionStore()
    from code_review_agent.access_policy import validate_target

    validate_target("github", "github.com", req.repo_id)
    store.suppress(
        repo_id=req.repo_id,
        fingerprint=req.fingerprint,
        reason=req.reason,
        author="operator",
        critical_override=req.critical_override,
        ttl_days=req.ttl_days,
        pr_id=req.pr_id,
    )
    return {
        "status": "success",
        "message": f"Finding '{req.fingerprint}' successfully suppressed for '{req.repo_id}'.",
    }


@app.delete("/api/suppressions/{fingerprint}")
def delete_suppression(
    fingerprint: str,
    repo: str = Query(..., description="Repository identifier (owner/repo)"),
):
    """Unsuppress a finding fingerprint."""
    from code_review_agent.suppression_store import SuppressionStore

    store = SuppressionStore()
    from code_review_agent.access_policy import validate_target

    validate_target("github", "github.com", repo)
    deleted = store.unsuppress(repo_id=repo, fingerprint=fingerprint, author="operator")
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail=f"Suppression for '{fingerprint}' in '{repo}' not found.",
        )
    return {
        "status": "success",
        "message": f"Finding '{fingerprint}' unsuppressed for '{repo}'.",
    }


app.include_router(calibration_router)


class RemediationApplyRequest(BaseModel):
    pr_identifier: str
    fingerprint: str
    custom_message: str | None = None
    replacement_code: str | None = None


@app.post("/api/remediation/apply", status_code=202)
def apply_remediation(req: RemediationApplyRequest):
    """Queue remediation under the existing deployment gate."""
    from code_review_agent.access_policy import require_remediation_enabled

    require_remediation_enabled()
    checked_target(req.pr_identifier)
    return enqueue_command(
        command_text=f"/apply {req.fingerprint}",
        pr_url=req.pr_identifier,
        trusted_operator=True,
        auto_post=False,
        custom_message=req.custom_message,
        replacement_code=req.replacement_code,
    )


@app.get("/api/webhook/config")
def get_webhook_config(request: Request):
    """Return status of GitHub token, webhook secret, and public webhook URL for live UI setup."""
    import httpx

    from code_review_agent.config import get_github_token, get_webhook_secret

    token = get_github_token()
    secret = get_webhook_secret()

    github_user = None
    github_connected = False
    if token:
        try:
            with httpx.Client(
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": "AI-Code-Review-Agent",
                },
                timeout=5.0,
            ) as client:
                res = client.get("https://api.github.com/user")
                if res.status_code == 200:
                    github_user = res.json().get("login")
                    github_connected = True
        except Exception:
            pass

    public_url = os.getenv("REVIEW_PUBLIC_URL", "http://localhost:8000").rstrip("/")
    webhook_url = f"{public_url}/webhook/github"

    return {
        "webhook_url": webhook_url,
        "secret_configured": bool(secret),
        "token_configured": bool(token),
        "github_connected": github_connected,
        "github_user": github_user,
        "test_events_enabled": os.getenv("REVIEW_ENABLE_TEST_EVENTS") == "true",
    }


@app.post("/api/webhook/test-event")
async def trigger_test_webhook():
    """Trigger a simulated GitHub PR webhook event directly into the durable queue for live UI testing."""
    if os.getenv("REVIEW_ENABLE_TEST_EVENTS") != "true":
        raise HTTPException(
            status_code=403, detail="Test events are disabled on this server."
        )
    sample_payload = {
        "action": "opened",
        "number": 42,
        "pull_request": {
            "number": 42,
            "title": "feat: add user authentication with parameterized queries",
            "user": {"login": "developer-demo"},
            "head": {"sha": "c0ffee123", "ref": "feat/auth"},
            "base": {"sha": "b45e456", "ref": "main"},
            "html_url": "https://github.com/demo/repo/pull/42",
        },
        "repository": {"name": "demo-repo", "owner": {"login": "demo-org"}},
    }
    pr_id = "demo-org/demo-repo/pull/42"
    job_id = queue.enqueue(pr_identifier=pr_id, payload=sample_payload)
    return {
        "status": "accepted",
        "job_id": job_id,
        "pr": pr_id,
        "message": "Simulated GitHub Pull Request #42 webhook enqueued successfully.",
    }


class BotCommandRequest(BaseModel):
    command: str
    pr_url: str | None = None
    raw_diff: str | None = None
    repo_root: str | None = None
    auto_post: bool = False


class BotCommandResponse(BaseModel):
    command: str
    status: str
    response_markdown: str
    action_taken: str
    metadata: dict[str, Any] = {}


@app.post("/webhook/github")
async def github_webhook(
    request: Request,
    x_github_event: str | None = Header(None),
    x_github_delivery: str | None = Header(None),
    x_hub_signature_256: str | None = Header(None),
):
    """Receive, authenticate, and durably enqueue incoming GitHub webhook events."""
    body_bytes = await request.body()

    if not verify_github_signature(body_bytes, x_hub_signature_256):
        logger.error("GitHub webhook signature verification failed.")
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    payload = json.loads(body_bytes.decode("utf-8"))

    # 1b. Handle PR comments & Slash Commands
    if x_github_event in ("issue_comment", "pull_request_review_comment"):
        action = payload.get("action", "")
        if action != "created":
            return {
                "status": "ignored",
                "reason": f"Comment action '{action}' does not require processing.",
            }

        issue = payload.get("issue", {})
        if x_github_event == "issue_comment" and "pull_request" not in issue:
            return {
                "status": "ignored",
                "reason": "Comment is on an issue, not a pull request.",
            }

        comment = payload.get("comment", {})
        user = comment.get("user", {})
        login = user.get("login", "")
        # Feedback-loop guard: skip GitHub App bots and this bot's own account.
        own_login = os.getenv("BOT_GITHUB_LOGIN", "").strip().lower()
        if (
            user.get("type") == "Bot"
            or "[bot]" in login.lower()
            or (own_login and login.lower() == own_login)
        ):
            return {
                "status": "ignored",
                "reason": "Bot/self comment ignored to avoid feedback loop.",
            }

        comment_body = comment.get("body", "").strip()
        if not CommandRouter.is_bot_command(comment_body):
            return {"status": "ignored", "reason": "Comment is not a slash command."}

        cmd, args = CommandRouter.parse_command(comment_body)
        args.strip().split()[0].lower() if args else ""

        pr_html_url = (
            issue.get("pull_request", {}).get("html_url")
            or payload.get("pull_request", {}).get("html_url")
            or issue.get("html_url")
        )
        logger.info(f"🤖 Queuing PR slash command '/{cmd}' for {pr_html_url}...")

        # Run off the request path: slash commands do GitHub + LLM work (a full /review
        # can take minutes). GitHub expects the webhook to be acknowledged within
        # seconds or it retries the delivery — which would re-trigger the command.
        enqueue_command(
            actor=login,
            delivery_id=x_github_delivery
            or (
                f"comment:{comment['id']}"
                if comment.get("id")
                else "body:" + hashlib.sha256(body_bytes).hexdigest()
            ),
            command_text=comment_body,
            pr_url=pr_html_url,
            auto_post=True,
            author_association=comment.get("author_association"),
            pr_metadata={
                "author_association": comment.get("author_association"),
                "author": login,
            },
        )
        return {
            "status": "accepted",
            "command": cmd,
            "message": "Command accepted and processing in the background.",
        }

    # 2. Handle PR lifecycle reviews
    if x_github_event != "pull_request":
        return {
            "status": "ignored",
            "reason": f"Event '{x_github_event}' is not handled.",
        }

    action = payload.get("action", "")

    # Trigger only on actionable PR lifecycle events
    if action in ["opened", "synchronize", "reopened"]:
        pr_data = payload.get("pull_request", {})
        repo_data = payload.get("repository", {})
        owner = repo_data.get("owner", {}).get("login", "")
        repo = repo_data.get("name", "")
        pull_number = pr_data.get("number") or payload.get("number")
        pr_identifier = f"{owner}/{repo}/pull/{pull_number}"
        head_sha = pr_data.get("head", {}).get("sha", "")
        idempotency_key = (
            f"github:{x_github_delivery}"
            if x_github_delivery
            else f"github:{pr_identifier}:{head_sha}:{action}"
        )

        checked_target(pr_identifier)
        job_id = queue.enqueue(
            pr_identifier=pr_identifier,
            payload=payload,
            idempotency_key=idempotency_key,
        )
        return {
            "status": "accepted",
            "job_id": job_id,
            "pr": pr_identifier,
            "action": action,
            "message": f"PR #{pull_number} review job durably enqueued (ID: {job_id})",
        }

    # Handle PR merged -> record accepted suggestions into Team Memory (Phase 3)
    if action == "closed":
        pr_data = payload.get("pull_request", {})
        if pr_data.get("merged"):
            repo_data = payload.get("repository", {})
            owner = repo_data.get("owner", {}).get("login", "")
            repo = repo_data.get("name", "")
            pull_number = pr_data.get("number") or payload.get("number")
            logger.info(
                f"🎓 PR #{pull_number} merged in {owner}/{repo}! Queuing learning loop..."
            )
            enqueue_merge(owner=owner, repo=repo, pr_number=pull_number)
            return {
                "status": "accepted",
                "action": "closed",
                "merged": True,
                "message": f"PR #{pull_number} merge processed; conventions recorded into Team Memory.",
            }

    return {
        "status": "ignored",
        "reason": f"Action '{action}' does not require automated review.",
    }


@app.post("/webhook/gitlab")
async def gitlab_webhook(
    request: Request,
    x_gitlab_event: str | None = Header(None),
    x_gitlab_token: str | None = Header(None),
    x_gitlab_event_uuid: str | None = Header(None),
):
    """Receive and process GitLab webhook events (Merge Requests and Notes)."""
    expected_token = os.environ.get("GITLAB_WEBHOOK_SECRET")
    if not expected_token or not hmac.compare_digest(
        (x_gitlab_token or "").encode(), expected_token.encode()
    ):
        logger.error("GitLab webhook token verification failed.")
        raise HTTPException(status_code=401, detail="Invalid GitLab webhook token")

    body_bytes = await request.body()
    payload = json.loads(body_bytes.decode("utf-8"))

    # 1. Handle MR comments & Slash Commands
    if x_gitlab_event == "Note Hook" or payload.get("object_kind") == "note":
        note_data = payload.get("object_attributes", {})
        note_body = note_data.get("note", "").strip()
        author = payload.get("user", {}).get("username", "")
        if author.lower() in _bot_identities():
            return {
                "status": "ignored",
                "reason": "Bot/self note ignored to avoid feedback loop.",
            }
        if not CommandRouter.is_bot_command(note_body):
            return {"status": "ignored", "reason": "Note is not a slash command."}

        mr_data = payload.get("merge_request", {})
        mr_url = mr_data.get("url") or payload.get("project", {}).get("web_url")
        cmd, args = CommandRouter.parse_command(note_body)
        args.strip().split()[0].lower() if args else ""
        logger.info(f"🤖 Processing GitLab slash command '/{cmd}' for {mr_url}...")

        enqueue_command(
            actor=author,
            delivery_id=x_gitlab_event_uuid
            or (
                f"note:{note_data['id']}"
                if note_data.get("id")
                else "body:" + hashlib.sha256(body_bytes).hexdigest()
            ),
            command_text=note_body,
            pr_url=mr_url,
            auto_post=True,
            pr_metadata={"author": author},
        )
        return {
            "status": "accepted",
            "command": cmd,
            "message": "Command scheduled for execution.",
        }

    # 2. Handle Merge Request lifecycle
    if (
        x_gitlab_event == "Merge Request Hook"
        or payload.get("object_kind") == "merge_request"
    ):
        mr_attrs = payload.get("object_attributes", {})
        action = mr_attrs.get("action", "")
        project_data = payload.get("project", {})
        project_path = project_data.get("path_with_namespace", "")
        mr_iid = mr_attrs.get("iid")
        mr_identifier = f"{project_path}/merge_requests/{mr_iid}"

        if action in ("open", "reopen", "update"):
            head_sha = mr_attrs.get("last_commit", {}).get("id", "")
            idempotency_key = (
                f"gitlab:{x_gitlab_event_uuid}"
                if x_gitlab_event_uuid
                else f"gitlab:{mr_identifier}:{head_sha}:{action}"
            )
            checked_target(mr_identifier)
            job_id = queue.enqueue(
                pr_identifier=mr_identifier,
                payload=payload,
                idempotency_key=idempotency_key,
            )
            return {
                "status": "accepted",
                "job_id": job_id,
                "mr": mr_identifier,
                "action": action,
                "message": f"GitLab MR !{mr_iid} review job enqueued (ID: {job_id})",
            }

        if action == "merge" or mr_attrs.get("state") == "merged":
            parts = project_path.split("/")
            owner = parts[0] if parts else "gitlab"
            repo = parts[1] if len(parts) > 1 else "project"
            enqueue_merge(
                owner=owner,
                repo=repo,
                pr_number=mr_iid,
                platform="gitlab",
            )
            return {
                "status": "accepted",
                "action": "merged",
                "message": "Conventions recorded into Team Memory.",
            }

    return {"status": "ignored", "reason": f"GitLab event '{x_gitlab_event}' ignored."}


@app.post("/webhook/bitbucket")
async def bitbucket_webhook(
    request: Request,
    x_event_key: str | None = Header(None),
    x_hook_uuid: str | None = Header(None),
    x_request_uuid: str | None = Header(None),
):
    """Receive and process Bitbucket Cloud webhook events."""
    bb_secret = os.environ.get("BITBUCKET_WEBHOOK_SECRET", "")
    supplied = request.headers.get("x-webhook-token") or request.query_params.get(
        "token", ""
    )
    if not bb_secret or not hmac.compare_digest(supplied.encode(), bb_secret.encode()):
        raise HTTPException(status_code=401, detail="Invalid Bitbucket webhook token")

    body_bytes = await request.body()
    payload = json.loads(body_bytes.decode("utf-8"))

    # 1. Handle PR comments & Slash Commands
    if x_event_key == "pullrequest:comment_created":
        comment = payload.get("comment", {})
        content = comment.get("content", {}).get("raw", "").strip()
        actor = payload.get("actor", {})
        author = (
            actor.get("nickname")
            or actor.get("username")
            or actor.get("display_name", "")
        )
        if (author or "").lower() in _bot_identities():
            return {
                "status": "ignored",
                "reason": "Bot/self comment ignored to avoid feedback loop.",
            }
        if not CommandRouter.is_bot_command(content):
            return {"status": "ignored", "reason": "Comment is not a slash command."}

        pr_data = payload.get("pullrequest", {})
        pr_url = pr_data.get("links", {}).get("html", {}).get("href")
        cmd, args = CommandRouter.parse_command(content)
        args.strip().split()[0].lower() if args else ""
        logger.info(f"🤖 Processing Bitbucket slash command '/{cmd}' for {pr_url}...")

        enqueue_command(
            actor=author,
            delivery_id=x_request_uuid
            or (
                f"comment:{comment['id']}"
                if comment.get("id")
                else "body:" + hashlib.sha256(body_bytes).hexdigest()
            ),
            command_text=content,
            pr_url=pr_url,
            auto_post=True,
            pr_metadata={"author": author},
        )
        return {
            "status": "accepted",
            "command": cmd,
            "message": "Command scheduled for execution.",
        }

    # 2. Handle PR lifecycle
    if x_event_key in ("pullrequest:created", "pullrequest:updated"):
        pr_data = payload.get("pullrequest", {})
        repo_data = payload.get("repository", {})
        repo_full = repo_data.get("full_name", "")
        pr_id = pr_data.get("id")
        pr_identifier = f"{repo_full}/pull-requests/{pr_id}"
        delivery_id = x_request_uuid
        commit_hash = pr_data.get("source", {}).get("commit", {}).get("hash", "")
        idempotency_key = (
            f"bitbucket:{delivery_id}"
            if delivery_id
            else f"bitbucket:{pr_identifier}:{commit_hash}:{x_event_key}"
        )

        checked_target(pr_identifier)
        job_id = queue.enqueue(
            pr_identifier=pr_identifier,
            payload=payload,
            idempotency_key=idempotency_key,
        )
        return {
            "status": "accepted",
            "job_id": job_id,
            "pr": pr_identifier,
            "action": x_event_key,
            "message": f"Bitbucket PR #{pr_id} review job enqueued (ID: {job_id})",
        }

    if x_event_key == "pullrequest:fulfilled":
        pr_data = payload.get("pullrequest", {})
        repo_data = payload.get("repository", {})
        repo_full = repo_data.get("full_name", "")
        parts = repo_full.split("/")
        owner = parts[0] if parts else "bitbucket"
        repo = parts[1] if len(parts) > 1 else "repo"
        pr_id = pr_data.get("id")
        enqueue_merge(owner=owner, repo=repo, pr_number=pr_id, platform="bitbucket")
        return {
            "status": "accepted",
            "action": "fulfilled",
            "message": "Conventions recorded into Team Memory.",
        }

    return {"status": "ignored", "reason": f"Bitbucket event '{x_event_key}' ignored."}


@app.get("/api/benchmark/metrics")
def get_benchmark_metrics():
    """Retrieve calculated benchmark accuracy, precision, recall, and F1 scores."""
    from code_review_agent.benchmarks import BenchmarkRunner

    try:
        runner = BenchmarkRunner()
        metrics = runner.run_deterministic_benchmark()
        return {"status": "success", "metrics": metrics.model_dump()}
    except Exception as e:
        logger.error(f"Error computing benchmark metrics: {e}")
        raise HTTPException(
            status_code=500, detail="Benchmark calculation failed. Check server logs."
        ) from e


@app.get("/api/session")
def operator_session():
    return {"authenticated": True, "role": "operator", "scope": "single-deployment"}


def reserve_review():
    # Authenticated requests share the operator quota. Forwarding headers cannot alter it.
    try:
        rate_limiter.check_rate_limit("operator")
    except RateLimitExceeded as error:
        raise HTTPException(
            429, str(error), headers={"Retry-After": str(error.retry_after)}
        ) from error


async def execute_bounded(operation, payload):
    try:
        return await run_job_async(operation, payload)
    except ReviewBusyError as error:
        raise HTTPException(503, str(error), headers={"Retry-After": "5"}) from error
    except ReviewDeadlineError:
        raise HTTPException(
            504, "Review timed out after 180 seconds; the worker was stopped."
        ) from None
    except ReviewWorkerError as error:
        raise HTTPException(error.status, str(error)) from error


@app.post("/api/bot/command", response_model=BotCommandResponse)
async def execute_bot_command(request_data: BotCommandRequest):
    from urllib.parse import urlsplit

    if request_data.repo_root:
        raise HTTPException(403, "Host repository paths are not accepted by the API.")
    if request_data.pr_url:
        try:
            url = urlsplit(request_data.pr_url)
            port = url.port
        except ValueError:
            raise HTTPException(400, "Invalid pull request URL.") from None
        if (
            url.scheme != "https"
            or url.hostname not in {"github.com", "gitlab.com", "bitbucket.org"}
            or url.username
            or url.password
            or port not in (None, 443)
        ):
            raise HTTPException(
                400, "Use an HTTPS pull request URL on GitHub, GitLab or Bitbucket."
            )
    if request_data.pr_url:
        checked_target(request_data.pr_url)
    if request_data.auto_post and os.getenv("REVIEW_ALLOW_API_POSTING") != "true":
        raise HTTPException(403, "Posting from the API is disabled on this server.")
    reserve_review()
    from code_review_agent.access_policy import effective_command

    cmd, args = CommandRouter.parse_command(request_data.command)
    if request_data.auto_post or effective_command(cmd, args) == "apply":
        result = enqueue_command(
            command_text=request_data.command,
            pr_url=request_data.pr_url,
            trusted_operator=True,
            actor="operator",
            auto_post=request_data.auto_post,
        )
        return JSONResponse(result, status_code=202)
    result = await execute_bounded(
        "bot",
        {
            "trusted_operator": True,
            "actor": "operator",
            "command_text": request_data.command,
            "pr_url": request_data.pr_url,
            "raw_diff": request_data.raw_diff,
            "auto_post": request_data.auto_post,
        },
    )
    return result


async def read_review_input(request, raw_diff, pr_url, file, zip_file):
    if "application/json" in request.headers.get("content-type", ""):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError()
            raw_diff = body.get("raw_diff") or body.get("diff")
            pr_url = body.get("pr_url") or body.get("pr")
            if any(
                value is not None and not isinstance(value, str)
                for value in (raw_diff, pr_url)
            ):
                raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(
                400, "Provide a JSON object with text diff or pull request URL fields."
            ) from None
    if sum(bool(value) for value in (raw_diff, pr_url, file, zip_file)) != 1:
        raise HTTPException(
            400, "Provide exactly one diff, pull request URL, source file or ZIP."
        )
    if raw_diff and len(raw_diff) > 500_000:
        raise HTTPException(413, "Pasted diff exceeds 500,000 characters.")
    if raw_diff:
        from code_review_agent.diff_parser import DiffParser
        from code_review_agent.path_validation import UnsafeDiffPathError

        try:
            DiffParser.parse_diff(raw_diff)
        except UnsafeDiffPathError as error:
            raise HTTPException(422, "Unsafe diff path; review refused.") from error
    if pr_url:
        checked_target(pr_url)
    payload = {
        "raw_diff": raw_diff,
        "pr_url": pr_url,
        "client_ip": "operator",
        "rate_limit_checked": True,
    }
    for upload, field in ((file, "file_bytes"), (zip_file, "zip_bytes")):
        if upload:
            data = await upload.read(2 * 1024 * 1024 + 1)
            if len(data) > 2 * 1024 * 1024:
                raise HTTPException(413, "Uploaded file exceeds the 2 MB limit.")
            payload[field] = data
            if field == "file_bytes":
                payload["file_name"] = upload.filename
    return payload


@app.post("/api/review", response_model=ReviewAPIResponse)
async def api_review(
    request: Request,
    raw_diff: str | None = Form(None),
    pr_url: str | None = Form(None),
    file: UploadFile | None = File(None),
    zip_file: UploadFile | None = File(None),
):
    payload = await read_review_input(request, raw_diff, pr_url, file, zip_file)
    reserve_review()
    if request.headers.get("prefer") == "respond-async":
        try:
            job_id = queue.enqueue(
                pr_identifier=pr_url or payload.get("pr_url") or "Browser review",
                payload=encode_inputs(payload),
                max_retries=1,
                job_kind="browser",
            )
        except ValueError:
            raise HTTPException(
                503,
                "Review queue is full. Try again shortly.",
                headers={"Retry-After": "10"},
            ) from None
        return JSONResponse(
            {"job_id": job_id, "status": "QUEUED"},
            status_code=202,
            headers={"Location": f"/jobs/{job_id}/result"},
        )
    return await execute_bounded("review", payload)


@app.post("/api/review/stream")
async def api_review_stream(
    request: Request,
    raw_diff: str | None = Form(None),
    pr_url: str | None = Form(None),
    file: UploadFile | None = File(None),
    zip_file: UploadFile | None = File(None),
):
    payload = await read_review_input(request, raw_diff, pr_url, file, zip_file)
    reserve_review()

    async def events():
        # No stage completion or percentage is fabricated.
        yield 'event: step\ndata: {"stage":"RUNNING","message":"Review worker requested; waiting for analysis."}\n\n'
        try:
            result = await execute_bounded("review", payload)
            yield f"event: complete\ndata: {json.dumps(result)}\n\n"
        except HTTPException as error:
            yield f"event: error\ndata: {json.dumps({'error': error.detail})}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/benchmark")
def run_benchmark():
    """Execute deterministic benchmark suite and return metrics and markdown summary."""
    from code_review_agent.benchmarks import BenchmarkRunner

    runner = BenchmarkRunner()
    metrics = runner.run_sast_benchmark()
    summary_md = BenchmarkRunner.format_summary_table(metrics)
    return {"metrics": metrics.model_dump(), "summary_markdown": summary_md}


@app.get("/api/cache/stats")
def get_cache_stats():
    """Retrieve cost optimization and memoization cache statistics."""
    from code_review_agent.cache import global_tool_cache

    return {
        "stats": global_tool_cache.stats.model_dump(),
        "hit_ratio": global_tool_cache.stats.hit_ratio,
    }


# Static file serving and UI routes (Prefers React Vite build in frontend/dist, fallbacks to static/)
repo_root = Path(__file__).resolve().parent.parent.parent
dist_dir = Path(os.getenv("REVIEW_STATIC_DIR", str(repo_root / "frontend" / "dist")))
legacy_static_dir = Path(__file__).resolve().parent / "static"

if dist_dir.exists() and (dist_dir / "assets").exists():
    app.mount("/assets", StaticFiles(directory=str(dist_dir / "assets")), name="assets")

if legacy_static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(legacy_static_dir)), name="static")


@app.get("/", response_class=FileResponse)
async def serve_ui_root():
    """Serve the single-page Code Review Web UI (Vite React or legacy static)."""
    if dist_dir.exists() and (dist_dir / "index.html").exists():
        return FileResponse(str(dist_dir / "index.html"))
    index_file = legacy_static_dir / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return HTMLResponse(
        "<h1>AI Code Review Agent</h1><h2>Submit Code for Review</h2><p>UI static files not found.</p>"
    )


@app.get("/ui", response_class=FileResponse)
async def serve_ui():
    """Serve the single-page Code Review Web UI at /ui."""
    if dist_dir.exists() and (dist_dir / "index.html").exists():
        return FileResponse(str(dist_dir / "index.html"))
    index_file = legacy_static_dir / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return HTMLResponse(
        "<h1>AI Code Review Agent</h1><h2>Submit Code for Review</h2><p>UI static files not found.</p>"
    )


def start_server(host: str = "127.0.0.1", port: int = 8000):
    """Launch the FastAPI Webhook Gateway with Uvicorn."""
    logger.info(
        f"Starting AI Code Review Webhook Gateway on http://localhost:{port}..."
    )
    uvicorn.run(
        "code_review_agent.webhook_server:app", host=host, port=port, reload=False
    )


if __name__ == "__main__":
    start_server()
