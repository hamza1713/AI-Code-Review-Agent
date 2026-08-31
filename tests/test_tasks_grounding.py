"""
Unit and integration tests for tasks.yaml grounding constraints, deduplication,
deterministic confidence scoring, and output schema consistency.
"""

import pytest
import yaml
from pathlib import Path
from code_review_agent.models import SummarizedFindingsJSON, SecurityVulnerability, ReviewSecurityJSON


class TestTasksGroundingAndRubric:
    """Validate tasks.yaml structure and deterministic evaluation logic."""

    def test_tasks_yaml_contains_all_five_fixes(self):
        tasks_file = Path(__file__).parent.parent / "src" / "code_review_agent" / "crews" / "code_review_crew" / "config" / "tasks.yaml"
        assert tasks_file.exists()

        with open(tasks_file, "r", encoding="utf-8") as f:
            tasks_data = yaml.safe_load(f)

        # 1. Grounding constraint & coverage_gaps
        summarize_desc = tasks_data["summarize_findings"]["description"]
        assert "IMPORTANT: Every claim in 'findings', 'recommendations', and 'inline_comments' MUST trace back" in summarize_desc
        assert "coverage_gaps" in tasks_data["summarize_findings"]["expected_output"]

        # 2. Assert CURRENT ACTUAL behavior
        assert "POST-FIX:" in summarize_desc
        assert "Tests must assert the CURRENT, ACTUAL behavior" in summarize_desc

        # 3. Deduplicate security findings with matched_by
        sec_desc = tasks_data["review_security"]["description"]
        assert "matched_by" in sec_desc
        assert "matched_by" in tasks_data["review_security"]["expected_output"]

        # 4. Both analyze_code_quality and review_security represented
        assert "'findings' and 'inline_comments' MUST include content sourced from BOTH analyze_code_quality and review_security" in summarize_desc

        # 5. Grounded confidence rubric & confidence_breakdown
        assert "Calculate 'confidence' using this explicit rubric, not free judgment:" in summarize_desc
        assert "Subtract 30 for each 'critical' risk_level vulnerability" in summarize_desc
        assert "confidence_breakdown" in tasks_data["summarize_findings"]["expected_output"]

    def test_deterministic_confidence_rubric_arithmetic(self):
        """Verify the deterministic rubric calculation."""
        def calculate_rubric_confidence(critical_vulns=0, high_vulns=0, critical_issues=0, minor_or_med_low=0):
            score = 100
            score -= (critical_vulns * 30)
            score -= (high_vulns * 15)
            score -= (critical_issues * 10)
            score -= (minor_or_med_low * 5)
            return max(0, score)

        # Clean run -> 100
        assert calculate_rubric_confidence() == 100
        # 1 critical vuln -> 70
        assert calculate_rubric_confidence(critical_vulns=1) == 70
        # 1 critical + 1 high + 1 critical issue + 2 minor -> 100 - 30 - 15 - 10 - 10 = 35
        assert calculate_rubric_confidence(critical_vulns=1, high_vulns=1, critical_issues=1, minor_or_med_low=2) == 35
        # Extreme floor at 0
        assert calculate_rubric_confidence(critical_vulns=5) == 0

    def test_pydantic_models_support_new_fields(self):
        vuln = SecurityVulnerability(
            description="SQL Injection in auth query",
            risk_level="critical",
            evidence="cursor.execute(f'SELECT...')",
            matched_by=["SAST-SQLI-001", "Bandit-B608"]
        )
        assert vuln.matched_by == ["SAST-SQLI-001", "Bandit-B608"]

        summary = SummarizedFindingsJSON(
            confidence=70,
            confidence_breakdown="100 - 30 (1 critical vuln: SQLi) = 70",
            findings="Security and code quality findings synthesized.",
            coverage_gaps=["No AST call graph available for non-Python assets"],
            fix=[],
            recommendations=["Use parameterized queries"],
            inline_comments=[]
        )
        assert summary.confidence == 70
        assert summary.confidence_breakdown is not None
        assert "100 - 30" in summary.confidence_breakdown
        assert len(summary.coverage_gaps) == 1
