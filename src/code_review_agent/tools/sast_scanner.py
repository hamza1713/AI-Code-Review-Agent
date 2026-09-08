"""
Unified Security Analysis Scanner & CrewAI Tool.
Coordinates Semgrep AST analysis, Bandit Python scanning, and fast heuristic regex patterns
into a unified vulnerability reporting pipeline before LLM reasoning.
"""

import re
from typing import List, Dict, Type, Any, Optional, Tuple
from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from code_review_agent.models import SastFinding, ParsedPR
from code_review_agent.diff_parser import DiffParser
from code_review_agent.tools.semgrep_runner import SemgrepRunner
from code_review_agent.tools.bandit_runner import BanditRunner, is_safe_subprocess_call
from code_review_agent.tools.ast_security_scanner import ASTSecurityScanner
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
    ),
    SecurityPatternRule(
        rule_id="SEC-PATH-001",
        cwe="CWE-22",
        name="Path Traversal / Arbitrary File Read",
        severity="HIGH",
        # open(os.path.join(<...>, <identifier>)) — a variable last segment is likely
        # caller-controlled; all-literal joins (…, "config.json") don't match. Also catches
        # f-string and concatenated user paths.
        pattern=(
            r"open\s*\(\s*os\.path\.join\s*\([^)]*,\s*[A-Za-z_]\w*\s*\)"
            r"|open\s*\(\s*[A-Za-z_]\w*\s*\+\s*[A-Za-z_]\w*"
            r"|open\s*\(\s*f[\"'].*?\{.*?\}.*?[\"']"
        ),
        description="Opening files using an unsanitized, caller-controlled file path can permit directory traversal (e.g. '../../etc/passwd').",
        fix_recommendation="Resolve and validate the path against an allowed base directory: base=os.path.realpath(base_dir); target=os.path.realpath(os.path.join(base, name)); assert target.startswith(base + os.sep)."
    ),
    SecurityPatternRule(
        rule_id="SEC-TLS-001",
        cwe="CWE-295",
        name="Disabled TLS Certificate Verification",
        severity="HIGH",
        pattern=(
            r"check_hostname\s*=\s*False"
            r"|verify_mode\s*=\s*(?:ssl\.)?CERT_NONE"
            r"|ssl\._create_unverified_context\s*\("
            r"|(?:^|[^\w.])verify\s*=\s*False"
        ),
        description="TLS/SSL certificate or hostname verification is disabled, allowing man-in-the-middle attacks on encrypted connections.",
        fix_recommendation="Keep verification enabled: use ssl.create_default_context() with check_hostname=True and CERT_REQUIRED, or pass verify=True (or a CA bundle path) to the HTTP client."
    ),
    SecurityPatternRule(
        rule_id="SEC-PASS-001",
        cwe="CWE-259",
        name="Hardcoded Password String",
        severity="HIGH",
        pattern=r"(?:db_password|password|passwd|pwd)\s*=\s*['\"][^'\"]{4,}['\"]",
        description="Hardcoded password string detected in assignment.",
        fix_recommendation="Load passwords from environment variables or a secrets manager."
    ),
    SecurityPatternRule(
        rule_id="SEC-SQLI-003",
        cwe="CWE-89",
        name="SQL Query String Formatting (% / format)",
        severity="CRITICAL",
        pattern=(
            r"['\"][^'\"]*?(?:SELECT|INSERT|UPDATE|DELETE)[^'\"]*?['\"]\s*%\s*[A-Za-z_(\[]"
            r"|['\"][^'\"]*?(?:SELECT|INSERT|UPDATE|DELETE)[^'\"]*?['\"]\s*\.format\s*\("
        ),
        description="String formatting (% or .format()) used directly in SQL query construction. Allows SQL injection.",
        fix_recommendation="Use parameterized queries: cursor.execute('SELECT * FROM users WHERE name = ?', (username,))"
    ),
    SecurityPatternRule(
        rule_id="SEC-EVAL-001",
        cwe="CWE-95",
        name="Dynamic Code Evaluation (eval / exec)",
        severity="CRITICAL",
        pattern=r"(?:^|[^\w.])(?:eval|exec)\s*\([^)]+\)",
        description="Direct use of eval() or exec() to dynamically execute code/expressions. Enables arbitrary code execution if inputs are untrusted.",
        fix_recommendation="Use ast.literal_eval() for parsing data structures, or avoid dynamic evaluation."
    ),
    SecurityPatternRule(
        rule_id="SEC-RAND-001",
        cwe="CWE-330",
        name="Insecure Pseudo-Random Generator for Security",
        severity="MEDIUM",
        pattern=r"(?:^|[^\w.])random\.(?:choice|random|randint|randrange|choices)\s*\(",
        description="Standard pseudo-random number generator (random module) used for generating tokens, keys, or security values.",
        fix_recommendation="Use secrets module for cryptographic or security tokens: secrets.token_hex() or secrets.choice()."
    ),
    SecurityPatternRule(
        rule_id="SEC-YAML-001",
        cwe="CWE-502",
        name="Unsafe YAML Deserialization (yaml.load)",
        severity="HIGH",
        pattern=r"yaml\.load\s*\([^)]*(?!Loader\s*=\s*(?:yaml\.)?SafeLoader)",
        description="Unsafe yaml.load() allows arbitrary object deserialization and remote code execution.",
        fix_recommendation="Use yaml.safe_load() or specify Loader=yaml.SafeLoader."
    ),
    SecurityPatternRule(
        rule_id="SEC-PERM-001",
        cwe="CWE-732",
        name="Permissive File Permissions (chmod 0777)",
        severity="HIGH",
        pattern=r"os\.chmod\s*\([^,]+,\s*(?:0o?777|0o?666|stat\.S_IRWXU\s*\|\s*stat\.S_IRWXG\s*\|\s*stat\.S_IRWXO)\)",
        description="Overly permissive file mask (0o777 / 0o666) grants read/write/execute permissions to all system users.",
        fix_recommendation="Restrict permissions to owner only (e.g. 0o600 or 0o700): os.chmod(path, 0o600)."
    ),
    SecurityPatternRule(
        rule_id="SEC-EXCEPT-001",
        cwe="CWE-703",
        name="Silent Exception Swallow",
        severity="LOW",
        pattern=r"except(?:\s+Exception)?\s*:\s*(?:\r?\n\+?\s*)?(?:pass|\.\.\.)",
        description="Bare except or except Exception with pass silently swallows unexpected errors and hides runtime bugs.",
        fix_recommendation="Catch specific exceptions and log the error: except SpecificError as e: logger.warning(f'... {e}')"
    ),
    SecurityPatternRule(
        rule_id="SEC-SQLI-004",
        cwe="CWE-89",
        name="SQL Query String Concatenation via Operator (+ / +=)",
        severity="CRITICAL",
        pattern=r"(?:query|sql|stmt)\s*(?:\+=|\+)\s*(?:['\"].*?['\"]\s*\+\s*)?[A-Za-z_]\w*",
        description="Accumulating SQL query strings by concatenating untrusted variables directly with + or +=.",
        fix_recommendation="Use parameterized queries instead of dynamically concatenating SQL strings."
    ),
    SecurityPatternRule(
        rule_id="SEC-CMD-002",
        cwe="CWE-78",
        name="Command String Concatenation for Shell Execution",
        severity="CRITICAL",
        pattern=r"(?:cmd|command|exec_cmd|shell_cmd)\s*(?:\+=|=)\s*['\"][^'\"]*?['\"]\s*\+\s*[A-Za-z_]\w*",
        description="Building operating system command string via concatenation with variables before shell execution.",
        fix_recommendation="Pass arguments as a list to subprocess without shell=True: subprocess.run(['cmd', arg], check=True)"
    )
]

