# M6–M8 implementation and verification

Date: 2026-10-01. Working branch: `codex/production-m0-m2`. Existing user changes and earlier modules were preserved. No commit, push, deployment, live model call, or GitHub comment was made.

## Status

| Module | Implemented | Acceptance status |
|---|---|---|
| M6 — Release pipeline | Repository-owned clean Ruff gate; all-version CI; typed production boundary; hashed locked dependency audit; source/history secret scan; frontend/browser checks; actual sandbox isolation gate; exact built-image authenticated smoke; evidence assembly | Local verification mostly passes. **Release gate fails on four ChromaDB dependency advisories.** Hosted CI on Python 3.10–3.13 remains unrun. |
| M7 — Operations and data | Queue migration backup; queue/calibration/pilot migrations; busy restore refusal; two-claim/100-waiting-job drill; stale-claim/crash/timeout/reconciliation/restore checks; source-free alerts; exact provider/repository consent; honest usage/cost telemetry; rollback and data runbooks | Local isolated recovery and fault checks pass. Production host capacity, monitoring integration, off-host encrypted backup/restore, provider terms, and owner consent still require the deployment operator. |
| M8 — Shadow/advisory pilot | Durable PR/head evidence; independent labels and third-reviewer adjudication; sample/quality gates; exact release-bound promotion; immediate pause; receipt and commit checks; weekly latency/cost/noise reporting; API and CLI controls | **Field acceptance remains pending:** 100 representative shadow PRs including 25 clean, then 100 independently labeled published findings with at least 85% actionable. The current public corpus seed has three PRs awaiting human labels. |

## Verified locally

- Recoverable starting snapshot: **299 files**, SHA-256 verified archive, Git bundle/diffs, and online backup of the existing queue database under `.cache/production-m6-m8`.
- Full Python suite after the Ruff cleanup: **646 passed, 6 skipped**. Four skipped sandbox tests subsequently ran against the built container: **4 passed**. Other platform/dependency skips remain visible in the full JUnit report.
- Subsequent safety/queue/module regressions: **128 passed, 1 skipped**. Final operations/advisory/evaluation regressions: **47 passed**. Latest pilot release-binding regressions: **6 passed**.
- Ruff: **PASS** for every file in `src`, `tests`, and `scripts` under the explicit repository gate. Safe compatibility/import cleanup and formatting affect much of the Python tree; this is a broad reviewable diff.
- Mypy: **PASS, 21 files** in the declared production boundary. Imports and untyped function bodies are not exhaustively checked.
- Frontend lint and build: **PASS**. Desktop and mobile viewport browser tests: **16 passed**. The existing JavaScript chunk-size warning remains.
- npm production dependency audit: **zero reported vulnerabilities**.
- Gitleaks current source scan: **PASS**; committed history scan: **29 commits, PASS**. Three exact synthetic scanner-test constants are narrowly allowed; default rules remain active.
- Isolated recovery drill: **two active claims + 100 waiting jobs**, overflow refused; **two expired leases** recovered; stale tokens rejected; **103 jobs** completed; one simulated provider send despite acknowledgement loss; online backup and isolated restore preserved jobs and the publication receipt.
- Final production Dockerfile: built and loaded. The **same immutable image** passed readiness, authenticated/unauthenticated route, non-root user, read-only container, and frontend smoke checks. Image identity: `sha256:1dd4391fb1ba7f6502caa7fac7d3e0c0c137d61053f9ca35f648a7bd2f6ac0b3`.

The image identifies its source as `unreleased-working-tree`. It is local verification evidence, not an approved registry release or a committed source artifact. The smoke container was removed afterward. Docker was started locally for container verification.

## Release blocker

The locked audit reports four advisories in `chromadb==1.1.1`, required by `crewai==1.15.22`. The audit lists no fixed version; upstream reports include server and client configuration loading concerns. **No advisory ignore or compatibility override was added.** CI deliberately stays red on the dependency gate. See [RELEASE_GATES.md](RELEASE_GATES.md) for advisory links and remediation options.

## Operator workflow

- [Release gates](RELEASE_GATES.md): exact CI scope, artifact records, and dependency blocker.
- [Operations](OPERATIONS.md): migration, backup, isolated restore, pause, reconciliation, alerts, and rollback.
- [Data transfer policy](DATA_TRANSFER_POLICY.md): repository consent, exact provider/model approval, expiry, retention, and cost attribution.
- [Pilot runbook](PILOT_RUNBOOK.md): collecting real shadow reviews, independent labels, adjudication, promotion, weekly reports, and stopping publication.

`review-pilot` provides `report`, `weekly`, `export`, `label`, `adjudicate`, and `control`. Authenticated `/api/pilot` and `/api/pilot/control` expose operator reporting and phase control. Production posting requires both the environment flag and a release-bound advisory approval; pause is checked immediately before provider writes.

## Evidence and limits

Local evidence lives under `.cache/production-m6-m8`: logs, JUnit reports, locked dependency audit, scan reports, recovery backup/report, image build and smoke reports, source hashes, and a module-only patch against the preserved pre-M6 checkout. The module-only patch passes reverse applicability checking. This directory is ignored by Git and remains available locally.

No real pilot labels, provider approvals, price values, release approvals, precision claims, or live production outcomes were fabricated. The public diff corpus can still lose Bandit coverage on fragments without full-file context; those reviews stay incomplete and cannot satisfy the pilot gate. Live model quality, workload capacity, production latency/cost, and field usefulness remain unverified. The release and pilot gates make those remaining requirements explicit and enforceable.
