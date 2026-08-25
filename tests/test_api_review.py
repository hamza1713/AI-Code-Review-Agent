"""
Comprehensive unit and integration test suite for the synchronous in-browser review endpoint (POST /api/review).
Tests cover:
- Valid diff paste
- Valid small zip archive review & cross-file AST impact
- Oversized zip and file count rejection (resource limits)
- Non-Python file handling (honest capability reporting)
- Request timeout behavior (clean HTTP 504)
- Malicious code execution prevention (ensuring uploaded code is NEVER executed or imported)
- In-memory rate limiting (HTTP 429)
- Static UI endpoint serving
"""

import io
import os
import zipfile
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path
from fastapi.testclient import TestClient

from code_review_agent.webhook_server import app
from code_review_agent.review_service import rate_limiter, ReviewService, InputValidationError
from code_review_agent.models import SummarizedFindingsJSON


client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Reset rate limiter state before each test."""
    rate_limiter.reset()


@pytest.fixture
def mock_llm_flow():
    """Mock LLM calls inside PRCodeReviewFlow so tests execute fast and deterministically offline."""
    mock_findings = SummarizedFindingsJSON(
        confidence=25,
        findings="Critical security anti-patterns (SQL injection & plaintext credentials) detected in auth module.",
        fix=[],
        recommendations=["Use parameterized SQL queries", "Hash passwords with bcrypt"],
        inline_comments=[],
        suggested_unit_tests="def test_auth():\n    assert True"
    )

    with patch("code_review_agent.main.PRCodeReviewFlow._get_llm") as mock_get_llm:
        mock_llm_inst = MagicMock()
        mock_llm_inst.call.return_value = "COMPLEX"
        mock_get_llm.return_value = mock_llm_inst

        with patch("code_review_agent.main.CodeReviewCrew") as mock_crew_class:
            mock_crew_inst = MagicMock()
            mock_crew_obj = MagicMock()
            mock_crew_obj.kickoff.return_value = mock_findings
            mock_crew_inst.crew.return_value = mock_crew_obj
            mock_crew_class.return_value = mock_crew_inst

            yield


class TestSynchronousReviewAPI:
    """Test suite for POST /api/review and in-browser review functionality."""

    def test_review_valid_diff_paste(self, mock_llm_flow):
        """Verify that pasting a valid unified diff returns 200 OK with all structured findings."""
        sample_diff = """diff --git a/app/user_auth.py b/app/user_auth.py
--- a/app/user_auth.py
+++ b/app/user_auth.py
@@ -10,6 +10,12 @@
+def authenticate_user(username, password):
+    user = db.query(f"SELECT * FROM users WHERE username = '{username}'")
+    if user and user.password == password:
+        print("Authenticated")
+        return True
"""
        response = client.post(
            "/api/review",
            json={"raw_diff": sample_diff}
        )

        assert response.status_code == 200
        data = response.json()

        # Verify structured schema
        assert data["verdict"] in ["ESCALATE", "REQUEST CHANGES"]
        assert isinstance(data["confidence_score"], int)
        assert data["pattern_findings_label"] == "Quick Pattern Scanner (heuristic)"
        assert len(data["pattern_findings"]) >= 2  # SQLi and Plaintext password

        # Verify pattern findings details
        rule_ids = [f["rule_id"] for f in data["pattern_findings"]]
        assert "SEC-SQLI-001" in rule_ids
        assert "SEC-AUTH-001" in rule_ids

        # Verify governance rules
        assert len(data["governance_violations"]) >= 1  # print or raw sql

        # Verify telemetry
        assert data["telemetry"] is not None
        assert "duration_seconds" in data["telemetry"]
        assert "estimated_cost_usd" in data["telemetry"]

        # Verify scope note
        assert "Scope Note" in data["scope_note"]
        assert "heuristic" in data["scope_note"].lower()

    def test_review_valid_small_zip(self, mock_llm_flow):
        """Verify that uploading a valid small zip extracts, indexes AST cross-file callers, and reviews."""
        # Create in-memory zip with 2 interconnected Python files
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "auth_service.py",
                """def authenticate_user(username, password):
    return db.query(f"SELECT * FROM users WHERE username = '{username}'")
"""
            )
            zf.writestr(
                "routes.py",
                """from auth_service import authenticate_user

def login_route(req):
    return authenticate_user(req.user, req.pw)
