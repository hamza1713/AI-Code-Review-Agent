"""
Unified Security Analysis Scanner & CrewAI Tool.
Coordinates Semgrep AST analysis, Bandit Python scanning, and fast heuristic regex patterns
into a unified vulnerability reporting pipeline before LLM reasoning.
"""

import re
from typing import List, Dict, Type, Any, Optional
from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from code_review_agent.models import SastFinding, ParsedPR
from code_review_agent.diff_parser import DiffParser
from code_review_agent.tools.semgrep_runner import SemgrepRunner
from code_review_agent.tools.bandit_runner import BanditRunner
from code_review_agent.config import logger
from code_review_agent.cache import memoize_by_content



class SecurityPatternRule(BaseModel):
    rule_id: str
    cwe: str
    name: str
    severity: str  # 'LOW', 'MEDIUM', 'HIGH', 'CRITICAL'
    pattern: str
    description: str
    fix_recommendation: str


# Built-in Regex Security Patterns (Heuristic Pattern Matchers)
SECURITY_PATTERN_RULES: List[SecurityPatternRule] = [
    SecurityPatternRule(
        rule_id="SEC-SQLI-001",
        cwe="CWE-89",
        name="SQL Query String Interpolation",
        severity="CRITICAL",
        pattern=r"(?:(?:db\.(?:query|execute)|cursor\.execute)\s*\(\s*f[\"']|(?:query|sql|stmt)\s*=\s*f[\"']).*?(?:SELECT|INSERT|UPDATE|DELETE).*?\{.*?\}",
        description="Formatted string (f-string) interpolation used directly in SQL query execution. Allows manipulation of SQL syntax.",
        fix_recommendation="Use parameterized queries or prepared statements: db.query('SELECT * FROM users WHERE username = %s', (username,))"
    ),
    SecurityPatternRule(
        rule_id="SEC-SQLI-002",
        cwe="CWE-89",
        name="SQL Query String Concatenation",
        severity="CRITICAL",
        pattern=r"(?:(?:db\.(?:query|execute)|cursor\.execute)\s*\(\s*[\"']|(?:query|sql|stmt)\s*=\s*[\"']).*?(?:SELECT|INSERT|UPDATE|DELETE).*?[\"']\s*\+",
        description="String concatenation used directly in SQL query execution.",
        fix_recommendation="Pass SQL parameters as a tuple/dictionary rather than concatenating strings."
    ),
    SecurityPatternRule(
        rule_id="SEC-AUTH-001",
        cwe="CWE-256",
        name="Plain-Text Password Comparison",
        severity="HIGH",
        pattern=r"user\.password\s*==\s*password|password\s*==\s*user\.password",
        description="Comparing passwords directly in plain text. Passwords must be verified via cryptographic hashes.",
        fix_recommendation="Use a password hashing library: bcrypt.checkpw(password.encode('utf-8'), user.password_hash.encode('utf-8'))"
    ),
    SecurityPatternRule(
        rule_id="SEC-SECRET-001",
        cwe="CWE-798",
        name="Hardcoded Secret or API Key Pattern",
        severity="CRITICAL",
        pattern=r"(?:api_key|secret_key|private_key|token|password|aws_secret|stripe_secret|_key)\s*=\s*['\"][A-Za-z0-9_\-]{16,}['\"]",
        description="Hardcoded credentials, API keys, or secrets detected in source code.",
        fix_recommendation="Move sensitive credentials to environment variables or a secure key management service."
    ),

    SecurityPatternRule(
        rule_id="SEC-CMD-001",
        cwe="CWE-78",
        name="OS Command Injection / Execution",
        severity="CRITICAL",
        pattern=r"(?:os\.system|subprocess\.(?:call|run|Popen))\s*\(",
        description="Executing operating system commands with os.system or subprocess.",
        fix_recommendation="Pass arguments as a list with shell=False: subprocess.run(['ls', '-l', directory], shell=False, check=True)"
    ),
    SecurityPatternRule(
        rule_id="SEC-DESER-001",
        cwe="CWE-502",
        name="Insecure Deserialization Pattern (Pickle)",
        severity="HIGH",
        pattern=r"pickle\.loads\s*\(",
        description="Deserializing untrusted data with python pickle can lead to arbitrary code execution.",
        fix_recommendation="Use safe serialization formats like JSON, Protocol Buffers, or MessagePack."
    ),
    SecurityPatternRule(
        rule_id="SEC-CRYPTO-001",
        cwe="CWE-327",
        name="Weak Hash Algorithm (MD5 / SHA1)",
        severity="MEDIUM",
        pattern=r"hashlib\.(?:md5|sha1)\s*\(",
        description="MD5 and SHA-1 are cryptographically broken for security verification.",
        fix_recommendation="Upgrade to SHA-256 (hashlib.sha256) or SHA-3 for hashing."
    )
]

# Backwards compatibility alias
SAST_RULES = SECURITY_PATTERN_RULES


