"""
Unit tests for SandboxTestRunner and Empirical Evidence Badging.
Verifies safe isolated test execution, defect reproduction, timeout enforcement,
and accurate badge assignment ([PASSING], [REPRODUCED], [UNVERIFIED], [HEURISTIC]).
"""

import pytest
from code_review_agent.sandbox.test_runner import SandboxTestRunner


class TestSandboxRunner:
    """Test suite for the isolated subprocess test execution sandbox."""

    @pytest.mark.slow
    def test_sandbox_executes_passing_tests(self):
        """Verify passing test suite receives PASSING badge."""
        test_code = """
import pytest

def test_addition():
    assert 2 + 2 == 4

def test_string_formatting():
    assert f"hello {'world'}" == "hello world"
"""
        result = SandboxTestRunner.run_tests(test_code, timeout_seconds=25.0)
        assert result.executed is True
        assert result.status == "PASSED"
        assert result.evidence_badge == "PASSING"
        assert result.tests_run >= 2
        assert result.failures == 0
        assert "passed" in result.summary_message.lower()

    @pytest.mark.slow
    def test_sandbox_reproduces_failing_defect(self):
        """Verify failing test asserting a defect receives REPRODUCED badge."""
        test_code = """
import pytest

def test_reproduce_bug():
    # Simulate a bug reproduction assertion
    actual_discount = 0.50
    expected_discount = 0.20
    assert actual_discount == expected_discount, "Discount calculation bug reproduced!"
"""
        result = SandboxTestRunner.run_tests(test_code, timeout_seconds=25.0)
        assert result.executed is True
        assert result.status == "REPRODUCED_DEFECT"
        assert result.evidence_badge == "REPRODUCED"
        assert result.failures >= 1
        assert "REPRODUCED" in result.summary_message

    def test_sandbox_handles_empty_tests(self):
        """Verify empty or whitespace test code returns SKIPPED / HEURISTIC badge."""
        result = SandboxTestRunner.run_tests("   ", timeout_seconds=5.0)
        assert result.executed is False
        assert result.status == "SKIPPED"
        assert result.evidence_badge == "HEURISTIC"

    @pytest.mark.slow
    def test_sandbox_timeout_safety(self):
        """Verify infinite loops or blocking calls are safely killed after timeout."""
        infinite_loop_test = """
import time

def test_infinite_loop():
    time.sleep(5)
    assert True
"""
        result = SandboxTestRunner.run_tests(infinite_loop_test, timeout_seconds=0.8)
        assert result.status == "TIMEOUT"
        assert result.evidence_badge == "UNVERIFIED"
        assert "timed out" in result.summary_message.lower()

    def test_sandbox_handles_syntax_or_collection_errors(self):
        """Verify invalid syntax in test produces ERROR / UNVERIFIED badge."""
        broken_test = """
def broken_syntax(
"""
        result = SandboxTestRunner.run_tests(broken_test, timeout_seconds=5.0)
        assert result.status == "ERROR"
        assert result.evidence_badge == "UNVERIFIED"

    @pytest.mark.slow
    def test_sandbox_blocks_host_environment_secrets(self, monkeypatch):
        """Verify host API keys and tokens are purged from the sandbox environment."""
        monkeypatch.setenv("GEMINI_API_KEY", "secret-gemini-key-123")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret_token_456")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-mock-secret-789")

        isolation_test = """
import os

def test_secrets_are_isolated():
    assert "GEMINI_API_KEY" not in os.environ, "GEMINI_API_KEY leaked into sandbox!"
    assert "GITHUB_TOKEN" not in os.environ, "GITHUB_TOKEN leaked into sandbox!"
    assert "OPENAI_API_KEY" not in os.environ, "OPENAI_API_KEY leaked into sandbox!"
"""
        result = SandboxTestRunner.run_tests(isolation_test, timeout_seconds=25.0)
        assert result.executed is True
        assert result.status == "PASSED"
        assert result.evidence_badge == "PASSING"
