"""
Unit tests for Sandbox Hardening, Credential Scrubbing, Resource Quotas,
and Dual Execution Mode.
"""

import io
import os
import shutil
from unittest.mock import MagicMock, patch
import pytest

from code_review_agent.sandbox.test_runner import SandboxTestRunner, scrub_credentials


def test_scrub_credentials():
    leaky_env = {
        "PATH": "/usr/local/bin:/usr/bin",
        "HOME": "/home/runner",
        "GITHUB_TOKEN": "ghp_super_secret_123",
        "GITLAB_TOKEN": "glpat-secret-token",
        "BITBUCKET_TOKEN": "bb_secret_pass",
        "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "DATABASE_PASSWORD": "super_secure_db_pass",
        "APP_AUTH_KEY": "jwt-signing-key",
        "GEMINI_API_KEY": "ai-key-456",
        "MY_PRIVATE_CREDENTIAL": "shh"
    }

    cleaned = scrub_credentials(leaky_env)

    # All sensitive keys must be excluded
    for sensitive_key in [
        "GITHUB_TOKEN", "GITLAB_TOKEN", "BITBUCKET_TOKEN",
        "AWS_SECRET_ACCESS_KEY", "DATABASE_PASSWORD", "APP_AUTH_KEY",
        "GEMINI_API_KEY", "MY_PRIVATE_CREDENTIAL"
    ]:
        assert sensitive_key not in cleaned

    # Non-sensitive keys must be preserved
    assert cleaned["PATH"] == "/usr/local/bin:/usr/bin"
    assert cleaned["HOME"] == "/home/runner"
    assert cleaned["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"


def test_resource_quotas_configurable(monkeypatch):
    monkeypatch.setenv("REVIEW_SANDBOX_IMAGE", "sha256:" + "b" * 64)
    monkeypatch.setenv("REVIEW_SANDBOX_MEMORY", "512m")
    monkeypatch.setenv("REVIEW_SANDBOX_CPUS", "2")
    monkeypatch.setenv("REVIEW_SANDBOX_PIDS_LIMIT", "128")
    monkeypatch.setattr(shutil, "which", lambda _: "docker")

    process = MagicMock()
    process.stdout = io.BytesIO(b"1 passed in 0.01s\n")
    process.poll.return_value = 0
    process.returncode = 0

    with patch("subprocess.Popen", return_value=process) as launch, patch("subprocess.run"):
        result = SandboxTestRunner.run_tests("def test_dummy(): assert True")
        command = launch.call_args.args[0]
        assert "--memory=512m" in command
        assert "--memory-swap=512m" in command
        assert "--cpus=2" in command
        assert "--pids-limit=128" in command
        assert result.status == "PASSED"


def test_restricted_local_sandbox_mode(monkeypatch):
    monkeypatch.setenv("REVIEW_ALLOW_LOCAL_SANDBOX", "true")
    monkeypatch.setenv("REVIEW_SANDBOX_IMAGE", "")  # No docker image

    test_code = "def test_addition():\n    assert 3 + 5 == 8"
    result = SandboxTestRunner.run_tests(test_code, timeout_seconds=10.0)

    assert result.executed is True
    assert result.status == "PASSED"
    assert result.evidence_badge == "PASSING"
    assert result.tests_run >= 1


def test_run_tests_with_healing_local(monkeypatch):
    monkeypatch.setenv("REVIEW_ALLOW_LOCAL_SANDBOX", "true")
    monkeypatch.setenv("REVIEW_SANDBOX_IMAGE", "")

    # Code wrapped in markdown fences
    fenced_test_code = """```python
def test_greeting():
    msg = "hello"
    assert msg.upper() == "HELLO"
```"""
    result, final_code = SandboxTestRunner.run_tests_with_healing(fenced_test_code)

    assert result.executed is True
    assert result.status == "PASSED"
    assert "```" not in final_code