class QuickPatternScanner:
    """
    Performs fast heuristic regex pattern scanning on PR diffs.
    Identifies common security anti-patterns and high-risk code constructs.
    """

    @staticmethod
    @memoize_by_content("quick_pattern")
    def scan_diff(raw_diff: str) -> List[SastFinding]:
        """Scan all added lines in a unified diff for known security patterns."""
        parsed_pr = DiffParser.parse_diff(raw_diff)
        findings: List[SastFinding] = []

        for file_diff in parsed_pr.files:
            file_path = file_diff.target_file
            added_lines = DiffParser.extract_added_lines_with_numbers(file_diff)

            # Check line-by-line
            for line_no, line_content in added_lines:
                for rule in SECURITY_PATTERN_RULES:
                    if re.search(rule.pattern, line_content, re.IGNORECASE):
                        findings.append(
                            SastFinding(
                                rule_id=rule.rule_id,
                                cwe=rule.cwe,
                                description=f"{rule.name}: {rule.description}",
                                severity=rule.severity,
                                file_path=file_path,
                                line_number=line_no,
                                snippet=line_content.strip(),
                                fix_recommendation=rule.fix_recommendation,
                                analyzer_source="regex"
                            )
                        )

            # Check multiline patches for patterns spanning lines
            raw_patch = file_diff.raw_patch
            for rule in SECURITY_PATTERN_RULES:
                for match in re.finditer(rule.pattern, raw_patch, re.IGNORECASE):
                    matched_snippet = match.group(0).strip()
                    if not any(f.file_path == file_path and f.rule_id == rule.rule_id for f in findings):
                        findings.append(
                            SastFinding(
                                rule_id=rule.rule_id,
                                cwe=rule.cwe,
                                description=f"{rule.name}: {rule.description}",
                                severity=rule.severity,
                                file_path=file_path,
                                line_number=1,
                                snippet=matched_snippet,
                                fix_recommendation=rule.fix_recommendation,
                                analyzer_source="regex"
                            )
                        )

        return findings


class UnifiedSecurityScanner:
    """
    Enterprise Unified Security Scanner combining:
    1. Semgrep AST static analysis (multi-language, if available)
    2. Bandit Python AST vulnerability scanning (Python, if available)
    3. Fast Heuristic Regex Pattern matching
    De-duplicates findings across tools.
    """

    @classmethod
    def scan_diff(cls, raw_diff: str) -> List[SastFinding]:
        """Execute all available security scanners and merge results without duplication."""
        all_findings: List[SastFinding] = []
        seen_keys = set()

        # 1. Built-in Regex Pattern scanner (always active, high priority for SEC- rules)
        regex_findings = QuickPatternScanner.scan_diff(raw_diff)
        for f in regex_findings:
            key = (f.file_path, f.line_number, f.rule_id)
            if key not in seen_keys:
                seen_keys.add(key)
                all_findings.append(f)

        # 2. Bandit AST scan (if available)
        if BanditRunner.is_available():
            bandit_findings = BanditRunner.scan_diff(raw_diff)
            for f in bandit_findings:
                key = (f.file_path, f.line_number, f.rule_id)
                if key not in seen_keys:
                    seen_keys.add(key)
                    all_findings.append(f)

        # 3. Semgrep AST scan (if available)
        if SemgrepRunner.is_available():
            semgrep_findings = SemgrepRunner.scan_diff(raw_diff)
            for f in semgrep_findings:
                key = (f.file_path, f.line_number, f.rule_id)
                if key not in seen_keys:
                    seen_keys.add(key)
                    all_findings.append(f)

        return all_findings


# Compatibility aliases
SastEngine = UnifiedSecurityScanner
PatternEngine = QuickPatternScanner


# --- CrewAI Tool Wrappers ---

class QuickPatternScannerInput(BaseModel):
    """Input parameters for QuickPatternScannerTool."""
    diff_content: str = Field(..., description="The raw unified git diff content to scan for security patterns")


class QuickPatternScannerTool(BaseTool):
    """CrewAI Tool for unified security vulnerability scanning (Semgrep, Bandit, Regex)."""
    name: str = "Unified Security Vulnerability Scanner"
    description: str = (
        "Scans PR diffs for security vulnerabilities, injection flaws (SQLi, XSS, Command Injection), "
        "plaintext credentials, hardcoded secrets, and dangerous deserialization using AST analysis "
        "(Semgrep, Bandit) and heuristic patterns. Returns structured findings with CWEs, line numbers, and fixes."
    )
    args_schema: Type[BaseModel] = QuickPatternScannerInput

    def _run(self, diff_content: str) -> str:
        """Execute unified scan and format results for agent context."""
        findings = UnifiedSecurityScanner.scan_diff(diff_content)
        if not findings:
            return "✅ No common security anti-patterns detected in the analyzed diff."

        output_lines = [f"🚨 Detected {len(findings)} Security Vulnerability Finding(s):"]
        for i, f in enumerate(findings, 1):
            source_tag = f"[{f.analyzer_source.upper()}] " if f.analyzer_source else ""
            output_lines.append(
                f"{i}. {source_tag}[{f.severity}] {f.rule_id} ({f.cwe}) in `{f.file_path}`:L{f.line_number}\n"
                f"   - Description: {f.description}\n"
                f"   - Code Snippet: `{f.snippet}`\n"
                f"   - Recommended Fix: {f.fix_recommendation}"
            )
        return "\n".join(output_lines)


# Backwards compatibility alias
SastScannerTool = QuickPatternScannerTool
SastScannerInput = QuickPatternScannerInput
UnifiedSecurityScannerTool = QuickPatternScannerTool
