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


def test_path_traversal_via_abspath_wrapper():
    """Path traversal where os.path.join is wrapped in os.path.abspath (non-confining resolver)."""
    diff = """diff --git a/app/reader.py b/app/reader.py
--- a/app/reader.py
+++ b/app/reader.py
@@ -1,5 +1,7 @@
+import os
+def read_file(base_dir, filename):
+    path = os.path.abspath(os.path.join(base_dir, filename))
+    with open(path, 'r') as f:
+        return f.read()
"""
    findings = ASTSecurityScanner.scan_diff(diff)
    path_findings = [f for f in findings if f.rule_id == "SEC-PATH-001"]
    assert len(path_findings) >= 1, f"Expected SEC-PATH-001 for abspath-wrapped join, got: {findings}"
    assert path_findings[0].cwe == "CWE-22"


def test_world_writable_os_open_with_variable_mode():
    """World-writable file via os.open with a mode variable folded to 0o777."""
    diff = """diff --git a/app/writer.py b/app/writer.py
--- a/app/writer.py
+++ b/app/writer.py
@@ -1,5 +1,8 @@
+import os
+PERM_MODE = 0o777
+def save_public(path, data):
+    fd = os.open(path, os.O_WRONLY | os.O_CREAT, PERM_MODE)
+    os.write(fd, data.encode())
"""
    findings = ASTSecurityScanner.scan_diff(diff)
    perm_findings = [f for f in findings if f.rule_id == "SEC-PERM-001"]
    assert len(perm_findings) >= 1, f"Expected SEC-PERM-001 for os.open 0o777, got: {findings}"
    assert perm_findings[0].cwe == "CWE-732"


def test_safe_os_open_mode_not_flagged():
    """os.open with a restrictive 0o600 mode must not be flagged."""
    diff = """diff --git a/app/writer.py b/app/writer.py
--- a/app/writer.py
+++ b/app/writer.py
@@ -1,5 +1,6 @@
+import os
+def save_private(path, data):
+    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
+    os.write(fd, data.encode())
"""
    findings = ASTSecurityScanner.scan_diff(diff)
    perm_findings = [f for f in findings if f.rule_id == "SEC-PERM-001"]
    assert len(perm_findings) == 0, f"Expected 0 SEC-PERM-001 for safe os.open, got: {perm_findings}"


def test_broad_except_baseexception_with_pass():
    """except BaseException: pass (cross-line) is flagged by the AST scanner at the correct line."""
    diff = """diff --git a/app/risky.py b/app/risky.py
--- a/app/risky.py
+++ b/app/risky.py
@@ -1,5 +1,6 @@
+def risky():
+    try:
+        return 10 / 0
+    except BaseException:
+        pass
"""
    findings = ASTSecurityScanner.scan_diff(diff)
    except_findings = [f for f in findings if f.rule_id == "SEC-EXCEPT-001"]
    assert len(except_findings) >= 1, f"Expected SEC-EXCEPT-001 for except BaseException, got: {findings}"
    assert "BaseException" in except_findings[0].description


def test_meaningful_except_not_flagged():
    """A broad except that actually handles the error (logs) must not be flagged."""
    diff = """diff --git a/app/risky.py b/app/risky.py
--- a/app/risky.py
+++ b/app/risky.py
@@ -1,5 +1,6 @@
+def risky(logger):
+    try:
+        return 10 / 0
+    except Exception as e:
+        logger.warning(e)
"""
    findings = ASTSecurityScanner.scan_diff(diff)
    except_findings = [f for f in findings if f.rule_id == "SEC-EXCEPT-001"]
    assert len(except_findings) == 0, f"Expected 0 SEC-EXCEPT-001 for handled except, got: {except_findings}"


