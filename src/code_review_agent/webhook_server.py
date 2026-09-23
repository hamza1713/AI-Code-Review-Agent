"""
FastAPI Webhook Gateway for GitHub Pull Request Ingestion.
Validates HMAC SHA-256 signatures, enqueues review jobs into a persistent task queue,
and executes asynchronous multi-agent reviews with retries and crash recovery.
"""

import os
import asyncio
import hmac
import hashlib
import json
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Dict, Any, Optional
from pydantic import BaseModel
from fastapi import FastAPI, Request, Header, HTTPException, Query, File, UploadFile, Form, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn


from code_review_agent.gateway_security import GatewaySecurityMiddleware
from code_review_agent.review_executor import run_job_async, ReviewBusyError, ReviewDeadlineError, ReviewWorkerError, encode_inputs, run_job

from code_review_agent.config import get_webhook_secret, logger
from code_review_agent.webhook_queue import WebhookJobQueue, WebhookWorker
from code_review_agent.models import ReviewAPIResponse
from code_review_agent.bot import CommandRouter
from code_review_agent.review_service import (
    ReviewService,
    RateLimitExceeded,
    InputValidationError,
    rate_limiter
)

# Global persistent queue and worker instances
queue = WebhookJobQueue()
worker = WebhookWorker(queue=queue, poll_interval=1.0, max_workers=2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage lifecycle of background queue worker and crash recovery."""
    logger.info("🚀 Starting Webhook Task Queue Worker and recovering orphan jobs...")
    worker.start()
    yield
    logger.info("🛑 Shutting down Webhook Task Queue Worker...")
    worker.stop(timeout=5.0)


app = FastAPI(
    title="AI Code Review Agent Webhook Gateway",
    description="Automated multi-agent code review service with durable job persistence for GitHub Pull Requests",
    version="2.0.0",
    lifespan=lifespan
)


app.add_middleware(GatewaySecurityMiddleware)

def verify_github_signature(payload_body: bytes, signature_header: Optional[str]) -> bool:
    """Verify HMAC SHA-256 signature from GitHub webhook request."""
    secret = get_webhook_secret()
    if not secret:
        logger.error("GITHUB_WEBHOOK_SECRET is not configured; webhook rejected.")
        return False

    if not signature_header or not signature_header.startswith("sha256="):
        return False

    expected_signature = "sha256=" + hmac.new(
        key=secret.encode("utf-8"),
        msg=payload_body,
        digestmod=hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(expected_signature, signature_header)


def dispatch_webhook_command(**payload):
    """Trusted signed webhook commands share bounded process capacity."""
    try:
        return run_job("bot", payload)
    except (ReviewBusyError, ReviewDeadlineError, ReviewWorkerError) as error:
        logger.error("Webhook command could not complete: %s", error)


def _bot_allowed_associations() -> set:
    """Author associations permitted to run non-help bot commands (configurable)."""
    raw = os.getenv("BOT_ALLOWED_ASSOCIATIONS", "OWNER,MEMBER,COLLABORATOR")
    return {a.strip().upper() for a in raw.split(",") if a.strip()}


def _bot_maintainer_associations() -> set:
    """Author associations permitted to run privileged remediation/suppression commands (maintainers only)."""
    raw = os.getenv("BOT_MAINTAINER_ASSOCIATIONS", "OWNER,MEMBER")
    return {a.strip().upper() for a in raw.split(",") if a.strip()}


def _is_authorized_commenter(comment: Dict[str, Any], command_name: str, subcommand: Optional[str] = None) -> bool:
    """
    Decide whether a PR commenter may run a given slash command.

    `/help` is read-only and free, so anyone may run it.
    `/apply`, `/suppress`, `/unsuppress` mutate state or code and are strictly restricted
    to repository maintainers (OWNER, MEMBER).
    Every other command spends LLM tokens and/or writes to the PR with the bot's token,
    so it is restricted to trusted author associations (repo owner, org member, or collaborator by default).
    """
    if command_name == "help":
        return True
    association = (comment.get("author_association") or "NONE").upper()
    effective_cmd = subcommand if (command_name == "review" and subcommand) else command_name
    if effective_cmd in ("apply", "suppress", "unsuppress"):
        return association in _bot_maintainer_associations()
    return association in _bot_allowed_associations()


def _bot_identities() -> set:
    """Usernames whose comments are the bot's own — skipped to avoid feedback loops."""
    raw = f"{os.getenv('BOT_GITHUB_LOGIN', '')},{os.getenv('BOT_BOT_USERNAMES', '')}"
    return {u.strip().lower() for u in raw.split(",") if u.strip()}


def _username_authorized(username: str, command_name: str, subcommand: Optional[str] = None) -> bool:
    """
    Authorize a slash command by username allowlist. GitLab and Bitbucket webhooks do
    not carry GitHub's author_association, so trusted users are listed in BOT_ALLOWED_USERS.
    Privileged commands (/apply, /suppress, /unsuppress) can be restricted via BOT_MAINTAINER_USERS.
    """
    if command_name == "help":
        return True
    effective_cmd = subcommand if (command_name == "review" and subcommand) else command_name
    if effective_cmd in ("apply", "suppress", "unsuppress"):
        maint_raw = os.getenv("BOT_MAINTAINER_USERS", "")
        if maint_raw.strip():
            maint_allow = {u.strip().lower() for u in maint_raw.split(",") if u.strip()}
            return (username or "").lower() in maint_allow
    allow = {u.strip().lower() for u in os.getenv("BOT_ALLOWED_USERS", "").split(",") if u.strip()}
    return bool(allow) and (username or "").lower() in allow


@app.get("/health")
def health_check():
    """Health check endpoint for monitoring and container orchestration."""
    metrics = queue.get_queue_metrics()
    return {
        "status": "healthy",
        "service": "code_review_agent",
        "version": "2.0.0",
        "queue": {
            "queued": metrics.get("queued", 0),
            "processing": metrics.get("processing", 0),
            "retrying": metrics.get("retrying", 0),
            "active_workers": metrics.get("active_workers", 0),
        }
    }


@app.get("/jobs")
def get_jobs(
    status: Optional[str] = Query(None, description="Filter by job status: QUEUED, PROCESSING, COMPLETED, FAILED, RETRYING, CANCELLED"),
    limit: int = Query(50, ge=1, le=200, description="Max jobs to return"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
):
    """List persistent webhook review jobs with pagination."""
    return {"jobs": queue.list_jobs(status=status, limit=limit, offset=offset)}


@app.get("/jobs/metrics")
def get_jobs_metrics():
    """Retrieve real-time queue depth, active worker count, and processing telemetry."""
    return queue.get_queue_metrics()


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
            detail=f"Job '{job_id}' is in status '{job.get('status')}' and cannot be cancelled."
        )
    return {"status": "CANCELLED", "job_id": job_id, "message": "Job successfully cancelled."}


@app.get("/jobs/{job_id}/result")
def get_job_result(job_id: str):
    """Retrieve completed review result for a webhook job."""
    job = queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    if job.get("status") != "COMPLETED":
        return JSONResponse(
            status_code=202,
            content={"status": job.get("status"), "message": f"Job is currently {job.get('status')}"}
        )
    return job.get("result") or {"message": "No result payload stored for this job"}


class SuppressionCreateRequest(BaseModel):
    repo_id: str
    fingerprint: str
    reason: str = ""
    author: str = "operator"
    pr_id: str = ""


@app.get("/api/suppressions")
def list_suppressions(repo: Optional[str] = Query(None, description="Repository identifier (owner/repo)")):
    """List active finding suppressions."""
    from code_review_agent.suppression_store import SuppressionStore
    store = SuppressionStore()
    return {"suppressions": store.list_suppressions(repo_id=repo)}


@app.post("/api/suppressions")
def create_suppression(req: SuppressionCreateRequest):
    """Suppress a finding fingerprint for a repository."""
    from code_review_agent.suppression_store import SuppressionStore
    store = SuppressionStore()
    store.suppress(
        repo_id=req.repo_id,
        fingerprint=req.fingerprint,
        reason=req.reason,
        author=req.author,
        pr_id=req.pr_id
    )
    return {
        "status": "success",
        "message": f"Finding '{req.fingerprint}' successfully suppressed for '{req.repo_id}'."
    }


@app.delete("/api/suppressions/{fingerprint}")
def delete_suppression(
    fingerprint: str,
    repo: str = Query(..., description="Repository identifier (owner/repo)")
):
    """Unsuppress a finding fingerprint."""
    from code_review_agent.suppression_store import SuppressionStore
    store = SuppressionStore()
    deleted = store.unsuppress(repo_id=repo, fingerprint=fingerprint)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Suppression for '{fingerprint}' in '{repo}' not found.")
    return {"status": "success", "message": f"Finding '{fingerprint}' unsuppressed for '{repo}'."}


class RemediationApplyRequest(BaseModel):
    pr_identifier: str
    fingerprint: str
    custom_message: Optional[str] = None
    replacement_code: Optional[str] = None


@app.post("/api/remediation/apply")
def apply_remediation(req: RemediationApplyRequest):
    """Apply automated 1-click remediation commit directly to a PR branch."""
    from code_review_agent.remediator import AutoRemediator
    result = AutoRemediator.apply_remediation(
        pr_identifier=req.pr_identifier,
        fingerprint=req.fingerprint,
        custom_message=req.custom_message,
        replacement_code=req.replacement_code
    )
    if result.get("status") != "success":
        raise HTTPException(
            status_code=400,
            detail=result.get("message", "Failed to apply remediation")
        )
    return result


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
            with httpx.Client(headers={"Authorization": f"Bearer {token}", "User-Agent": "AI-Code-Review-Agent"}, timeout=5.0) as client:
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
        "test_events_enabled": os.getenv("REVIEW_ENABLE_TEST_EVENTS") == "true"
    }


