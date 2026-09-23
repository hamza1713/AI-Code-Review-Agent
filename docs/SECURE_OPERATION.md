# Secure operation and migration

The operator-token gate is currently disabled for the local/demo workflow. Static pages, review APIs, and job routes remain directly usable. The security middleware is retained and can be enabled for the next security phase with `REVIEW_REQUIRE_AUTH=true`. Webhooks still require their provider secret. Generated Python is never executed by host pytest.

## Configure operator access

1. Generate a random token of at least 32 characters, for example with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
2. Set `REVIEW_API_TOKEN` in the server environment or your existing local `.env`. Do not commit the value.
3. Set `REVIEW_REQUIRE_AUTH=true` to activate the gate.
3. Start the backend normally and enter that value in the workspace connection form. The frontend retains it in memory only; a page refresh requires reconnection.
4. API clients must send `Authorization: Bearer <your-token>`.

This is a **single-deployment, trusted-operator** access model. Everyone with the token can read the deployment's jobs/results and request reviews. Deploy separate instances for untrusted teams; this is not per-user or per-repository multi-tenant authorization. Keep the default single Uvicorn application process. Quotas and process capacity are local to that process; replicated deployments need a shared quota and worker coordinator.

Use HTTPS at the reverse proxy for remote access. Set `REVIEW_PUBLIC_URL` to your configured public origin. Forwarding headers never choose the quota identity or advertised webhook URL.

## Webhooks and posting

- GitHub requires `GITHUB_WEBHOOK_SECRET` and a valid SHA-256 signature.
- GitLab requires `GITLAB_WEBHOOK_SECRET` and a matching `X-Gitlab-Token`. A general GitLab API token is no longer used as a fallback webhook secret.
- Bitbucket requires `BITBUCKET_WEBHOOK_SECRET`. Supply it through `X-Webhook-Token` when your integration supports custom headers; the existing `?token=` form remains supported for compatibility. Avoid logging webhook query strings at your proxy.
- Missing or incorrect provider secrets return 401; there is no unsigned demo bypass.
- REST bot commands reject host `repo_root` paths and local repository URLs. API posting defaults off; `REVIEW_ALLOW_API_POSTING=true` is a deliberate administrator opt-in. Signed webhook commands still respect commenter permissions and can post through the configured bot as before.
- `REVIEW_ENABLE_TEST_EVENTS=true` enables the operator-only test-event control; leave it off in production.

## Saved jobs, limits and deadlines

The browser sends `Prefer: respond-async` to `POST /api/review`. The server returns 202 with a job ID and `Location`. Poll `/jobs/{id}` or `/jobs/{id}/result` using the operator token. Jobs and results are stored in SQLite, and the browser saves only the latest opaque job ID in session storage so it can reconnect after refreshing. Closing a browser tab does not cancel a saved job.

The queue accepts at most 100 pending jobs. There are two bounded review subprocess slots per application process. Each job has a 180-second wall-clock deadline, including process startup; expiry terminates its host process tree. Browser jobs use one attempt to avoid silently repeating paid reviews; webhook jobs retain their configured retry policy. Shutdown signals active queue workers to stop. After a crash, persisted jobs are recovered by the existing queue recovery mechanism.

The synchronous compatibility endpoint uses the same bounded executor. The streaming endpoint emits only a real waiting state followed by completion/error; it does not invent stage percentages. Provider calls created by the factory have a 45-second request timeout and one retry. A remote provider may still bill work already submitted before local cancellation.

The operator gets 30 admitted review submissions per minute. The quota is reserved once at the gateway and is not charged again by the review service. Spoofed forwarding headers cannot create new identities. Direct in-process service callers retain their existing limits unless using explicitly trusted internal parameters.

Ingress is bounded before JSON/multipart parsing to 2 MiB plus 64 KiB form overhead, even without Content-Length. Body transfer has a 15-second total deadline. Individual source/ZIP uploads are capped at 2 MiB; raw diffs are capped at 500,000 characters. Keep an equivalent request-body limit at the reverse proxy. Oversized input returns 413.

## Optional generated-test containers

Generated tests remain viewable without Docker, but their execution is reported as **UNVERIFIED / disabled**. There is no host fallback. To opt in:

1. Review `sandbox/Dockerfile` and build it explicitly: `docker build -t code-review-sandbox:test sandbox`.
2. Inspect its immutable ID with `docker image inspect code-review-sandbox:test --format '{{.Id}}'`.
3. Set `REVIEW_SANDBOX_IMAGE` to that `sha256:...` ID (or an approved repository digest). Mutable tags are refused, and the runner never pulls missing images.
4. Run `python -m pytest -m sandbox_integration tests/test_sandbox_runner.py` before enabling it for your deployment.

Containers run as UID 65534, with no network, all capabilities dropped, no-new-privileges, a read-only root and read-only temporary source mount, bounded memory/CPU/processes, limited output, and a deadline. The host repository is never mounted. Only reconstructed added lines and generated tests are included, so imports or existing definitions may be unavailable. Failed generated assertions remain UNVERIFIED; they are not automatically labeled reproduced defects.

Use a dedicated restricted Docker host/daemon for this feature. Docker access is privileged infrastructure; do not put server secrets in the approved image. An unavailable engine, invalid digest, unsafe path, excessive output, timeout, or failed setup never triggers host execution. The implementation uses Docker's documented [container execution controls](https://docs.docker.com/reference/cli/docker/container/run/).

## Validation commands

- `python -m pytest -q` — fast suite, including gateway/security and queue persistence regressions.
- `python -m pytest -q -m slow` — real subprocess/CLI tests; container integration cases require the configured Docker image and otherwise report skips.
- `npm --prefix frontend run build`
- `npm --prefix frontend run lint` — source-only lint, with warnings treated as failures.
- In `frontend`: `npx playwright install chromium`, then `npm run test:e2e`.

CI builds/lints the frontend, runs browser regressions on desktop and mobile widths, and builds the sandbox image for the Python 3.12 job to run its container integration probes. No live LLM calls or real repository posting are required for these checks.