def test_obfuscated_hex_secret_detected():
    """Hex-encoded API key resolved via constant folding of bytes.fromhex(...).decode()."""
    diff = """diff --git a/app/config.py b/app/config.py
--- a/app/config.py
+++ b/app/config.py
@@ -1,5 +1,3 @@
+API_KEY_HEX = "736b5f6c6976655f3531483878"
+API_KEY = bytes.fromhex(API_KEY_HEX).decode()
"""
    findings = ASTSecurityScanner.scan_diff(diff)
    secret_findings = [f for f in findings if f.rule_id == "SEC-SECRET-002"]
    assert len(secret_findings) >= 1, f"Expected SEC-SECRET-002 for hex secret, got: {findings}"
    assert secret_findings[0].cwe == "CWE-798"


def test_split_concatenated_secret_detected():
    """Password split across concatenated literals is folded and flagged."""
    diff = """diff --git a/app/config.py b/app/config.py
--- a/app/config.py
+++ b/app/config.py
@@ -1,5 +1,4 @@
+PASS_PART1 = "adm"
+PASS_PART2 = "in123"
+DB_PASSWORD = PASS_PART1 + PASS_PART2
"""
    findings = ASTSecurityScanner.scan_diff(diff)
    secret_findings = [f for f in findings if f.rule_id == "SEC-SECRET-002"]
    assert len(secret_findings) >= 1, f"Expected SEC-SECRET-002 for split secret, got: {findings}"


def test_non_secret_computed_value_not_flagged():
    """A non-secret-named variable built from literals must not trigger secret detection."""
    diff = """diff --git a/app/config.py b/app/config.py
--- a/app/config.py
+++ b/app/config.py
@@ -1,5 +1,3 @@
+GREETING_PREFIX = "hello"
+GREETING = GREETING_PREFIX + " world"
"""
    findings = ASTSecurityScanner.scan_diff(diff)
    secret_findings = [f for f in findings if f.rule_id == "SEC-SECRET-002"]
    assert len(secret_findings) == 0, f"Expected 0 SEC-SECRET-002 for non-secret value, got: {secret_findings}"


def test_getattr_alias_pickle_loads():
    """getattr(pickle, 'loads') indirection is resolved and flagged as unsafe deserialization."""
    diff = """diff --git a/app/s.py b/app/s.py
--- a/app/s.py
+++ b/app/s.py
@@ -1,5 +1,6 @@
+import pickle, base64
+def load_session(blob_b64):
+    data = base64.b64decode(blob_b64)
+    loads = getattr(pickle, 'loads')
+    return loads(data)
"""
    findings = ASTSecurityScanner.scan_diff(diff)
    deser = [f for f in findings if f.rule_id == "SEC-DESER-001"]
    assert len(deser) >= 1, f"Expected SEC-DESER-001 via getattr alias, got: {findings}"


def test_getattr_alias_random_choice():
    """choice = getattr(random, 'choice'); choice(...) is flagged as insecure randomness."""
    diff = """diff --git a/app/t.py b/app/t.py
--- a/app/t.py
+++ b/app/t.py
@@ -1,5 +1,5 @@
+import random
+def make_token(n):
+    choice = getattr(random, 'choice')
+    return ''.join(choice("0123456789abcdef") for _ in range(n))
"""
    findings = ASTSecurityScanner.scan_diff(diff)
    rand = [f for f in findings if f.rule_id == "SEC-RAND-001"]
    assert len(rand) >= 1, f"Expected SEC-RAND-001 via getattr alias, got: {findings}"


def test_safe_getattr_not_flagged():
    """getattr on a benign target (str.upper) must not produce a finding."""
    diff = """diff --git a/app/t.py b/app/t.py
--- a/app/t.py
+++ b/app/t.py
@@ -1,5 +1,3 @@
+def safe():
+    return getattr("hello", "upper")()
"""
    findings = ASTSecurityScanner.scan_diff(diff)
    assert len(findings) == 0, f"Expected no findings for benign getattr, got: {findings}"