"""
            )

        zip_buffer.seek(0)
        response = client.post(
            "/api/review",
            files={"zip_file": ("test_repo.zip", zip_buffer.getvalue(), "application/zip")}
        )

        assert response.status_code == 200
        data = response.json()

        assert data["cross_file_impact"]["is_python"] is True
        assert data["cross_file_impact"]["available"] is True
        assert len(data["pattern_findings"]) >= 1

    def test_review_oversized_zip_rejection(self):
        """Verify that a zip with more than 20 files is rejected immediately with HTTP 400."""
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for i in range(25):  # 25 files > MAX_ZIP_FILES (20)
                zf.writestr(f"file_{i}.py", "print('hello')\n")

        zip_buffer.seek(0)
        response = client.post(
            "/api/review",
            files={"zip_file": ("too_many_files.zip", zip_buffer.getvalue(), "application/zip")}
        )

        assert response.status_code == 400
        assert "exceeds maximum limit of 20 files" in response.json()["detail"]

    def test_review_oversized_bytes_zip_rejection(self):
        """Verify that a zip larger than 2MB is rejected immediately with HTTP 400."""
        # Create pseudo-large zip bytes
        large_bytes = b"PK\x03\x04" + b"0" * (3 * 1024 * 1024)  # 3MB > 2MB limit
        response = client.post(
            "/api/review",
            files={"zip_file": ("too_large.zip", large_bytes, "application/zip")}
        )

        assert response.status_code == 400
        assert "exceeds maximum size limit" in response.json()["detail"]

    def test_review_non_python_file_honest_labeling(self, mock_llm_flow):
        """Verify non-Python code reviews report cross-file impact as unavailable: Python only."""
        js_code = """
function login(user, pass) {
    const api_key = "1234567890abcdef123456789";
    console.log("Logging in: " + user);
    return true;
}
"""
        response = client.post(
            "/api/review",
            files={"file": ("login.js", js_code.encode("utf-8"), "application/javascript")}
        )

        assert response.status_code == 200
        data = response.json()

        # Non-Python check
        assert data["cross_file_impact"]["is_python"] is False
        assert data["cross_file_impact"]["available"] is False
        assert "cross-file impact analysis unavailable: Python only" in data["cross_file_impact"]["message"]

        # Pattern scanner should still detect hardcoded secret
        rule_ids = [f["rule_id"] for f in data["pattern_findings"]]
        assert "SEC-SECRET-001" in rule_ids

    def test_review_timeout_handling(self):
        """Verify that requests taking longer than 60s trigger clean HTTP 504 Gateway Timeout."""
        import asyncio

        def slow_review(*args, **kwargs):
            raise asyncio.TimeoutError()

        with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
            response = client.post(
                "/api/review",
                json={"raw_diff": "diff --git a/test.py b/test.py\n+x = 1"}
            )
            assert response.status_code == 504
            assert "timed out after 180 seconds" in response.json()["detail"]

    def test_uploaded_file_never_executed_or_imported(self, tmp_path, mock_llm_flow):
        """
        CRITICAL SECURITY CHECK:
        Upload a file containing malicious code execution payloads (os.system, file creation, globals).
        Verify the code is only parsed via AST and regex, and NEVER executed or imported.
        """
        marker_file = tmp_path / "MALICIOUS_SIDE_EFFECT.txt"
        if marker_file.exists():
            marker_file.unlink()

        malicious_env_var = "TEST_MALICIOUS_EXEC_TRIGGERED"
        if malicious_env_var in os.environ:
            del os.environ[malicious_env_var]

        malicious_code = f"""
import os
os.environ['{malicious_env_var}'] = 'YES_IT_RAN'
try:
    with open(r'{marker_file}', 'w') as f:
        f.write('INJECTED')
except Exception:
    pass

def dangerous_function(cmd):
    import os
    os.system(f"echo {{cmd}}")
"""
        response = client.post(
            "/api/review",
            files={"file": ("malicious_test.py", malicious_code.encode("utf-8"), "text/x-python")}
        )

        assert response.status_code == 200

        # Verify side effects were NEVER triggered
        assert os.environ.get(malicious_env_var) is None, "Security Violation: Uploaded code was dynamically executed!"
        assert not marker_file.exists(), "Security Violation: Uploaded code created a file on disk!"

        # Verify scanner caught the command injection pattern safely
        data = response.json()
        rule_ids = [f["rule_id"] for f in data["pattern_findings"]]
        assert "SEC-CMD-001" in rule_ids

    def test_rate_limiter_blocks_excessive_requests(self):
        """Verify that exceeding the 30 requests/minute IP rate limit returns HTTP 429."""
        # Use low-overhead invalid payload to quickly trigger rate limiter
        for _ in range(30):
            rate_limiter.check_rate_limit("192.168.1.50")

        # 31st request from same IP should raise / return 429
        response = client.post(
            "/api/review",
            json={"raw_diff": "diff --git a/a.py b/a.py\n+x=1"},
            headers={"X-Forwarded-For": "192.168.1.50"}
        )
        assert response.status_code == 429
        assert "Rate limit exceeded" in response.json()["detail"]
        assert "Retry-After" in response.headers

    def test_ui_static_endpoints(self):
        """Verify that GET / and GET /ui serve the single-page HTML interface."""
        res_root = client.get("/")
        assert res_root.status_code == 200
        assert "AI Code Review Agent" in res_root.text
        assert "Submit Code for Review" in res_root.text

        res_ui = client.get("/ui")
        assert res_ui.status_code == 200
        assert "AI Code Review Agent" in res_ui.text
