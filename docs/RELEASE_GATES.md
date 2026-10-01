# Release checks and retained evidence

The repository owns its Ruff gate in `pyproject.toml`: syntax/undefined code, imports, Python compatibility, common bugs, comprehensions, and redundant code. This replaces environment-dependent Ruff settings; it is not a claim of exhaustive security analysis. All `src`, `tests`, and `scripts` files must pass. FastAPI dependency defaults are declared immutable calls. Credential scrubbing in `tests/conftest.py` intentionally precedes application imports and carries local E402 comments.

CI runs the full suite on Python 3.10–3.13, Windows/Linux path tests, the declared typed production boundary, a dependency audit of the exported hashed lock without the local project, Gitleaks, frontend build/lint/runtime dependency audit, and desktop/mobile browser tests. The typed boundary uses `follow-imports=skip` and `ignore-missing-imports`; untyped function bodies and third-party internals are not comprehensively checked.

Image CI builds the production Dockerfile, loads the result into Docker, and authenticates against its exact image ID. It checks startup/readiness, non-root execution, a read-only container, protected routes returning 401 before login and 200 after login, and served frontend assets. The image identifies its source commit. A separate sandbox image runs all four actual isolation tests; missing Docker/image or skipped isolation tests fail the release gate. Runtime source changes preserve the cached locked dependency stage.

CI collects checks, JUnit reports, scan outputs, image identity, and isolated recovery evidence into a hashed release record. The record starts `AWAITING_OPERATOR_APPROVAL`. It cannot approve itself or enable production posting. Before deployment, record the registry manifest digest and actual deployment database backup as well as the local tested image content ID. A tag by itself is insufficient. The operator must verify that the registry manifest resolves to the tested image and enter matching deployment settings.

## Dependency replacement, 2026-10-01

M9 removed CrewAI and its mandatory ChromaDB dependency chain. Native HTTPX transports now support Gemini, OpenAI, Anthropic, Groq and Ollama. The exported locked production requirements passed the local vulnerability audit without advisory ignores. Retained evidence is in `.cache/production-m9-m13/python-audit.json`; CI must rerun the audit for the release commit. Earlier Chroma audit failures remain historical evidence in the M6–M8 checkpoint.

## Bundle verification and approval

Release-record schema v2 binds a 40-character source commit, server reviewer profile, tested local image content ID, backup hash, required gates and relative artifact paths. Missing, ambiguous or modified artifacts, commit disagreement, failing matrix reports, skipped mandatory sandbox tests and mismatched image identity are rejected. Legacy release records cannot authorize a new release.

```powershell
python scripts/release_record.py --evidence retained-evidence --record release-record.json
python scripts/release_record.py --evidence retained-evidence --record release-record.json --require-approval
python scripts/release_record.py --evidence retained-evidence --record release-record.json --manifest registry-manifest.json --manifest-digest sha256:ACTUAL_PLATFORM_MANIFEST_DIGEST
```

CI creates `AWAITING_OPERATOR_APPROVAL` records. After reviewing the exact retained bundle, an operator records `status: APPROVED`, `approved_by` and `approval_reference`; verification requires all three. CI success does not supply human approval. A registry platform manifest must reference the tested image config digest; a tag or multi-platform index alone is insufficient.

Reviewer model, prompts, scanners, policy or memory changes require new matching pilot evidence. Saved legacy intents remain available for reconciliation, but cannot initiate new publication. The deployed source/image identity must match the approved record and retained evidence is checked again before publication.

Local logs from an uncommitted workspace are diagnostic evidence. They cannot replace hosted Python 3.10–3.13 results for an immutable source commit.

## Secret scan fixture handling

`.gitleaks.toml` inherits the default rules and allows only three exact synthetic constants in three named scanner/API tests. It does not allow arbitrary keys in tests or exclude test directories. The local scan covers the current Git-listed source files, excluding ignored runtime data; CI also scans committed history.
