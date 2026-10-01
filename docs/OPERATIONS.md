# Production operations

## Supported deployment

One Docker host, one trusted team, one shared operator credential, one application process with an embedded pool of two review workers. SQLite must reside on a persistent **local** volume; network filesystems and multiple replicas are unsupported. Queue admission allows 100 waiting jobs in addition to active workers. GitHub is the production publication provider; other adapters remain available for analysis, with production writes denied pending provider-specific reconciliation gates.

Use `docker-compose.yml`. Only Caddy publishes ports. Set `REVIEW_DOMAIN` to a domain resolving to the host, allow inbound 80/443, and let Caddy obtain TLS certificates. Do not expose port 8000. Render's ephemeral preset is retired.

Required settings: `REVIEW_IMAGE` (approved registry reference including `@sha256:…`), `REVIEW_API_TOKEN` (random secret, at least 32 characters), `REVIEW_ALLOWED_REPOSITORIES` (comma-separated `github:github.com/owner/repo` identifiers), `GITHUB_TOKEN`, `BOT_GITHUB_LOGIN`, `GITHUB_WEBHOOK_SECRET`, `GEMINI_API_KEY`, and an approved `LLM_MODEL`. Keep the environment file outside source control and readable only by the host operator. Rotate credentials after suspected exposure.

The application runs as UID/GID 10001 with a read-only application filesystem. The data volume is writable; `/tmp` is bounded ephemeral storage. Never mount the host Docker socket. Generated-code execution is disabled without an independently reviewed immutable sandbox image and its isolation tests. There is no host-execution fallback.

## Login and permissions

The browser starts at operator login. Credentials stay in memory; refresh or logout requires another login. An opaque saved job ID can reconnect only after authentication. API, job, metrics and documentation routes require the operator token. `/`, static assets, and minimal health responses are public. Webhooks use their separate provider signature/secret.

Unauthenticated local development requires **both** `APP_ENV=development` and `REVIEW_REQUIRE_AUTH=false`. Production startup rejects missing/short operator credentials even if `REVIEW_REQUIRE_AUTH=false` is set.

GitHub commands resolve the comment author's current repository permission. Write/maintain/admin can submit feedback; maintain/admin can suppress, unsuppress or remediate. Organization membership is not authorization. Permission lookup errors deny access. The shared operator token represents the trusted deployment operator, so its actions are attributed to `operator`, not a client-supplied name.

Suppression requires a separate reasoned maintainer action and expires after 30 days by default. A critical override additionally requires `critical_override: true` or the `--critical-override` command flag. Words in a reason do not grant authority. False-positive feedback, audit rejection, reactions, merge events, and committed remediation never create suppressions.

## Deploy and migrate

1. Block review admission at the proxy and drain active jobs. Keep authentication enabled.
2. Create and verify an online backup. Copy it off-host before proceeding.
3. Stop the web service. Run the migration using the **new** pinned image with the existing data volume:
   `docker compose run --rm web review-db migrate --database /data/review.db`
4. Inspect the JSON migration report, backup path, counts and unresolved records. Migration uses an exclusive lock and a SQLite transaction. Restart/rerun after interruption is safe. A newer incompatible schema fails startup.
5. Start the pinned image with `REVIEW_ENABLE_POSTING=false` and remediation disabled. Startup also performs a locked migration before workers start. A process ownership lock rejects an accidental second application instance.
6. Verify login, readiness, queue persistence after restart, backup restoration, and test-repository delivery with fault injection. After approval, set `REVIEW_ENABLE_POSTING=true` and restart. Keep remediation off until its separate release gate passes.

Legacy finding rows are preserved in `finding_outcomes_legacy_v1`; legacy suppressions are copied before migration changes. Legacy records without verifiable publication remain `LEGACY_UNVERIFIED`, excluded from trusted metrics. Lost overwritten PR history is not reconstructed. Automatically created suppressions are retired and all old critical overrides require explicit reauthorization.

## Backup, restore, and rollback

Daily online backup:

```sh
docker compose exec web review-db backup --database /data/review.db --file /data/backups/daily-YYYYMMDD.db
```

The command uses SQLite's online backup API and runs an integrity check. Never copy only a live `.db` file while ignoring its WAL. Copy completed backups off-host using your approved encrypted storage service. Retain at least 7 daily and 4 weekly backups; protect them like source code and credentials. Perform a restoration drill monthly and before release approval.

Offline restore (stop the web service first):

```sh
docker compose run --rm web review-db restore --database /data/review.db --file /data/backups/daily-YYYYMMDD.db
```

