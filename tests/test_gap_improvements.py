"""
Unit tests for the AST and pipeline gap improvements:
- Lock defined but never acquired (CWE-362)
- Singleton metaclass TOCTOU race (CWE-362)
- Insecure password hashing (CWE-916 CRITICAL)
- Unsalted password hash (CWE-760 HIGH)
- O(n) pop(0) on List (PERF-LIST-001)
- Repeated O(n log n) Sort on List Insertion (PERF-SORT-001)
- O(n^2) Quadratic Nested Loop Iteration (PERF-NESTED-001)
- Implicit None Return Violating Return Type Contract (QUAL-RETURN-001)
- Crew Advisory Findings Promotion in Reconciler
"""

import pytest
from code_review_agent.tools.ast_security_scanner import ASTSecurityScanner
from code_review_agent.synthesis.reconciler import SynthesisReconciler
from code_review_agent.tools.crew_finding_parser import parse_crew_advisory_findings
from code_review_agent.models import SummarizedFindingsJSON, Fix, SastFinding


def test_unused_lock_detected():
    diff = """diff --git a/app/db.py b/app/db.py
--- a/app/db.py
+++ b/app/db.py
@@ -1,5 +1,8 @@
+import threading
+import sqlite3
+class ConnectionPool:
+    def __init__(self):
+        self._lock = threading.Lock()
+    def run_query(self, query):
+        return []
"""
    findings = ASTSecurityScanner.scan_diff(diff)
    lock_findings = [f for f in findings if f.rule_id == "SEC-LOCK-001"]
    assert len(lock_findings) == 1
    assert lock_findings[0].cwe == "CWE-362"
    assert lock_findings[0].severity == "HIGH"


def test_used_lock_not_flagged():
    diff = """diff --git a/app/db.py b/app/db.py
--- a/app/db.py
+++ b/app/db.py
@@ -1,5 +1,9 @@
+import threading
+class SafePool:
+    def __init__(self):
+        self._lock = threading.Lock()
+    def run_query(self, query):
+        with self._lock:
+            return []
"""
    findings = ASTSecurityScanner.scan_diff(diff)
    lock_findings = [f for f in findings if f.rule_id == "SEC-LOCK-001"]
    assert len(lock_findings) == 0


def test_singleton_toctou_race_detected():
    diff = """diff --git a/app/meta.py b/app/meta.py
--- a/app/meta.py
+++ b/app/meta.py
@@ -1,5 +1,9 @@
+class SingletonMeta(type):
+    _instances = {}
+    def __call__(cls, *args, **kwargs):
+        if cls not in cls._instances:
+            cls._instances[cls] = super().__call__(*args, **kwargs)
+        return cls._instances[cls]
"""
    findings = ASTSecurityScanner.scan_diff(diff)
    race_findings = [f for f in findings if f.rule_id == "SEC-RACE-001"]
    assert len(race_findings) == 1
    assert race_findings[0].cwe == "CWE-362"
    assert race_findings[0].severity == "HIGH"


def test_password_hash_md5_escalates_to_critical_and_flags_salt():
    diff = """diff --git a/app/auth.py b/app/auth.py
--- a/app/auth.py
+++ b/app/auth.py
@@ -1,5 +1,4 @@
+import hashlib
+def hash_password(password: str) -> str:
+    return hashlib.md5(password.encode()).hexdigest()
"""
    findings = ASTSecurityScanner.scan_diff(diff)
    pw_findings = [f for f in findings if f.cwe == "CWE-327"]
    assert len(pw_findings) == 1
    assert pw_findings[0].severity == "CRITICAL"

    salt_findings = [f for f in findings if f.rule_id == "SEC-CRYPTO-002"]
    assert len(salt_findings) == 1
    assert salt_findings[0].cwe == "CWE-760"
    assert salt_findings[0].severity == "HIGH"