@app.post("/api/webhook/test-event")
async def trigger_test_webhook():
    """Trigger a simulated GitHub PR webhook event directly into the durable queue for live UI testing."""
    if os.getenv("REVIEW_ENABLE_TEST_EVENTS") != "true":
        raise HTTPException(status_code=403, detail="Test events are disabled on this server.")
    sample_payload = {
        "action": "opened",
        "number": 42,
        "pull_request": {
            "number": 42,
            "title": "feat: add user authentication with parameterized queries",
            "user": {"login": "developer-demo"},
            "head": {"sha": "c0ffee123", "ref": "feat/auth"},
            "base": {"sha": "b45e456", "ref": "main"},
            "html_url": "https://github.com/demo/repo/pull/42"
        },
        "repository": {
            "name": "demo-repo",
            "owner": {"login": "demo-org"}
        }
    }
    pr_id = "demo-org/demo-repo/pull/42"
    job_id = queue.enqueue(pr_identifier=pr_id, payload=sample_payload)
    return {
        "status": "accepted",
        "job_id": job_id,
        "pr": pr_id,
        "message": "Simulated GitHub Pull Request #42 webhook enqueued successfully."
    }



class BotCommandRequest(BaseModel):
    command: str
    pr_url: Optional[str] = None
    raw_diff: Optional[str] = None
    repo_root: Optional[str] = None
    auto_post: bool = False


