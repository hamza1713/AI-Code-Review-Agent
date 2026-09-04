"""
Unit tests for Model Context Protocol (MCP) Server.
Verifies tool registration, FastMCP schema compliance, tool execution,
and graceful error handling for IDE clients (Cursor, Claude Code).
"""

import json
import pytest
from code_review_agent.mcp_server import (
    mcp,
    scan_sast_patterns,
    check_governance_rules,
    find_impacted_callers,
    generate_unit_tests,
    review_diff
)


class TestMCPServer:
    """Test suite verifying MCP tool definitions and execution."""

    def test_mcp_tools_registered(self):
        """Ensure all 5 MCP tools are properly declared on the server."""
        assert mcp.name == "ai-code-reviewer"
        assert callable(review_diff)
        assert callable(scan_sast_patterns)
        assert callable(check_governance_rules)
        assert callable(find_impacted_callers)
        assert callable(generate_unit_tests)

    def test_mcp_scan_sast_patterns(self):
        """Verify scan_sast_patterns returns valid JSON with detected vulnerabilities."""
        sql_diff = """--- a/db.py
+++ b/db.py
@@ -10,3 +10,3 @@
 def get_user(user_id):
-    return db.query("SELECT * FROM users WHERE id = %s", (user_id,))
+    return db.query(f"SELECT * FROM users WHERE id = {user_id}")
"""
        raw_json = scan_sast_patterns(sql_diff)
        data = json.loads(raw_json)
        assert "count" in data
        assert data["count"] >= 1
        assert any(f["cwe"] == "CWE-89" for f in data["findings"])

    def test_mcp_check_governance_rules(self):
        """Verify check_governance_rules returns valid JSON with rule violations."""
        print_diff = """--- a/app/core/service.py
+++ b/app/core/service.py
@@ -5,3 +5,4 @@
 def compute():
+    print("debug log")
     return True
"""
        raw_json = check_governance_rules(print_diff)
        data = json.loads(raw_json)
        assert "violations" in data
        assert isinstance(data["violations"], list)

    def test_mcp_generate_unit_tests(self):
        """Verify generate_unit_tests produces pytest code."""
        code = generate_unit_tests("def add_numbers(a: int, b: int = 0) -> int:")
        assert "import pytest" in code
        assert "TestAddNumbers" in code or "test_" in code

    def test_mcp_find_impacted_callers(self):
        """Verify find_impacted_callers returns JSON map."""
        raw_json = find_impacted_callers(["process_payment"])
        data = json.loads(raw_json)
        assert "impacted_callers" in data
        assert isinstance(data["impacted_callers"], dict)

    def test_mcp_review_diff_clean(self):
        """Verify review_diff executes review flow and returns structured JSON."""
        clean_diff = """--- a/app/math_utils.py
+++ b/app/math_utils.py
@@ -1,3 +1,5 @@
 def multiply(a: int, b: int) -> int:
+    # Multiply two integers
     return a * b
"""
        raw_json = review_diff(clean_diff)
        data = json.loads(raw_json)
        assert "verdict" in data
        assert data["verdict"] in ["APPROVE", "REQUEST CHANGES", "ESCALATE"]
        assert "confidence_score" in data
