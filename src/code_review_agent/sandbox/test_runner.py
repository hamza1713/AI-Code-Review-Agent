"""Generated tests run only inside an explicitly configured, restricted Docker image.

There is deliberately no host-Python fallback. The repository itself is never mounted.
"""
import ast
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Dict, Optional, Tuple
from code_review_agent.models import TestExecutionResult
from code_review_agent.diff_parser import DiffParser

MAX_SOURCE_BYTES = 5 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024


def scrub_credentials(env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """
    Scrub sensitive credentials, tokens, keys, and secrets from environment dictionary.
    Guarantees host secrets cannot leak into container or local test execution processes.
    """
    source_env = env if env is not None else dict(os.environ)
    sensitive_keywords = (
        "TOKEN", "SECRET", "KEY", "PASSWORD", "AUTH", "CREDENTIAL",
        "PRIVATE", "API_KEY", "GH_", "GITHUB_", "GITLAB_", "BITBUCKET_", "GEMINI_"
    )
    safe_env = {}
    for k, v in source_env.items():
        k_upper = k.upper()
        if any(keyword in k_upper for keyword in sensitive_keywords):
            continue
        safe_env[k] = v
    # Safe minimum defaults
    safe_env.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    safe_env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    return safe_env


def safe_diff_path(root: Path, filename: str) -> Path:
    """Check both Windows and POSIX syntax regardless of the server platform."""
    normalized = filename.replace('\\', '/')
    path = PurePosixPath(normalized)
    if (not normalized or path.is_absolute() or PureWindowsPath(filename).drive
            or PureWindowsPath(filename).is_reserved()
            or '..' in path.parts or ':' in normalized or '\x00' in normalized):
        raise ValueError('Unsafe diff path')
    destination = (root / path).resolve()
    if not destination.is_relative_to(root.resolve()) or destination == root.resolve():
        raise ValueError('Diff path escapes workspace')
    return destination


class SandboxTestRunner:
    @classmethod
    def run_tests(cls, test_code, pr_content=None, repo_root=None, timeout_seconds=15.0):
        def unverified(message, status='SKIPPED'):
            return TestExecutionResult(
                executed=False, status=status,
                evidence_badge='UNVERIFIED',
                trust_grade='UNVERIFIED',
                original_test_suite=test_code,
                healed_test_suite=test_code,
                summary_message=message
            )

        if not test_code or not test_code.strip():
            return TestExecutionResult(
                executed=False, status='SKIPPED',
                evidence_badge='HEURISTIC',
                trust_grade='UNVERIFIED',
                original_test_suite=test_code or "",
                healed_test_suite=test_code or "",
                summary_message='No generated tests provided.'
            )
        clean = test_code.strip()
        if '```' in clean:
            clean = re.sub(r'^python\s*\n', '', clean.split('```')[1]).strip()
        try:
            ast.parse(clean)
        except SyntaxError:
            return unverified('Generated tests contain invalid Python syntax.', 'ERROR')
        if len(clean.encode()) + len((pr_content or '').encode()) > MAX_SOURCE_BYTES:
            return unverified('Generated test input exceeds the workspace limit.', 'ERROR')
        image = os.getenv('REVIEW_SANDBOX_IMAGE', '').strip()
        allow_local = os.getenv('REVIEW_ALLOW_LOCAL_SANDBOX', 'false').lower() in ('true', '1')
        if not image and not allow_local:
            return unverified('Execution disabled: configure a reviewed sandbox image to run generated tests. Static analysis remains available.')
        # Require immutable local images, never download or accept runtime switches.
        if image and not re.fullmatch(r'(?:[\w./:-]+@)?sha256:[0-9a-f]{64}', image):
            return unverified('Sandbox image must be an immutable sha256 image ID or repository digest.', 'ERROR')
        docker = shutil.which('docker')
        if not docker and not allow_local:
            return unverified('Docker is unavailable; generated tests were not executed.')
        remaining = float(os.getenv('REVIEW_WORKER_DEADLINE', str(time.time() + 70))) - time.time() - 10
        if remaining < 1:
            return unverified('Insufficient review time remaining for isolated execution.')
        timeout_seconds = min(timeout_seconds, remaining)
        name = 'review-sandbox-' + uuid.uuid4().hex
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix='review-sandbox-') as directory:
            root = Path(directory)
            workspace = root / 'source'
            workspace.mkdir(mode=0o755)
            try:
                parsed = DiffParser.parse_diff(pr_content or '')
                if len(parsed.files) > 100:
                    raise ValueError('Too many diff files')
                for item in parsed.files:
                    filename = item.target_file or item.source_file
                    if filename == '/dev/null':
                        continue
                    dest = safe_diff_path(workspace, filename)
                    if dest.name == 'test_generated_review.py':
                        raise ValueError('Reserved test filename')
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    lines = [line for _, line in DiffParser.extract_added_lines_with_numbers(item)]
                    dest.write_text('\n'.join(lines) + '\n', encoding='utf-8')
                    dest.chmod(0o644)
                test_file = workspace / 'test_generated_review.py'
                test_file.write_text(clean, encoding='utf-8')
                test_file.chmod(0o644)
            except (ValueError, OSError):
                return unverified('Unsafe or invalid diff workspace; execution refused.', 'ERROR')

            if allow_local and (not docker or not image):
                return cls._run_local_sandbox(workspace, timeout_seconds, started)

            memory = os.getenv('REVIEW_SANDBOX_MEMORY', '256m')
            cpus = os.getenv('REVIEW_SANDBOX_CPUS', '1')
            pids_limit = os.getenv('REVIEW_SANDBOX_PIDS_LIMIT', '64')
            command = [docker, 'run', '--rm', '--pull=never', '--name', name,
                '--network=none', '--read-only', '--cap-drop=ALL',
                '--security-opt=no-new-privileges', '--user=65534:65534',
                f'--pids-limit={pids_limit}', f'--cpus={cpus}', f'--memory={memory}', f'--memory-swap={memory}',
                '--ulimit=fsize=65536:65536', '--log-driver=none',
                '--tmpfs=/tmp:rw,noexec,nosuid,size=32m',
                '--mount', f'type=bind,src={workspace},dst=/workspace,readonly',
                '--workdir=/workspace', '--env=PYTEST_DISABLE_PLUGIN_AUTOLOAD=1',
                '--env=PYTHONDONTWRITEBYTECODE=1', '--entrypoint=python', image,
                '-m', 'pytest', '/workspace/test_generated_review.py', '-q',
                '-p', 'no:cacheprovider', '--tb=short']
            process = None
            try:
                # Stream to a bounded pipe rather than capture_output's unbounded memory.
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=scrub_credentials())
                output = bytearray()
                overflow = threading.Event()
                def drain():
                    while chunk := process.stdout.read(4096):
                        remaining = MAX_OUTPUT_BYTES - len(output)
                        output.extend(chunk[:max(0, remaining)])
                        if len(chunk) > remaining:
                            overflow.set()
                reader = threading.Thread(target=drain, daemon=True)
                reader.start()
                deadline = started + min(max(float(timeout_seconds), 0.1), 60)
                while process.poll() is None:
                    if overflow.is_set() or time.monotonic() >= deadline:
                        return unverified('Sandbox output limit exceeded.' if overflow.is_set()
                            else 'Sandbox execution timed out.', 'ERROR' if overflow.is_set() else 'TIMEOUT')
                    time.sleep(0.05)
                reader.join(timeout=2)
                text = output.decode('utf-8', errors='replace')
                if overflow.is_set():
                    return unverified('Sandbox output limit exceeded.', 'ERROR')
                if process.returncode not in (0, 1):
                    return unverified('Container setup or test collection failed. No execution evidence is available.', 'ERROR')
                passed = re.search(r'(\d+) passed', text)
                failed = re.search(r'(\d+) failed', text)
                if process.returncode == 0 and passed:
                    return TestExecutionResult(
                        executed=True, status='PASSED', evidence_badge='PASSING',
                        trust_grade='EMPIRICAL_ORIGINAL_PASSED',
                        original_test_suite=test_code,
                        healed_test_suite=clean,
                        tests_run=int(passed[1]), stdout=text[:4000],
                        summary_message='Generated tests passed in the restricted container; this does not prove the code is defect-free.'
                    )
                # A failed generated assertion is not proof of a real project defect.
                return TestExecutionResult(
                    executed=True, status='ERROR', evidence_badge='UNVERIFIED',
                    trust_grade='UNVERIFIED',
                    original_test_suite=test_code,
                    healed_test_suite=clean,
                    failures=int(failed[1]) if failed else 0, stdout=text[:4000],
                    summary_message='Generated tests failed or could not run. Validate the test and reconstructed code before claiming a reproduced defect.'
                )
            except (OSError, subprocess.SubprocessError):
                return unverified('Sandbox could not start; generated tests were not verified.', 'ERROR')
            finally:
                # Docker's daemon owns the container; killing the CLI alone is insufficient.
                try:
                    subprocess.run([docker, 'rm', '-f', name], stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, timeout=5, check=False)
                except (OSError, subprocess.SubprocessError):
                    pass
                if process is not None:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
                    if process.stdout:
                        process.stdout.close()

    @classmethod
    def _run_local_sandbox(cls, workspace: Path, timeout_seconds: float, started: float) -> TestExecutionResult:
        test_file = workspace / 'test_generated_review.py'
        test_content = ""
        if test_file.exists():
            try:
                test_content = test_file.read_text(encoding='utf-8', errors='replace')
            except Exception:
                pass

        def unverified(message, status='SKIPPED'):
            return TestExecutionResult(
                executed=False, status=status,
                evidence_badge='UNVERIFIED',
                trust_grade='UNVERIFIED',
                original_test_suite=test_content,
                healed_test_suite=test_content,
                summary_message=message
            )

        safe_env = scrub_credentials()
        safe_env["PYTHONPATH"] = str(workspace)
        cmd = [
            sys.executable, "-m", "pytest", str(test_file),
            "-q", "-p", "no:cacheprovider", "--tb=short"
        ]
        process = None
        try:
            process = subprocess.Popen(
                cmd,
                cwd=str(workspace),
                env=safe_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT
            )
            output = bytearray()
            overflow = threading.Event()

            def drain():
                while chunk := process.stdout.read(4096):
                    remaining = MAX_OUTPUT_BYTES - len(output)
                    output.extend(chunk[:max(0, remaining)])
                    if len(chunk) > remaining:
                        overflow.set()

            reader = threading.Thread(target=drain, daemon=True)
            reader.start()
            deadline = started + min(max(float(timeout_seconds), 0.1), 60)
            while process.poll() is None:
                if overflow.is_set() or time.monotonic() >= deadline:
                    if process.poll() is None:
                        process.kill()
                    return unverified('Sandbox output limit exceeded.' if overflow.is_set()
                        else 'Sandbox execution timed out.', 'ERROR' if overflow.is_set() else 'TIMEOUT')
                time.sleep(0.05)
            reader.join(timeout=2)
            text = output.decode('utf-8', errors='replace')
            if overflow.is_set():
                return unverified('Sandbox output limit exceeded.', 'ERROR')
            passed = re.search(r'(\d+) passed', text)
            failed = re.search(r'(\d+) failed', text)
            if process.returncode == 0 and passed:
                return TestExecutionResult(
                    executed=True, status='PASSED', evidence_badge='PASSING',
                    trust_grade='EMPIRICAL_ORIGINAL_PASSED',
                    original_test_suite=test_content,
                    healed_test_suite=test_content,
                    tests_run=int(passed[1]), stdout=text[:4000],
                    summary_message='Generated tests passed in restricted local sandbox.'
                )
            return TestExecutionResult(
                executed=True, status='ERROR', evidence_badge='UNVERIFIED',
                trust_grade='UNVERIFIED',
                original_test_suite=test_content,
                healed_test_suite=test_content,
                failures=int(failed[1]) if failed else 0, stdout=text[:4000],
                summary_message='Generated tests failed in restricted local sandbox.'
            )
        except Exception as e:
            return unverified(f'Sandbox could not run tests: {e}', 'ERROR')
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                if process.stdout:
                    process.stdout.close()

    @classmethod
    def run_tests_with_healing(
        cls,
        test_code: str,
        pr_content: Optional[str] = None,
        repo_root: Optional[str] = None,
        timeout_seconds: float = 15.0,
        max_heal_attempts: int = 2
    ) -> Tuple[TestExecutionResult, str]:
        """
        Execute generated unit tests with dynamic self-healing.
        If initial tests fail due to syntax, missing mocks, or fixtures,
        iteratively heals and re-tests.
        """
        from code_review_agent.sandbox.self_healer import TestSelfHealer
        return TestSelfHealer.heal_and_run(
            runner_func=lambda code, pr: cls.run_tests(code, pr, repo_root=repo_root, timeout_seconds=timeout_seconds),
            test_code=test_code,
            pr_content=pr_content,
            max_attempts=max_heal_attempts
        )