class BotCommandResponse(BaseModel):
    command: str
    status: str
    response_markdown: str
    action_taken: str
    metadata: Dict[str, Any] = {}


@app.post("/webhook/github")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_github_event: Optional[str] = Header(None),
    x_github_delivery: Optional[str] = Header(None),
    x_hub_signature_256: Optional[str] = Header(None)
):
    """Receive, authenticate, and durably enqueue incoming GitHub webhook events."""
    body_bytes = await request.body()

    if not verify_github_signature(body_bytes, x_hub_signature_256):
        logger.error("GitHub webhook signature verification failed.")
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    payload = json.loads(body_bytes.decode("utf-8"))

    # 1. Handle PR comments & Slash Commands
    if x_github_event == "issue_comment":
        action = payload.get("action", "")
        if action != "created":
            return {"status": "ignored", "reason": f"Comment action '{action}' does not require processing."}

        issue = payload.get("issue", {})
        if "pull_request" not in issue:
            return {"status": "ignored", "reason": "Comment is on an issue, not a pull request."}

        comment = payload.get("comment", {})
        user = comment.get("user", {})
        login = user.get("login", "")
        # Feedback-loop guard: skip GitHub App bots and this bot's own account.
        own_login = os.getenv("BOT_GITHUB_LOGIN", "").strip().lower()
        if user.get("type") == "Bot" or "[bot]" in login.lower() or (own_login and login.lower() == own_login):
            return {"status": "ignored", "reason": "Bot/self comment ignored to avoid feedback loop."}

        comment_body = comment.get("body", "").strip()
        if not CommandRouter.is_bot_command(comment_body):
            return {"status": "ignored", "reason": "Comment is not a slash command."}

        cmd, args = CommandRouter.parse_command(comment_body)
        first_token = args.strip().split()[0].lower() if args else ""
        subcmd = first_token if first_token in ("apply", "suppress", "unsuppress", "rerun", "explain") else None

        # Authorization: only trusted associations may run cost-bearing / writing commands.
        # Mutating commands (/apply, /suppress, /unsuppress) require maintainer role (OWNER, MEMBER).
        if not _is_authorized_commenter(comment, cmd, subcommand=subcmd):
            logger.warning(
                f"Rejected '/{cmd}{(' ' + subcmd) if subcmd else ''}' from '{login}' "
                f"(association={comment.get('author_association')}) — not authorized."
            )
            return {"status": "ignored", "reason": "Commenter is not authorized to run this command."}

        pr_html_url = issue.get("pull_request", {}).get("html_url") or issue.get("html_url")
        logger.info(f"🤖 Queuing PR slash command '/{cmd}' for {pr_html_url}...")

        # Run off the request path: slash commands do GitHub + LLM work (a full /review
        # can take minutes). GitHub expects the webhook to be acknowledged within
        # seconds or it retries the delivery — which would re-trigger the command.
        background_tasks.add_task(
            dispatch_webhook_command,
            command_text=comment_body,
            pr_url=pr_html_url,
            auto_post=True,
            author_association=comment.get("author_association"),
            pr_metadata={"author_association": comment.get("author_association"), "author": login},
        )
        return {"status": "accepted", "command": cmd, "message": "Command accepted and processing in the background."}

    # 2. Handle PR lifecycle reviews
    if x_github_event != "pull_request":
        return {"status": "ignored", "reason": f"Event '{x_github_event}' is not handled."}

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
        idempotency_key = f"github:{x_github_delivery}" if x_github_delivery else f"github:{pr_identifier}:{head_sha}:{action}"

        job_id = queue.enqueue(pr_identifier=pr_identifier, payload=payload, idempotency_key=idempotency_key)
        return {
            "status": "accepted",
            "job_id": job_id,
            "pr": pr_identifier,
            "action": action,
            "message": f"PR #{pull_number} review job durably enqueued (ID: {job_id})"
        }

    # Handle PR merged -> record accepted suggestions into Team Memory (Phase 3)
    if action == "closed":
        pr_data = payload.get("pull_request", {})
        if pr_data.get("merged"):
            repo_data = payload.get("repository", {})
            owner = repo_data.get("owner", {}).get("login", "")
            repo = repo_data.get("name", "")
            pull_number = pr_data.get("number") or payload.get("number")
            logger.info(f"🎓 PR #{pull_number} merged in {owner}/{repo}! Queuing learning loop...")
            from code_review_agent.learning.suggestion_tracker import SuggestionTracker
            background_tasks.add_task(
                SuggestionTracker().process_merged_pr,
                owner=owner,
                repo=repo,
                pr_number=pull_number
            )
            return {
                "status": "accepted",
                "action": "closed",
                "merged": True,
                "message": f"PR #{pull_number} merge processed; conventions recorded into Team Memory."
            }

    return {"status": "ignored", "reason": f"Action '{action}' does not require automated review."}


