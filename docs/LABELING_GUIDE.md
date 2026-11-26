# Labeling guide: development split (public-pr-v2)

This guide lets two people produce the human labels needed to publish precision, recall and false-alarm rate. It follows [EVALUATION_PROTOCOL_V2.md](EVALUATION_PROTOCOL_V2.md). The tool never invents reviewer names; every label is typed by a real person.

## Roles

| Role | Who | What they do |
| --- | --- | --- |
| Reviewer A | One engineer | Labels all 20 development cases without seeing reviewer B's labels |
| Reviewer B | A different engineer | Labels the same 20 cases independently |
| Adjudicator | A third person, not A or B | Resolves only the cases where A and B disagree |

Held-out cases (100) are never shown in the workbench. Label them in a separate, offline workspace after the development numbers are frozen.

## Workflow

1. Start the server (`uv run review-server`) and open the **Labeling** view in the workbench.
2. Choose your slot (A or B) and enter your name. The slot is locked to that name from then on.
3. For each case, read the diff and decide:
   - **Cohort**
     - `clean`: no real defect in the changed lines.
     - `hard_negative`: code that looks risky but is safe in context (for example, a parameterised query that resembles SQL injection).
     - `defect`: at least one real bug, security issue or contract break.
   - **Findings** (only for real issues). Click the line in the diff, then add:
     - `file_path` and `line_number`: the exact new-file line.
     - `rule_ids` (for example `SEC-001`, `B608`) and/or `cwe` (for example `CWE-89`). At least one is required.
     - `severity`: `CRITICAL`, `HIGH`, `MEDIUM`, `LOW` or `INFO`.
   - **Rationale**: one or two sentences on why.
4. Keep the "Show tool predictions" toggle **off** while labeling. Predictions are only for later review; seeing them first biases the label.
5. When both reviewers finish, press **Merge labels** (or run `uv run python -m code_review_agent.eval.corpus merge-labels --corpus samples/evaluation/public-pr-v2 --split development`).
   - Agreeing cases are adjudicated automatically.
   - Disagreements appear under **Adjudication** for the third person.
6. When every case is adjudicated, the **Score** panel shows the results, each with a 95% Wilson interval: precision, recall, F1, false comments per PR and the clean-PR false-alarm rate. It also shows inter-rater agreement (Cohen's kappa on cohort).

## Matching rules

A tool finding counts as a true positive only when its file path, exact line number, and rule ID or CWE all match a gold finding (one-to-one matching). A finding one line off is a miss. Label the line where the defect is introduced, not a neighbouring line.

## Example finding

```json
{
  "file_path": "src/flask/sessions.py",
  "line_number": 184,
  "rule_ids": ["SEC-WEAK-HASH"],
  "cwe": "CWE-328",
  "severity": "MEDIUM",
  "note": "SHA-1 used for a new session signature path"
}
```

## Files written

All under `samples/evaluation/public-pr-v2/labeling/`:

- `development-reviewer-a.json`: reviewer A's labels and name.
- `development-reviewer-b.json`: reviewer B's labels and name.
- `development-adjudication.json`: the merged gold labels the scorer reads.

Commit these files once labeling is finished, so the published numbers can be reproduced.
