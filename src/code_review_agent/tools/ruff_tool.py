"""
Ruff Fast Python Linter & Code Quality Tool for CrewAI Agents.
Executes Ruff CLI on Python code/diffs to catch syntax errors, unused imports,
complexity issues, and code smells in milliseconds.
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Dict, Any, Optional, Type
from pydantic import BaseModel, Field
from crewai.tools import BaseTool

from code_review_agent.diff_parser import DiffParser
from code_review_agent.config import logger


class RuffLintFinding(BaseModel):
    """Normalized finding from Ruff linter."""
    code: str
    message: str
    file_path: str
    line_number: int
    column: int
    fix_available: bool = False
    fix_applicability: Optional[str] = None


class RuffRunner:
    """Wrapper around Ruff CLI for fast Python linting."""

    _availability_cache: Optional[bool] = None

    @classmethod
    def is_available(cls) -> bool:
        """
        Check if Ruff is available in PATH or Python environment.
        Cached for the process lifetime — see BanditRunner.is_available for why.
        """
        if cls._availability_cache is not None:
            return cls._availability_cache
        if shutil.which("ruff") is not None:
            cls._availability_cache = True
            return True
        try:
            res = subprocess.run([sys.executable, "-m", "ruff", "--version"], capture_output=True, text=True, check=False)
            cls._availability_cache = res.returncode == 0
        except Exception:
            cls._availability_cache = False
        return cls._availability_cache

    @classmethod
    def scan_diff(cls, raw_diff: str) -> List[RuffLintFinding]:
        """Scan modified Python code in a diff with Ruff."""
        if not cls.is_available() or not raw_diff or not raw_diff.strip():
            return []

        parsed_pr = DiffParser.parse_diff(raw_diff)
        py_files = [f for f in parsed_pr.files if f.target_file.endswith(".py") and not f.is_deleted_file]
        if not py_files:
            return []

        findings: List[RuffLintFinding] = []

        with tempfile.TemporaryDirectory(prefix="ruff_lint_") as tmp_dir:
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

            # Run Ruff
            cmd = ["ruff", "check", "--output-format=json", str(tmp_path)] if shutil.which("ruff") else ["python", "-m", "ruff", "check", "--output-format=json", str(tmp_path)]
            try:
                res = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False
                )
                if res.stdout:
                    findings.extend(cls._parse_ruff_json(res.stdout, file_map))
            except Exception as e:
                logger.debug(f"Error running Ruff linter: {e}")

        return findings

    @classmethod
    def scan_path(cls, file_or_dir: str) -> List[RuffLintFinding]:
        """Scan a path directly with Ruff."""
        if not cls.is_available():
            return []

        target = Path(file_or_dir).resolve()
        if not target.exists():
            return []

        cmd = ["ruff", "check", "--output-format=json", str(target)] if shutil.which("ruff") else ["python", "-m", "ruff", "check", "--output-format=json", str(target)]
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=20,
                check=False
            )
            if res.stdout:
                return cls._parse_ruff_json(res.stdout, base_path=str(target if target.is_dir() else target.parent))
        except Exception as e:
            logger.debug(f"Error running Ruff path scan: {e}")

        return []

    @classmethod
    def _parse_ruff_json(cls, json_output: str, file_map: Optional[dict] = None, base_path: Optional[str] = None) -> List[RuffLintFinding]:
        """Parse Ruff JSON output."""
        findings: List[RuffLintFinding] = []
        try:
            items = json.loads(json_output)
            if not isinstance(items, list):
                return []

            for item in items:
                code = item.get("code", "")
                message = item.get("message", "")
                loc = item.get("location", {})
                line = loc.get("row", 1)
                col = loc.get("column", 1)
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
                            norm_k = k.replace("\\", "/")
                            if norm_raw.endswith(norm_k) or norm_k.endswith(norm_raw) or Path(raw_filename).name == Path(k).name:
                                rel_file = v
                                break
                if not rel_file:
                    if base_path:
                        try:
                            rel_file = str(Path(abs_filename).relative_to(base_path)).replace("\\", "/")
                        except ValueError:
                            rel_file = raw_filename
                    else:
                        rel_file = raw_filename

                fix = item.get("fix")
                fix_available = fix is not None
                fix_app = fix.get("applicability") if fix else None

                findings.append(
                    RuffLintFinding(
                        code=code,
                        message=message,
                        file_path=rel_file,
                        line_number=line,
                        column=col,
                        fix_available=fix_available,
                        fix_applicability=fix_app
                    )
                )
        except Exception as e:
            logger.debug(f"Error parsing Ruff JSON: {e}")

        return findings


# --- CrewAI Tool Wrapper ---

class RuffToolInput(BaseModel):
    """Input schema for RuffTool."""
    diff_or_code: str = Field(..., description="The git diff or Python source code snippet to lint with Ruff")


class RuffTool(BaseTool):
    """CrewAI Tool for fast Python static linting using Ruff."""
    name: str = "Ruff Fast Python Linter"
    description: str = (
        "Runs Ruff to check Python code quality, style, unused imports, undefined variables, "
        "and complexity issues. Returns structured lint findings with rule codes and line numbers."
    )
    args_schema: Type[BaseModel] = RuffToolInput

    def _run(self, diff_or_code: str) -> str:
        """Execute Ruff on the given code/diff."""
        if not RuffRunner.is_available():
            return "ℹ️ Ruff linter is not installed on PATH. Skipping Ruff static linting."

        findings = RuffRunner.scan_diff(diff_or_code)
        if not findings:
            return "✅ Ruff Linter: No code quality issues, unused imports, or style violations detected."

        lines = [f"⚡ Ruff Linter: Found {len(findings)} issue(s):"]
        for i, f in enumerate(findings[:15], 1):
            fix_msg = " [Auto-fixable]" if f.fix_available else ""
            lines.append(f"{i}. [{f.code}] {f.message} in `{f.file_path}`:L{f.line_number}:{f.column}{fix_msg}")

        if len(findings) > 15:
            lines.append(f"... and {len(findings) - 15} more lint findings omitted.")

        return "\n".join(lines)
