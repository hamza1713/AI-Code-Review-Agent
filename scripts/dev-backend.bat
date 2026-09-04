@echo off
REM Launch the FastAPI webhook gateway + review API on http://localhost:8000
cd /d "%~dp0.."
python run.py --server --port 8000
