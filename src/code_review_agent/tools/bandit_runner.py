"""
Bandit Python Security Scanner & Finding Parser.
Executes Bandit AST security scanner on Python files/diffs when available,
and parses the structured JSON results into normalized SastFinding objects.
"""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional
import importlib.util

from code_review_agent.models import SastFinding
from code_review_agent.diff_parser import DiffParser
from code_review_agent.config import logger


import sys

class BanditRunner:
    """Wrapper around Bandit for Python AST security scanning."""

    _availability_cache: Optional[bool] = None

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
            res = subprocess.run([sys.executable, "-m", "bandit", "--version"], capture_output=True, text=True, timeout=5, check=False)
            cls._availability_cache = res.returncode == 0
        except Exception:
            cls._availability_cache = False
        return cls._availability_cache

    @classmethod
    def scan_diff(cls, raw_diff: str) -> List[SastFinding]:
        """
        Scan Python added code in a diff using Bandit.
        """
        if not cls.is_available():
            logger.debug("Bandit is not available. Skipping Bandit scan.")
            return []

        if not raw_diff or not raw_diff.strip():
            return []

        parsed_pr = DiffParser.parse_diff(raw_diff)
        py_files = [f for f in parsed_pr.files if f.target_file.endswith(".py") and not f.is_deleted_file]
        if not py_files:
            return []

        findings: List[SastFinding] = []

        with tempfile.TemporaryDirectory(prefix="bandit_scan_") as tmp_dir:
            tmp_path = Path(tmp_dir)
            file_map = {}

            for file_diff in py_files:
                target_rel = file_diff.target_file
                dest = tmp_path / target_rel
                dest.parent.mkdir(parents=True, exist_ok=True)

                # Reconstruct syntactically valid Python code preserving context
                clean_lines = []
                for line in file_diff.raw_patch.splitlines():
                    if line.startswith("+") and not line.startswith("+++"):
                        clean_lines.append(line[1:])
                    elif line.startswith(" "):
                        clean_lines.append(line[1:])
                    elif line.startswith("-") or line.startswith("@@") or line.startswith("diff ") or line.startswith("index "):
                        continue
                    elif line.startswith("---") or line.startswith("+++"):
                        continue
                    elif line.startswith("new file mode") or line.startswith("deleted file mode"):
                        continue
                    else:
                        clean_lines.append(line)

                dest.write_text("\n".join(clean_lines), encoding="utf-8", errors="ignore")
                file_map[str(dest.resolve())] = target_rel
                file_map[str(dest)] = target_rel
                file_map[dest.name] = target_rel

            try:
                cmd = [sys.executable, "-m", "bandit", "-r", str(tmp_path), "-f", "json", "-q"]
                res = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False
                )
                if res.stdout:
                    findings.extend(cls._parse_bandit_json(res.stdout, file_map))
            except Exception as e:
                logger.debug(f"Error running Bandit CLI: {e}")

        return findings

    @classmethod
    def _parse_bandit_json(cls, json_output: str, file_map: Optional[dict] = None) -> List[SastFinding]:
        """Parse Bandit JSON results into SastFinding list."""
        findings: List[SastFinding] = []
        try:
            data = json.loads(json_output)
            results = data.get("results", [])
            for item in results:
                test_id = item.get("test_id", "B000")
                test_name = item.get("test_name", "bandit_check")
                issue_text = item.get("issue_text", "")
                issue_severity = item.get("issue_severity", "MEDIUM").upper()
                cwe_info = item.get("issue_cwe", {})
                cwe_id = cwe_info.get("id")
                cwe = f"CWE-{cwe_id}" if cwe_id else "CWE-Security"

                code = item.get("code", "")
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
                        norm_raw = raw_filename.replace("\\", "/")
                        for k, v in file_map.items():
                            norm_v = v.replace("\\", "/")
                            if norm_raw.endswith(norm_v) or Path(raw_filename).name == Path(v).name:
                                rel_file = v
                                break

                if not rel_file:
                    rel_file = Path(raw_filename).name if raw_filename else "app/file.py"

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
                        analyzer_source="bandit"
                    )
                )
        except Exception as e:
            logger.debug(f"Error parsing Bandit JSON: {e}")

        return findings
