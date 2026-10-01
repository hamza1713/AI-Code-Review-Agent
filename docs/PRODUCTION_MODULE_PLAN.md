# Modular roadmap to a production advisory reviewer

## Summary

Target one trusted GitHub team on the existing single host deployment. The first release posts advisory comments only. Generated test execution, merge blocking, automated fixes, and production writes to other Git providers remain outside this release.

Each module below has a deliverable and an acceptance gate. **Completing M0–M7 makes the system ready for a controlled rollout; M8 supplies the evidence needed to enable posting.** No implementation starts as part of this plan.

## Working modules

| Module | Work and deliverable | Done when |
|---|---|---|
| **M0 — Preserve and baseline** | Save a recoverable snapshot of the current tracked and untracked work. Establish an isolated implementation branch and record current test, lint, build, and benchmark results. | Existing work is preserved, and later results can be compared against a recorded baseline. |
| **M1 — Safe diff workspaces** | Add one shared diff path validator and use it before every Bandit, Ruff, Semgrep, and sandbox file write. Reject traversal, absolute, drive, UNC, reserved, and symlink escaping paths. | Malicious diff tests on Windows and Linux cannot write outside a scanner workspace. Invalid API input returns 422; queued reviews fail without publication. |
| **M2 — Single command execution path** | Remove webhook `BackgroundTasks` command execution. Send every slash command through the durable queue. Require an active job claim for provider writes; route or disable direct CLI posting that bypasses it. | One webhook delivery produces one job and at most one provider write, including under retries, concurrent workers, cancellation, and crash injection. Uncertain writes enter reconciliation. |
| **M3 — Advisory publication policy** | Keep internal verdicts, but publish GitHub reviews only as `COMMENT`. Do not write merge affecting check runs or commit statuses. Retain `REVIEW_ENABLE_POSTING=false` as the default. | A high severity finding or incomplete review cannot approve, request changes, or change merge status on GitHub. Posting remains idempotent and tied to the analyzed commit. |
| **M4 — Finding quality and capability truth** | Publish only findings with a changed line, stable identity, specific evidence, and an actionable explanation. Record why other candidates were withheld. Wire Ruff into the declared Python scan with explicit tool status; describe Semgrep as optional. Keep sandbox execution and self healing disabled at launch and correct the README’s evidence claims. | Review responses distinguish completed, skipped, failed, heuristic, and unverified work. Weak candidates stay available for audit without becoming PR comments. |
| **M5 — Honest evaluation** | Remove gold-label leakage and forgiving false-positive matching from the benchmark. Version a blind real-PR corpus with clean and hard-negative examples. Run deterministic output invariants during reviews. Make LLM judge outcomes `PASS`, `FAIL`, or `UNAVAILABLE`; keep judging offline. | Benchmark predictions never read expected labels; every unmatched emitted finding counts as a false positive. Invariant failure marks analysis incomplete. Judge outage cannot produce a pass. |
| **M6 — Green release pipeline** | Define and clear an explicit Ruff gate. Run all Python tests including slow and sandbox isolation tests, frontend build/lint/browser tests, typing, dependency, and secret scans. Build the Dockerfile into an image and smoke test that same image with authentication. | All required CI jobs pass on supported Python versions; a release record contains commit, image digest, scan results, and smoke results. |
| **M7 — Operations and data controls** | Exercise migration, two-worker/100-waiting-job recovery, backup and isolated restore, provider timeout, and uncertain publication. Document approved provider data transfer and repository consent. Add redacted job, latency, token-usage, queue, and publication telemetry, alerts, and a rollback runbook. | An operator can detect failure, stop posting, reconcile external writes, and restore the matching image/database backup from a tested procedure. Cost estimates are labeled until actual provider usage is available. |
| **M8 — Shadow and advisory pilot** | First review without posting; independently label proposed findings. Then enable advisory comments for the allowlisted team and collect human outcomes. Review quality, latency, cost, and noise weekly. | Shadow set includes at least 100 representative PRs, including 25 clean changes. After at least 100 posted comments receive labels, at least 85% are actionable, with no duplicate or wrong-commit comments and no merge affecting status. |

## Dependency order and checkpoints

```text
M0
 ├─ M1 ─┐
 ├─ M2 ─┼─> M3 ──────────────┐
 └─ M5 ─┘                   │
       M4 ──────────────────┼─> M6 + M7 ─> M8
```

- **Checkpoint A, safety:** M0–M3 pass. Provider posting stays disabled until then.
- **Checkpoint B, release candidate:** M4–M7 pass in CI and against the built image. Complete a backup restore and provider fault drill.
- **Checkpoint C, posting:** M8 shadow results meet the stated sample and quality gates. Enable comments for the allowlisted GitHub team only.
- **Rollback:** turn off `REVIEW_ENABLE_POSTING` first; retain queue jobs and publication intents for reconciliation. Restore database and image as a matched pair if required.

## Interfaces and assumptions

Keep `ReviewAPIResponse` compatible. Use its existing `analysis_complete`, `analyzer_status`, evidence, and publication fields to expose output validation and withheld or unverified findings. Add an explicit unavailable state to the evaluation result type. The first release uses the existing SQLite single host boundary, one approved model provider, GitHub publication, and human merge authority.

After the pilot, plan separate modules for isolated generated test execution, automated remediation, merge gating, additional provider publication, and multi tenant scaling. Their designs should use the pilot’s measured usefulness, latency, and cost rather than being included in the first production release.
