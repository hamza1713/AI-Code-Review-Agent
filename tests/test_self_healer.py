"""
Unit tests for Dynamic Test Self-Healing Engine (TestSelfHealer).
Verifies failure diagnostics, syntax cleanup, missing module mocking,
fixture reconciliation, and iterative healing loop.
"""

import ast
import pytest
from unittest.mock import MagicMock
from code_review_agent.models import TestExecutionResult
from code_review_agent.sandbox.self_healer import FailureCategory, TestSelfHealer


def test_diagnose_failure():
    # Syntax error
    diag1 = TestSelfHealer.diagnose_failure("def broken(", "")
    assert FailureCategory.SYNTAX_ERROR in diag1

    # Missing module
    log_module = "E   ModuleNotFoundError: No module named 'jwt'"
    diag2 = TestSelfHealer.diagnose_failure("import jwt\ndef test_jwt(): pass", log_module)
    assert FailureCategory.MISSING_MODULE in diag2

    # Missing fixture
    log_fixture = "E       fixture 'custom_fixture' not found"
    diag3 = TestSelfHealer.diagnose_failure("def test_it(custom_fixture): pass", log_fixture)
    assert FailureCategory.FIXTURE_ERROR in diag3

    # Assertion failure
    log_assert = "E       AssertionError: assert 2 == 3"
    diag4 = TestSelfHealer.diagnose_failure("def test_math(): assert 2 == 3", log_assert)
    assert FailureCategory.ASSERTION_FAILURE in diag4


def test_heal_syntax_markdown_fences():
    fenced_code = """```python
def test_sample():
    assert True
```"""
    healed = TestSelfHealer.heal_syntax(fenced_code)
    assert "```" not in healed
    ast.parse(healed)
    assert "def test_sample():" in healed


def test_heal_syntax_unclosed_docstring():
    broken_code = '"""Unclosed docstring\ndef test_dummy():\n    assert True'
    healed = TestSelfHealer.heal_syntax(broken_code)
    ast.parse(healed)
    assert "def test_dummy():" in healed


def test_heal_missing_modules():
    code = "import stripe\ndef test_charge():\n    assert stripe.Charge is not None"
    error_log = "ModuleNotFoundError: No module named 'stripe'"

    healed = TestSelfHealer.heal_missing_modules(code, error_log)
    assert "sys.modules['stripe'] = MagicMock()" in healed or "'stripe'" in healed
    # Verify code executes without ModuleNotFoundError
    exec_scope = {}
    exec(healed, exec_scope)
    assert "stripe" in exec_scope or "stripe" in exec_scope.get("__builtins__", {})


def test_heal_missing_fixtures():
    code = "def test_with_session(db_client):\n    assert db_client is not None"
    error_log = "fixture 'db_client' not found"

    healed = TestSelfHealer.heal_fixtures(code, error_log)
    assert "def db_client():" in healed
    assert "return MagicMock()" in healed
    ast.parse(healed)


def test_heal_and_run_iterative_success():
    # Runner fails on attempt 1 with missing module, passes once healed
    def mock_runner(code: str, pr_diff: str = ""):
        if "sys.modules" in code and "fakelib" in code:
            return TestExecutionResult(
                executed=True,
                status="PASSED",
                evidence_badge="PASSING",
                tests_run=1,
                summary_message="Tests passed"
            )
        return TestExecutionResult(
            executed=True,
            status="ERROR",
            evidence_badge="UNVERIFIED",
            stdout="ModuleNotFoundError: No module named 'fakelib'",
            summary_message="Collection failed"
        )

    initial_code = "import fakelib\ndef test_fn(): assert True"
    final_result, final_code = TestSelfHealer.heal_and_run(
        mock_runner,
        test_code=initial_code,
        max_attempts=2
    )

    assert final_result.status == "PASSED"
    assert final_result.self_healed is True
    assert final_result.heal_attempts == 1
    assert "fakelib" in final_code
    assert "sys.modules" in final_code
