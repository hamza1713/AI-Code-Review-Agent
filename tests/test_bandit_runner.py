"""
Unit tests for BanditRunner.
Verifies Bandit execution on Python code diffs and JSON output normalization.
"""

import pytest
from code_review_agent.tools.bandit_runner import BanditRunner


class TestBanditRunner:
    """Test suite for Bandit Python AST vulnerability scanner."""

    def test_bandit_is_available(self):
        """Confirm Bandit is detected as available."""
        assert BanditRunner.is_available() is True

    @pytest.mark.slow
    def test_bandit_scans_vulnerable_diff(self):
        """Confirm Bandit detects high-risk vulnerabilities like exec / eval / hardcoded passwords."""
        vuln_diff = """diff --git a/app/tasks.py b/app/tasks.py
--- a/app/tasks.py
+++ b/app/tasks.py
@@ -1,3 +1,5 @@
 def run_code(user_input):
+    exec(user_input)
     return True
"""
        findings = BanditRunner.scan_diff(vuln_diff)
        # Bandit should catch B102 (exec_used)
        assert len(findings) >= 1
        rule_ids = [f.rule_id for f in findings]
        assert any("B102" in rid for rid in rule_ids)
        assert findings[0].analyzer_source == "bandit"

    @pytest.mark.slow
    def test_bandit_on_safe_code(self):
        """Confirm Bandit produces no findings on safe math operations."""
        safe_diff = """diff --git a/app/math.py b/app/math.py
--- a/app/math.py
+++ b/app/math.py
@@ -1,2 +1,3 @@
 def add(a: int, b: int) -> int:
+    return a + b
"""
        findings = BanditRunner.scan_diff(safe_diff)
        assert len(findings) == 0
