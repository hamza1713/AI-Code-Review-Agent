"""
Bandit Python Security Scanner & Finding Parser.
Executes Bandit AST security scanner on Python files/diffs when available,
and parses the structured JSON results into normalized SastFinding objects.
"""

import json
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

from code_review_agent.config import logger
from code_review_agent.diff_parser import DiffParser
from code_review_agent.models import SastFinding
from code_review_agent.path_validation import safe_diff_path


def is_safe_subprocess_call(text: str) -> bool:
    """
    Determine if a subprocess invocation is safe from CWE-78 shell injection.
    Safe calls:
    - Lists of arguments without shell=True or /bin/sh wrapper
    - Explicit shell=False
    - Invocations without shell=True and without string interpolation/concatenation
    """
    if not text:
        return True
    if "os.system" in text:
        return False
    if re.search(r"shell\s*=\s*True", text, re.IGNORECASE):
        return False
    # Check for shell wrapper in command list (e.g. ["/bin/sh", "-c", ...])
    if re.search(r"['\"](?:/bin/)?(?:ba)?sh['\"]\s*,\s*['\"]-c['\"]", text):
        return False
    # Explicit shell=False is always safe from shell injection
    if re.search(r"shell\s*=\s*False", text, re.IGNORECASE):
        return True
    # List arguments are safe from shell injection
    if re.search(r"subprocess\.(?:call|run|Popen|check_output)\s*\(\s*\[", text):
        return True
    # If no string formatting, concatenation, or interpolation is used, Python defaults to shell=False
    if not re.search(r"f[\"']|\s*\+\s*|%s|%\s*\(|\.format\s*\(", text):
        return True
    return False


