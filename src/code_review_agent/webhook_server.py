"""
FastAPI Webhook Gateway for GitHub Pull Request Ingestion.
Validates HMAC SHA-256 signatures, enqueues review jobs into a persistent task queue,
and executes asynchronous multi-agent reviews with retries and crash recovery.
"""

import asyncio
import hmac
import hashlib
import json
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Dict, Any, Optional
from fastapi import FastAPI, Request, Header, HTTPException, Query, File, UploadFile, Form
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import uvicorn


from code_review_agent.config import get_webhook_secret, logger
from code_review_agent.github_client import GitHubClient
from code_review_agent.webhook_queue import WebhookJobQueue, WebhookWorker
from code_review_agent.models import ReviewAPIResponse
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


def verify_github_signature(payload_body: bytes, signature_header: Optional[str]) -> bool:
    """Verify HMAC SHA-256 signature from GitHub webhook request."""
    secret = get_webhook_secret()
    if not secret:
        logger.warning("GITHUB_WEBHOOK_SECRET is not configured; skipping signature verification.")
        return True

    if not signature_header or not signature_header.startswith("sha256="):
        return False

    expected_signature = "sha256=" + hmac.new(
        key=secret.encode("utf-8"),
        msg=payload_body,
        digestmod=hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(expected_signature, signature_header)


@app.get("/health")
def health_check():
    """Health check endpoint for monitoring and container orchestration."""
    queued_jobs = len(queue.list_jobs(status="QUEUED"))
    processing_jobs = len(queue.list_jobs(status="PROCESSING"))
    return {
        "status": "healthy",
        "service": "code_review_agent",
        "version": "2.0.0",
        "queue": {
            "queued": queued_jobs,
            "processing": processing_jobs
        }
    }


@app.get("/jobs")
def get_jobs(status: Optional[str] = Query(None, description="Filter by job status: QUEUED, PROCESSING, COMPLETED, FAILED, RETRYING")):
    """List persistent webhook review jobs."""
    return {"jobs": queue.list_jobs(status=status)}


@app.get("/jobs/{job_id}")
def get_job_detail(job_id: str):
    """Retrieve details for a specific webhook job."""
    job = queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return job


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

    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "localhost:8000"
    scheme = request.headers.get("x-forwarded-proto") or ("https" if "ngrok" in host or "tunnel" in host else "http")
    webhook_url = f"{scheme}://{host}/webhook/github"

    return {
        "webhook_url": webhook_url,
        "secret_configured": bool(secret),
        "token_configured": bool(token),
        "github_connected": github_connected,
        "github_user": github_user
    }


@app.post("/api/webhook/test-event")
async def trigger_test_webhook():
    """Trigger a simulated GitHub PR webhook event directly into the durable queue for live UI testing."""
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



@app.post("/webhook/github")
async def github_webhook(
    request: Request,
    x_github_event: Optional[str] = Header(None),
    x_hub_signature_256: Optional[str] = Header(None)
):
    """Receive, authenticate, and durably enqueue incoming GitHub webhook events."""
    body_bytes = await request.body()

    if not verify_github_signature(body_bytes, x_hub_signature_256):
        logger.error("GitHub webhook signature verification failed.")
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    if x_github_event != "pull_request":
        return {"status": "ignored", "reason": f"Event '{x_github_event}' is not a pull_request event."}

    payload = json.loads(body_bytes.decode("utf-8"))
    action = payload.get("action", "")

    # Trigger only on actionable PR lifecycle events
    if action in ["opened", "synchronize", "reopened"]:
        pr_data = payload.get("pull_request", {})
        repo_data = payload.get("repository", {})
        owner = repo_data.get("owner", {}).get("login", "")
        repo = repo_data.get("name", "")
        pull_number = pr_data.get("number") or payload.get("number")
        pr_identifier = f"{owner}/{repo}/pull/{pull_number}"

        job_id = queue.enqueue(pr_identifier=pr_identifier, payload=payload)
        return {
            "status": "accepted",
            "job_id": job_id,
            "pr": pr_identifier,
            "action": action,
            "message": f"PR #{pull_number} review job durably enqueued (ID: {job_id})"
        }

    return {"status": "ignored", "reason": f"Action '{action}' does not require automated review."}


@app.post("/api/review", response_model=ReviewAPIResponse)
async def api_review(
    request: Request,
    raw_diff: Optional[str] = Form(None),
    pr_url: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    zip_file: Optional[UploadFile] = File(None)
):
    """
    Synchronous in-browser review endpoint.
    Accepts raw git diff, live GitHub PR URL, single file upload, or small zip archive (<=2MB, <=20 files).
    Bypasses the durable webhook queue for fast, synchronous review with a 180s timeout.
    """
    client_ip = request.client.host if request.client else "127.0.0.1"
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        client_ip = forwarded_for.split(",")[0].strip()

    # Determine input type
    content_type = request.headers.get("content-type", "")
    pasted_diff = raw_diff
    target_pr_url = pr_url
    file_bytes = None
    file_name = None
    zip_bytes = None

    if "application/json" in content_type:
        try:
            body = await request.json()
            pasted_diff = body.get("raw_diff") or body.get("diff")
            target_pr_url = body.get("pr_url") or body.get("pr")
        except Exception:
            pass

    if file:
        file_bytes = await file.read()
        file_name = file.filename
    if zip_file:
        zip_bytes = await zip_file.read()

    # Rate limiting pre-check
    try:
        rate_limiter.check_rate_limit(client_ip)
    except RateLimitExceeded as e:
        raise HTTPException(
            status_code=429,
            detail=str(e),
            headers={"Retry-After": str(e.retry_after)}
        )

    # Execute review with 180-second hard timeout (multi-agent crew needs multiple LLM calls)
    try:
        response: ReviewAPIResponse = await asyncio.wait_for(
            asyncio.to_thread(
                ReviewService.execute_review,
                raw_diff=pasted_diff,
                pr_url=target_pr_url,
                file_name=file_name,
                file_bytes=file_bytes,
                zip_bytes=zip_bytes,
                client_ip=client_ip
            ),
            timeout=180.0
        )
        return response


    except asyncio.TimeoutError:
        logger.error(f"Review request from {client_ip} timed out after 180 seconds.")
        raise HTTPException(
            status_code=504,
            detail="Review request timed out after 180 seconds. The input may be too large or complex for real-time analysis."
        )
    except InputValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RateLimitExceeded as e:
        raise HTTPException(
            status_code=429,
            detail=str(e),
            headers={"Retry-After": str(e.retry_after)}
        )
    except Exception as e:
        logger.error(f"Error executing synchronous review: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Review execution failed: {str(e)}")


@app.post("/api/review/stream")
async def api_review_stream(
    request: Request,
    raw_diff: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    zip_file: Optional[UploadFile] = File(None)
):
    """
    Streaming Server-Sent Events (SSE) review endpoint.
    Emits real-time progression events ('step', 'complete', 'error')
    so frontend UIs receive step-by-step progress during multi-agent analysis.
    """
    client_ip = request.client.host if request.client else "127.0.0.1"
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        client_ip = forwarded_for.split(",")[0].strip()

    content_type = request.headers.get("content-type", "")
    pasted_diff = raw_diff
    file_bytes = None
    file_name = None
    zip_bytes = None

    if "application/json" in content_type:
        try:
            body = await request.json()
            pasted_diff = body.get("raw_diff") or body.get("diff")
        except Exception:
            pass

    if file:
        file_bytes = await file.read()
        file_name = file.filename
    if zip_file:
        zip_bytes = await zip_file.read()

    try:
        rate_limiter.check_rate_limit(client_ip)
    except RateLimitExceeded as e:
        raise HTTPException(
            status_code=429,
            detail=str(e),
            headers={"Retry-After": str(e.retry_after)}
        )

    async def event_generator():
        yield f"event: step\ndata: {json.dumps({'stage': 'INGESTION', 'message': 'Ingesting diff and preparing static analysis...', 'progress': 15})}\n\n"
        await asyncio.sleep(0.05)

        yield f"event: step\ndata: {json.dumps({'stage': 'SECURITY_SCAN', 'message': 'Executing heuristic security pattern scanner and SAST checks...', 'progress': 35})}\n\n"
        await asyncio.sleep(0.05)

        yield f"event: step\ndata: {json.dumps({'stage': 'GOVERNANCE', 'message': 'Evaluating organization rules and AST code graph...', 'progress': 55})}\n\n"
        await asyncio.sleep(0.05)

        yield f"event: step\ndata: {json.dumps({'stage': 'MULTI_AGENT_CREW', 'message': 'Deploying Senior Developer, Security Engineer, and Tech Lead agents...', 'progress': 75})}\n\n"

        try:
            response = await asyncio.to_thread(
                ReviewService.execute_review,
                raw_diff=pasted_diff,
                file_name=file_name,
                file_bytes=file_bytes,
                zip_bytes=zip_bytes,
                client_ip=client_ip
            )

            yield f"event: step\ndata: {json.dumps({'stage': 'SYNTHESIS', 'message': 'Synthesizing final executive review and merge decision...', 'progress': 95})}\n\n"
            await asyncio.sleep(0.05)

            yield f"event: complete\ndata: {response.model_dump_json()}\n\n"

        except Exception as err:
            logger.error(f"Error during streaming review: {err}")
            yield f"event: error\ndata: {json.dumps({'error': str(err)})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


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


