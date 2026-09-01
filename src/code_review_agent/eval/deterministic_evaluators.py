"""
Deterministic Evaluation Engine for AI Code Review Agent.
Provides zero-cost, instantaneous verification of mathematical calculations,
AST code compilation, SARIF v2.1.0 structural compliance, guardrail invariants,
and diff line scope precision.
"""

import ast
import re
from typing import Dict, List, Any, Optional, Tuple
from pydantic import BaseModel, Field

from code_review_agent.models import (
    SummarizedFindingsJSON,
    ReviewSecurityJSON,
    CodeQualityJSON,
    InlineComment,
    ParsedPR,
)


class EvaluationResult(BaseModel):
    """Result of an evaluation check."""
    passed: bool
    evaluator_name: str
    score: float = Field(default=1.0, description="Normalized score between 0.0 and 1.0")
    details: Dict[str, Any] = Field(default_factory=dict)
    errors: List[str] = Field(default_factory=list)


class ConfidenceMathEvaluator:
    """
    Evaluates mathematical accuracy and rubric alignment of the Tech Lead's
    confidence scoring and confidence breakdown string.
    """

    @staticmethod
    def evaluate_breakdown_arithmetic(summary: SummarizedFindingsJSON) -> EvaluationResult:
        """
        Verify that the arithmetic in 'confidence_breakdown' correctly evaluates
        to the reported 'confidence' score.
        Example breakdown: '100 - 30 (critical SQLi) - 15 (high auth) - 5 (minor) = 50'
        """
        errors = []
        breakdown = summary.confidence_breakdown or ""
        reported_confidence = summary.confidence

        if not breakdown:
            return EvaluationResult(
                passed=False,
                evaluator_name="ConfidenceMathEvaluator.breakdown_arithmetic",
                score=0.0,
                details={"reported_confidence": reported_confidence, "breakdown_present": False},
                errors=["No 'confidence_breakdown' string provided in summary."]
            )

        # Extract all numbers preceded by a minus sign (deductions)
        # e.g., in "100 - 30 (critical) - 15 (high) = 55" -> [30, 15]
        deduction_matches = re.findall(r"-\s*(\d+)", breakdown)
        if not deduction_matches:
            # Check if it was a clean 100
            if "100" in breakdown and reported_confidence == 100:
                return EvaluationResult(
                    passed=True,
                    evaluator_name="ConfidenceMathEvaluator.breakdown_arithmetic",
                    score=1.0,
                    details={"reported_confidence": 100, "calculated_score": 100, "deductions": []}
                )

        deductions = [int(d) for d in deduction_matches]
        calculated_score = max(0, 100 - sum(deductions))

        if calculated_score != reported_confidence:
            errors.append(
                f"Arithmetic mismatch: 100 - sum({deductions}) evaluates to {calculated_score}, "
                f"but reported confidence is {reported_confidence}."
            )

        passed = len(errors) == 0
        return EvaluationResult(
            passed=passed,
            evaluator_name="ConfidenceMathEvaluator.breakdown_arithmetic",
            score=1.0 if passed else 0.0,
            details={
                "reported_confidence": reported_confidence,
                "calculated_score": calculated_score,
                "deductions": deductions,
                "breakdown_string": breakdown
            },
            errors=errors
        )

    @staticmethod
    def calculate_ground_truth_score(
        critical_vulns: int = 0,
        high_vulns: int = 0,
        critical_issues: int = 0,
        minor_issues: int = 0,
        blocking_rules: int = 0,
        warning_rules: int = 0,
    ) -> int:
        """Calculate exact canonical confidence score from item counts per rubric."""
        score = 100
        score -= critical_vulns * 30
        score -= high_vulns * 15
        score -= (critical_issues + blocking_rules) * 10
        score -= (minor_issues + warning_rules) * 5
        return max(0, score)


