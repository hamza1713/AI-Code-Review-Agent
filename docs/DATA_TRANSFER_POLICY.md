# Repository consent and provider data transfer

Production requires an operator-owned JSON file mounted read-only through `REVIEW_DATA_POLICY_FILE`. Model construction fails before a call if the exact model or repository is absent, approval is expired, or required approval and retention references are missing. Production model remapping, cross-model fallback, remote embeddings, and Langfuse export are disabled. Repository admission and provider transfer are separate approvals.

An operator must verify the provider's current terms, account configuration, region, training usage, retention, subprocessors, and repository owner consent. The software checks the approval record; it cannot establish that a person's declaration is authentic or that the provider honors the terms. Public permissive licensing authorizes the evaluation source seed; it does not constitute consent for every private repository or a paid model call.

Example schema (replace every placeholder; keep outside Git):

```json
{
  "approved_by": "responsible operator identity",
  "approval_reference": "record of repository owner consent",
  "expires_at": "2027-01-01T00:00:00Z",
  "models": ["provider/exact-approved-model"],
  "repositories": ["github:github.com/owner/repository"],
  "provider_terms_reference": "approved current contract and account settings",
  "retention_policy": "approved provider and local retention schedule"
}
```

Use the explicit repository `local-upload` only if operators are authorized to submit source files or pasted diffs through the API. Remote reviews propagate their repository scope to model construction. Unknown scopes deny transfer. Generated source remains untrusted data.

## Local data

The protected SQLite volume contains queue inputs/results, finding evidence, feedback, publication intents/receipts, and pilot snapshots/labels. Pilot snapshots omit the full raw diff and full model report, but finding snippets can contain source code. Backups and manual exports have the same sensitivity. Restrict host access, encrypt off-host backups, and delete expired exports according to the approved schedule. Expiring consent stops new transfers; it does not automatically delete retained operational evidence.

Retain uncertain publication intents until reconciled and retain receipts needed for deduplication. Do not purge those records during an incident. The operator must apply their approved retention procedure after reconciling outstanding writes. Automated retention deletion and multi-tenant access control are outside this single-team release.

## Usage and cost

Router token counts use character estimates and are labeled `ESTIMATED`. CrewAI usage reports are labeled `PROVIDER_REPORTED`; an aggregate containing both is `MIXED`. Missing usage is `UNAVAILABLE`. USD cost is always an estimate, including when token usage was reported by a provider. Missing prices produce `null`, not a zero-dollar claim.

To supply approved rates, set `REVIEW_MODEL_PRICING_JSON` to an object keyed by exact model, with `prompt` and `completion` rates in USD per million tokens and an `as_of` date. No default model price is substituted. Reconcile actual billing separately against provider invoices.
