# Frozen public PR evaluation

## Scope and evidence

`samples/evaluation/public-pr-v2/inputs.json` freezes 120 merged Python PRs: 40 from Flask (BSD-3-Clause), 40 from HTTPX (BSD-3-Clause), and 40 from Requests (Apache-2.0). Each case retains its base/head commits, immutable comparison URL, license at the captured head, diff hash, full head-file hashes, and input snapshot hash. `manifest.sha256` seals the selected manifest. The original three-case `public-pr-v1` corpus remains historical evidence.

Selection uses descending merge date within a closed update-date window. `selection-audit.json` records exclusions and eligible cases outside the selected 40 per repository. Acquisition errors that later succeeded are recorded as retries rather than final exclusions. Selection metadata is separate from predictor inputs.

The split is reproducible: order by SHA256 of `split-v1:` followed by case ID; the first 20 cases are development and the remaining 100 are held out. Do not tune prompts, policies, models or thresholds using held-out labels. Public PRs may have appeared in model training data; this corpus does not establish an uncontaminated holdout.

## Independent labeling

Blank exports are under `samples/evaluation/public-pr-v2/labeling/`. They are templates, not gold labels. The development and held-out files are separate. Give reviewer A and reviewer B separate copies and code/provenance access. Withhold model predictions, the other reviewer's findings, PR titles, discussions, and any proposed clean/defect cohort.

Each reviewer inspects the changed code and verified head files independently. Record defects introduced or exposed on added target lines, with exact repository path, target line, rule/CWE where applicable, severity, explanation and concrete remediation. Count stylistic preferences as defects only when a frozen policy requires them. A clean case has no actionable defects; a hard negative has suspicious code whose behavior is valid. An empty findings list is not evidence that a case was reviewed.

After both reviews, reconcile agreement. A third independent reviewer adjudicates disagreements. Preserve the original exports and adjudication notes separately. The final scoring file requires:

- `status: ADJUDICATED`, two distinct attributed reviewer identities, and a rationale;
- `cohort: clean`, `hard_negative` or `defect`;
- findings with exact locations; clean/hard-negative cases must have no gold defects;
- unchanged diff, snapshot and manifest hashes;
- `disagreement: true` and a third distinct `adjudicator` when opinions differ.

Reviewers must supply these fields. Tooling never invents human labels or reviewer identities. Hold held-out labels outside tuning workspaces and retain access history.

## Capture

Use the same frozen configuration and reviewer profile for all three modes. RAG must be disabled; team memory is frozen to empty for these evaluation runs. The application policy is copied into a private evaluation workspace. A changed model, prompt, scanner, policy, source implementation or runtime creates a new profile and requires a new set of captures.

```powershell
$env:PYTHON_DOTENV_DISABLED='1'
$env:RAG_ENABLED='false'
python -m code_review_agent.eval.corpus capture --corpus samples/evaluation/public-pr-v2 --mode deterministic --predictions evaluation-runs/deterministic.json
python -m code_review_agent.eval.corpus capture --corpus samples/evaluation/public-pr-v2 --mode model --predictions evaluation-runs/model.json
python -m code_review_agent.eval.corpus capture --corpus samples/evaluation/public-pr-v2 --mode combined --predictions evaluation-runs/combined.json
```

Paid model captures require explicit approval and the existing repository/model transfer approvals. Do not run them merely because these commands are documented. Capture refuses overwrites. Predictors receive code and technical snapshot provenance; labels, cohorts, titles and discussions are excluded. Retain failed or incomplete predictions instead of silently dropping cases.

## Score and report

```powershell
python -m code_review_agent.eval.corpus score --corpus samples/evaluation/public-pr-v2 --predictions evaluation-runs/combined.json --labels independently-adjudicated-labels.json --output evaluation-runs/combined-score.json
```

Use `--split development` or `--split held_out` during capture when scoring a separate split. Scoring requires an exact case set, matching input hashes and one reviewer profile. It rejects unreviewed templates, missing adjudication, duplicate cases, mixed identities and changed manifests. Preserve all three captures; compare modes only with the same case set and profile.

Reports include precision/recall/F1 with denominators, verdict accuracy, clean-PR false alarms, emitted severities, incomplete coverage, latency and cost coverage. Missing usage/cost and undefined quality metrics remain unavailable, never zero. Scanner predictions are heuristic evidence and cannot substitute for human labels. This workflow does not by itself satisfy the production pilot gates.
