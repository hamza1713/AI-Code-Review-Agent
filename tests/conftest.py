"""Pytest configuration and environment fixtures."""
import sys
from pathlib import Path

# Ensure src directory is in sys.path for test runs
src_dir = Path(__file__).resolve().parent.parent / "src"
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))