@app.post("/webhook/gitlab")
async def gitlab_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_gitlab_event: Optional[str] = Header(None),
    x_gitlab_token: Optional[str] = Header(None),
    x_gitlab_event_uuid: Optional[str] = Header(None)
):
    """Receive and process GitLab webhook events (Merge Requests and Notes)."""
    expected_token = os.environ.get("GITLAB_WEBHOOK_SECRET")
    if not expected_token or not hmac.compare_digest((x_gitlab_token or "").encode(), expected_token.encode()):
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
            return {"status": "ignored", "reason": "Bot/self note ignored to avoid feedback loop."}
        if not CommandRouter.is_bot_command(note_body):
            return {"status": "ignored", "reason": "Note is not a slash command."}

        mr_data = payload.get("merge_request", {})
        mr_url = mr_data.get("url") or payload.get("project", {}).get("web_url")
        cmd, args = CommandRouter.parse_command(note_body)
        first_token = args.strip().split()[0].lower() if args else ""
        subcmd = first_token if first_token in ("apply", "suppress", "unsuppress", "rerun", "explain") else None
        if not _username_authorized(author, cmd, subcommand=subcmd):
            logger.warning(f"Rejected GitLab '/{cmd}' from '{author}' — not authorized.")
            return {"status": "ignored", "reason": "Commenter is not authorized to run this command."}
        logger.info(f"🤖 Processing GitLab slash command '/{cmd}' for {mr_url}...")

        background_tasks.add_task(
            dispatch_webhook_command,
            command_text=note_body,
            pr_url=mr_url,
            auto_post=True,
            pr_metadata={"author": author},
        )
        return {"status": "accepted", "command": cmd, "message": "Command scheduled for execution."}

    # 2. Handle Merge Request lifecycle
    if x_gitlab_event == "Merge Request Hook" or payload.get("object_kind") == "merge_request":
        mr_attrs = payload.get("object_attributes", {})
        action = mr_attrs.get("action", "")
        project_data = payload.get("project", {})
        project_path = project_data.get("path_with_namespace", "")
        mr_iid = mr_attrs.get("iid")
        mr_identifier = f"{project_path}/merge_requests/{mr_iid}"

        if action in ("open", "reopen", "update"):
            head_sha = mr_attrs.get("last_commit", {}).get("id", "")
            idempotency_key = f"gitlab:{x_gitlab_event_uuid}" if x_gitlab_event_uuid else f"gitlab:{mr_identifier}:{head_sha}:{action}"
            job_id = queue.enqueue(pr_identifier=mr_identifier, payload=payload, idempotency_key=idempotency_key)
            return {
                "status": "accepted",
                "job_id": job_id,
                "mr": mr_identifier,
                "action": action,
                "message": f"GitLab MR !{mr_iid} review job enqueued (ID: {job_id})"
            }

        if action == "merge" or mr_attrs.get("state") == "merged":
            from code_review_agent.learning.suggestion_tracker import SuggestionTracker
            parts = project_path.split("/")
            owner = parts[0] if parts else "gitlab"
            repo = parts[1] if len(parts) > 1 else "project"
            background_tasks.add_task(
                SuggestionTracker().process_merged_pr,
                owner=owner,
                repo=repo,
                pr_number=mr_iid,
                platform="gitlab",
            )
            return {"status": "accepted", "action": "merged", "message": "Conventions recorded into Team Memory."}

    return {"status": "ignored", "reason": f"GitLab event '{x_gitlab_event}' ignored."}


