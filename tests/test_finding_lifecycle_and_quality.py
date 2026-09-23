import pytest
from code_review_agent.models import (
    SastFinding,
    InlineComment,
    compute_finding_fingerprint,
    compute_finding_lifecycle,
)
from code_review_agent.tools.ast_security_scanner import ASTSecurityScanner


def test_fingerprint_generation_and_normalization():
    fp1 = compute_finding_fingerprint(
        rule_id="SEC-SQLI-001",
        file_path="src/app/api.py",
        snippet="   cursor.execute('SELECT * FROM users WHERE id = %s' % user_id)  \n",
    )
    fp2 = compute_finding_fingerprint(
        rule_id="SEC-SQLI-001",
        file_path="./src/app/api.py",
        snippet="cursor.execute('SELECT * FROM users WHERE id = %s' % user_id)",
    )
    assert fp1 == fp2, "Fingerprints should be invariant to whitespace and relative path prefixes"


def test_finding_auto_fingerprint():
    finding = SastFinding(
        rule_id="SEC-SQLI-001",
        file_path="app/db.py",
        line_number=10,
        snippet="query = f'SELECT * FROM {table}'",
        description="SQL Injection",
        severity="HIGH",
        fix_recommendation="Use parameterized queries",
    )
    assert finding.fingerprint is not None
    assert len(finding.fingerprint) == 16
    assert finding.lifecycle_status == "NEW"


def test_compute_finding_lifecycle():
    current_finding_1 = SastFinding(
        rule_id="SEC-SQLI-001",
        file_path="app/db.py",
        line_number=10,
        snippet="cursor.execute(f'SELECT * FROM {tbl}')",
        description="SQL injection",
        severity="HIGH",
        fix_recommendation="Use parameterized queries",
    )
    current_finding_2 = SastFinding(
        rule_id="ARCH-SIG-001",
        file_path="app/api.py",
        line_number=45,
        snippet="def fetch_data(id):",
        description="Breaking signature",
        severity="HIGH",
        fix_recommendation="Provide default values",
    )

    # 1. First run: No prior findings -> all NEW
    res1 = compute_finding_lifecycle([current_finding_1, current_finding_2], prior_findings=[])
    assert len(res1.current) == 2
    assert all(f.lifecycle_status == "NEW" for f in res1.current)
    assert len(res1.resolved) == 0

    # 2. Second run: Finding 1 resolved, Finding 2 suppressed
    suppressed_comment = InlineComment(
        path="app/api.py",
        line=45,
        comment_body="Suppressed breaking API signature",
        fingerprint=current_finding_2.fingerprint,
        lifecycle_status="SUPPRESSED",
    )
    res2 = compute_finding_lifecycle(
        [current_finding_2],
        prior_findings=[current_finding_1, suppressed_comment],
        suppressed_fingerprints={current_finding_2.fingerprint},
    )
    assert res2.current[0].lifecycle_status == "SUPPRESSED"
    assert len(res2.resolved) == 1
    assert res2.resolved[0].fingerprint == current_finding_1.fingerprint
    assert res2.resolved[0].lifecycle_status == "RESOLVED"

    # 3. Third run: Finding 1 returns -> REGRESSED
    resolved_finding_1 = res2.resolved[0]
    res3 = compute_finding_lifecycle(
        [current_finding_1],
        prior_findings=[resolved_finding_1],
    )
    assert res3.current[0].lifecycle_status == "REGRESSED"


def test_ast_n_plus_one_detection():
    diff = """diff --git a/app/services.py b/app/services.py
--- a/app/services.py
+++ b/app/services.py
@@ -10,6 +10,8 @@
 def get_order_details(orders, db):
     results = []
+    for order in orders:
+        item = db.query(OrderItem).filter_by(order_id=order.id).first()
+        results.append(item)
     return results
"""
    findings = ASTSecurityScanner.scan_diff(diff)
    nplus_findings = [f for f in findings if f.rule_id == "QUAL-NPLUS1-001"]
    assert len(nplus_findings) == 1
    assert nplus_findings[0].category == "QUALITY"
    assert "N+1" in nplus_findings[0].name


def test_ast_breaking_signature_detection():
    diff = """diff --git a/app/api.py b/app/api.py
--- a/app/api.py
+++ b/app/api.py
@@ -20,3 +20,3 @@
-def process_transaction(user_id, amount):
+def process_transaction(user_id, amount, currency, timeout):
     pass
"""
    findings = ASTSecurityScanner.scan_diff(diff)
    sig_findings = [f for f in findings if f.rule_id == "ARCH-SIG-001"]
    assert len(sig_findings) == 1
    assert sig_findings[0].category == "ARCHITECTURE"
    assert "breaking api" in sig_findings[0].description.lower()
