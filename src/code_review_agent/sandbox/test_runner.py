"""
Isolated Subprocess Sandbox Test Execution Runner.
Executes AI-generated pytest suites safely against PR diff code,
verifying whether reported defects are empirically reproducible.
Assigns verifiable evidence badges: [REPRODUCED], [PASSING], [UNVERIFIED], or [HEURISTIC].
"""

import ast
import os
import re
import sys
import time
import tempfile
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any, List

from code_review_agent.models import TestExecutionResult
from code_review_agent.diff_parser import DiffParser
from code_review_agent.config import logger


class SandboxTestRunner:
    """
    Safely executes generated unit tests in an isolated, temporary subprocess sandbox.
    Prevents execution hanging with strict timeouts and extracts empirical verification badges.
    """

    @classmethod
    def run_tests(
        cls,
        test_code: str,
        pr_content: Optional[str] = None,
        repo_root: Optional[str] = None,
        timeout_seconds: float = 15.0
    ) -> TestExecutionResult:
        """
        Run the provided pytest test code in an isolated subprocess.
        """
        if not test_code or not test_code.strip():
            return TestExecutionResult(
                executed=False,
                status="SKIPPED",
                evidence_badge="HEURISTIC",
                summary_message="No generated unit tests provided for empirical execution."
            )

        start_time = time.time()

        # Clean test_code from markdown blocks if present
        clean_test = test_code.strip()
        if "```python" in clean_test:
            clean_test = clean_test.split("```python")[1].split("```")[0].strip()
        elif "```" in clean_test:
            clean_test = clean_test.split("```")[1].split("```")[0].strip()

        # Fast AST syntax validation
        try:
            ast.parse(clean_test)
        except SyntaxError as e:
            return TestExecutionResult(
                executed=True,
                status="ERROR",
                evidence_badge="UNVERIFIED",
                tests_run=0,
                failures=0,
                errors=1,
                duration_seconds=round(time.time() - start_time, 3),
                summary_message=f"Test syntax error: {e}"
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            # 1. Target pythonpath setup
            target_pythonpath = [str(temp_path)]
            if repo_root and Path(repo_root).exists():
                target_pythonpath.insert(0, str(Path(repo_root).resolve()))

            # 2. Materialize diff added code if present
            if pr_content:
                try:
                    parsed_pr = DiffParser.parse_diff(pr_content)
                    for file_diff in parsed_pr.files:
                        target_file = file_diff.target_file or file_diff.source_file
                        if target_file and not target_file.startswith("/dev/null"):
                            dest = temp_path / target_file
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            if not dest.exists():
                                added_lines = [line for _, line in DiffParser.extract_added_lines_with_numbers(file_diff)]
                                if added_lines:
                                    dest.write_text("\n".join(added_lines) + "\n", encoding="utf-8")
                except Exception as e:
                    logger.debug(f"Could not reconstruct diff files in sandbox: {e}")

            test_file = temp_path / "test_generated_review.py"
            try:
                test_file.write_text(clean_test, encoding="utf-8")
            except Exception as e:
                return TestExecutionResult(
                    executed=False,
                    status="ERROR",
                    evidence_badge="UNVERIFIED",
                    summary_message=f"Failed to write generated test to sandbox: {e}"
                )

            # 3. Prepare sanitized execution environment
            # Prevent leaking host API keys, cloud credentials, tokens, or private secrets
            SAFE_ENV_ALLOWLIST = {
                "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE",
                "LOCALAPPDATA", "APPDATA", "COMSPEC", "PATHEXT", "LANG", "LC_ALL"
            }
            clean_env = {k: v for k, v in os.environ.items() if k.upper() in SAFE_ENV_ALLOWLIST}
            clean_env["PYTHONPATH"] = os.pathsep.join(target_pythonpath)
            clean_env["PYTHONDONTWRITEBYTECODE"] = "1"
            clean_env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"  # Fast 0.3s cold-start, bypass heavy global plugins

            # Explicitly purge any credential or token keys
            for secret_key in list(clean_env.keys()):
                upper_k = secret_key.upper()
                if any(term in upper_k for term in ["KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH", "CREDENTIAL"]):
                    clean_env.pop(secret_key, None)

            cmd = [
                sys.executable,
                "-m",
                "pytest",
                str(test_file),
                "-v",
                "--tb=short",
                "-o",
                "asyncio_mode=auto",
                "-p", "no:playwright",
                "-p", "no:langsmith",
                "-p", "no:typeguard",
                "-p", "no:Faker",
                "-p", "no:anyio"
            ]

            try:
                proc = subprocess.run(
                    cmd,
                    cwd=temp_dir,
                    env=clean_env,
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                    shell=False
                )
                duration = round(time.time() - start_time, 3)
                stdout = proc.stdout or ""
                stderr = proc.stderr or ""
                combined_output = stdout + "\n" + stderr

                passed_match = re.search(r"(\d+)\s+passed", combined_output)
                failed_match = re.search(r"(\d+)\s+failed", combined_output)
                error_match = re.search(r"(\d+)\s+error", combined_output)

                passed_cnt = int(passed_match.group(1)) if passed_match else 0
                failed_cnt = int(failed_match.group(1)) if failed_match else 0
                error_cnt = int(error_match.group(1)) if error_match else 0
                total_run = passed_cnt + failed_cnt + error_cnt

                if proc.returncode == 0:
                    return TestExecutionResult(
                        executed=True,
                        status="PASSED",
                        evidence_badge="PASSING",
                        tests_run=total_run or passed_cnt or 1,
                        failures=0,
                        errors=0,
                        duration_seconds=duration,
                        stdout=stdout[:4000],
                        stderr=stderr[:2000],
                        summary_message=f"All {total_run or 1} unit test(s) passed successfully in sandbox ({duration}s)."
                    )
                elif proc.returncode == 1:
                    return TestExecutionResult(
                        executed=True,
                        status="REPRODUCED_DEFECT",
                        evidence_badge="REPRODUCED",
                        tests_run=total_run or (failed_cnt + passed_cnt) or 1,
                        failures=failed_cnt or 1,
                        errors=error_cnt,
                        duration_seconds=duration,
                        stdout=stdout[:4000],
                        stderr=stderr[:2000],
                        summary_message=f"Defect empirically REPRODUCED in sandbox: {failed_cnt} assertion failure(s) observed ({duration}s)."
                    )
                else:
                    return TestExecutionResult(
                        executed=True,
                        status="ERROR",
                        evidence_badge="UNVERIFIED",
                        tests_run=0,
                        failures=0,
                        errors=1,
                        duration_seconds=duration,
                        stdout=stdout[:2000],
                        stderr=stderr[:2000],
                        summary_message=f"Test sandbox execution error (exit code {proc.returncode}). Environment unverified."
                    )

            except subprocess.TimeoutExpired:
                duration = round(time.time() - start_time, 3)
                logger.warning(f"Test execution sandbox timed out after {timeout_seconds}s.")
                return TestExecutionResult(
                    executed=True,
                    status="TIMEOUT",
                    evidence_badge="UNVERIFIED",
                    tests_run=0,
                    failures=0,
                    errors=1,
                    duration_seconds=duration,
                    summary_message=f"Sandbox test execution timed out after {timeout_seconds}s (possible infinite loop or blocking I/O)."
                )
            except Exception as e:
                duration = round(time.time() - start_time, 3)
                logger.error(f"Error running sandbox tests: {e}")
                return TestExecutionResult(
                    executed=False,
                    status="ERROR",
                    evidence_badge="UNVERIFIED",
                    duration_seconds=duration,
                    summary_message=f"Sandbox execution setup error: {str(e)}"
                )
