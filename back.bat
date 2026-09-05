@echo off
REM Launch the FastAPI backend review server
cd /d "%~dp0"
python run.py --server --port 8000
