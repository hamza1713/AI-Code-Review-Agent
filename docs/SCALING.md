# Scaling: current limits and the path beyond one node

The service today is a **single-node deployment** sized for one team or one organization's pilot. This page states the real limits, how to tell when you are reaching them, and the planned path past them. Nothing below is implemented unless marked as current.

## Current architecture (implemented)

| Component | Today | Setting |
| --- | --- | --- |
| Web process | One Uvicorn worker (`--workers 1` in the Dockerfile) | Fixed |
| Review execution | One isolated subprocess per review, killed as a process tree at its deadline | `REVIEW_JOB_TIMEOUT` (default 900 s) |
| Concurrency | Bounded semaphore and thread pool | `MAX_CONCURRENT_REVIEWS` (default 2) |
| Queue capacity | Rejects new work with HTTP 503 when full | `MAX_QUEUE_CAPACITY` (default 100) |
| Job queue | SQLite in WAL mode, `synchronous=FULL`, `busy_timeout=30000`, `BEGIN IMMEDIATE` claims with leases and heartbeats | `QUEUE_DB_PATH` |
| Other stores | Calibration, suppressions, pilot and telemetry share the same SQLite file | `QUEUE_DB_PATH` |
| Rate limiting | In-process, per deployment | Local to the process |

## What this supports

Each review spends most of its time waiting on LLM calls. So throughput is roughly `MAX_CONCURRENT_REVIEWS × 3600 / median review seconds`.

- Example: 2 workers at a 60-second median review is about 120 reviews an hour.
- Raising `MAX_CONCURRENT_REVIEWS` to 4–8 is safe on a host with 4+ cores and 8 GB RAM. Each review subprocess uses roughly 300–600 MB at peak, mostly Tree-Sitter and analyzers.

These are planning estimates, not measured benchmarks. Measure your own throughput with `GET /api/telemetry/latency` (p50/p95) and `GET /metrics` (queue depth and oldest queued age).

## Signs you are outgrowing one node

- `GET /metrics` shows oldest queued age regularly above `REVIEW_QUEUE_SLA_SECONDS` (default 300).
- p95 review latency in the Calibration view keeps rising while model latency stays flat.
- Logs show `database is locked` despite the 30-second busy timeout.
- You need more than one replica for availability, not only for capacity.

## Path beyond one node (planned, not implemented)

1. **Queue interface.** Extract a `JobQueue` protocol from `webhook_queue.WebhookJobQueue` (enqueue, claim with lease, heartbeat, complete, fail, cancel, list). The HTTP layer queries `queue._get_connection()` directly today; those queries move behind the interface first.
2. **PostgreSQL backend.** Implement the protocol on Postgres, claiming with `SELECT … FOR UPDATE SKIP LOCKED`. Keep SQLite as the default for single-node installs.
3. **Split stores.** Move calibration, suppression, pilot and telemetry tables to the same Postgres database, with per-tenant columns. (Tenancy today covers only the job queue.)
4. **Shared rate limiting and quotas.** Move the limiter to the database or Redis so replicas share one budget.
5. **Stateless web tier and separate workers.** Run N web replicas and M worker processes against the shared queue.

## Multi-tenancy today

With `REVIEW_MULTI_TENANT=true`, the tenancy rules are:

- **Tenant tokens.** Tokens listed in `REVIEW_TENANT_TOKENS` are bound to one tenant and may only reach their own review and job routes.
- **Operator token.** The operator token stays the single trusted administrator.
- **Webhooks.** Webhooks derive the tenant from the verified repository owner.

Calibration, suppression, pilot and telemetry data are **not** tenant-scoped yet. For mutually untrusted customers, run separate deployments until step 3 above is done.
