"""
Tests for Deterministic Evaluation Suite (Confidence Math, AST Compilation,
Guardrails, SARIF schema, and Line Scope).
"""

import pytest
from code_review_agent.eval.deterministic_evaluators import (
    ConfidenceMathEvaluator,
    CodeCompilationEvaluator,
    GuardrailConsistencyEvaluator,
    SarifComplianceEvaluator,
    DiffLineScopeEvaluator,
)
from code_review_agent.eval.eval_runner import EvalRunner
from code_review_agent.models import (
    SummarizedFindingsJSON,
    ReviewSecurityJSON,
    SecurityVulnerability,
    InlineComment,
    ReviewState,
    ParsedPR,
    FileDiff,
    DiffHunk,
)


@pytest.mark.deterministic_eval
class TestConfidenceMathEvaluator:
    """Validate arithmetic evaluation of confidence score breakdowns."""

    def test_breakdown_arithmetic_valid(self, sample_vulnerable_summary):
        res = ConfidenceMathEvaluator.evaluate_breakdown_arithmetic(sample_vulnerable_summary)
        assert res.passed is True
        assert res.score == 1.0
        assert res.details["calculated_score"] == 35

    def test_breakdown_arithmetic_clean_100(self, sample_clean_summary):
        res = ConfidenceMathEvaluator.evaluate_breakdown_arithmetic(sample_clean_summary)
        assert res.passed is True
        assert res.score == 1.0

    def test_breakdown_arithmetic_mismatch(self):
        mismatched_summary = SummarizedFindingsJSON(
            confidence=90,  # Claims 90, but math says 100 - 30 = 70
            confidence_breakdown="100 - 30 (critical SQLi) = 70",
            findings="Vulnerable.",
            fix=[],
            recommendations=[],
            inline_comments=[]
        )
        res = ConfidenceMathEvaluator.evaluate_breakdown_arithmetic(mismatched_summary)
        assert res.passed is False
        assert res.score == 0.0
        assert "Arithmetic mismatch" in res.errors[0]

    def test_ground_truth_score_formula(self):
        # 1 critical (30), 1 high (15), 1 critical issue (10), 2 minor (10) -> 35
        score = ConfidenceMathEvaluator.calculate_ground_truth_score(
            critical_vulns=1,
            high_vulns=1,
            critical_issues=1,
            minor_issues=2
        )
        assert score == 35


@pytest.mark.deterministic_eval
class TestCodeCompilationEvaluator:
    """Validate AST syntax compilation checks for generated code."""

    def test_valid_inline_suggestions(self, sample_vulnerable_summary):
        res = CodeCompilationEvaluator.evaluate_inline_suggestions(sample_vulnerable_summary.inline_comments)
        assert res.passed is True
        assert res.score == 1.0
        assert res.details["valid_count"] == 1

    def test_invalid_syntax_inline_suggestion(self):
        broken_comments = [
            InlineComment(
                path="app/test.py",
                line=5,
                comment_body="Broken code suggestion",
                suggestion_code="def broken_syntax(x: int\n    return x + 1"  # Missing colon
            )
        ]
        res = CodeCompilationEvaluator.evaluate_inline_suggestions(broken_comments)
        assert res.passed is False
        assert res.score == 0.0
        assert len(res.errors) == 1

    def test_valid_suggested_unit_tests(self, sample_clean_summary):
        res = CodeCompilationEvaluator.evaluate_suggested_unit_tests(sample_clean_summary.suggested_unit_tests)
        assert res.passed is True
        assert res.score == 1.0
        assert "test_calculate_average_valid" in res.details["test_functions_found"]

    def test_invalid_syntax_unit_tests(self):
        res = CodeCompilationEvaluator.evaluate_suggested_unit_tests("import pytest\ndef test_something(\n    assert True")
        assert res.passed is False
        assert res.score == 0.0
        assert "syntax error" in res.errors[0].lower()


@pytest.mark.deterministic_eval
class TestGuardrailConsistencyEvaluator:
    """Validate risk-level invariants and blocking flags."""

    def test_guardrail_consistency_valid(self, sample_security_review):
        res = GuardrailConsistencyEvaluator.evaluate_security_output(sample_security_review)
        assert res.passed is True
        assert res.score == 1.0

    def test_guardrail_highest_risk_mismatch(self):
        bad_sec = ReviewSecurityJSON(
            security_vulnerabilities=[
                SecurityVulnerability(
                    description="SQL Injection",
                    risk_level="critical",
                    evidence="SELECT...",
                    matched_by=["B608"]
                )
            ],
            blocking=True,
            highest_risk="low",  # Inconsistent with 'critical' vuln!
            security_recommendations=[],
            inline_security_comments=[]
        )
        res = GuardrailConsistencyEvaluator.evaluate_security_output(bad_sec)
        assert res.passed is False
        assert "highest_risk mismatch" in res.errors[0]

    def test_guardrail_blocking_flag_inconsistency(self):
        bad_sec = ReviewSecurityJSON(
            security_vulnerabilities=[
                SecurityVulnerability(
                    description="SQL Injection",
                    risk_level="critical",
                    evidence="SELECT...",
                    matched_by=["B608"]
                )
            ],
            blocking=False,  # Should be True for critical!
            highest_risk="critical",
            security_recommendations=[],
            inline_security_comments=[]
        )
        res = GuardrailConsistencyEvaluator.evaluate_security_output(bad_sec)
        assert res.passed is False
        assert "blocking is False" in res.errors[0]


@pytest.mark.deterministic_eval
class TestSarifComplianceEvaluator:
    """Validate OASIS SARIF v2.1.0 JSON format."""

    def test_valid_sarif_structure(self):
        valid_sarif = {
            "$schema": "https://schemastore.azurewebsites.net/schemas/json/sarif-2.1.0.json",
            "version": "2.1.0",
            "runs": [
                {
                    "tool": {
                        "driver": {
                            "name": "AICodeReviewAgent",
                            "rules": [{"id": "CWE-89", "name": "SQLInjection"}]
                        }
                    },
                    "results": [
                        {
                            "ruleId": "CWE-89",
                            "level": "error",
                            "message": {"text": "SQL Injection found"},
                            "locations": [
                                {
                                    "physicalLocation": {
                                        "artifactLocation": {"uri": "app/user.py"},
                                        "region": {"startLine": 15}
                                    }
                                }
                            ]
                        }
                    ]
                }
            ]
        }
        res = SarifComplianceEvaluator.evaluate_sarif_dict(valid_sarif)
        assert res.passed is True
        assert res.score == 1.0

    def test_invalid_sarif_version(self):
        invalid_sarif = {
            "version": "1.0.0",
            "runs": []
        }
        res = SarifComplianceEvaluator.evaluate_sarif_dict(invalid_sarif)
        assert res.passed is False
        assert "Invalid SARIF version" in res.errors[0]


@pytest.mark.deterministic_eval
class TestEvalRunnerOrchestrator:
    """Validate overall EvalRunner report aggregation."""

    def test_eval_runner_full_pass(self, sample_vulnerable_summary, sample_security_review):
        state = ReviewState(
            summarized_findings=sample_vulnerable_summary,
            security_review=sample_security_review
        )
        report = EvalRunner.evaluate_review(state, include_llm_judge=False)

        assert report.overall_passed is True
        assert report.deterministic_score == 1.0
        assert report.failed_count == 0
        assert len(report.results) >= 4
        assert "PASSED ✅" in report.summary_text
