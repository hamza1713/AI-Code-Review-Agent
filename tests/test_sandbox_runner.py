"""Container execution regressions. Integration cases never fall back to host pytest."""

import os
import re
import shutil
import subprocess
from unittest.mock import patch

import pytest

from code_review_agent.sandbox.test_runner import SandboxTestRunner


def test_empty_tests():
    result = SandboxTestRunner.run_tests(" ")
    assert not result.executed and result.evidence_badge == "HEURISTIC"


def test_syntax_error():
    result = SandboxTestRunner.run_tests("def broken(")
    assert not result.executed and result.status == "ERROR"


@pytest.fixture(scope="module")
def configured_container():
    if not os.getenv("REVIEW_SANDBOX_IMAGE") or not shutil.which("docker"):
        if os.getenv("REVIEW_REQUIRE_SANDBOX_TESTS") == "true":
            pytest.fail(
                "Release gate requires a built immutable sandbox image and Docker"
            )
        pytest.skip(
            "Requires an explicitly configured immutable sandbox image and Docker."
        )
    image = os.environ["REVIEW_SANDBOX_IMAGE"]
    assert re.fullmatch(r"(?:[\w./:-]+@)?sha256:[0-9a-f]{64}", image)
    # Warm the trusted interpreter image before testing reviewed-code execution.
    # Daemon/image startup is infrastructure setup; every test still enforces its
    # own execution deadline and the timeout case must kill a running container.
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--pull=never",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user=65534:65534",
            "--entrypoint=python",
            image,
            "--version",
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )


@pytest.mark.slow
@pytest.mark.sandbox_integration
def test_passing_tests_in_container(configured_container):
    result = SandboxTestRunner.run_tests(
        "def test_sum():\n    assert 2 + 2 == 4", timeout_seconds=60
    )
    assert result.executed and result.status == "PASSED"


@pytest.mark.slow
@pytest.mark.sandbox_integration
def test_failed_generated_assertion_is_not_proof(configured_container):
    result = SandboxTestRunner.run_tests(
        "def test_wrong_assertion():\n    assert 2 + 2 == 5", timeout_seconds=60
    )
    assert result.executed and result.failures == 1
    assert result.evidence_badge == "UNVERIFIED"


@pytest.mark.slow
@pytest.mark.sandbox_integration
def test_network_host_files_and_secrets_are_isolated(
    configured_container, tmp_path, monkeypatch
):
    canary = tmp_path / "outside-canary.txt"
    canary.write_text("not-for-container")
    monkeypatch.setenv("GITHUB_TOKEN", "never-pass-this-to-the-container")
    code = f"""
import os
import socket
from pathlib import Path
import pytest

def test_boundary():
    assert 'GITHUB_TOKEN' not in os.environ
    assert not Path({str(canary)!r}).exists()
    with pytest.raises(OSError):
        Path('/workspace/outside.txt').write_text('blocked')
    with pytest.raises(OSError):
        socket.create_connection(('192.0.2.1', 443), timeout=0.2)
"""
    result = SandboxTestRunner.run_tests(code, timeout_seconds=60)
    assert result.status == "PASSED", result.summary_message + result.stdout
    assert canary.read_text() == "not-for-container"


@pytest.mark.slow
@pytest.mark.sandbox_integration
def test_timeout_stops_container(configured_container):
    result = SandboxTestRunner.run_tests(
        "def test_wait():\n    import time\n    time.sleep(60)", timeout_seconds=2
    )
    assert result.status == "TIMEOUT"


def test_unsafe_path_never_launches_container(monkeypatch):
    monkeypatch.setenv("REVIEW_SANDBOX_IMAGE", "sha256:" + "a" * 64)
    monkeypatch.setattr(shutil, "which", lambda _: "docker")
    with patch("subprocess.Popen") as launch:
        result = SandboxTestRunner.run_tests(
            "assert True",
            "diff --git a/../bad.py b/../bad.py\n@@ -0,0 +1 @@\n+value=1\n",
        )
        assert result.status == "ERROR"
        launch.assert_not_called()


def test_container_command_enforces_boundary(monkeypatch):
    import io
    from unittest.mock import MagicMock

    monkeypatch.setenv("REVIEW_SANDBOX_IMAGE", "sha256:" + "a" * 64)
    monkeypatch.setattr(shutil, "which", lambda _: "docker")
    process = MagicMock()
    process.stdout = io.BytesIO(b"1 passed in 0.01s\n")
    process.poll.return_value = 0
    process.returncode = 0
    with (
        patch("subprocess.Popen", return_value=process) as launch,
        patch("subprocess.run") as cleanup,
    ):
        result = SandboxTestRunner.run_tests(
            "def test_ok(): assert True", repo_root="/private-host-repo"
        )
        command = launch.call_args.args[0]
        for flag in [
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user=65534:65534",
            "--pids-limit=64",
            "--cpus=1",
            "--memory=256m",
            "--pull=never",
        ]:
            assert flag in command
        assert "/private-host-repo" not in " ".join(command)
        assert command[command.index("--mount") + 1].endswith(
            ",dst=/workspace,readonly"
        )
        assert cleanup.call_args.args[0][:3] == ["docker", "rm", "-f"]
        assert result.status == "PASSED"


def test_reconstruct_file_from_hunks():
    from code_review_agent.diff_parser import DiffParser

    raw_diff = """diff --git a/app/calc.py b/app/calc.py
--- a/app/calc.py
+++ b/app/calc.py
@@ -1,4 +1,4 @@
 def add(a, b):
-    return a
+    return a + b
"""
    parsed = DiffParser.parse_diff(raw_diff)
    reconstructed = SandboxTestRunner._reconstruct_file_from_hunks(parsed.files[0])
    assert "def add(a, b):" in reconstructed
    assert "return a + b" in reconstructed
    assert "-    return a" not in reconstructed


def test_apply_diff_to_base():
    from code_review_agent.diff_parser import DiffParser

    base_content = "def calculate(x):\n    # base calculation\n    return x * 1\n"
    raw_diff = """diff --git a/app/calc.py b/app/calc.py
--- a/app/calc.py
+++ b/app/calc.py
@@ -2,2 +2,2 @@
     # base calculation
-    return x * 1
+    return x * 2
"""
    parsed = DiffParser.parse_diff(raw_diff)
    patched = SandboxTestRunner._apply_diff_to_base(base_content, parsed.files[0])
    assert "def calculate(x):" in patched
    assert "return x * 2" in patched
    assert "return x * 1" not in patched
