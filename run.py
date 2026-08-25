"""
Root runner script for the AI Code Review Agent.
Adds 'src' directory to Python path and invokes the CLI entrypoint.
"""

import sys
import os
from pathlib import Path

# Force UTF-8 stdout/stderr on Windows to avoid charmap UnicodeEncodeErrors
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Automatically add 'src' directory to sys.path
src_path = Path(__file__).resolve().parent / "src"
if str(src_path) not in sys.path:
    sys.path.insert(0, str(src_path))

from code_review_agent.main import cli_main

if __name__ == "__main__":
    cli_main()
