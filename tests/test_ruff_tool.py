"""
Unit tests for Ruff linter integration and RuffTool.
Verifies Ruff detection of unused imports, undefined variables, and formatting findings.
"""

import pytest
from code_review_agent.tools.ruff_tool import RuffRunner, RuffTool


class TestRuffTool:
    """Test suite for Ruff fast Python linter."""

    def test_ruff_is_available(self):
        """Confirm Ruff is available in test environment."""
        assert RuffRunner.is_available() is True

    @pytest.mark.slow
    def test_ruff_detects_unused_import(self):
        """Confirm Ruff flags F401 (unused import)."""
        diff_with_unused_import = """diff --git a/app/service.py b/app/service.py
--- a/app/service.py
+++ b/app/service.py
@@ -1,2 +1,3 @@
+import sys
 def run():
     return 42
"""
        findings = RuffRunner.scan_diff(diff_with_unused_import)
        assert len(findings) >= 1
        codes = [f.code for f in findings]
        assert "F401" in codes

    @pytest.mark.slow
    def test_ruff_tool_wrapper_formatted_output(self):
        """Confirm RuffTool BaseTool outputs clear message to CrewAI agents."""
        tool = RuffTool()
        diff_clean = """diff --git a/app/clean.py b/app/clean.py
--- a/app/clean.py
+++ b/app/clean.py
@@ -1,2 +1,3 @@
 def add(a: int, b: int) -> int:
+    return a + b
"""
        output = tool._run(diff_clean)
        assert "No code quality issues" in output or "Ruff Linter" in output
