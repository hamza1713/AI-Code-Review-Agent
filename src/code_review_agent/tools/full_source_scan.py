"""Strict scanners over full source; only changed target lines are reported."""

import ast
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from code_review_agent.diff_parser import DiffParser
from code_review_agent.models import SastFinding
from code_review_agent.path_validation import safe_diff_path
from code_review_agent.review_contracts import ReviewInputSnapshot
from code_review_agent.tools.ast_security_scanner import ASTSecurityScanner
from code_review_agent.tools.bandit_runner import BanditRunner
from code_review_agent.tools.ruff_tool import RuffRunner


def scan_full_source(snapshot: ReviewInputSnapshot, scanner: str) -> list[SastFinding]:
    parsed = DiffParser.parse_diff(snapshot.diff)
    changed = {
        f.target_file: dict(DiffParser.extract_added_lines_with_numbers(f))
        for f in parsed.files
        if f.target_file.endswith(".py") and not f.is_deleted_file
    }
    sources = {f.path: f for f in snapshot.files}
    if any(path not in sources for path in changed):
        raise ValueError("FULL_SOURCE_UNAVAILABLE")
    findings = []
    if scanner == "ast":
        for path, added in changed.items():
            lines = sources[path].text.splitlines()
            tree = ast.parse(sources[path].text, filename=path)
            findings.extend(
                ASTSecurityScanner._analyze_tree(
                    tree, path, lines, {i: i for i in range(1, len(lines) + 1)}, added
                )
            )
        # Signature comparisons require the old/new diff, in addition to full-head AST.
        findings.extend(
            f
            for f in ASTSecurityScanner.scan_diff(snapshot.diff)
            if f.rule_id == "ARCH-SIG-001"
        )
    else:
        with tempfile.TemporaryDirectory(prefix="full_source_scan_") as folder:
            mapping = {}
            for path in changed:
                dest = safe_diff_path(Path(folder), path)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(sources[path].raw)
                mapping[str(dest.resolve())] = path
            cmd = (
                [
                    sys.executable,
                    "-I",
                    "-m",
                    "bandit",
                    "-r",
                    folder,
                    "-f",
                    "json",
                    "-q",
                    "--ignore-nosec",
                    "-s",
                    "B401,B402,B403,B404,B405,B406,B407,B408,B409,B410,B411,B412,B413",
                ]
                if scanner == "bandit"
                else [
                    sys.executable,
                    "-I",
                    "-m",
                    "ruff",
                    "check",
                    "--isolated",
                    "--no-cache",
                    "--output-format=json",
                    folder,
                ]
            )
            res = subprocess.run(
                cmd,
                cwd=folder,
                capture_output=True,
                text=True,
                timeout=20 if scanner == "bandit" else 15,
                check=False,
            )
            if res.returncode not in (0, 1):
                raise RuntimeError("TOOL_EXECUTION_FAILED")
            data = json.loads(res.stdout)
            items = (
                data.get("results")
                if scanner == "bandit" and isinstance(data, dict)
                else data
            )
            if not isinstance(items, list) or (
                scanner == "bandit" and data.get("errors")
            ):
                raise ValueError("INVALID_OR_PARTIAL_TOOL_OUTPUT")
            for item in items:
                filename = str(Path(item["filename"]).resolve())
                if filename not in mapping:
                    raise ValueError("UNKNOWN_SCANNER_PATH")
            if scanner == "bandit":
                findings = BanditRunner._parse_bandit_json(res.stdout, mapping)
            else:
                for item in RuffRunner._parse_ruff_json(res.stdout, mapping):
                    findings.append(
                        SastFinding(
                            rule_id=f"ruff-{item.code}",
                            category="QUALITY",
                            severity="MEDIUM",
                            file_path=item.file_path,
                            line_number=item.line_number,
                            description=item.message,
                            snippet=changed[item.file_path].get(item.line_number, ""),
                            fix_recommendation=f"Address Ruff rule {item.code}: {item.message}",
                            analyzer_source="ruff",
                        )
                    )
    result = []
    for finding in findings:
        added = changed.get(finding.file_path, {})
        if finding.line_number in added:
            # A single exact changed line is structural evidence, not a truth label.
            finding.snippet = added[finding.line_number]
            from code_review_agent.models import compute_finding_fingerprint

            finding.fingerprint = compute_finding_fingerprint(
                finding.file_path, finding.rule_id, finding.snippet
            )
            result.append(finding)
    return result