# Backwards compatibility alias
SAST_RULES = SECURITY_PATTERN_RULES


def strip_line_comment(line: str) -> str:
    """
    Strip trailing and full-line comments from code lines while preserving
    comment characters (#, //) inside string literals (e.g. 'https://...' or 'key_#123').
    """
    if not line:
        return ""

    in_single_quote = False
    in_double_quote = False
    escape = False
    i = 0
    n = len(line)

    while i < n:
        c = line[i]

        if escape:
            escape = False
            i += 1
            continue

        if c == "\\":
            escape = True
            i += 1
            continue

        if c == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            i += 1
            continue

        if c == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            i += 1
            continue

        if not in_single_quote and not in_double_quote:
            # Python / Shell comment: #
            if c == "#":
                return line[:i].rstrip()
            # C / Java / JS / Go comment: //
            if c == "/" and i + 1 < n and line[i + 1] == "/":
                return line[:i].rstrip()
            # Start of block comment: /*
            if c == "/" and i + 1 < n and line[i + 1] == "*":
                return line[:i].rstrip()

        i += 1

    return line


def is_comment_or_docstring_line(line: str, in_docstring: bool) -> Tuple[bool, bool]:
    """
    Determine if a line is a comment or inside a multiline docstring.
    Returns (is_comment, new_in_docstring_state).
    """
    s = line.strip()
    if not s:
        return False, in_docstring

    # Check for docstring toggling: """ or '''
    triple_double = s.count('"""')
    triple_single = s.count("'''")

    if triple_double % 2 == 1:
        new_state = not in_docstring
        return True, new_state
    if triple_single % 2 == 1:
        new_state = not in_docstring
        return True, new_state

    if in_docstring:
        return True, True

    # Single-line full docstring e.g. """docstring here"""
    if (s.startswith('"""') and s.endswith('"""') and len(s) >= 6) or \
       (s.startswith("'''") and s.endswith("'''") and len(s) >= 6):
        return True, False

    # Full line comment
    if s.startswith("#") or s.startswith("//") or s.startswith("/*") or s.startswith("*"):
        return True, in_docstring

    return False, in_docstring


