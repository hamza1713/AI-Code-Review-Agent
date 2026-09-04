"""Pytest configuration and environment fixtures."""
import sys
from pathlib import Path

# Ensure src directory is in sys.path for test runs
src_dir = Path(__file__).resolve().parent.parent / "src"
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

import pytest
from code_review_agent.cache import global_tool_cache
from code_review_agent.context_engine.code_graph import CodeGraphIndexer
from code_review_agent.crews.code_review_crew.tool_registry import ToolRegistry
from code_review_agent.tools.bandit_runner import BanditRunner
from code_review_agent.tools.ruff_tool import RuffRunner


def _reset_all_caches():
    global_tool_cache.clear()
    CodeGraphIndexer.clear_cache()
    ToolRegistry.reset()
    # Process-lifetime tool-availability caches (see BanditRunner.is_available) —
    # reset so a test that mocks shutil.which isn't poisoned by an earlier test's result.
    BanditRunner._availability_cache = None
    RuffRunner._availability_cache = None


@pytest.fixture(autouse=True)
def reset_global_test_caches():
    """Reset global caches and registries between test runs to ensure complete isolation."""
    _reset_all_caches()
    yield
    _reset_all_caches()
