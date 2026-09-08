"""
Data models and state schemas for the Code Review Flow, Quick Security Pattern Scanner,
AST Code Graph, Governance Rules Engine, and Telemetry.
"""

from typing import List, Dict, Any, Optional, Literal
from pydantic import BaseModel, Field


# --- Diff & AST Structures ---

class DiffHunk(BaseModel):
    """Represents a contiguous block of diff lines in a file."""
    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    header: str
    lines: List[str] = Field(default_factory=list)


class FileDiff(BaseModel):
    """Represents changes in a single file."""
    source_file: str
    target_file: str
    is_new_file: bool = False
    is_deleted_file: bool = False
    is_renamed_file: bool = False
    hunks: List[DiffHunk] = Field(default_factory=list)
    added_lines_count: int = 0
    deleted_lines_count: int = 0
    raw_patch: str = ""


class ParsedPR(BaseModel):
    """Structured representation of an entire PR diff."""
    files: List[FileDiff] = Field(default_factory=list)
    total_added: int = 0
    total_deleted: int = 0
    files_changed: int = 0


# --- Code Graph & Context Engine Structures ---

class SymbolInfo(BaseModel):
    """Represents an AST symbol (function, method, class) in the codebase."""
    name: str = Field(..., description="Name of the symbol")
    qualified_name: Optional[str] = Field(default=None, description="Fully-qualified identifier (e.g. path/to/file.py:ClassName.function_name)")
    kind: str = Field(..., description="'function', 'class', or 'method'")
    file_path: str = Field(..., description="File path where symbol is declared")
    line_start: int = Field(..., description="Starting line number")
    line_end: int = Field(..., description="Ending line number")
    docstring: Optional[str] = Field(default=None, description="Docstring if available")
    parameters: List[str] = Field(default_factory=list, description="Function parameters")
    callers: List[str] = Field(default_factory=list, description="Functions that call this symbol")
    callees: List[str] = Field(default_factory=list, description="Functions called by this symbol")


class CodeGraphSummary(BaseModel):
    """Summary of the indexed repository graph."""
    files_indexed: int = 0
    symbols_count: int = 0
    impacted_callers: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Mapping of modified functions to their cross-file callers"
    )


# --- Governance & Custom Rules Structures ---

class RuleViolation(BaseModel):
    """Represents a violation of a project custom rule (.code-review.yaml)."""
    rule_id: str = Field(..., description="Rule identifier (e.g., custom-no-raw-queries)")
    rule_name: str = Field(..., description="Human-readable rule name")
    severity: str = Field(default="WARNING", description="'BLOCKING', 'WARNING', or 'INFO'")
    file_path: str = Field(..., description="File where violation was found")
    line_number: int = Field(..., description="Line number of violation")
    description: str = Field(..., description="Explanation of why the rule failed")
    suggested_fix: str = Field(..., description="Actionable fix recommendation")


# --- SAST & Security Findings ---

class SastFinding(BaseModel):
    """Deterministic security vulnerability finding."""
    rule_id: str = Field(..., description="Unique rule identifier (e.g., SAST-SQLI-001)")
    cwe: str = Field(..., description="Common Weakness Enumeration ID (e.g., CWE-89)")
    description: str = Field(..., description="Vulnerability description")
    severity: str = Field(..., description="Severity level: 'LOW', 'MEDIUM', 'HIGH', or 'CRITICAL'")
    file_path: str = Field(..., description="Relative path of affected file")
    line_number: int = Field(..., description="Target line number in the new file")
    snippet: str = Field(default="", description="Code snippet containing the vulnerability")
    fix_recommendation: str = Field(..., description="Remediation steps or suggested replacement code")
    analyzer_source: str = Field(default="regex", description="Source tool: 'semgrep', 'bandit', 'regex', etc.")


# --- Inline GitHub Comments ---

