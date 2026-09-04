@echo off
REM Launch the Vite dev server on http://localhost:3000 (proxies /api, /jobs, /health to :8000)
cd /d "%~dp0..\frontend"
npm run dev
