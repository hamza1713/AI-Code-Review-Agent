"""
Parser for converting LLM Tech Lead synthesized advisory findings into structured SastFinding objects.
Allows high-value agent-discovered issues (race conditions, infinite recursion, algorithmic traps)
to be formally promoted into the reconciled findings table with analyzer_source='llm-crew'.
"""

import re
from typing import List, Any, Optional, Dict
from code_review_agent.models import SastFinding, SummarizedFindingsJSON, Fix

_SEV_RE = re.compile(r"\b(CRITICAL|HIGH|MEDIUM|LOW|INFO)\b", re.IGNORECASE)

# Mapping common advisory keywords to CWE identifiers
_KEYWORD_CWE_MAP = [
    (re.compile(r"sql\s*injection|sqli", re.I), "CWE-89"),
    (re.compile(r"race\s*condition|concurrency|synchroniz|unsynchronized|thread\s*safe", re.I), "CWE-362"),
    (re.compile(r"infinite\s*recursion|recursion\s*depth", re.I), "CWE-674"),
    (re.compile(r"timing\s*(?:side-channel|attack|comparison)", re.I), "CWE-208"),
    (re.compile(r"password\s*hash|md5.*password|insecure\s*hash", re.I), "CWE-916"),
    (re.compile(r"deserializ|pickle", re.I), "CWE-502"),
    (re.compile(r"command\s*injection|shell\s*=\s*true", re.I), "CWE-78"),
    (re.compile(r"path\s*traversal", re.I), "CWE-22"),
    (re.compile(r"divide-by-zero|zerodivision", re.I), "CWE-369"),
    (re.compile(r"swallow.*exception|silent.*exception", re.I), "CWE-703"),
]


def extract_crew_severity(text: str, default: str = "MEDIUM") -> str:
    """Extract explicit severity keyword if present in description, else fallback."""
    m = _SEV_RE.search(text or "")
    if m:
        return m.group(1).upper()
    upper_text = (text or "").upper()
    if any(k in upper_text for k in ("CRITICAL", "VULNERABILITY", "EXPLOIT", "CORRUPT", "DEADLOCK")):
        return "HIGH"
    if any(k in upper_text for k in ("BOTTLENECK", "RACE CONDITION", "RECURSION", "LEAK")):
        return "MEDIUM"
    return default


def extract_crew_cwe(text: str) -> str:
    """Extract or infer CWE identifier from advisory text."""
    m = re.search(r"CWE-(\d+)", text or "", re.IGNORECASE)
    if m:
        return f"CWE-{m.group(1)}"
    for pattern, cwe in _KEYWORD_CWE_MAP:
        if pattern.search(text or ""):
            return cwe
    return ""


def parse_crew_advisory_findings(
    summarized: Any,
    file_hint: str = "unknown",
) -> List[SastFinding]:
    """
    Convert the Tech Lead advisory fix items and blocking reasons into SastFinding objects.
    These findings are tagged with analyzer_source='llm-crew'.
    """
    findings: List[SastFinding] = []
    seen_keys = set()

    if not summarized:
        return findings

    fixes: List[Any] = []
    blocking_reasons: List[str] = []

    if isinstance(summarized, SummarizedFindingsJSON):
        fixes = summarized.fix or []
        blocking_reasons = summarized.blocking_reasons or []
    elif isinstance(summarized, dict):
        fixes = summarized.get("fix", []) or []
        blocking_reasons = summarized.get("blocking_reasons", []) or []

    for item in fixes:
        if isinstance(item, Fix):
            desc = item.description
            sol = item.solutions
            expl = item.explanation
            fp = item.file_path or file_hint
            line = item.line_number or 1
        elif isinstance(item, dict):
            desc = item.get("description", "")
            sol = item.get("solutions", "")
            expl = item.get("explanation", "")
            fp = item.get("file_path") or file_hint
            line = item.get("line_number") or 1
        else:
            continue

        if not desc:
            continue

        combined_text = f"{desc} {expl}"
        sev = extract_crew_severity(combined_text)
        cwe = extract_crew_cwe(combined_text)

        key = (fp, line, desc[:50].lower())
        if key in seen_keys:
            continue
        seen_keys.add(key)

        findings.append(
            SastFinding(
                rule_id="CREW-ADVISORY-001",
                cwe=cwe,
                category="SECURITY" if cwe in ("CWE-89", "CWE-78", "CWE-362", "CWE-916", "CWE-208", "CWE-502") else "QUALITY",
                name=desc[:80].strip(),
                description=expl or desc,
                severity=sev,
                file_path=fp,
                line_number=line,
                snippet="",
                fix_recommendation=sol or "Review advisory recommendation from Tech Lead.",
                analyzer_source="llm-crew",
            )
        )

    existing_cwes = {f.cwe for f in findings if f.cwe}
    for reason in blocking_reasons:
        if not reason or not isinstance(reason, str):
            continue
        cwe = extract_crew_cwe(reason)
        # Avoid creating generic unanchored L1 findings if the CWE is already covered by a concrete finding
        if cwe and cwe in existing_cwes:
            continue
        if any(reason[:40].lower() in k[2] for k in seen_keys):
            continue

        sev = extract_crew_severity(reason, default="HIGH")
        key = (file_hint, 1, reason[:50].lower())
        if key in seen_keys:
            continue
        seen_keys.add(key)
        if cwe:
            existing_cwes.add(cwe)

        findings.append(
            SastFinding(
                rule_id="CREW-BLOCKING-001",
                cwe=cwe,
                category="SECURITY" if cwe else "QUALITY",
                name=reason[:80].strip(),
                description=reason,
                severity=sev,
                file_path=file_hint,
                line_number=1,
                snippet="",
                fix_recommendation="Remediate blocking issue identified by multi-agent review crew.",
                analyzer_source="llm-crew",
            )
        )

    return findings
