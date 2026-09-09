"""
Semgrep Static Analysis Runner & Finding Parser.
Executes Semgrep CLI on source files/diffs when available,
and parses the structured JSON results into normalized SastFinding objects.
"""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional
import re

from code_review_agent.models import SastFinding
from code_review_agent.diff_parser import DiffParser
from code_review_agent.config import logger


class SemgrepRunner:
    """Wrapper around Semgrep CLI for multi-language AST security analysis."""

    @staticmethod
    def is_available() -> bool:
        """Check if Semgrep is installed and available in PATH."""
        return shutil.which("semgrep") is not None

    @classmethod
    def scan_diff(cls, raw_diff: str, config: str = "auto") -> List[SastFinding]:
        """
        Scan added code in a git diff using Semgrep.
        Writes modified files to a temporary workspace and runs Semgrep against them.
        """
        if not cls.is_available():
            logger.debug("Semgrep CLI is not available on PATH. Skipping Semgrep scan.")
            return []

        if not raw_diff or not raw_diff.strip():
            return []

        parsed_pr = DiffParser.parse_diff(raw_diff)
        if not parsed_pr.files:
            return []

        findings: List[SastFinding] = []

        with tempfile.TemporaryDirectory(prefix="semgrep_scan_") as tmp_dir:
            tmp_path = Path(tmp_dir)
            file_map = {}

            for file_diff in parsed_pr.files:
                target_rel = file_diff.target_file
                if not target_rel or file_diff.is_deleted_file:
                    continue

                # Write patch or reconstructed file to temp directory
                dest = tmp_path / target_rel
                dest.parent.mkdir(parents=True, exist_ok=True)

                # Reconstruct added lines or write raw patch
                added_lines = DiffParser.extract_added_lines_with_numbers(file_diff)
                if added_lines:
                    # Write lines at their approximate line numbers
                    max_line = max(l[0] for l in added_lines)
                    file_lines = ["\n"] * (max_line + 1)
                    for line_no, content in added_lines:
                        if line_no < len(file_lines):
                            file_lines[line_no] = content + "\n"
                    dest.write_text("".join(file_lines), encoding="utf-8", errors="ignore")
                else:
                    dest.write_text(file_diff.raw_patch, encoding="utf-8", errors="ignore")

                file_map[str(dest.resolve())] = target_rel

            # Run Semgrep CLI
            try:
                cmd = [
                    "semgrep",
                    "scan",
                    "--json",
                    f"--config={config}",
                    "--quiet",
                    "--disable-version-check",
                    "--no-git-ignore",
                    str(tmp_path)
                ]
                res = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False
                )
                if res.stdout:
                    findings.extend(cls._parse_semgrep_json(res.stdout, file_map))
            except subprocess.TimeoutExpired:
                logger.warning("Semgrep scan timed out after 30 seconds.")
            except Exception as e:
                logger.warning(f"Error executing Semgrep CLI: {e}")

        return findings

    @classmethod
    def scan_path(cls, path: str, config: str = "auto") -> List[SastFinding]:
        """Scan a local directory or file path directly with Semgrep."""
        if not cls.is_available():
            logger.debug("Semgrep CLI is not available on PATH.")
            return []

        target = Path(path).resolve()
        if not target.exists():
            return []

        try:
            cmd = [
                "semgrep",
                "scan",
                "--json",
                f"--config={config}",
                "--quiet",
                "--disable-version-check",
                str(target)
            ]
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=45,
                check=False
            )
            if res.stdout:
                return cls._parse_semgrep_json(res.stdout, base_path=str(target if target.is_dir() else target.parent))
        except Exception as e:
            logger.warning(f"Semgrep path scan failed: {e}")

        return []

    @classmethod
    def _parse_semgrep_json(
        cls,
        json_output: str,
        file_map: Optional[dict] = None,
        base_path: Optional[str] = None
    ) -> List[SastFinding]:
        """Convert raw Semgrep JSON output into standard SastFinding objects."""
        findings: List[SastFinding] = []
        try:
            data = json.loads(json_output)
            results = data.get("results", [])

            for item in results:
                check_id = item.get("check_id", "semgrep-rule")
                extra = item.get("extra", {})
                message = extra.get("message", "Semgrep security finding")
                raw_severity = extra.get("severity", "WARNING").upper()

                # Map Semgrep severity to our schema: LOW, MEDIUM, HIGH, CRITICAL
                severity = "MEDIUM"
                if raw_severity == "ERROR":
                    severity = "HIGH"
                elif raw_severity == "WARNING":
                    severity = "MEDIUM"
                elif raw_severity == "INFO":
                    severity = "LOW"

                metadata = extra.get("metadata", {})
                cwe_raw = metadata.get("cwe", "CWE-Unknown")
                cwe_val = cwe_raw[0] if isinstance(cwe_raw, list) else str(cwe_raw)
                cwe_match = re.search(r"CWE-\d+", cwe_val, re.IGNORECASE)
                cwe = cwe_match.group(0).upper() if cwe_match else (cwe_val if cwe_val.startswith("CWE-") else "CWE-General")

                fix = extra.get("fix", "") or metadata.get("fix_recommendation", "Review and remediate pattern according to OWASP guidelines.")
                lines = extra.get("lines", "")

                raw_path = item.get("path", "")
                abs_file = str(Path(raw_path).resolve()) if raw_path else ""
                rel_file = None
                if file_map:
                    if abs_file in file_map:
                        rel_file = file_map[abs_file]
                    elif raw_path in file_map:
                        rel_file = file_map[raw_path]
                    else:
                        norm_raw = raw_path.replace("\\", "/")
                        for k, v in file_map.items():
                            norm_k = k.replace("\\", "/")
                            if norm_raw.endswith(norm_k) or norm_k.endswith(norm_raw) or Path(raw_path).name == Path(k).name:
                                rel_file = v
                                break
                if not rel_file:
                    if base_path:
                        try:
                            rel_file = str(Path(abs_file).relative_to(base_path)).replace("\\", "/")
                        except ValueError:
                            rel_file = raw_path
                    else:
                        rel_file = raw_path

                line_start = item.get("start", {}).get("line", 1)

                findings.append(
                    SastFinding(
                        rule_id=check_id,
                        cwe=cwe,
                        description=f"[Semgrep] {message.strip()}",
                        severity=severity,
                        file_path=rel_file,
                        line_number=line_start,
                        snippet=lines.strip() if lines else "",
                        fix_recommendation=fix,
                        analyzer_source="semgrep"
                    )
                )
        except Exception as e:
            logger.debug(f"Error parsing Semgrep JSON: {e}")

        return findings
