# Production modules 3–5: implementation record

## Status and scope

M3 and M4 are implemented. M5's evaluation infrastructure, repaired synthetic scorer, runtime checks and versioned public PR inputs are implemented. **The real PR quality acceptance gate awaits independent adjudicated labels.** No production quality score, representative holdout result or pilot approval is claimed.

The original numbered roadmap is saved in `PRODUCTION_MODULE_PLAN.md`. Work continues on `codex/production-m0-m2`, preserving the earlier changes and existing user work. No commit or deployment was performed.

## M3 — advisory publication

- The durable worker publishes GitHub reviews with `event=COMMENT`, pinned to the analyzed head. Internal verdicts remain available in the analysis response; suppression does not turn a verdict into approval.
- Removed review-path check runs and commit statuses. High severity findings cannot approve, request changes or affect merge status through this path.
- Incomplete or invalid output is withheld from publication. Non-GitHub analysis remains available, with publication marked `UNSUPPORTED_PROVIDER`.
- Posting remains disabled by default in application code, `.env.example` and Compose.
- Existing durable intent/receipt handling, claim checks and reconciliation remain in force. Saved non-advisory review intents require operator inspection; they cannot be silently replayed under the new policy. Existing pending check/status operations also remain records for explicit reconciliation.

## M4 — finding quality and capability truth

`review_policy.py` is applied to both service response builders and the worker boundary. Every candidate stays in `ReviewAPIResponse.findings`; `publication_reasons` records withholding reasons. Candidates use `ELIGIBLE` or `WITHHELD` until a provider receipt establishes `PUBLISHED`.

A candidate needs an exact changed target line and path, a stable fingerprint, evidence containing the code at that line, and a sufficiently specific explanation and proposed action. Duplicate identities, missing evidence, weak explanations, incomplete analysis and unverified reproduction claims are withheld. Finding-specific reproduction requires matching fingerprint, analyzed commit, execution flag, test ID and recorded reproduction outcome.

Provider comments are rebuilt from eligible canonical findings. The summary never copies the unfiltered model report, so rejected candidates cannot leak through summary text. Suppressed findings are excluded from both summary and comments. Only acknowledged published identities enter calibration records. The full narrative and rejected candidates remain available locally for audit.

The declared pre-scan now runs Python AST analysis, patterns, Bandit and Ruff, with optional Semgrep. `analyzer_status` records each tool as completed, heuristic, not applicable, unavailable or failed. Required-tool failures mark analysis incomplete. CLI execution errors, malformed output, timeouts and reported partial failures are no longer treated as a clean strict scan. Existing list-returning scanner methods remain compatible for development callers.

Ruff reconstruction preserves target line numbers across hunks and uses the active Python interpreter for its module fallback. `pattern_findings` remains a compatibility field; its label now says that it contains static scanner candidates and directs callers to actual tool statuses.

Production review flows skip generated execution even when the experimental development flag is set. Self healing is not called by the review flow. README language now distinguishes heuristic hits, test passes and finding-specific reproduction, and identifies Semgrep as optional.

**Limit:** these filters establish structural grounding and an actionable-looking explanation. They do not prove semantic correctness or reviewer usefulness. Diff-fragment reconstruction can fail without full file context; this produces explicit coverage loss and withheld publication. The public seed run exposes that limitation rather than hiding it.

## M5 — honest evaluation

### Repaired synthetic scorer

- Predictions use emitted output only. Gold labels are passed exclusively to scoring.
- Matches require full path, exact target line and rule ID or CWE identity. Maximum one-to-one matching avoids label-order artifacts.
- Every unmatched emitted hit is an FP, including duplicate and related detections. Missing input files fail the run.
- Synthetic labels now contain explicit locations and identities. Removed the duplicated gold label for a single MD5 call.
- Binary verdict prediction flags any emitted candidate. It is a fixture classification proxy, not the product's synthesis verdict or merge authority.
- Zero-denominator synthetic metrics are reported as zero with an explicit explanation. The public corpus scorer uses `null` and exposes denominators.
- Removed historical F1/accuracy badges, compiler-grade claims and competitor comparisons from the generated report. The bot summary identifies synthetic fixtures and no longer invents a numerator equal to the total number of cases.

The recomputed 14-case scanner result is **15 TP, 13 FP, 3 FN; precision 53.6%, recall 83.3%, F1 65.2%**. Its binary verdict accuracy happens to be 100% on these narrow fixtures. Neither figure demonstrates production accuracy. See `BENCHMARK_REPORT.md` for the case breakdown.

### Runtime checks and offline judges

`EvalRunner` runs cheap deterministic checks during final synthesis. API response validation additionally checks exact changed-line scope, Python suggestion/test syntax and confidence bounds. A failed invariant records failure, sets `analysis_complete=false` and `ESCALATE`, and prevents provider publication. No submitted code is executed by these checks.

