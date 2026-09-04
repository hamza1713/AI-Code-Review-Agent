"""
Model Context Protocol (MCP) Server for AI Code Review Agent.
Exposes code review capabilities to Claude Code, Cursor, Windsurf, VS Code,
and other MCP-compatible clients via JSON-RPC over stdio.
"""

import sys
import json
from typing import Optional, List, Dict, Any

from mcp.server.fastmcp import FastMCP

from code_review_agent.review_service import ReviewService
from code_review_agent.tools.sast_scanner import SastEngine
from code_review_agent.governance.rules_engine import RulesEngine
from code_review_agent.context_engine.code_graph import CodeGraphIndexer
from code_review_agent.tools.test_generator import TestGeneratorTool
from code_review_agent.config import logger


# Initialize FastMCP Server
mcp = FastMCP("ai-code-reviewer")


@mcp.tool()
def review_diff(diff: str, repo_root: Optional[str] = None) -> str:
    """
    Execute an automated pull request code review on a git unified diff.
    Returns executive merge verdict (APPROVE, REQUEST CHANGES, ESCALATE),
    confidence score, security findings, governance violations, and empirical test evidence badges.
    """
    try:
        response = ReviewService.execute_review(raw_diff=diff)
        result_dict = response.model_dump(mode="json")
        return json.dumps(result_dict, indent=2)
    except Exception as e:
        logger.error(f"MCP review_diff error: {e}")
        return json.dumps({"error": str(e), "verdict": "ERROR"}, indent=2)


@mcp.tool()
def scan_sast_patterns(diff: str) -> str:
    """
    Execute fast, deterministic SAST vulnerability scanning on a git diff.
    Detects SQL injection (CWE-89), command injection (CWE-78), hardcoded secrets (CWE-798),
    insecure deserialization (CWE-502), and weak cryptography (CWE-327).
    """
    try:
        findings = SastEngine.scan_diff(diff)
        findings_json = [f.model_dump(mode="json") for f in findings]
        return json.dumps({"count": len(findings), "findings": findings_json}, indent=2)
    except Exception as e:
        logger.error(f"MCP scan_sast_patterns error: {e}")
        return json.dumps({"error": str(e)}, indent=2)


@mcp.tool()
def check_governance_rules(diff: str, repo_root: Optional[str] = None) -> str:
    """
    Validate a git diff against team coding standards and architecture governance rules (.code-review.yaml).
    Checks for forbidden print statements, wildcard imports, cyclomatic complexity, and file size limits.
    """
    try:
        engine = RulesEngine(repo_root=repo_root)
        violations = engine.evaluate_diff(diff)
        violations_json = [v.model_dump(mode="json") for v in violations]
        return json.dumps({"count": len(violations), "violations": violations_json}, indent=2)
    except Exception as e:
        logger.error(f"MCP check_governance_rules error: {e}")
        return json.dumps({"error": str(e)}, indent=2)


@mcp.tool()
def find_impacted_callers(target_identifiers: List[str], repo_root: Optional[str] = None) -> str:
    """
    Query repository-wide AST Code Graph to find all callers and downstream dependencies
    impacted by modifications to specified functions or classes.
    """
    try:
        indexer = CodeGraphIndexer(repo_root=repo_root)
        indexer.index_repository()
        impact = indexer.get_impacted_callers(target_identifiers)
        return json.dumps({"impacted_callers": impact}, indent=2)
    except Exception as e:
        logger.error(f"MCP find_impacted_callers error: {e}")
        return json.dumps({"error": str(e)}, indent=2)


@mcp.tool()
def generate_unit_tests(function_signature: str, module_path: str = "app.service") -> str:
    """
    Generate targeted, production-ready pytest unit tests for a function signature or code block.
    Uses Python AST introspection to construct fixtures, mocks, boundary tests, and assertion suites.
    """
    try:
        tool = TestGeneratorTool()
        suite_code = tool._run(function_signature=function_signature, module_path=module_path)
        return suite_code
    except Exception as e:
        logger.error(f"MCP generate_unit_tests error: {e}")
        return f"# Error generating unit tests: {e}"


def main():
    """Entrypoint for code-review-mcp CLI command."""
    mcp.run()


if __name__ == "__main__":
    main()