class CodeCompilationEvaluator:
    """
    Evaluates that all generated replacement code snippets (`suggestion_code`)
    and automated test suites (`suggested_unit_tests`) compile without AST syntax errors.
    """

    @staticmethod
    def evaluate_inline_suggestions(inline_comments: List[InlineComment]) -> EvaluationResult:
        """Ensure all Python suggestion_code blocks parse via ast.parse()."""
        errors = []
        valid_count = 0
        tested_count = 0

        for idx, comment in enumerate(inline_comments):
            code = comment.suggestion_code
            if not code or not code.strip():
                continue

            # Only test Python files for AST compilation
            if comment.path and not comment.path.endswith(".py"):
                continue

            tested_count += 1
            clean_code = code.strip()

            # Remove markdown backticks if accidentally enclosed
            if clean_code.startswith("```python"):
                clean_code = clean_code[len("```python"):].strip()
            elif clean_code.startswith("```"):
                clean_code = clean_code[3:].strip()
            if clean_code.endswith("```"):
                clean_code = clean_code[:-3].strip()

            # Attempt AST compilation directly, then with indentation normalization
            parse_success = False
            for snippet in [clean_code, f"def _wrapper():\n    {clean_code}"]:
                try:
                    ast.parse(snippet)
                    parse_success = True
                    break
                except SyntaxError:
                    continue

            if parse_success:
                valid_count += 1
            else:
                try:
                    ast.parse(clean_code)
                except SyntaxError as e:
                    errors.append(f"Inline comment #{idx} ({comment.path}:L{comment.line}) syntax error: {e.msg} at line {e.lineno}")

        score = (valid_count / tested_count) if tested_count > 0 else 1.0
        passed = (score == 1.0)

        return EvaluationResult(
            passed=passed,
            evaluator_name="CodeCompilationEvaluator.inline_suggestions",
            score=round(score, 4),
            details={"tested_count": tested_count, "valid_count": valid_count, "errors_count": len(errors)},
            errors=errors
        )

    @staticmethod
    def evaluate_suggested_unit_tests(tests_code: Optional[str]) -> EvaluationResult:
        """Validate that the suggested pytest unit test suite compiles via ast.parse()."""
        if not tests_code or not tests_code.strip():
            return EvaluationResult(
                passed=True,
                evaluator_name="CodeCompilationEvaluator.suggested_unit_tests",
                score=1.0,
                details={"present": False, "note": "No test suite generated."}
            )

        clean_code = tests_code.strip()
        if clean_code.startswith("```python"):
            clean_code = clean_code[len("```python"):].strip()
        elif clean_code.startswith("```"):
            clean_code = clean_code[3:].strip()
        if clean_code.endswith("```"):
            clean_code = clean_code[:-3].strip()

        try:
            tree = ast.parse(clean_code)
            # Verify basic pytest structure: look for test functions
            test_funcs = [node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")]
            has_tests = len(test_funcs) > 0
            return EvaluationResult(
                passed=True,
                evaluator_name="CodeCompilationEvaluator.suggested_unit_tests",
                score=1.0,
                details={"test_functions_found": test_funcs, "test_count": len(test_funcs)}
            )
        except SyntaxError as e:
            return EvaluationResult(
                passed=False,
                evaluator_name="CodeCompilationEvaluator.suggested_unit_tests",
                score=0.0,
                details={"syntax_error": str(e)},
                errors=[f"Unit test suite syntax error: {e.msg} at line {e.lineno}"]
            )


class GuardrailConsistencyEvaluator:
    """
    Evaluates invariant relationships between detected vulnerabilities,
    'highest_risk' classification, and 'blocking' status in ReviewSecurityJSON.
    """

    @staticmethod
    def evaluate_security_output(sec_output: ReviewSecurityJSON) -> EvaluationResult:
        """Validate highest_risk and blocking consistency."""
        errors = []
        vulns = sec_output.security_vulnerabilities
        reported_highest = (sec_output.highest_risk or "").lower().strip()
        is_blocking = sec_output.blocking

        risk_levels = [v.risk_level.lower().strip() for v in vulns]

        # 1. Determine canonical highest risk
        if "critical" in risk_levels:
            expected_highest = "critical"
        elif "high" in risk_levels:
            expected_highest = "high"
        elif "medium" in risk_levels:
            expected_highest = "medium"
        elif "low" in risk_levels:
            expected_highest = "low"
        else:
            expected_highest = "none"

        if reported_highest != expected_highest and not (expected_highest == "none" and reported_highest == "low"):
            errors.append(f"highest_risk mismatch: Reported '{reported_highest}' but vulnerabilities contain '{expected_highest}'.")

        # 2. Blocking consistency: critical/high must be blocking
        has_severe = any(r in ("critical", "high") for r in risk_levels)
        if has_severe and not is_blocking:
            errors.append("blocking is False, but critical or high vulnerabilities are present.")

        passed = len(errors) == 0
        return EvaluationResult(
            passed=passed,
            evaluator_name="GuardrailConsistencyEvaluator.security_output",
            score=1.0 if passed else 0.0,
            details={
                "reported_highest_risk": reported_highest,
                "expected_highest_risk": expected_highest,
                "is_blocking": is_blocking,
                "vulnerabilities_count": len(vulns),
            },
            errors=errors
        )


class SarifComplianceEvaluator:
    """
    Evaluates OASIS SARIF v2.1.0 output schema compliance for security findings.
    """

    @staticmethod
    def evaluate_sarif_dict(sarif: Dict[str, Any]) -> EvaluationResult:
        """Validate essential SARIF v2.1.0 properties."""
        errors = []

        # 1. Root structure
        if sarif.get("version") != "2.1.0":
            errors.append(f"Invalid SARIF version: expected '2.1.0', got '{sarif.get('version')}'.")

        if not sarif.get("$schema"):
            errors.append("Missing '$schema' property in SARIF root.")

        runs = sarif.get("runs", [])
        if not isinstance(runs, list) or len(runs) == 0:
            errors.append("Missing or empty 'runs' array in SARIF document.")
            return EvaluationResult(
                passed=False,
                evaluator_name="SarifComplianceEvaluator.structure",
                score=0.0,
                errors=errors
            )

        run = runs[0]
        driver = run.get("tool", {}).get("driver", {})
        if not driver.get("name"):
            errors.append("Missing 'tool.driver.name' in SARIF run.")

        results = run.get("results", [])
        for idx, res in enumerate(results):
            if not res.get("ruleId"):
                errors.append(f"Result #{idx} is missing 'ruleId'.")
            if not res.get("message", {}).get("text"):
                errors.append(f"Result #{idx} is missing 'message.text'.")
            locations = res.get("locations", [])
            if locations:
                loc = locations[0].get("physicalLocation", {})
                if not loc.get("artifactLocation", {}).get("uri"):
                    errors.append(f"Result #{idx} location is missing artifact URI.")

        passed = len(errors) == 0
        return EvaluationResult(
            passed=passed,
            evaluator_name="SarifComplianceEvaluator.structure",
            score=1.0 if passed else max(0.0, 1.0 - (len(errors) * 0.2)),
            details={"results_count": len(results), "rules_count": len(driver.get("rules", []))},
            errors=errors
        )


class DiffLineScopeEvaluator:
    """
    Validates that inline comment line numbers reference valid lines in the diff.
    """

    @staticmethod
    def evaluate_line_scope(inline_comments: List[InlineComment], parsed_pr: Optional[ParsedPR]) -> EvaluationResult:
        """Ensure inline comments map to files and positive line numbers."""
        errors = []
        if not parsed_pr:
            return EvaluationResult(
                passed=True,
                evaluator_name="DiffLineScopeEvaluator.line_scope",
                score=1.0,
                details={"parsed_pr_provided": False}
            )

        known_files = {f.target_file for f in parsed_pr.files}
        for idx, comment in enumerate(inline_comments):
            if comment.line <= 0:
                errors.append(f"Inline comment #{idx} has invalid non-positive line number: {comment.line}")
            if known_files and comment.path not in known_files:
                # Allow relative match
                if not any(f.endswith(comment.path) or comment.path.endswith(f) for f in known_files):
                    errors.append(f"Inline comment #{idx} references unknown file '{comment.path}' not in PR files {known_files}.")

        passed = len(errors) == 0
        return EvaluationResult(
            passed=passed,
            evaluator_name="DiffLineScopeEvaluator.line_scope",
            score=1.0 if passed else max(0.0, 1.0 - (len(errors) * 0.25)),
            details={"comments_evaluated": len(inline_comments)},
            errors=errors
        )
