"""
Automated Security Benchmark Test for vuln_samples.py.
Validates:
1. 100% Recall on all 12 tagged vulnerabilities across OWASP Top 10 & CWEs.
2. 0 False Positives on all 4 clean controls (get_user_safe, hash_password_safe, make_token_safe, ping_host_safe).
3. 0 False Positives on standard library imports (import subprocess, import pickle).
4. No severity inflation (safe subprocess calls and imports are never CRITICAL).
5. Clean CWE normalization without duplicate 'CWE-CWE-' prefixes.
6. Calibrated bounded quality scoring.
"""

import sys
from pathlib import Path

# Add src to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from code_review_agent.review_service import ReviewService
from code_review_agent.tools import SastEngine
from code_review_agent.synthesis import SynthesisReconciler

VULN_BENCHMARK_CODE = '''"""
vuln_samples.py benchmark: 12 tagged issues, 4 clean controls.
"""
import os
import hashlib
import pickle
import sqlite3
import subprocess
import ssl
import random
import yaml

# ISSUE 1 — Hardcoded credentials / secrets (CWE-798 & CWE-259)
API_KEY = "mock_secret_key_1234567890abcdef"
DB_PASSWORD = "admin123"

# ISSUE 2 — SQL injection via string formatting (CWE-89)
def get_user(conn: sqlite3.Connection, username: str):
    cur = conn.cursor()
    query = "SELECT * FROM users WHERE name = '%s'" % username
    cur.execute(query)
    return cur.fetchall()

# ISSUE 3 — OS command injection (CWE-78)
def ping_host(host: str):
    return subprocess.run("ping -c 1 " + host, shell=True, capture_output=True)

# ISSUE 4 — Unsafe deserialization (CWE-502)
def load_session(blob: bytes):
    return pickle.loads(blob)

# ISSUE 5 — Weak / broken cryptographic hash (CWE-327)
def hash_password(password: str) -> str:
    return hashlib.md5(password.encode()).hexdigest()

# ISSUE 6 — eval() on external input (CWE-95)
def calculate(expr: str):
    return eval(expr)

# ISSUE 7 — Disabled TLS certificate verification (CWE-295)
def insecure_context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx

# ISSUE 8 — Insecure randomness for security use (CWE-330)
def make_token(n: int = 16) -> str:
    return "".join(random.choice("0123456789abcdef") for _ in range(n))

# ISSUE 9 — Unsafe YAML load (CWE-502)
def parse_config(text: str):
    return yaml.load(text)

# ISSUE 10 — Path traversal (CWE-22)
def read_file(base_dir: str, filename: str) -> str:
    with open(os.path.join(base_dir, filename)) as f:
        return f.read()

# ISSUE 11 — World-writable file permissions (CWE-732)
def save_public(path: str, data: str):
    with open(path, "w") as f:
        f.write(data)
    os.chmod(path, 0o777)

# ISSUE 12 — Overly broad exception swallow (CWE-703 / CWE-390)
def risky():
    try:
        return 10 / 0
    except Exception:
        pass

# =============================================================================
# CLEAN CONTROLS — a good reviewer should NOT flag these.
# =============================================================================
def get_user_safe(conn: sqlite3.Connection, username: str):
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE name = ?", (username,))
    return cur.fetchall()

def hash_password_safe(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)

def make_token_safe(n: int = 32) -> str:
    import secrets
    return secrets.token_hex(n)

def ping_host_safe(host: str):
    return subprocess.run(["ping", "-c", "1", host], capture_output=True)

if __name__ == "__main__":
    print("Static-analysis benchmark file. 12 tagged issues, 4 clean controls.")
'''

