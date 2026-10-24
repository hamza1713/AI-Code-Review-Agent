"""Explicit status for each attempted scanner, including optional tools."""

import subprocess
from typing import Any

from pydantic import BaseModel, Field

from code_review_agent.diff_parser import DiffParser
from code_review_agent.models import SastFinding
from code_review_agent.review_contracts import ReviewInputSnapshot
from code_review_agent.tools.ruff_tool import RuffRunner


class ScanReport(BaseModel):
    findings: list[SastFinding] = Field(default_factory=list)
    statuses: dict[str, str] = Field(default_factory=dict)


def run_scanners(
    raw_diff: str, snapshot: ReviewInputSnapshot | None = None
) -> ScanReport:
    from code_review_agent.security.trojan_source import TrojanSourceScanner
    from code_review_agent.tools.ai_code_detectors import AICodeDefectScanner
    from code_review_agent.tools.bandit_runner import BanditRunner
    from code_review_agent.tools.sast_scanner import (
        ASTSecurityScanner,
        QuickPatternScanner,
    )
    from code_review_agent.tools.semgrep_runner import SemgrepRunner
    from code_review_agent.tools.supply_chain_scanner import (
        SupplyChainAndSlopsquatScanner,
    )

    parsed = DiffParser.parse_diff(raw_diff)
    if snapshot is None:
        from code_review_agent.source_snapshot import build_snapshot

        snapshot = build_snapshot(raw_diff)
    if snapshot.diff != raw_diff:
        raise ValueError("Snapshot diff mismatch")
    python = any(
        f.target_file.endswith(".py") and not f.is_deleted_file for f in parsed.files
    )
    report = ScanReport()
    seen = set()
    for name, scanner, applicable, optional in [
        ("ast", ASTSecurityScanner, python, False),
        ("patterns", QuickPatternScanner, True, False),
        ("supply_chain", SupplyChainAndSlopsquatScanner, bool(parsed.files), False),
        ("ai_code_defects", AICodeDefectScanner, python, False),
        ("trojan_source", TrojanSourceScanner, bool(parsed.files), False),
        ("bandit", BanditRunner, python, False),
        ("ruff", RuffRunner, python, False),
        ("semgrep", SemgrepRunner, bool(parsed.files), True),
    ]:
        if not applicable:
            report.statuses[name] = "SKIPPED_NOT_APPLICABLE"
            continue
        if hasattr(scanner, "is_available") and not scanner.is_available():
            report.statuses[name] = (
                "SKIPPED_OPTIONAL_UNAVAILABLE" if optional else "FAILED_UNAVAILABLE"
            )
            continue
        if name == "semgrep":
            report.statuses[name] = "SKIPPED_OPTIONAL_UNPINNED_RULES"
            continue
        try:
            if name in {"ast", "bandit", "ruff"}:
                from code_review_agent.tools.full_source_scan import scan_full_source

                found = scan_full_source(snapshot, name)
            else:
                scanner_obj: Any = scanner
                found = (
                    scanner_obj.scan_diff(raw_diff, strict=True)
                    if hasattr(scanner_obj, "is_available")
                    else scanner_obj.scan_diff(raw_diff)
                )
            if name == "ruff" and found and not isinstance(found[0], SastFinding):
                added = {
                    (file.target_file, line): content
                    for file in parsed.files
                    for line, content in DiffParser.extract_added_lines_with_numbers(
                        file
                    )
                }
                found = [
                    SastFinding(
                        rule_id=f"ruff-{f.code}",
                        category="QUALITY",
                        severity="MEDIUM",
                        file_path=f.file_path,
                        line_number=f.line_number,
                        description=f.message,
                        snippet=added.get((f.file_path, f.line_number), ""),
                        fix_recommendation=f"Address Ruff rule {f.code}: {f.message}",
                        analyzer_source="ruff",
                    )
                    for f in found
                ]
            report.statuses[name] = (
                "COMPLETED_HEURISTIC" if name == "patterns" else "COMPLETED"
            )
            for finding in found:
                key = (finding.file_path, finding.line_number, finding.rule_id)
                if key not in seen:
                    seen.add(key)
                    report.findings.append(finding)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            # Tool failure is coverage loss, never a clean scan.
            report.statuses[f"{name}_error"] = type(error).__name__
            report.statuses[name] = "FAILED"
    return report
