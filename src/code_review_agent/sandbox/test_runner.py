"""Generated tests run only inside an explicitly configured, restricted Docker image.

There is deliberately no host-Python fallback. The repository itself is never mounted.
"""

import ast
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from code_review_agent.diff_parser import DiffParser
from code_review_agent.models import TestExecutionResult
from code_review_agent.path_validation import safe_diff_path

MAX_SOURCE_BYTES = 5 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024


def scrub_credentials(env: dict[str, str] | None = None) -> dict[str, str]:
    """
    Scrub sensitive credentials, tokens, keys, and secrets from environment dictionary.
    Guarantees host secrets cannot leak into container or local test execution processes.
    """
    source_env = env if env is not None else dict(os.environ)
    sensitive_keywords = (
        "TOKEN",
        "SECRET",
        "KEY",
        "PASSWORD",
        "AUTH",
        "CREDENTIAL",
        "PRIVATE",
        "API_KEY",
        "GH_",
        "GITHUB_",
        "GITLAB_",
        "BITBUCKET_",
        "GEMINI_",
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


class SandboxTestRunner:
    @classmethod
    def run_tests(
        cls, test_code, pr_content=None, repo_root=None, timeout_seconds=15.0
    ):
        def unverified(message, status="SKIPPED"):
            return TestExecutionResult(
                executed=False,
                status=status,
                evidence_badge="UNVERIFIED",
                trust_grade="UNVERIFIED",
                original_test_suite=test_code,
                healed_test_suite=test_code,
                summary_message=message,
            )

        if not test_code or not test_code.strip():
            return TestExecutionResult(
                executed=False,
                status="SKIPPED",
                evidence_badge="HEURISTIC",
                trust_grade="UNVERIFIED",
                original_test_suite=test_code or "",
                healed_test_suite=test_code or "",
                summary_message="No generated tests provided.",
            )
        clean = test_code.strip()
        if "```" in clean:
            clean = re.sub(r"^python\s*\n", "", clean.split("```")[1]).strip()
        try:
            ast.parse(clean)
        except SyntaxError:
            return unverified("Generated tests contain invalid Python syntax.", "ERROR")
        if len(clean.encode()) + len((pr_content or "").encode()) > MAX_SOURCE_BYTES:
            return unverified(
                "Generated test input exceeds the workspace limit.", "ERROR"
            )
        image = os.getenv("REVIEW_SANDBOX_IMAGE", "").strip()
        if not image:
            return unverified(
                "Execution disabled: configure a reviewed sandbox image to run generated tests. Static analysis remains available."
            )
        # Require immutable local images, never download or accept runtime switches.
        if image and not re.fullmatch(r"(?:[\w./:-]+@)?sha256:[0-9a-f]{64}", image):
            return unverified(
                "Sandbox image must be an immutable sha256 image ID or repository digest.",
                "ERROR",
            )
        docker = shutil.which("docker")
        if not docker:
            return unverified(
                "Docker is unavailable; generated tests were not executed."
            )
        remaining = (
            float(os.getenv("REVIEW_WORKER_DEADLINE", str(time.time() + 70)))
            - time.time()
            - 10
        )
        if remaining < 1:
            return unverified(
                "Insufficient review time remaining for isolated execution."
            )
        timeout_seconds = min(timeout_seconds, remaining)
        name = "review-sandbox-" + uuid.uuid4().hex
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="review-sandbox-") as directory:
            root = Path(directory)
            workspace = root / "source"
            workspace.mkdir(mode=0o755)
            try:
                parsed = DiffParser.parse_diff(pr_content or "")
                if len(parsed.files) > 100:
                    raise ValueError("Too many diff files")
                # Provide sibling repo modules so unchanged imports resolve in the sandbox.
                if repo_root and Path(repo_root).is_dir():
                    copied = 0
                    root_path = Path(repo_root).resolve()
                    for src in root_path.rglob("*.py"):
                        if copied >= 200:
                            break
                        rel = src.relative_to(root_path)
                        if any(
                            part.startswith(".")
                            or part in {"node_modules", "venv", ".venv", "__pycache__", "tests", "test"}
                            for part in rel.parts
                        ):
                            continue
                        if src.is_symlink() or src.stat().st_size > 256 * 1024:
                            continue
                        try:
                            sib = safe_diff_path(workspace, rel.as_posix())
                            sib.parent.mkdir(parents=True, exist_ok=True)
                            sib.write_bytes(src.read_bytes())
                            sib.chmod(0o644)
                            copied += 1
                        except (ValueError, OSError):
                            continue
                for item in parsed.files:
                    filename = item.target_file or item.source_file
                    if filename == "/dev/null":
                        continue
                    dest = safe_diff_path(workspace, filename)
                    if dest.name == "test_generated_review.py":
                        raise ValueError("Reserved test filename")
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    base_file = (
                        safe_diff_path(Path(repo_root), filename) if repo_root else None
                    )
                    if (
                        base_file
                        and base_file.is_file()
                        and not getattr(item, "is_new_file", False)
                    ):
                        try:
                            content = cls._apply_diff_to_base(
                                base_file.read_text(encoding="utf-8", errors="replace"),
                                item,
                            )
                            dest.write_text(content, encoding="utf-8")
                        except Exception:
                            dest.write_text(
                                cls._reconstruct_file_from_hunks(item), encoding="utf-8"
                            )
                    else:
                        dest.write_text(
                            cls._reconstruct_file_from_hunks(item), encoding="utf-8"
                        )
                    dest.chmod(0o644)
                test_file = workspace / "test_generated_review.py"
                test_file.write_text(clean, encoding="utf-8")
                test_file.chmod(0o644)
            except (ValueError, OSError):
                return unverified(
                    "Unsafe or invalid diff workspace; execution refused.", "ERROR"
                )

            memory = os.getenv("REVIEW_SANDBOX_MEMORY", "256m")
            cpus = os.getenv("REVIEW_SANDBOX_CPUS", "1")
            pids_limit = os.getenv("REVIEW_SANDBOX_PIDS_LIMIT", "64")
            command = [
                docker,
                "run",
                "--rm",
                "--pull=never",
                "--name",
                name,
                "--network=none",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--user=65534:65534",
                f"--pids-limit={pids_limit}",
                f"--cpus={cpus}",
                f"--memory={memory}",
                f"--memory-swap={memory}",
                "--ulimit=fsize=65536:65536",
                "--log-driver=none",
                "--tmpfs=/tmp:rw,noexec,nosuid,size=32m",
                "--mount",
                f"type=bind,src={workspace},dst=/workspace,readonly",
                "--workdir=/workspace",
                "--env=PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
                "--env=PYTHONDONTWRITEBYTECODE=1",
                "--entrypoint=python",
                image,
                "-m",
                "pytest",
                "/workspace/test_generated_review.py",
                "-q",
                "-p",
                "no:cacheprovider",
                "--tb=short",
            ]
            process = None
            try:
                # Stream to a bounded pipe rather than capture_output's unbounded memory.
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    env=scrub_credentials(),
                )
                output = bytearray()
                overflow = threading.Event()

                def drain():
                    while chunk := process.stdout.read(4096):
                        remaining = MAX_OUTPUT_BYTES - len(output)
                        output.extend(chunk[: max(0, remaining)])
                        if len(chunk) > remaining:
                            overflow.set()

                reader = threading.Thread(target=drain, daemon=True)
                reader.start()
                deadline = started + min(max(float(timeout_seconds), 0.1), 60)
                while process.poll() is None:
                    if overflow.is_set() or time.monotonic() >= deadline:
                        return unverified(
                            "Sandbox output limit exceeded."
                            if overflow.is_set()
                            else "Sandbox execution timed out.",
                            "ERROR" if overflow.is_set() else "TIMEOUT",
                        )
                    time.sleep(0.05)
                reader.join(timeout=2)
                text = output.decode("utf-8", errors="replace")
                if overflow.is_set():
                    return unverified("Sandbox output limit exceeded.", "ERROR")
                if process.returncode not in (0, 1):
                    return unverified(
                        "Container setup or test collection failed. No execution evidence is available.",
                        "ERROR",
                    )
                passed = re.search(r"(\d+) passed", text)
                failed = re.search(r"(\d+) failed", text)
                if process.returncode == 0 and passed:
                    return TestExecutionResult(
                        executed=True,
                        status="PASSED",
                        evidence_badge="PASSING",
                        trust_grade="EMPIRICAL_ORIGINAL_PASSED",
                        original_test_suite=test_code,
                        healed_test_suite=clean,
                        tests_run=int(passed[1]),
                        stdout=text[:4000],
                        summary_message="Generated tests passed in the restricted container; this does not prove the code is defect-free.",
                    )
                # A failed generated assertion is not proof of a real project defect.
                return TestExecutionResult(
                    executed=True,
                    status="ERROR",
                    evidence_badge="UNVERIFIED",
                    trust_grade="UNVERIFIED",
                    original_test_suite=test_code,
                    healed_test_suite=clean,
                    failures=int(failed[1]) if failed else 0,
                    stdout=text[:4000],
                    summary_message="Generated tests failed or could not run. Validate the test and reconstructed code before claiming a reproduced defect.",
                )
            except (OSError, subprocess.SubprocessError):
                return unverified(
                    "Sandbox could not start; generated tests were not verified.",
                    "ERROR",
                )
            finally:
                # Docker's daemon owns the container; killing the CLI alone is insufficient.
                try:
                    subprocess.run(
                        [docker, "rm", "-f", name],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                        check=False,
                    )
                except (OSError, subprocess.SubprocessError):
                    pass
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
        pr_content: str | None = None,
        repo_root: str | None = None,
        timeout_seconds: float = 15.0,
        max_heal_attempts: int = 2,
    ) -> tuple[TestExecutionResult, str]:
        """
        Execute generated unit tests with dynamic self-healing.
        If initial tests fail due to syntax, missing mocks, or fixtures,
        iteratively heals and re-tests.
        """
        from code_review_agent.sandbox.self_healer import TestSelfHealer

        return TestSelfHealer.heal_and_run(
            runner_func=lambda code, pr: cls.run_tests(
                code, pr, repo_root=repo_root, timeout_seconds=timeout_seconds
            ),
            test_code=test_code,
            pr_content=pr_content,
            max_attempts=max_heal_attempts,
        )

    @classmethod
    def _reconstruct_file_from_hunks(cls, item) -> str:
        """Reconstruct source file from diff hunks by combining context and added lines."""
        reconstructed = []
        if getattr(item, "hunks", None):
            for hunk in item.hunks:
                for hline in hunk.lines:
                    if hline.startswith("+") and not hline.startswith("+++"):
                        reconstructed.append(hline[1:])
                    elif hline.startswith(" ") or (hline == ""):
                        reconstructed.append(
                            hline[1:] if hline.startswith(" ") else hline
                        )
        if not reconstructed:
            reconstructed = [
                line for _, line in DiffParser.extract_added_lines_with_numbers(item)
            ]
        return "\n".join(reconstructed) + "\n"

    @classmethod
    def _apply_diff_to_base(cls, base_text: str, item) -> str:
        """Apply diff hunks onto base file text to reconstruct the post-PR file state."""
        if not getattr(item, "hunks", None):
            return base_text
        base_lines = base_text.splitlines()
        result_lines = []
        last_idx = 0

        sorted_hunks = sorted(item.hunks, key=lambda h: h.old_start)
        for hunk in sorted_hunks:
            hunk_start = max(0, hunk.old_start - 1)
            if hunk_start > last_idx:
                result_lines.extend(base_lines[last_idx:hunk_start])

            for hline in hunk.lines:
                if hline.startswith("+") and not hline.startswith("+++"):
                    result_lines.append(hline[1:])
                elif hline.startswith(" ") or (hline == ""):
                    result_lines.append(hline[1:] if hline.startswith(" ") else hline)

            last_idx = max(last_idx, hunk_start + hunk.old_lines)

        if last_idx < len(base_lines):
            result_lines.extend(base_lines[last_idx:])

        return "\n".join(result_lines) + "\n"