def test_vuln_samples_benchmark_precision_and_recall():
    """Verify 100% recall on 12 security issues, 0 FP on clean controls, and calibrated scoring."""
    diff = ReviewService.file_to_unified_diff("vuln_samples.py", VULN_BENCHMARK_CODE)
    findings = SastEngine.scan_diff(diff)
    reconciled = SynthesisReconciler.reconcile(sast_findings=findings, pr_content=diff)

    print(f"\\n--- Benchmark Results ---")
    print(f"Reconciled findings count: {reconciled.findings_count}")
    print(f"Score: {reconciled.score}/100 | Verdict: {reconciled.verdict} | Worst: {reconciled.worst_severity}")
    for i, rf in enumerate(reconciled.findings, 1):
        print(f"  {i}. [{rf.severity}] {rf.cwe} @ L{rf.line}: {rf.title[:65]} | sources={rf.sources}")

    # 1. Verify No Double CWE Prefixes
    for rf in reconciled.findings:
        if rf.cwe:
            assert not rf.cwe.startswith("CWE-CWE-"), f"Double prefix detected in CWE: {rf.cwe}"

    # 2. Verify 0 False Positives on Clean Controls
    clean_lines = {
        "get_user_safe",
        "hash_password_safe",
        "make_token_safe",
        "ping_host_safe",
    }
    for rf in reconciled.findings:
        for cl in clean_lines:
            assert cl not in rf.title, f"Clean control '{cl}' was falsely flagged: {rf}"
            assert cl not in rf.fix, f"Clean control '{cl}' referenced in fix: {rf}"

    # Specifically verify ping_host_safe has no command injection or B607/B603 findings
    ping_safe_findings = [f for f in findings if "ping_host_safe" in f.snippet]
    assert len(ping_safe_findings) == 0, f"ping_host_safe had unexpected SAST findings: {ping_safe_findings}"

    # 3. Verify 0 False Positives on Standard Library Imports
    import_findings = [
        f for f in findings
        if f.snippet.strip().startswith("import ") and f.rule_id.startswith("BANDIT-B4")
    ]
    assert len(import_findings) == 0, f"Import statements were falsely flagged: {import_findings}"

    # 4. Verify 100% Recall on Tagged Vulnerabilities
    detected_cwes = {rf.cwe for rf in reconciled.findings if rf.cwe}
    expected_cwes = {
        "CWE-798",  # API_KEY
        "CWE-259",  # DB_PASSWORD
        "CWE-89",   # SQL injection
        "CWE-78",   # OS command injection
        "CWE-502",  # Insecure deserialization (pickle / yaml)
        "CWE-327",  # Weak hash
        "CWE-95",   # Eval injection
        "CWE-295",  # Disabled TLS
        "CWE-330",  # Insecure random
        "CWE-22",   # Path traversal
        "CWE-732",  # Permissive chmod 0777
        "CWE-703",  # Swallowed exception
    }
    missing_cwes = expected_cwes - detected_cwes
    assert not missing_cwes, f"Missing tagged CWEs in benchmark: {missing_cwes}"

    # 5. Verify Calibrated Scoring and Escalation
    assert reconciled.verdict == "ESCALATE", "PR with critical command/SQL injection must escalate"
    assert reconciled.worst_severity == "CRITICAL"
    # Score should be non-saturating and floored gracefully (calibrated to 15, not collapsing to 0 or 5)
    assert 10 <= reconciled.score <= 30, f"Unexpected score: {reconciled.score}"


def test_user_uploaded_media_file():
    """Verify performance on the exact file uploaded by the user."""
    uploaded_file = Path(r"C:\Users\Hamza Ali\.gemini\antigravity\brain\0454e770-47ad-46fb-97b3-50eed558cc84\.user_uploaded\media_1788895538218.py")
    if not uploaded_file.exists():
        return

    content = uploaded_file.read_text(encoding="utf-8")
    diff = ReviewService.file_to_unified_diff("vuln_samples.py", content)
    findings = SastEngine.scan_diff(diff)
    reconciled = SynthesisReconciler.reconcile(sast_findings=findings, pr_content=diff)

    # Clean controls check
    clean_lines = {"get_user_safe", "hash_password_safe", "make_token_safe", "ping_host_safe"}
    for rf in reconciled.findings:
        for cl in clean_lines:
            assert cl not in rf.title, f"Clean control '{cl}' was flagged in uploaded file: {rf}"

    # Imports check
    import_findings = [
        f for f in findings
        if f.snippet.strip().startswith("import ") and f.rule_id.startswith("BANDIT-B4")
    ]
    assert len(import_findings) == 0, f"Imports flagged: {import_findings}"

    # 100% recall on tagged issues
    detected_cwes = {rf.cwe for rf in reconciled.findings if rf.cwe}
    expected_cwes = {
        "CWE-798", "CWE-259", "CWE-89", "CWE-78", "CWE-502",
        "CWE-327", "CWE-95", "CWE-295", "CWE-330", "CWE-22",
        "CWE-732", "CWE-703"
    }
    missing = expected_cwes - detected_cwes
    assert not missing, f"Missing CWEs on user uploaded file: {missing}"
    assert reconciled.verdict == "ESCALATE"

