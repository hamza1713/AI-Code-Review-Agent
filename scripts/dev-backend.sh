#!/usr/bin/env bash
# Launch the FastAPI webhook gateway + review API on http://localhost:8000
set -euo pipefail
cd "$(dirname "$0")/.."
python run.py --server --port 8000
