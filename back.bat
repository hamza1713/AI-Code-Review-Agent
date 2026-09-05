@echo off
REM Auto-cleanup any stale process listening on port 8000
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8000 ^| findstr LISTENING') do (
    echo [back.bat] Freeing port 8000 from existing process (PID %%a)...
    taskkill /f /pid %%a >nul 2>&1
)

echo [back.bat] Starting AI Code Review Webhook Gateway on http://localhost:8000...
cd /d "%~dp0"
python run.py --server --port 8000
