#!/usr/bin/env bash
# Launch the Vite dev server on http://localhost:3000 (proxies /api, /jobs, /health to :8000)
set -euo pipefail
cd "$(dirname "$0")/../frontend"
npm run dev
