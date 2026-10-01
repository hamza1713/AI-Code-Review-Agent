# Shadow and advisory pilot

## Evidence and controls

The pilot ledger lives in the queue SQLite database. A remote review records its repository, PR, analyzed head, mode, analysis status, canonical findings, and telemetry. The same repository/PR/head/mode is recorded once; retries preserve the original snapshot. Each shadow PR counts once at its latest revision. Advisory outcomes count acknowledged published findings across revisions. All labels and adjudications are retained; duplicate labels from the same normalized reviewer identity fail.

Default posting remains disabled. Every production review write also requires an `ADVISORY` control for the repository. That control must reference an operator-approved release record and match `REVIEW_DEPLOYED_COMMIT` and `REVIEW_IMAGE_DIGEST`. Approval checks run again immediately before a provider call. A pause blocks new review writes, including active jobs that have not called the provider yet. Previously accepted uncertain writes still require reconciliation.

## 1. Prepare the release and consent

Run CI on the intended committed source. Preserve the release artifact, image identity, test reports, dependency/secret scan outputs, migration report, and matching database backup. Complete the operator approval fields only after all gates pass. An advisory check cannot be promoted from this uncommitted checkout or a failing dependency scan.

Configure the data transfer approval described in [DATA_TRANSFER_POLICY.md](DATA_TRANSFER_POLICY.md). Set `REVIEW_ENABLE_POSTING=false` and use an explicitly allowlisted GitHub repository. Confirm the operator identities and sampling protocol before collecting data.

## 2. Collect representative shadow PRs

Submit real PR reviews through the existing durable `/review` command/API path. No provider review is published. Expand the permissively licensed public seed at `samples/evaluation/public-pr-v1` or use approved team PRs. Collect at least 100 distinct representative PRs, including 25 independently established clean changes, covering realistic files, sizes, languages, defects, and hard negatives. Public historical examples can be contaminated by model training and cannot alone establish deployment quality.

Export protected local evidence:

```sh
review-pilot export --database /data/review.db --repository github:github.com/owner/repository --file /data/pilot-export.json
review-pilot report --database /data/review.db --repository github:github.com/owner/repository
```

Two independent humans review each run. Each label file contains `run_id`, `reviewer`, `cohort` (`CLEAN`, `DEFECT`, `HARD_NEGATIVE`), `outcomes` mapping every eligible fingerprint to `ACTIONABLE` or `NOT_ACTIONABLE`, and a specific `rationale`. Empty outcomes are correct when no findings are eligible. Do not use automated labels as independent human evidence.

```sh
review-pilot label --database /data/review.db --repository github:github.com/owner/repository --file /data/label-alice.json
review-pilot label --database /data/review.db --repository github:github.com/owner/repository --file /data/label-bob.json
```

Disagreements stay unresolved until a third reviewer submits the same schema through `review-pilot adjudicate`. The two original reviews remain intact. Incomplete analysis and unresolved labeling block promotion. The implementation also requires at least 85% actionable proposed findings when the shadow denominator is nonzero; an empty denominator stays `null`.

## 3. Promote to the limited advisory pilot

Prepare a control JSON file with `mode: ADVISORY`, attributed `actor`, specific `reason`, and the approved `release` record. Promotion checks require every declared CI gate, a commit, image digest, backup SHA, evidence reference, and operator approval; the shadow gate must pass.

```sh
review-pilot control --database /data/review.db --repository github:github.com/owner/repository --file /data/advisory-control.json
```

After authorized promotion, configure `REVIEW_ENABLE_POSTING=true` for this allowlisted team and restart. The API equivalent is authenticated `POST /api/pilot/control?repository=...`; actor attribution is server-owned `operator`. Findings remain advisory `COMMENT` reviews, pinned to the analyzed head, with no merge-affecting statuses. Remediation stays disabled.

## 4. Measure and stop when needed

Independently label published findings. The cumulative advisory gate requires at least 100 labeled acknowledged findings and an actionable rate of at least 85%. Receipt reuse does not increase the denominator; different receipts for a repeated run/fingerprint and wrong-commit/non-advisory receipts are rejected. Investigate provider-side duplication using stored markers and receipts. The ledger cannot detect out-of-band manual writes that were never imported.

```sh
review-pilot weekly --database /data/review.db --repository github:github.com/owner/repository --days 7
```

Weekly reports include all observed runs, incomplete runs, p50/p95 latency, usage sources, estimated cost with coverage, candidate/withheld noise, and cumulative independently labeled quality gates. Review repository coverage and label-selection bias alongside the numbers.

An operator can immediately set `PAUSED` using the control API/CLI, then turn off `REVIEW_ENABLE_POSTING` and restart. Keep jobs, receipts, and uncertain intents for reconciliation. Follow [OPERATIONS.md](OPERATIONS.md) for database/image rollback.

This repository currently contains a three-PR unlabelled source seed. Synthetic gate tests, successful local browser tests, and fault drills do not satisfy the 100-PR or 100-comment field gates.
