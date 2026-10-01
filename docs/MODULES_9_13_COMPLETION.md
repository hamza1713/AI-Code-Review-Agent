# M9–M13 implementation checkpoints

This is an engineering checkpoint. Hosted CI, attributed release approval, paid model captures, independent labels and production pilot acceptance remain separate gates. No production deployment or GitHub publication is authorized by local test success.

## M9: native providers and explicit orchestration

- HTTPX transports normalize Gemini, OpenAI, Anthropic, Groq and Ollama responses into typed text/model/request/finish/usage results. `.call()` remains compatible.
- Every outbound attempt rechecks repository/model approval. Calls are bounded to 60 seconds including one transient retry; simple review uses 45 seconds and the worker deadline bounds the whole review.
- Quality/security run concurrently with separate copied consent contexts. Schemas are validated before synthesis; synthesis cannot invent findings. The provider instance is retained across stages.
- No autonomous model tools, CrewAI dependency, ChromaDB dependency or hidden LiteLLM judge fallback. Production restrictions on generated execution, remote embeddings, remediation and publication remain enforced.
- Invalid/refused/truncated output makes analysis incomplete and preserves deterministic evidence.

## M10: verified full-source analysis

- Immutable snapshots bind diff, exact analyzed commit, full raw file bytes, SHA256 hashes and explicit per-file coverage.
- GitHub fork source is acquired at the head commit; commit/tree/blob identities and regular-file mode must agree. Failed checkout cannot expose default-branch context.
- Uploaded bytes are retained directly; incomplete modification hunks cannot masquerade as complete files. Complete new-file diffs may supply source.
- Full Python AST/Bandit/Ruff scans run in temporary workspaces with isolated settings and interpreter search paths. Only added target lines are retained, using exact paths.
- Limits are 100 changed files, 2 MiB per file and 20 MiB total. Missing, mismatched, unsafe or oversized source is recorded as incomplete.

## M11: configuration-bound pilot evidence

- Profiles hash application source/commit, model identity, generation settings, prompts, scanners, runtime, policy and context configuration. PR input hashes are separate.
- Pilot runs/promotions are profile-bound. Undefined actionability cannot pass. Legacy runs remain readable and cannot qualify a reviewer.
- Schema v2 migration takes a verified backup, preserves IDs/labels/receipts, pauses advisory controls and retains attributed history. Uncertain publication intents remain reconcilable.
- Saved unversioned responses cannot initiate new publication; release bytes are reverified at the publication boundary.

## M12: real PR evaluation preparation

- Frozen 120-case corpus with 40 PRs each from Flask, HTTPX and Requests, captured permissive licenses, full head files and reproducible 20/100 split.
- Blind typed predictor inputs, separate deterministic/model/combined captures, overwrite protection, strict input/profile checks and expanded reporting.
- Blank independent label exports and instructions: [evaluation protocol](EVALUATION_PROTOCOL_V2.md).
- No independent labels or model quality acceptance are claimed. Public data contamination remains unknown.

## M13: retained release evidence

- Versioned typed release records bind source commit, reviewer profile, tested image content identity, required checks and relative artifact hashes.
- Missing/modified/ambiguous evidence, failed matrix checks, skipped sandbox execution and image/commit mismatches are rejected. Registry platform manifests must resolve to the tested image config digest.
- CI runs Python 3.10–3.13, static/typing/audit/secrets gates, frontend/browser checks, Linux/Windows path checks, mandatory sandbox execution, recovery and exact-image smoke.
- CI assembly starts `AWAITING_OPERATOR_APPROVAL`; attributed human approval is required separately.

## Retained local evidence

Local logs and machine-readable outputs are under `.cache/production-m9-m13/`. Final results are recorded in `local-checkpoint.json` when verification finishes. Local evidence from an uncommitted workspace is diagnostic; it cannot be presented as a green hosted matrix or an approved immutable release.

## Acceptance still outside this checkpoint

1. Commit reviewed source and run hosted CI for that exact commit.
2. Verify the eventual registry manifest mapping and obtain attributed approval of the tested artifact.
3. Run explicitly approved paid model captures and obtain independent corpus labels.
4. Operate the production-host migration, backup/restore and deployment under the existing runbooks.
5. Complete 100 representative shadow PRs, including 25 independently labeled clean changes, then 100 labeled published findings with at least 85% actionable and the existing publication safeguards.