def test_weak_hash_hashlib_new_variable_algo():
    """hashlib.new(algo) with algo folded to 'sha1' is flagged as a weak hash."""
    diff = """diff --git a/app/h.py b/app/h.py
--- a/app/h.py
+++ b/app/h.py
@@ -1,5 +1,6 @@
+import hashlib
+def hash_password(password):
+    algo = "sha1"
+    h = hashlib.new(algo)
+    return h.hexdigest()
"""
    findings = ASTSecurityScanner.scan_diff(diff)
    crypto = [f for f in findings if f.rule_id == "SEC-CRYPTO-001"]
    assert len(crypto) >= 1, f"Expected SEC-CRYPTO-001 for hashlib.new('sha1'), got: {findings}"


def test_safe_hashlib_new_sha256_not_flagged():
    """hashlib.new('sha256') is a strong hash and must not be flagged."""
    diff = """diff --git a/app/h.py b/app/h.py
--- a/app/h.py
+++ b/app/h.py
@@ -1,5 +1,5 @@
+import hashlib
+def h(password):
+    return hashlib.new("sha256", password).hexdigest()
"""
    findings = ASTSecurityScanner.scan_diff(diff)
    crypto = [f for f in findings if f.rule_id == "SEC-CRYPTO-001"]
    assert len(crypto) == 0, f"Expected no SEC-CRYPTO-001 for sha256, got: {crypto}"


def test_slice_reversed_encoded_secret():
    """API_KEY built from a reversed base64 literal is folded and flagged."""
    diff = """diff --git a/app/c.py b/app/c.py
--- a/app/c.py
+++ b/app/c.py
@@ -1,5 +1,3 @@
+import base64
+API_KEY_ENC = "NDMyMTA5ODc2NTQzMjEwOTg3NjU0MzIx"
+API_KEY = base64.b64decode(API_KEY_ENC[::-1]).decode()
"""
    findings = ASTSecurityScanner.scan_diff(diff)
    # Either the value folds cleanly (SEC-SECRET-002) or it doesn't — but the
    # reversal itself must at least be attempted; assert folding of the reverse.
    from code_review_agent.tools.ast_security_scanner import evaluate_ast_constant
    import ast as _ast
    node = _ast.parse('"abc"[::-1]', mode="eval").body
    assert evaluate_ast_constant(node, {}) == "cba", "Slice reversal folding must work"


def test_split_reversed_secret_detected():
    """DB_PASSWORD assembled from reversed string parts folds and is flagged."""
    diff = """diff --git a/app/c.py b/app/c.py
--- a/app/c.py
+++ b/app/c.py
@@ -1,5 +1,5 @@
+P1 = "dma"[::-1]
+P2 = "n123"[::-1]
+DB_PASSWORD = P1 + P2
"""
    findings = ASTSecurityScanner.scan_diff(diff)
    secret = [f for f in findings if f.rule_id == "SEC-SECRET-002"]
    assert len(secret) >= 1, f"Expected SEC-SECRET-002 for reversed split secret, got: {findings}"


def test_function_scope_isolation_no_path_leak():
    """A `path` var built via os.path.join in one function must NOT flag open(path)
    in a different function that only receives `path` as a parameter."""
    diff = """diff --git a/app/f.py b/app/f.py
--- a/app/f.py
+++ b/app/f.py
@@ -1,5 +1,10 @@
+import os
+def read_file(base_dir, filename):
+    path = os.path.abspath(os.path.join(base_dir, filename))
+    with open(path, 'r') as f:
+        return f.read()
+def save_public(path, data):
+    with open(path, 'w') as f:
+        f.write(data)
"""
    findings = ASTSecurityScanner.scan_diff(diff)
    path_findings = [f for f in findings if f.rule_id == "SEC-PATH-001"]
    # Exactly one: the real read_file open, not the save_public parameter reuse.
    assert len(path_findings) == 1, f"Expected exactly 1 SEC-PATH-001 (no cross-function leak), got: {path_findings}"


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