@app.post("/webhook/bitbucket")
async def bitbucket_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_event_key: Optional[str] = Header(None),
    x_hook_uuid: Optional[str] = Header(None),
    x_request_uuid: Optional[str] = Header(None)
):
    """Receive and process Bitbucket Cloud webhook events."""
    bb_secret = os.environ.get("BITBUCKET_WEBHOOK_SECRET", "")
    supplied = request.headers.get("x-webhook-token") or request.query_params.get("token", "")
    if not bb_secret or not hmac.compare_digest(supplied.encode(), bb_secret.encode()):
        raise HTTPException(status_code=401, detail="Invalid Bitbucket webhook token")

    body_bytes = await request.body()
    payload = json.loads(body_bytes.decode("utf-8"))

    # 1. Handle PR comments & Slash Commands
    if x_event_key == "pullrequest:comment_created":
        comment = payload.get("comment", {})
        content = comment.get("content", {}).get("raw", "").strip()
        actor = payload.get("actor", {})
        author = actor.get("nickname") or actor.get("username") or actor.get("display_name", "")
        if (author or "").lower() in _bot_identities():
            return {"status": "ignored", "reason": "Bot/self comment ignored to avoid feedback loop."}
        if not CommandRouter.is_bot_command(content):
            return {"status": "ignored", "reason": "Comment is not a slash command."}

        pr_data = payload.get("pullrequest", {})
        pr_url = pr_data.get("links", {}).get("html", {}).get("href")
        cmd, args = CommandRouter.parse_command(content)
        first_token = args.strip().split()[0].lower() if args else ""
        subcmd = first_token if first_token in ("apply", "suppress", "unsuppress", "rerun", "explain") else None
        if not _username_authorized(author, cmd, subcommand=subcmd):
            logger.warning(f"Rejected Bitbucket '/{cmd}' from '{author}' — not authorized.")
            return {"status": "ignored", "reason": "Commenter is not authorized to run this command."}
        logger.info(f"🤖 Processing Bitbucket slash command '/{cmd}' for {pr_url}...")

        background_tasks.add_task(
            dispatch_webhook_command,
            command_text=content,
            pr_url=pr_url,
            auto_post=True,
            pr_metadata={"author": author},
        )
        return {"status": "accepted", "command": cmd, "message": "Command scheduled for execution."}

    # 2. Handle PR lifecycle
    if x_event_key in ("pullrequest:created", "pullrequest:updated"):
        pr_data = payload.get("pullrequest", {})
        repo_data = payload.get("repository", {})
        repo_full = repo_data.get("full_name", "")
        pr_id = pr_data.get("id")
        pr_identifier = f"{repo_full}/pull-requests/{pr_id}"
        delivery_id = x_hook_uuid or x_request_uuid
        commit_hash = pr_data.get("source", {}).get("commit", {}).get("hash", "")
        idempotency_key = f"bitbucket:{delivery_id}" if delivery_id else f"bitbucket:{pr_identifier}:{commit_hash}:{x_event_key}"

        job_id = queue.enqueue(pr_identifier=pr_identifier, payload=payload, idempotency_key=idempotency_key)
        return {
            "status": "accepted",
            "job_id": job_id,
            "pr": pr_identifier,
            "action": x_event_key,
            "message": f"Bitbucket PR #{pr_id} review job enqueued (ID: {job_id})"
        }

    if x_event_key == "pullrequest:fulfilled":
        pr_data = payload.get("pullrequest", {})
        repo_data = payload.get("repository", {})
        repo_full = repo_data.get("full_name", "")
        parts = repo_full.split("/")
        owner = parts[0] if parts else "bitbucket"
        repo = parts[1] if len(parts) > 1 else "repo"
        pr_id = pr_data.get("id")
        from code_review_agent.learning.suggestion_tracker import SuggestionTracker
        background_tasks.add_task(
            SuggestionTracker().process_merged_pr,
            owner=owner,
            repo=repo,
            pr_number=pr_id
        )
        return {"status": "accepted", "action": "fulfilled", "message": "Conventions recorded into Team Memory."}

    return {"status": "ignored", "reason": f"Bitbucket event '{x_event_key}' ignored."}