Restore takes the application/migration locks, checks backup integrity, and preserves a pre-restore backup. Roll back **both** the image and its matching database backup. Do not start an older image against a migrated schema. Restore drills must use an isolated volume, then verify schema, queue records, feedback history, and review receipts before admitting traffic.

## Delivery and incident response

Each review is pinned to a PR head; publication checks the head again. An incomplete analysis cannot approve. Publication intent and a stable marker are persisted before a provider write; successful receipt IDs are retained. If a timeout or crash leaves the result uncertain, the worker searches paginated bot-owned reviews/comments for that marker. Undetermined outcomes appear as `RECONCILIATION_REQUIRED`; never delete the intent and blindly retry. Check the provider and operation marker before manual intervention. Cancelled jobs cannot be completed by a late worker. Cancellation stops the active subprocess; an already accepted provider write cannot be undone by cancellation.

Review findings count as published only after acknowledgement/reconciliation. An absent scanner finding means “no longer observed,” and a committed fix means “remediation committed”; neither confirms the defect.

## Reaction feedback and metrics

GitHub reactions are polled through the API every five minutes, with 100 requests per cycle by default. There is no reaction webhook. Polling includes recorded bot review comments on open PRs and PRs closed within 30 days. Pagination, conditional requests, a persisted cursor and provider retry times bound work. A partial/error response never deletes feedback. Authorized thumbs-up/down affect calibration only. Conflicting reactions remain pending triage, and explicit human decisions take precedence.

The UI reports “Not enough data” for empty denominators. Windows use first confirmed publication time; revision timestamps cannot move a finding into a newer window. Human-audit results describe observed sample statistics, not an unbiased repository estimate.

## Monitoring

`/health/live` checks process liveness. `/health/ready` checks schema compatibility, database access and worker-loop operation. Neither exposes queue contents. Authenticated `/metrics` returns queue depth/age, active workers, failures, uncertain publications, database availability and reaction polling lag. JSON application logs redact configured secrets and bearer credentials. Disable proxy request logging or redact query strings; never enable raw request-body logging.

Configure host alerts for readiness failure, any reconciliation-required publication, a nonzero failed-job increase, oldest queue age above the agreed review latency, reaction lag above 15 minutes, and absent daily off-host backup confirmation. Alert at 80% disk utilization and at less than two days of measured storage growth remaining. A database error/readiness failure must stop admission; preserve the database and WAL before recovery. Monitor container memory/OOM events and tune limits from the release load test.

## Release evidence

M6–M8 add [RELEASE_GATES.md](RELEASE_GATES.md), [DATA_TRANSFER_POLICY.md](DATA_TRANSFER_POLICY.md), and [PILOT_RUNBOOK.md](PILOT_RUNBOOK.md). Compose now also requires the mounted approval file, `REVIEW_DEPLOYED_COMMIT`, and `REVIEW_IMAGE_DIGEST`. Turning on the environment flag alone cannot authorize production review posting.

Use authenticated `POST /api/pilot/control?repository=github:github.com/owner/repo` with `{"mode":"PAUSED","reason":"Investigating a delivery incident"}` to stop new review writes immediately, including active jobs before their provider call. Then disable `REVIEW_ENABLE_POSTING` and restart. Keep uncertain intents for reconciliation. Protected `GET /api/pilot` reports quality gates; `/metrics` includes source-free alert codes. Set `REVIEW_QUEUE_SLA_SECONDS` for the queue-age alert; configure readiness, disk, OOM, and off-host backup alerts in the host monitoring system.

Run the credential-free recovery drill in a new, isolated directory before release:

```sh
uv run python scripts/recovery_drill.py --directory .cache/release-drill --output recovery.json
```

This exercises two concurrent claims, 100 waiting jobs, admission rejection, expired-lease recovery and stale-token rejection, 103 completed jobs, a simulated accepted-but-timed-out publication with one provider send, migration, SQLite online backup, and isolated restore with receipt verification. It does not simulate production model workload, measure host capacity, test encrypted off-host backup delivery, or authorize live provider writes. Production host load and off-host restore checks remain operator release requirements.

Approval requires the complete Python suite including slow tests, frontend build/lint and desktop/mobile tests, static typing, a clean production image and authenticated smoke checks against it, dependency/secret scans, two-worker/100-job saturation and recovery tests, and a real backup restore. CI sandbox isolation is mandatory even while production execution is disabled. Enabling remediation or sandbox execution requires its own integration gate. Record the commit, image digest, migration report, backup identifier, test results, and reviewer approval in the release record.