class QuickPatternScanner:
    """
    Performs fast heuristic regex pattern scanning on PR diffs with comment & docstring filtering.
    Identifies common security anti-patterns and high-risk code constructs.
    """

    @staticmethod
    @memoize_by_content("quick_pattern")
    def scan_diff(raw_diff: str) -> List[SastFinding]:
        """Scan all added lines in a unified diff for known security patterns with comment filtering."""
        parsed_pr = DiffParser.parse_diff(raw_diff)
        findings: List[SastFinding] = []

        for file_diff in parsed_pr.files:
            file_path = file_diff.target_file
            added_lines = DiffParser.extract_added_lines_with_numbers(file_diff)

            # Check line-by-line with comment stripping
            in_docstring = False
            for line_no, line_content in added_lines:
                is_comm, in_docstring = is_comment_or_docstring_line(line_content, in_docstring)
                if is_comm:
                    continue

                code_part = strip_line_comment(line_content)
                if not code_part.strip():
                    continue

                for rule in SECURITY_PATTERN_RULES:
                    if rule.rule_id == "SEC-CMD-001" and is_safe_subprocess_call(code_part):
                        continue
                    if re.search(rule.pattern, code_part, re.IGNORECASE):
                        findings.append(
                            SastFinding(
                                rule_id=rule.rule_id,
                                cwe=rule.cwe,
                                description=f"{rule.name}: {rule.description}",
                                severity=rule.severity,
                                file_path=file_path,
                                line_number=line_no,
                                snippet=code_part.strip(),
                                fix_recommendation=rule.fix_recommendation,
                                analyzer_source="regex"
                            )
                        )

            # Check multiline patches after cleaning comments/docstrings
            raw_patch = file_diff.raw_patch
            clean_patch_lines = []
            in_doc = False
            for l in raw_patch.splitlines():
                content_only = l[1:] if l.startswith(("+", "-", " ")) else l
                is_c, in_doc = is_comment_or_docstring_line(content_only, in_doc)
                prefix = l[:1] if l.startswith(("+", "-", " ")) else ""
                if is_c:
                    clean_patch_lines.append(prefix)
                else:
                    clean_patch_lines.append(prefix + strip_line_comment(content_only))
            cleaned_patch = "\n".join(clean_patch_lines)

            for rule in SECURITY_PATTERN_RULES:
                if rule.rule_id == "SEC-CMD-001":
                    continue
                for match in re.finditer(rule.pattern, cleaned_patch, re.IGNORECASE):
                    matched_snippet = match.group(0).strip()
                    if not matched_snippet:
                        continue
                    if not any(f.file_path == file_path and f.rule_id == rule.rule_id for f in findings):
                        first_line = matched_snippet.splitlines()[0].lstrip("+- ").strip()
                        line_no = next((ln for ln, content in added_lines if first_line and first_line in content), 1)
                        findings.append(
                            SastFinding(
                                rule_id=rule.rule_id,
                                cwe=rule.cwe,
                                description=f"{rule.name}: {rule.description}",
                                severity=rule.severity,
                                file_path=file_path,
                                line_number=line_no,
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

        # 2. AST Constant Folding & Variable Indirection Scanner (Python AST)
        ast_findings = ASTSecurityScanner.scan_diff(raw_diff)
        for f in ast_findings:
            key = (f.file_path, f.line_number, f.rule_id)
            if key not in seen_keys:
                seen_keys.add(key)
                all_findings.append(f)

        # 3. Bandit AST scan (if available)
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