@app.get("/api/benchmark/metrics")
def get_benchmark_metrics():
    """Retrieve calculated benchmark accuracy, precision, recall, and F1 scores."""
    from code_review_agent.benchmarks import BenchmarkRunner
    try:
        runner = BenchmarkRunner()
        metrics = runner.run_deterministic_benchmark()
        return {
            "status": "success",
            "metrics": metrics.model_dump()
        }
    except Exception as e:
        logger.error(f"Error computing benchmark metrics: {e}")
        raise HTTPException(status_code=500, detail="Benchmark calculation failed. Check server logs.")



@app.get("/api/session")
def operator_session():
    return {"authenticated": True, "role": "operator", "scope": "single-deployment"}


def reserve_review():
    # Authenticated requests share the operator quota. Forwarding headers cannot alter it.
    try:
        rate_limiter.check_rate_limit("operator")
    except RateLimitExceeded as error:
        raise HTTPException(429, str(error), headers={"Retry-After": str(error.retry_after)})


async def execute_bounded(operation, payload):
    try:
        return await run_job_async(operation, payload)
    except ReviewBusyError as error:
        raise HTTPException(503, str(error), headers={"Retry-After": "5"})
    except ReviewDeadlineError:
        raise HTTPException(504, "Review timed out after 180 seconds; the worker was stopped.")
    except ReviewWorkerError as error:
        raise HTTPException(error.status, str(error))


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
            raise HTTPException(400, "Invalid pull request URL.")
        if (url.scheme != "https" or url.hostname not in {"github.com", "gitlab.com", "bitbucket.org"}
                or url.username or url.password or port not in (None, 443)):
            raise HTTPException(400, "Use an HTTPS pull request URL on GitHub, GitLab or Bitbucket.")
    if request_data.auto_post and os.getenv("REVIEW_ALLOW_API_POSTING") != "true":
        raise HTTPException(403, "Posting from the API is disabled on this server.")
    reserve_review()
    result = await execute_bounded("bot", {
        "command_text": request_data.command, "pr_url": request_data.pr_url,
        "raw_diff": request_data.raw_diff, "auto_post": request_data.auto_post})
    return result


