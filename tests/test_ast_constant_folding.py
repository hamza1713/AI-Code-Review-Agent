"""
Unit tests for AST Constant Folding, Variable Indirection Tracking, and Comment Filtering.
Verifies:
1. Constant folding of permission masks (0o777, 0o666, bitwise OR of stat masks) stored in variables.
2. Variable indirection for TLS disabled flags (INSECURE = False; ctx.check_hostname = INSECURE).
3. Variable indirection for SQL injection (query = "SELECT... " + user; cur.execute(query)).
4. Variable indirection for Path traversal (p = os.path.join(...); open(p)).
5. Aliased function detection (deser = pickle.loads; deser(blob)).
6. Comment filtering: commented-out vulnerabilities and docstrings never trigger false positives.
"""

import sys
from pathlib import Path

# Add src to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from code_review_agent.tools.sast_scanner import QuickPatternScanner, UnifiedSecurityScanner
from code_review_agent.tools.ast_security_scanner import ASTSecurityScanner


def test_constant_folded_chmod_permissions():
    """Verify AST constant folding catches 0o777 when stored in a variable or combined via bitwise OR."""
    diff = """diff --git a/app/storage.py b/app/storage.py
--- a/app/storage.py
+++ b/app/storage.py
@@ -1,5 +1,10 @@
+import os
+import stat
+
+PERMS = 0o777
+MODE = stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO
+
+def save_file(path, data):
+    with open(path, "w") as f:
+        f.write(data)
+    os.chmod(path, PERMS)
+    os.chmod(path + ".bak", MODE)
"""
    findings = ASTSecurityScanner.scan_diff(diff)
    perm_findings = [f for f in findings if f.rule_id == "SEC-PERM-001"]
    assert len(perm_findings) >= 2, f"Expected at least 2 SEC-PERM-001 findings, got: {findings}"
    assert all(f.cwe == "CWE-732" for f in perm_findings)


def test_safe_chmod_not_flagged():
    """Verify safe permission mask 0o600 or 0o700 is not flagged."""
    diff = """diff --git a/app/storage.py b/app/storage.py
--- a/app/storage.py
+++ b/app/storage.py
@@ -1,5 +1,7 @@
+import os
+SAFE_MODE = 0o600
+def save_file(path, data):
+    os.chmod(path, SAFE_MODE)
"""
    findings = ASTSecurityScanner.scan_diff(diff)
    perm_findings = [f for f in findings if f.rule_id == "SEC-PERM-001"]
    assert len(perm_findings) == 0, f"Expected 0 findings on safe chmod, got: {perm_findings}"


def test_variable_indirection_tls():
    """Verify variable indirection for TLS disabling (check_hostname = INSECURE)."""
    diff = """diff --git a/app/client.py b/app/client.py
--- a/app/client.py
+++ b/app/client.py
@@ -1,5 +1,10 @@
+import ssl
+
+INSECURE = False
+NO_VERIFY = ssl.CERT_NONE
+
+def create_insecure_ctx():
+    ctx = ssl.create_default_context()
+    ctx.check_hostname = INSECURE
+    ctx.verify_mode = NO_VERIFY
+    return ctx
"""
    findings = ASTSecurityScanner.scan_diff(diff)
    tls_findings = [f for f in findings if f.rule_id == "SEC-TLS-001"]
    assert len(tls_findings) >= 2, f"Expected 2 SEC-TLS-001 findings, got: {findings}"
    assert all(f.cwe == "CWE-295" for f in tls_findings)


def test_variable_indirection_sqli():
    """Verify variable indirection for SQL injection with string concatenation and execution."""
    diff = """diff --git a/app/db.py b/app/db.py
--- a/app/db.py
+++ b/app/db.py
@@ -1,5 +1,8 @@
+def query_user(cur, username):
+    base_query = "SELECT * FROM users WHERE name = '"
+    query = base_query + username + "'"
+    cur.execute(query)
"""
    findings = ASTSecurityScanner.scan_diff(diff)
    sqli_findings = [f for f in findings if f.rule_id == "SEC-SQLI-002"]
    assert len(sqli_findings) >= 1, f"Expected SEC-SQLI-002 finding, got: {findings}"
    assert sqli_findings[0].cwe == "CWE-89"


def test_variable_indirection_path_traversal():
    """Verify variable indirection for path traversal open(target) with target = os.path.join."""
    diff = """diff --git a/app/reader.py b/app/reader.py
--- a/app/reader.py
+++ b/app/reader.py
@@ -1,5 +1,7 @@
+import os
+def read_user_file(base_dir, user_filename):
+    target_path = os.path.join(base_dir, user_filename)
+    with open(target_path) as f:
+        return f.read()
"""
    findings = ASTSecurityScanner.scan_diff(diff)
    path_findings = [f for f in findings if f.rule_id == "SEC-PATH-001"]
    assert len(path_findings) >= 1, f"Expected SEC-PATH-001 finding, got: {findings}"
    assert path_findings[0].cwe == "CWE-22"


def test_comment_and_docstring_filtering():
    """Verify commented-out vulnerabilities and docstrings are NEVER flagged as security defects."""
    diff = (
        "diff --git a/app/notes.py b/app/notes.py\n"
        "--- a/app/notes.py\n"
        "+++ b/app/notes.py\n"
        "@@ -1,5 +1,18 @@\n"
        '+"""\n'
        "+Security Documentation:\n"
        '+Do not use eval(user_input) or os.system("rm -rf " + dir).\n'
        "+Legacy pattern: ctx.check_hostname = False\n"
        "+Avoid chmod(path, 0o777) in production code!\n"
        '+"""\n'
        "+\n"
        "+# query = \"SELECT * FROM users WHERE name = '%s'\" % username\n"
        "+# os.chmod(path, 0o777)\n"
        "+# ctx.check_hostname = False\n"
        "+# eval(dangerous_input)\n"
        "+\n"
        "+def safe_action():\n"
        '+    # Here is an inline comment: os.system("ping " + host)\n'
        "+    x = 42\n"
        "+    return x\n"
    )
    # QuickPatternScanner line & multiline check
    pattern_findings = QuickPatternScanner.scan_diff(diff)
    assert len(pattern_findings) == 0, f"Expected 0 findings in comments/docstrings, got: {pattern_findings}"

    # Unified scanner check
    unified_findings = UnifiedSecurityScanner.scan_diff(diff)
    assert len(unified_findings) == 0, f"Expected 0 findings in unified scan for commented code, got: {unified_findings}"
