# scripts/

Utility and release-automation scripts for the AI Code Review Agent.

## Development Launchers

| Script | Platform | What it does |
|--------|----------|--------------|
| `dev-backend.sh` / `dev-backend.bat` | Linux/macOS / Windows | Starts the FastAPI gateway on `:8000` via `uv run python run.py --server` |
| `dev-frontend.sh` / `dev-frontend.bat` | Linux/macOS / Windows | Starts the Vite dev server on `:3000` (`cd frontend && npm run dev`) |

Run both in separate terminals for local development with hot-reload on both sides.

## Release & Corpus Tooling

| Script | Purpose |
|--------|---------|
| `freeze_public_corpus_v2.py` | **Canonical.** Freezes the licensed public PR corpus for evaluation. Produces a versioned snapshot under `samples/evaluation/`. |
| `freeze_public_corpus_v1.py` | Prior iteration, kept for reference; superseded by v2. |
| `pin_images.py` | Resolves Docker image tags to immutable `sha256:` digests and writes them to `docker-compose.yml`. Run before every production deploy. |
| `record_gate.py` | Records a production approval gate attestation (commit SHA, model ID, evaluator identity) into the release-evidence directory. |
| `release_record.py` | Assembles the final release record bundle (evidence dir → signed artifact). |
| `recovery_drill.py` | Exercises the rollback runbook end-to-end against a staging deployment. |
| `smoke_image.py` | Smoke-tests a pinned Docker image: starts the container, hits `/health`, asserts the response, tears down. |