async def read_review_input(request, raw_diff, pr_url, file, zip_file):
    if "application/json" in request.headers.get("content-type", ""):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError()
            raw_diff = body.get("raw_diff") or body.get("diff")
            pr_url = body.get("pr_url") or body.get("pr")
            if any(value is not None and not isinstance(value, str) for value in (raw_diff, pr_url)):
                raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(400, "Provide a JSON object with text diff or pull request URL fields.")
    if sum(bool(value) for value in (raw_diff, pr_url, file, zip_file)) != 1:
        raise HTTPException(400, "Provide exactly one diff, pull request URL, source file or ZIP.")
    if raw_diff and len(raw_diff) > 500_000:
        raise HTTPException(413, "Pasted diff exceeds 500,000 characters.")
    payload = {"raw_diff": raw_diff, "pr_url": pr_url, "client_ip": "operator", "rate_limit_checked": True}
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
async def api_review(request: Request, raw_diff: Optional[str] = Form(None),
    pr_url: Optional[str] = Form(None), file: Optional[UploadFile] = File(None),
    zip_file: Optional[UploadFile] = File(None)):
    payload = await read_review_input(request, raw_diff, pr_url, file, zip_file)
    reserve_review()
    if request.headers.get("prefer") == "respond-async":
        try:
            job_id = queue.enqueue(pr_identifier=pr_url or payload.get("pr_url") or "Browser review",
                payload=encode_inputs(payload), max_retries=1, job_kind="browser")
        except ValueError:
            raise HTTPException(503, "Review queue is full. Try again shortly.", headers={"Retry-After": "10"})
        return JSONResponse({"job_id": job_id, "status": "QUEUED"}, status_code=202,
            headers={"Location": f"/jobs/{job_id}/result"})
    return await execute_bounded("review", payload)


@app.post("/api/review/stream")
async def api_review_stream(request: Request, raw_diff: Optional[str] = Form(None),
    pr_url: Optional[str] = Form(None), file: Optional[UploadFile] = File(None),
    zip_file: Optional[UploadFile] = File(None)):
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
    return StreamingResponse(events(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/benchmark")
def run_benchmark():
    """Execute deterministic benchmark suite and return metrics and markdown summary."""
    from code_review_agent.benchmarks import BenchmarkRunner
    runner = BenchmarkRunner()
    metrics = runner.run_sast_benchmark()
    summary_md = BenchmarkRunner.format_summary_table(metrics)
    return {
        "metrics": metrics.model_dump(),
        "summary_markdown": summary_md
    }


@app.get("/api/cache/stats")
def get_cache_stats():
    """Retrieve cost optimization and memoization cache statistics."""
    from code_review_agent.cache import global_tool_cache
    return {
        "stats": global_tool_cache.stats.model_dump(),
        "hit_ratio": global_tool_cache.stats.hit_ratio
    }



# Static file serving and UI routes (Prefers React Vite build in frontend/dist, fallbacks to static/)
repo_root = Path(__file__).resolve().parent.parent.parent
dist_dir = repo_root / "frontend" / "dist"
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
    return HTMLResponse("<h1>AI Code Review Agent</h1><h2>Submit Code for Review</h2><p>UI static files not found.</p>")


@app.get("/ui", response_class=FileResponse)
async def serve_ui():
    """Serve the single-page Code Review Web UI at /ui."""
    if dist_dir.exists() and (dist_dir / "index.html").exists():
        return FileResponse(str(dist_dir / "index.html"))
    index_file = legacy_static_dir / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return HTMLResponse("<h1>AI Code Review Agent</h1><h2>Submit Code for Review</h2><p>UI static files not found.</p>")



def start_server(host: str = "127.0.0.1", port: int = 8000):
    """Launch the FastAPI Webhook Gateway with Uvicorn."""
    logger.info(f"Starting AI Code Review Webhook Gateway on http://localhost:{port}...")
    uvicorn.run("code_review_agent.webhook_server:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    start_server()


