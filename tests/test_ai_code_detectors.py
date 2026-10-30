"""
Unit tests for AICodeDefectScanner.
Verifies detection of unawaited async coroutines, blocking calls in async context,
hollow security stubs, and cross-file contract signature drift.
"""

from code_review_agent.context_engine.code_graph import CodeGraphIndexer
from code_review_agent.tools.ai_code_detectors import AICodeDefectScanner


class TestAICodeDetectors:
    """Test suite for AI-generated code failure patterns."""

    def test_detects_unawaited_coroutine_in_if_condition(self):
        diff = """diff --git a/app/routes.py b/app/routes.py
--- a/app/routes.py
+++ b/app/routes.py
@@ -1,4 +1,7 @@
+async def check_access(user_id):
+    return False
+
 def handle_request(req):
+    if check_access(req.user):
+        return "authorized"
"""
        findings = AICodeDefectScanner.scan_diff(diff)
        assert len(findings) >= 1
        async_finding = next(
            f for f in findings if f.rule_id == "AI-ASYNC-001"
        )
        assert async_finding.severity == "HIGH"
        assert "check_access" in async_finding.description
        assert "await" in async_finding.fix_recommendation

    def test_detects_blocking_sync_calls_in_async_def(self):
        diff = """diff --git a/app/async_service.py b/app/async_service.py
--- a/app/async_service.py
+++ b/app/async_service.py
@@ -1,2 +1,4 @@
 async def fetch_data(url):
+    time.sleep(5)
+    return requests.get(url)
"""
        findings = AICodeDefectScanner.scan_diff(diff)
        blocking_findings = [f for f in findings if f.rule_id == "AI-ASYNC-002"]
        assert len(blocking_findings) >= 1
        call_names = [f.description for f in blocking_findings]
        assert any("time.sleep" in d for d in call_names)

    def test_detects_hollow_security_function_stub(self):
        diff = """diff --git a/app/auth.py b/app/auth.py
--- a/app/auth.py
+++ b/app/auth.py
@@ -1,1 +1,3 @@
+def validate_jwt_token(token):
+    pass
"""
        findings = AICodeDefectScanner.scan_diff(diff)
        stub_findings = [f for f in findings if f.rule_id == "AI-STUB-001"]
        assert len(stub_findings) >= 1
        assert stub_findings[0].severity == "HIGH"
        assert "validate_jwt_token" in stub_findings[0].description

    def test_detects_contract_drift_argument_count_mismatch(self, tmp_path):
        f_target = tmp_path / "service.py"
        f_target.write_text(
            """def send_notification(recipient, message):
    return True
""",
            encoding="utf-8",
        )

        indexer = CodeGraphIndexer(repo_root=str(tmp_path))
        indexer.index_repository(force_reindex=True)

        diff = """diff --git a/app/controller.py b/app/controller.py
--- a/app/controller.py
+++ b/app/controller.py
@@ -1,2 +1,3 @@
 def on_event(event):
+    send_notification("alice@example.com", "Hello", "priority_high", "extra_arg")
"""
        findings = AICodeDefectScanner.scan_diff(diff, code_graph=indexer)
        contract_findings = [
            f for f in findings if f.rule_id == "AI-CONTRACT-001"
        ]
        assert len(contract_findings) >= 1
        assert "send_notification" in contract_findings[0].description
        assert "positional argument" in contract_findings[0].description

    def test_does_not_flag_awaited_coroutine_in_if_condition(self):
        diff = """diff --git a/app/routes.py b/app/routes.py
--- a/app/routes.py
+++ b/app/routes.py
@@ -1,4 +1,7 @@
+async def check_access(user_id):
+    return False
+
 def handle_request(req):
+    if await check_access(req.user):
+        return "authorized"
"""
        findings = AICodeDefectScanner.scan_diff(diff)
        async_findings = [f for f in findings if f.rule_id == "AI-ASYNC-001"]
        assert len(async_findings) == 0

    def test_does_not_flag_contract_drift_with_starred_args(self, tmp_path):
        f_target = tmp_path / "service.py"
        f_target.write_text(
            """def send_notification(recipient, message):
    return True
""",
            encoding="utf-8",
        )
        indexer = CodeGraphIndexer(repo_root=str(tmp_path))
        indexer.index_repository(force_reindex=True)
        diff = """diff --git a/app/controller.py b/app/controller.py
--- a/app/controller.py
+++ b/app/controller.py
@@ -1,2 +1,3 @@
 def on_event(event):
+    send_notification(*event.args)
"""
        findings = AICodeDefectScanner.scan_diff(diff, code_graph=indexer)
        contract_findings = [f for f in findings if f.rule_id == "AI-CONTRACT-001"]
        assert len(contract_findings) == 0