class BanditRunner:
    """Wrapper around Bandit for Python AST security scanning."""

    _availability_cache: bool | None = None

    @classmethod
    def is_available(cls) -> bool:
        """
        Check if Bandit is installed via CLI or Python package.
        Cached for the lifetime of the process — availability cannot change
        mid-run, and the subprocess fallback below is expensive enough
        (a full Python interpreter spin-up) that re-checking it on every
        single SAST scan call materially slows down every review.
        """
        if cls._availability_cache is not None:
            return cls._availability_cache
        if shutil.which("bandit") is not None:
            cls._availability_cache = True
            return True
        try:
            res = subprocess.run(
                [sys.executable, "-m", "bandit", "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            cls._availability_cache = res.returncode == 0
        except Exception:
            cls._availability_cache = False
        return cls._availability_cache

    @classmethod
    def scan_diff(cls, raw_diff: str, *, strict: bool = False) -> list[SastFinding]:
        """
        Scan Python added code in a diff using Bandit.
        """
        if not cls.is_available():
            logger.debug("Bandit is not available. Skipping Bandit scan.")
            return []

        if not raw_diff or not raw_diff.strip():
            return []

        parsed_pr = DiffParser.parse_diff(raw_diff)
        py_files = [
            f
            for f in parsed_pr.files
            if f.target_file.endswith(".py") and not f.is_deleted_file
        ]
        if not py_files:
            return []

        findings: list[SastFinding] = []

        with tempfile.TemporaryDirectory(prefix="bandit_scan_") as tmp_dir:
            tmp_path = Path(tmp_dir)
            file_map = {}
            line_maps: dict[str, tuple[dict[int, int], set[int]]] = {}

            for file_diff in py_files:
                target_rel = file_diff.target_file
                dest = safe_diff_path(tmp_path, target_rel)
                added_lines = {
                    line_no
                    for line_no, _ in DiffParser.extract_added_lines_with_numbers(
                        file_diff
                    )
                }
                if not added_lines:
                    continue

                dest.parent.mkdir(parents=True, exist_ok=True)

                # Reconstruct syntactically valid Python code preserving target line mappings
                clean_lines: list[str] = []
                line_no_mapping: dict[int, int] = {}
                if getattr(file_diff, "hunks", None):
                    for hunk in file_diff.hunks:
                        curr_target_line = hunk.new_start
                        for line in hunk.lines:
                            if line.startswith("+") and not line.startswith("+++"):
                                clean_lines.append(line[1:])
                                line_no_mapping[len(clean_lines)] = curr_target_line
                                curr_target_line += 1
                            elif line.startswith("-"):
                                continue
                            else:
                                clean_lines.append(
                                    line[1:] if line.startswith(" ") else line
                                )
                                line_no_mapping[len(clean_lines)] = curr_target_line
                                curr_target_line += 1
                else:
                    curr_target_line = 1
                    for line in file_diff.raw_patch.splitlines():
                        if line.startswith("@@"):
                            m = re.search(r"\+(\d+)", line)
                            if m:
                                curr_target_line = int(m.group(1))
                            continue
                        if line.startswith(
                            (
                                "-",
                                "---",
                                "+++",
                                "diff ",
                                "index ",
                                "new file mode",
                                "deleted file mode",
                            )
                        ):
                            continue
                        if line.startswith(("+", " ")):
                            clean_lines.append(line[1:])
                        else:
                            clean_lines.append(line)
                        line_no_mapping[len(clean_lines)] = curr_target_line
                        curr_target_line += 1

                code_text = textwrap.dedent("\n".join(clean_lines))
                dest.write_text(
                    code_text, encoding="utf-8", errors="ignore"
                )
                file_map[str(dest.resolve())] = target_rel
                file_map[str(dest)] = target_rel
                line_maps[target_rel] = (line_no_mapping, added_lines)

            if not file_map:
                return []

            try:
                cmd = [
                    sys.executable,
                    "-m",
                    "bandit",
                    "-r",
                    str(tmp_path),
                    "-f",
                    "json",
                    "-q",
                    "-s",
                    "B401,B402,B403,B404,B405,B406,B407,B408,B409,B410,B411,B412,B413",
                ]
                res = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=20, check=False
                )
                if strict:
                    if res.returncode not in (0, 1):
                        raise RuntimeError("TOOL_EXECUTION_FAILED")
                    data = json.loads(res.stdout)
                    if (
                        not isinstance(data, dict)
                        or "results" not in data
                        or data.get("errors")
                    ):
                        raise ValueError("INVALID_OR_PARTIAL_TOOL_OUTPUT")
                if res.stdout:
                    findings.extend(
                        cls._parse_bandit_json(
                            res.stdout, file_map, line_maps=line_maps
                        )
                    )
            except Exception as e:
                if strict:
                    raise
                logger.debug(f"Error running Bandit CLI: {e}")

        return findings

    @classmethod
    def _parse_bandit_json(
        cls,
        json_output: str,
        file_map: dict | None = None,
        line_maps: dict[str, tuple[dict[int, int], set[int]]] | None = None,
    ) -> list[SastFinding]:
        """Parse Bandit JSON results into SastFinding list."""
        findings: list[SastFinding] = []
        try:
            data = json.loads(json_output)
            results = data.get("results", [])
            for item in results:
                test_id = item.get("test_id", "B000")

                # 1. Skip B4xx import blacklist notices (importing a library is not an exploit on its own)
                if test_id.startswith("B4"):
                    continue

                code = item.get("code", "")

                # 2. Skip B603 (subprocess_without_shell_equals_true) and B607 (start_process_with_partial_path)
                # when the invocation is safe from CWE-78 command injection (list arguments, shell=False)
                if test_id in ("B603", "B607") and is_safe_subprocess_call(code):
                    continue

                test_name = item.get("test_name", "bandit_check")
                issue_text = item.get("issue_text", "")
                issue_severity = item.get("issue_severity", "MEDIUM").upper()
                cwe_info = item.get("issue_cwe", {})
                cwe_id = cwe_info.get("id")
                cwe = f"CWE-{cwe_id}" if cwe_id else "CWE-Security"

                line_number = item.get("line_number", 1)
                raw_filename = item.get("filename", "")
                abs_filename = str(Path(raw_filename).resolve()) if raw_filename else ""

                rel_file = None
                if file_map:
                    if abs_filename in file_map:
                        rel_file = file_map[abs_filename]
                    elif raw_filename in file_map:
                        rel_file = file_map[raw_filename]
                    else:
                        raise ValueError("UNKNOWN_SCANNER_PATH")
                if not rel_file:
                    rel_file = raw_filename or "app/file.py"

                if line_maps and rel_file in line_maps:
                    line_no_mapping, added_lines = line_maps[rel_file]
                    actual_line = line_no_mapping.get(line_number, line_number)
                    if actual_line not in added_lines:
                        continue
                    line_number = actual_line

                severity = "MEDIUM"
                if issue_severity == "HIGH":
                    severity = "HIGH"
                elif issue_severity == "LOW":
                    severity = "LOW"

                findings.append(
                    SastFinding(
                        rule_id=f"BANDIT-{test_id}",
                        cwe=cwe,
                        description=f"[Bandit] {test_name}: {issue_text}",
                        severity=severity,
                        file_path=rel_file,
                        line_number=line_number,
                        snippet=code.strip() if code else "",
                        fix_recommendation=f"Remediate according to Bandit {test_id} guidelines ({item.get('more_info', '')})",
                        analyzer_source="bandit",
                    )
                )
        except Exception as e:
            logger.debug(f"Error parsing Bandit JSON: {e}")

        return findings
