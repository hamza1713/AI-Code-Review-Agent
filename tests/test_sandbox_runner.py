"""Container execution regressions. Integration cases never fall back to host pytest."""
import os
import shutil
from unittest.mock import patch
import pytest
from code_review_agent.sandbox.test_runner import SandboxTestRunner


def test_empty_tests():
    result = SandboxTestRunner.run_tests(' ')
    assert not result.executed and result.evidence_badge == 'HEURISTIC'


def test_syntax_error():
    result = SandboxTestRunner.run_tests('def broken(')
    assert not result.executed and result.status == 'ERROR'


@pytest.fixture
def configured_container():
    if not os.getenv('REVIEW_SANDBOX_IMAGE') or not shutil.which('docker'):
        pytest.skip('Requires an explicitly configured immutable sandbox image and Docker.')


@pytest.mark.slow
@pytest.mark.sandbox_integration
def test_passing_tests_in_container(configured_container):
    result = SandboxTestRunner.run_tests('def test_sum():\n    assert 2 + 2 == 4', timeout_seconds=30)
    assert result.executed and result.status == 'PASSED'


@pytest.mark.slow
@pytest.mark.sandbox_integration
def test_failed_generated_assertion_is_not_proof(configured_container):
    result = SandboxTestRunner.run_tests('def test_wrong_assertion():\n    assert 2 + 2 == 5', timeout_seconds=30)
    assert result.executed and result.failures == 1
    assert result.evidence_badge == 'UNVERIFIED'


@pytest.mark.slow
@pytest.mark.sandbox_integration
def test_network_host_files_and_secrets_are_isolated(configured_container, tmp_path, monkeypatch):
    canary = tmp_path / 'outside-canary.txt'
    canary.write_text('not-for-container')
    monkeypatch.setenv('GITHUB_TOKEN', 'never-pass-this-to-the-container')
    code = f'''
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
'''
    result = SandboxTestRunner.run_tests(code, timeout_seconds=30)
    assert result.status == 'PASSED', result.summary_message + result.stdout
    assert canary.read_text() == 'not-for-container'


@pytest.mark.slow
@pytest.mark.sandbox_integration
def test_timeout_stops_container(configured_container):
    result = SandboxTestRunner.run_tests('def test_wait():\n    import time\n    time.sleep(60)', timeout_seconds=2)
    assert result.status == 'TIMEOUT'


def test_unsafe_path_never_launches_container(monkeypatch):
    monkeypatch.setenv('REVIEW_SANDBOX_IMAGE', 'sha256:' + 'a' * 64)
    monkeypatch.setattr(shutil, 'which', lambda _: 'docker')
    with patch('subprocess.Popen') as launch:
        result = SandboxTestRunner.run_tests('assert True', 'diff --git a/../bad.py b/../bad.py\n@@ -0,0 +1 @@\n+value=1\n')
        assert result.status == 'ERROR'
        launch.assert_not_called()


def test_container_command_enforces_boundary(monkeypatch):
    import io
    from unittest.mock import MagicMock
    monkeypatch.setenv('REVIEW_SANDBOX_IMAGE', 'sha256:' + 'a' * 64)
    monkeypatch.setattr(shutil, 'which', lambda _: 'docker')
    process = MagicMock()
    process.stdout = io.BytesIO(b'1 passed in 0.01s\n')
    process.poll.return_value = 0
    process.returncode = 0
    with patch('subprocess.Popen', return_value=process) as launch, patch('subprocess.run') as cleanup:
        result = SandboxTestRunner.run_tests('def test_ok(): assert True', repo_root='/private-host-repo')
        command = launch.call_args.args[0]
        for flag in ['--network=none', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                     '--user=65534:65534', '--pids-limit=64', '--cpus=1', '--memory=256m', '--pull=never']:
            assert flag in command
        assert '/private-host-repo' not in ' '.join(command)
        assert command[command.index('--mount') + 1].endswith(',dst=/workspace,readonly')
        assert cleanup.call_args.args[0][:3] == ['docker', 'rm', '-f']
        assert result.status == 'PASSED'
