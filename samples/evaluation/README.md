# Versioned public PR corpus

`public-pr-v1` contains three merged Flask PR inputs frozen at their base/head commits. The source is BSD-3-Clause; each diff includes a corresponding original license notice. `inputs.json` records provenance and SHA-256 hashes. Source titles and discussions are excluded from predictor inputs.

These are seed cases, not a representative product study. The typo/type changes are clean *candidates*, and the scaffold change is a hard-negative *candidate*. No finding labels or quality scores have been invented. Public PRs may occur in model training data, so this corpus is blind to the scoring harness but cannot be called uncontaminated model holdout data.

## Workflow

1. Freeze predictions before opening label files. `capture_predictions` passes only the diff to the predictor. Record deterministic, model, and combined predictions separately with the exact corpus version and input hashes.
2. Two independent reviewers inspect each diff and its base code without seeing predictions. Adjudicate disagreements. Record actual reviewer identities, rationale, cohort and precise findings. A clean or hard-negative case has an empty finding list only after this review.
3. Score frozen predictions against the adjudicated labels. Every unmatched emitted finding, including duplicate scanner hits, is a false positive. Matching uses full paths, exact target lines, and rule IDs or CWE identity. Undefined precision/recall are `null` with explicit denominators.
4. Expand across repositories, languages, sizes and defect types before the M8 100-PR gate. Do not tune rules on the held-out split; version corpus changes and keep development cases separate.

```powershell
python -m code_review_agent.eval.corpus capture --predictions predictions.json
python -m code_review_agent.eval.corpus score --predictions predictions.json --labels labels.json --output scores.json
```

Label schema (one entry per input case, actual identities required):

```json
{
  "corpus_version": "public-pr-v1",
  "cases": [{
    "id": "flask-5344",
    "sha256": "COPY_FROM_INPUTS",
    "reviewers": ["ACTUAL_REVIEWER_A", "ACTUAL_REVIEWER_B"],
    "status": "ADJUDICATED",
    "cohort": "clean",
    "rationale": "Independent review conclusion",
    "findings": []
  }]
}
```

For defect labels each finding needs `file_path`, `line_number`, and `rule_ids` or `cwe`. LLM judges remain an optional offline diagnostic; `UNAVAILABLE` has no score and cannot pass a quality gate. Syntax and scope invariants run during ordinary reviews and do not establish defect truth.