class InlineComment(BaseModel):
    """Line-level review comment compatible with GitHub REST API."""
    path: str = Field(..., description="Target file path (e.g., app/user_auth.py)")
    line: int = Field(..., description="Line number of the new file to attach comment to")
    side: str = Field(default="RIGHT", description="Diff side: 'RIGHT' (new) or 'LEFT' (old)")
    severity: str = Field(default="WARNING", description="'INFO', 'WARNING', or 'CRITICAL'")
    comment_body: str = Field(..., description="Explanation of the issue or feedback")
    why: Optional[str] = Field(default=None, description="One-sentence plain-English explanation of the causal mechanism")
    suggestion_code: Optional[str] = Field(
        default=None,
        description="Suggested replacement code (will be rendered in GitHub suggestion blocks)"
    )


    def to_github_markdown(self) -> str:
        """Format comment into GitHub-flavored Markdown with 1-click suggestion block."""
        badge = "🚨 **CRITICAL**" if self.severity == "CRITICAL" else ("⚠️ **WARNING**" if self.severity == "WARNING" else "💡 **SUGGESTION**")
        md = f"{badge}: {self.comment_body}\n"
        if self.suggestion_code:
            md += f"\n```suggestion\n{self.suggestion_code.strip()}\n```\n"
        return md


# --- Crew Output Schemas ---

class CodeQualityJSON(BaseModel):
    """Output schema for Senior Developer code quality analysis."""
    critical_issues: List[str] = Field(
        default_factory=list,
        description="List of severe bugs, architectural flaws, or maintainability violations"
    )
    minor_issues: List[str] = Field(
        default_factory=list,
        description="List of minor suggestions, style fixes, or cosmetic improvements"
    )
    reasoning: str = Field(..., description="Detailed explanation of code quality assessments")
    cross_file_risks: List[str] = Field(
        default_factory=list,
        description="Potential ripple effects and risks identified across caller files"
    )
    inline_suggestions: List[InlineComment] = Field(
        default_factory=list,
        description="Line-level code quality improvement suggestions"
    )


class SecurityVulnerability(BaseModel):
    """Detailed schema for an individual security vulnerability."""
    description: str = Field(..., description="Vulnerability description")
    risk_level: str = Field(..., description="Risk severity rating: low, medium, high, critical")
    evidence: str = Field(..., description="Specific line or code snippet demonstrating the vulnerability")
    matched_by: List[str] = Field(default_factory=list, description="Rule IDs or analyzers that contributed to this finding")
    line_number: Optional[int] = Field(default=None, description="Line number in the modified file")
    file_path: Optional[str] = Field(default=None, description="Path to the file")


class ReviewSecurityJSON(BaseModel):
    """Output schema for Security Engineer vulnerability assessment."""
    security_vulnerabilities: List[SecurityVulnerability] = Field(
        default_factory=list,
        description="List of identified security vulnerabilities"
    )
    blocking: bool = Field(
        ...,
        description="Whether security vulnerabilities should block PR approval"
    )
    highest_risk: str = Field(
        ...,
        description="Highest detected risk level among all vulnerabilities ('low', 'medium', 'high', 'critical', 'none')"
    )
    security_recommendations: List[str] = Field(
        default_factory=list,
        description="Actionable remediation steps to patch security flaws"
    )
    inline_security_comments: List[InlineComment] = Field(
        default_factory=list,
        description="Line-level security comments with suggested code fixes"
    )


class Fix(BaseModel):
    """Actionable fix item generated by the Tech Lead."""
    description: str = Field(..., description="Summary of the defect or vulnerability to fix")
    solutions: str = Field(..., description="Concrete code or architectural solution proposed")
    explanation: str = Field(..., description="Rationale for the chosen solution")
    file_path: Optional[str] = Field(default=None, description="File path to fix")
    line_number: Optional[int] = Field(default=None, description="Line number to fix")


class SummarizedFindingsJSON(BaseModel):
    """Consolidated findings schema generated by the Tech Lead."""
    verdict: Optional[Literal["APPROVE", "REQUEST CHANGES", "ESCALATE"]] = Field(
        default=None,
        description="Explicit merge verdict: APPROVE, REQUEST CHANGES, or ESCALATE"
    )
    blocking_reasons: List[str] = Field(
        default_factory=list,
        description="Explicit blocking reasons or security/governance violations preventing approval"
    )
    confidence: int = Field(
        ...,
        description="Confidence score between 0 and 100 for merging the PR"
    )
    confidence_breakdown: Optional[str] = Field(
        default=None,
        description="Arithmetic breakdown explaining how the confidence score was derived"
    )
    findings: str = Field(
        ...,
        description="Executive summary synthesizing code quality, security, and rule checks"
    )
    coverage_gaps: List[str] = Field(
        default_factory=list,
        description="Potential gaps or areas not covered by upstream analysis"
    )
    fix: List[Fix] = Field(
        default_factory=list,
        description="Ordered list of required and recommended fixes"
    )
    recommendations: List[str] = Field(
        default_factory=list,
        description="Additional architectural or organizational recommendations"
    )
    inline_comments: List[InlineComment] = Field(
        default_factory=list,
        description="Consolidated list of inline comments to post to GitHub PR"
    )
    suggested_unit_tests: Optional[str] = Field(
        default=None,
        description="Automated pytest test suite covering modified functions and regression cases"
    )


