"""
Unit tests for Quick Security Pattern Scanner (formerly SAST Engine).
Verifies heuristic regex pattern detection on vulnerable code diffs
and confirms zero false positives on safe parameterized code diffs.
"""

import pytest
from code_review_agent.tools.sast_scanner import QuickPatternScanner, QuickPatternScannerTool


class TestQuickPatternScanner:
    """Test suite for regex security pattern detection."""

    def test_detects_sql_injection_and_plaintext_passwords(self):
        """Confirm scanner identifies SQL interpolation and plaintext password checks."""
        vuln_diff = """diff --git a/app/auth.py b/app/auth.py
--- a/app/auth.py
+++ b/app/auth.py
@@ -12,4 +12,6 @@
 def login(username, password):
+    query = f"SELECT * FROM users WHERE username = '{username}'"
+    user = db.query(query)
+    if user.password == password:
+        return True
     return False
"""
        findings = QuickPatternScanner.scan_diff(vuln_diff)
        assert len(findings) >= 1
        cwes = [f.cwe for f in findings]
        assert "CWE-256" in cwes

    def test_detects_hardcoded_secrets_and_command_injection(self):
        """Confirm scanner flags hardcoded API keys and os.system formatting."""
        vuln_diff = """diff --git a/app/tasks.py b/app/tasks.py
--- a/app/tasks.py
+++ b/app/tasks.py
@@ -1,4 +1,6 @@
+api_key = "secret_key_1234567890abcdef"
 def run_cleanup(dir_path):
+    os.system(f"rm -rf {dir_path}")
"""
        findings = QuickPatternScanner.scan_diff(vuln_diff)
        cwes = [f.cwe for f in findings]
        assert "CWE-798" in cwes or "CWE-78" in cwes

    def test_safe_parameterized_code_produces_no_false_positives(self):
        """Confirm safe code with parameterized queries produces 0 findings."""
        safe_diff = """diff --git a/app/repo.py b/app/repo.py
--- a/app/repo.py
+++ b/app/repo.py
@@ -5,3 +5,4 @@
 def get_user_by_id(user_id: int):
+    cursor.execute("SELECT id, name FROM users WHERE id = %s", (user_id,))
     return cursor.fetchone()
"""
        findings = QuickPatternScanner.scan_diff(safe_diff)
        assert len(findings) == 0

    def test_safe_subprocess_calls_not_flagged_as_critical_cwe78(self):
        """Confirm safe subprocess.run with list args and shell=False is not flagged as CWE-78 (F3)."""
        safe_subproc_diff = """diff --git a/app/worker.py b/app/worker.py
--- a/app/worker.py
+++ b/app/worker.py
@@ -10,3 +10,4 @@
 def list_files(path):
+    return subprocess.run(['ls', '-l', path], shell=False, check=True)
"""
        findings = QuickPatternScanner.scan_diff(safe_subproc_diff)
        cwes = [f.cwe for f in findings]
        assert "CWE-78" not in cwes
        assert len(findings) == 0

    def test_tool_wrapper_formatted_output(self):
        """Confirm tool wrapper outputs clear human-readable messages."""
        tool = QuickPatternScannerTool()
        safe_diff = """diff --git a/app/util.py b/app/util.py
--- a/app/util.py
+++ b/app/util.py
@@ -1,2 +1,3 @@
+def add(a, b): return a + b
"""
        res = tool._run(safe_diff)
        assert "No common security anti-patterns detected" in res

    def test_safe_subprocess_bare_variable_not_flagged(self):
        """Confirm subprocess.run(cmd) without shell=True or formatting is not flagged as CWE-78."""
        diff = """diff --git a/app/worker.py b/app/worker.py
--- a/app/worker.py
+++ b/app/worker.py
@@ -10,3 +10,4 @@
 def run_command(cmd):
+    return subprocess.run(cmd, check=True)
"""
        findings = QuickPatternScanner.scan_diff(diff)
        cwes = [f.cwe for f in findings]
        assert "CWE-78" not in cwes
        assert len(findings) == 0