def test_perf_pop_zero_detected():
    diff = """diff --git a/app/queue.py b/app/queue.py
--- a/app/queue.py
+++ b/app/queue.py
@@ -1,5 +1,5 @@
+class TaskQueue:
+    def pop_next(self):
+        return self._items.pop(0)
"""
    findings = ASTSecurityScanner.scan_diff(diff)
    perf_findings = [f for f in findings if f.rule_id == "PERF-LIST-001"]
    assert len(perf_findings) == 1
    assert perf_findings[0].category == "QUALITY"
    assert perf_findings[0].severity == "MEDIUM"


def test_perf_sort_on_append_receiver_detected():
    diff = """diff --git a/app/queue.py b/app/queue.py
--- a/app/queue.py
+++ b/app/queue.py
@@ -1,5 +1,6 @@
+class TaskQueue:
+    def add(self, item):
+        self._items.append(item)
+        self._items.sort()
"""
    findings = ASTSecurityScanner.scan_diff(diff)
    perf_findings = [f for f in findings if f.rule_id == "PERF-SORT-001"]
    assert len(perf_findings) == 1
    assert "self._items" in perf_findings[0].description


def test_perf_nested_loop_duplicates_detected():
    diff = """diff --git a/app/queue.py b/app/queue.py
--- a/app/queue.py
+++ b/app/queue.py
@@ -1,5 +1,7 @@
+class TaskQueue:
+    def find_duplicates(self):
+        for i in range(len(self._items)):
+            for j in range(len(self._items)):
+                if i != j and self._items[i] == self._items[j]:
+                    pass
"""
    findings = ASTSecurityScanner.scan_diff(diff)
    perf_findings = [f for f in findings if f.rule_id == "PERF-NESTED-001"]
    assert len(perf_findings) == 1
    assert perf_findings[0].category == "QUALITY"


def test_implicit_none_return_detected():
    diff = """diff --git a/app/report.py b/app/report.py
--- a/app/report.py
+++ b/app/report.py
@@ -1,5 +1,8 @@
+class ReportGenerator:
+    def generate(self, name: str) -> str:
+        try:
+            return "data"
+        except Exception:
+            pass
"""
    findings = ASTSecurityScanner.scan_diff(diff)
    ret_findings = [f for f in findings if f.rule_id == "QUAL-RETURN-001"]
    assert len(ret_findings) == 1
    assert ret_findings[0].category == "QUALITY"
    assert ret_findings[0].severity == "MEDIUM"


def test_crew_advisory_parser_and_reconcile_integration():
    summarized = SummarizedFindingsJSON(
        verdict="ESCALATE",
        confidence=20,
        findings="Critical race condition in WorkerPool and infinite recursion in fib().",
        fix=[
            Fix(
                description="Critical race condition in WorkerPool.processed increment.",
                solutions="Use asyncio.Lock() to synchronize the increment.",
                explanation="Multiple async workers increment self.processed concurrently without synchronization.",
                file_path="app/worker.py",
                line_number=120,
            ),
            Fix(
                description="High severity infinite recursion in fib() for negative input.",
                solutions="Raise ValueError for n < 0.",
                explanation="fib(-1) calls fib(-1) recursively leading to RecursionError.",
                file_path="app/math_util.py",
                line_number=195,
            ),
        ],
        blocking_reasons=[
            "Critical race condition in WorkerPool processing logic",
        ],
    )

    crew_findings = parse_crew_advisory_findings(summarized, file_hint="app/worker.py")
    assert len(crew_findings) >= 2
    assert any(f.cwe == "CWE-362" for f in crew_findings)
    assert any(f.cwe == "CWE-674" for f in crew_findings)
    assert all(f.analyzer_source == "llm-crew" for f in crew_findings)

    # Reconcile report should include these findings
    report = SynthesisReconciler.reconcile(crew_findings=crew_findings)
    assert report.findings_count >= 2
    cwes = [f.cwe for f in report.findings]
    assert "CWE-362" in cwes
    assert "CWE-674" in cwes
    assert report.self_check() == []