# --- Empirical Test Sandbox & Evidence Badges ---

class TestExecutionResult(BaseModel):
    """Result of running generated unit tests in the isolated sandbox."""
    executed: bool = Field(default=False, description="Whether tests were executed")
    status: Literal["PASSED", "REPRODUCED_DEFECT", "ERROR", "TIMEOUT", "SKIPPED"] = Field(
        default="SKIPPED",
        description="Execution status of the generated test suite"
    )
    evidence_badge: Literal["REPRODUCED", "PASSING", "UNVERIFIED", "HEURISTIC"] = Field(
        default="HEURISTIC",
        description="Empirical evidence badge verifying findings groundedness"
    )
    tests_run: int = Field(default=0, description="Total number of tests executed")
    failures: int = Field(default=0, description="Number of failed tests")
    errors: int = Field(default=0, description="Number of test execution or collection errors")
    duration_seconds: float = Field(default=0.0, description="Execution runtime in seconds")
    stdout: str = Field(default="", description="Captured standard output from pytest")
    stderr: str = Field(default="", description="Captured standard error from pytest")
    summary_message: str = Field(default="", description="Human-readable summary of empirical test results")


# --- Telemetry & Metrics ---

class TelemetryMetrics(BaseModel):
    """Telemetry, latency, and token cost tracking metrics."""
    duration_seconds: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    model_used: str = "gemini/gemini-3.1-flash-lite-preview"
    sast_findings_count: int = 0
    rule_violations_count: int = 0
    inline_comments_count: int = 0
    final_verdict: str = ""



# --- Flow State ---

class ReviewState(BaseModel):
    """State tracked across the entire CrewAI Flow lifecycle."""
    pr_file_path: str = Field(
        default="samples/sql_injection_pr.txt",
        description="Path to the local PR diff file (if reviewing locally)"
    )
    pr_url: Optional[str] = Field(
        default=None,
        description="Full URL or 'owner/repo/pull/123' if reviewing a live GitHub PR"
    )
    pr_metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="GitHub PR metadata"
    )
    pr_content: str = Field(
        default="",
        description="Raw text content of the PR diff"
    )
    parsed_pr: Optional[ParsedPR] = Field(
        default=None,
        description="Parsed diff structure with files, hunks, and line number mappings"
    )
    code_graph_context: Dict[str, Any] = Field(
        default_factory=dict,
        description="Cross-file caller and dependency graph context"
    )
    sast_findings: List[SastFinding] = Field(
        default_factory=list,
        description="Quick Security Pattern Scanner results"
    )
    rule_violations: List[RuleViolation] = Field(
        default_factory=list,
        description="Violations of project custom rules (.code-review.yaml)"
    )
    errors: List[str] = Field(
        default_factory=list,
        description="List of error messages encountered during flow execution"
    )
    review_result: Dict[str, Any] = Field(
        default_factory=dict,
        description="Structured dictionary holding review results (simple or crew)"
    )
    summarized_findings: Optional[SummarizedFindingsJSON] = Field(
        default=None,
        description="Typed Tech Lead synthesized review output"
    )
    quality_review: Optional[CodeQualityJSON] = Field(
        default=None,
        description="Typed Senior Developer code quality review output"
    )
    security_review: Optional[ReviewSecurityJSON] = Field(
        default=None,
        description="Typed Security Engineer vulnerability review output"
    )
    sast_context: str = Field(
        default="",
        description="Pre-computed SAST vulnerability context string"
    )
    rules_context: str = Field(
        default="",
        description="Pre-computed Governance rules context string"
    )
    team_memory_context: str = Field(
        default="",
        description="Pre-computed learned team conventions and best practices context"
    )
    ticket_context: str = Field(
        default="",
        description="Pre-computed ticket details and acceptance criteria context"
    )
    ticket_compliance: Optional[Any] = Field(
        default=None,
        description="Requirement fulfillment and intent verification against linked ticket"
    )
    inline_comments: List[InlineComment] = Field(
        default_factory=list,
        description="Line-level inline review comments generated for GitHub"
    )
    generated_unit_tests: str = Field(
        default="",
        description="Generated pytest test suite for the modified code"
    )
    test_execution: Optional[TestExecutionResult] = Field(
        default=None,
        description="Empirical sandbox test execution results and evidence badge"
    )
    sarif_output_path: Optional[str] = Field(
        default=None,
        description="File path where SARIF report will be saved"
    )
    telemetry: Optional[TelemetryMetrics] = Field(
        default=None,
        description="Telemetry and cost tracking metrics for the review run"
    )
    crew_needed: bool = Field(
        default=False,
        description="Flag indicating whether a full multi-agent crew was invoked"
    )
    tokens_used: Dict[str, Any] = Field(
        default_factory=dict,
        description="Token consumption statistics from LLM executions"
    )
    final_answer: str = Field(
        default="",
        description="Final synthesized review decision and report"
    )
    github_review_submitted: bool = Field(
        default=False,
        description="Whether the review was posted to GitHub via REST API"
    )
    repo_root: Optional[str] = Field(
        default=None,
        description="Target root directory for AST Code Graph indexing (defaults to current dir if None)"
    )