LLM judges remain offline and return `PASS`, `FAIL` or `UNAVAILABLE`. Outages, malformed responses, missing reasoning and invalid scores produce no score and cannot pass. Reports track unavailable evaluators separately and average only actual scores. Interface tests use explicit offline fixtures.

### Versioned real PR inputs

With the user's authorization to use public permissively licensed repositories, three merged Flask PRs were frozen at exact base/head commits: `5344`, `5383` and `5270`. Their BSD-3-Clause notices, immutable diff URLs and SHA-256 hashes are retained under `samples/evaluation/public-pr-v1`.

The capture harness supplies only the diff to a predictor. Titles, discussions, cohorts and gold labels are excluded from that contract. Prediction files record corpus version, mode, hashes, findings and coverage status. Deterministic, model and combined results are kept separate.

Scoring rejects missing or duplicate cases, changed inputs and mismatched versions/hashes. Label files must name two distinct reviewers and declare adjudication; the operator remains responsible for authentic independent review. The label workflow is in `samples/evaluation/README.md`. The seed inputs include clean and hard-negative *candidates*, awaiting independent review; no human labels have been fabricated. Public PRs may be in model training data, so this is not a verified uncontaminated holdout.

The local deterministic capture is saved as evidence, including incomplete scans. It is not scored against invented gold labels. Expand the corpus across repositories and defect types and obtain independent labels before a meaningful model comparison or the M8 pilot gate.

## Verification and evidence

Verification evidence is recorded in `.cache/production-m3-m5/`:

| Check | Result |
| --- | --- |
| Full Python suite, including slow tests | **639 passed, 6 skipped**, 29 warnings |
| Final regressions after the last generic-action filter adjustment | **42 passed**, 1 warning |
| Earlier module/evaluation/reporting regressions | **47 passed**, 1 warning |
| New core files and regression file: configured Ruff | Pass |
| Focused mypy on five new core modules | Pass; dependency imports skipped and untyped bodies outside default coverage |
| Frontend strict lint / TypeScript and Vite build | Pass; existing large-bundle warning remains |
| Repository-wide Ruff | Fails on existing debt: **1,734 → 1,712** diagnostics; core diagnostics **132 → 131**; no added diagnostic identities |
| Synthetic scanner benchmark | 14 cases; 15 TP, 13 FP, 3 FN; F1 65.2% |

The complete run preceded the final one-string generic-action rejection. The subsequent 42-test run covers that final change, API response contracts and benchmark publishing. A full Git whitespace check still reports existing EOF blank-line issues in files unchanged by M3–M5; those were preserved.

| Evidence | Purpose |
| --- | --- |
| `starting-checkout.zip`, `manifest.json`, `starting-head.bundle` | Verified recoverable pre-M3 source and history |
| `databases/webhook_jobs.db` | Online database backup with integrity check |
| `release-check-tests.log` | Final complete Python run, including slow tests |
| `final-focused-tests.log` | 47 passing module, judge and reporting regressions |
| `last-tests.log` | 42 passing final policy, API and benchmark publishing regressions |
| `new-files-lint.log`, `mypy.log` | New-file lint and focused typing; dependency imports are skipped |
| `frozen-baseline-ruff.json`, `final-ruff.json` | Repository lint comparison |
| `benchmark.log` | Recomputed synthetic benchmark |
| `public-deterministic-predictions.json` | Blind capture with actual tool coverage status |
| `frontend-lint.log`, `frontend-build.log` | Frontend lint and build to a separate evidence directory |
| `modules-only.patch`, `evidence-summary.json` | Diff and inventory against the pre-M3 dirty checkout |

Existing repository-wide lint debt remains for M6. No live provider write, paid model quality run, production deployment or representative independent quality study was performed. Environment-dependent isolation/filesystem tests remain skipped locally.

## Compatibility and rollout

1. Clients should display the canonical findings' `publication_reasons`; the TypeScript contract includes the optional field. Existing analysis collections and full reports remain available for audit.
2. `MetricScore.score` is nullable and `status` is explicit. Consumers must not replace an unavailable score with a passing default.
3. The scanner label changed; use per-tool statuses for coverage rather than interpreting the compatibility collection as regex-only.
4. Stop admission, drain workers and keep posting disabled during upgrade. Preserve and inspect unresolved pre-policy intents. Test advisory delivery in an approved repository only after the remaining release/pilot gates pass.
5. No database schema change is introduced by these modules. Retain the matching code and database backup for rollback. Disable posting first; never delete pending intents to force another send.

M6–M8 and independently labelled quality evidence remain required before a production rollout.