# --- Synchronous Review API Schemas ---

class CrossFileImpactSummary(BaseModel):
    """Cross-file impact analysis status and impacted caller map."""
    available: bool = Field(
        default=True,
        description="Whether cross-file impact analysis is available for this code"
    )
    is_python: bool = Field(
        default=True,
        description="Whether the analyzed diff or files are Python"
    )
    message: str = Field(
        default="",
        description="Informational message regarding AST cross-file dependency status"
    )
    impacted_callers: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Map of modified functions to their callers"
    )


class ReviewAPIResponse(BaseModel):
    """Structured response returned by synchronous POST /api/review endpoint."""
    verdict: str = Field(
        ...,
        description="Executive merge verdict: 'APPROVE', 'REQUEST CHANGES', or 'ESCALATE'"
    )
    confidence_score: int = Field(
        ...,
        description="Confidence score between 0 and 100"
    )
    summary: str = Field(
        ...,
        description="Executive summary synthesizing code quality, security, and rule checks"
    )
    full_report: Optional[str] = Field(
        default=None,
        description="Complete synthesized markdown PR review report"
    )
    pattern_findings_label: str = Field(
        default="Quick Pattern Scanner (heuristic)",
        description="Explicit honest capability label for pattern findings"
    )
    pattern_findings: List[SastFinding] = Field(
        default_factory=list,
        description="Quick Security Pattern Scanner findings"
    )
    governance_violations: List[RuleViolation] = Field(
        default_factory=list,
        description="Violations of team governance rules (.code-review.yaml)"
    )
    cross_file_impact: CrossFileImpactSummary = Field(
        default_factory=CrossFileImpactSummary,
        description="Cross-file AST impact analysis results"
    )
    ticket_compliance: Optional[Any] = Field(
        default=None,
        description="Ticket compliance verification result"
    )
    generated_unit_tests: Optional[str] = Field(
        default=None,
        description="Generated pytest unit test suite if available"
    )
    test_execution: Optional[TestExecutionResult] = Field(
        default=None,
        description="Empirical sandbox test execution outcome and evidence badge"
    )
    inline_comments: List[InlineComment] = Field(
        default_factory=list,
        description="Line-level inline comments with suggested fixes"
    )
    telemetry: Optional[TelemetryMetrics] = Field(
        default=None,
        description="Execution latency and token cost telemetry"
    )
    trace: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Hierarchical multi-agent execution trace and decision tree"
    )
    reviewed_diff: Optional[str] = Field(
        default=None,
        description="Raw diff or source file content that was reviewed"
    )
    scope_note: str = Field(
        default=(
            "Scope Note: Review performed via heuristic regex pattern scanning, AST Code Graph indexer "
            "(Python only), and governance rules engine. Not a full dataflow SAST or formal verification engine."
        ),
        description="Permanent non-dismissible scope capability statement"
    )


