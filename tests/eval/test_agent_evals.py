"""
Evaluation tests for LLM-as-a-Judge and G-Eval custom metrics
(Provenance Grounding, Security Deduplication, Quality Refactoring).
"""

import pytest
from code_review_agent.eval.custom_metrics import (
    ProvenanceGroundingEvaluator,
    SecurityDeduplicationEvaluator,
    MetricScore,
)


@pytest.mark.eval
class TestCustomGEvalMetrics:
    """Validate G-Eval custom metrics with representative scenarios."""

    def test_provenance_grounding_evaluator_interface(self):
        context = """
Senior Developer Output:
- Critical Issue: Raw SQL query concatenation in user lookup (app/services/user.py:L15).
- Minor Issue: Missing docstring in helper function.

Security Engineer Output:
- Vulnerability: SQL Injection (CWE-89) at app/services/user.py:L15. Matched by: bandit-B608.
"""
        grounded_output = """
{
  "confidence": 60,
  "confidence_breakdown": "100 - 30 (critical SQLi) - 10 (critical quality) = 60",
  "findings": "Critical SQL injection vulnerability detected in user lookup alongside unhandled input.",
  "coverage_gaps": [],
  "fix": [
    {
      "description": "Fix SQL injection in user query",
      "solutions": "Use parameterized query",
      "explanation": "Direct interpolation allows SQL injection"
    }
  ],
  "recommendations": ["Add docstring to helper function"],
  "inline_comments": []
}
"""
        result = ProvenanceGroundingEvaluator.evaluate(
            context=context,
            actual_output=grounded_output,
            threshold=0.75
        )
        assert isinstance(result, MetricScore)
        assert result.metric_name == "ProvenanceGrounding"
        assert 0.0 <= result.score <= 1.0
        assert len(result.reasoning) > 0

    def test_security_deduplication_evaluator_interface(self):
        raw_sast = """
Finding 1: [Bandit-B608] Possible SQL injection at app/services/user.py line 15
Finding 2: [Regex-SQLI] SQL injection pattern hit at app/services/user.py line 15
"""
        deduplicated_sec_output = """
{
  "security_vulnerabilities": [
    {
      "description": "SQL Injection in user search query",
      "risk_level": "critical",
      "evidence": "query = f'SELECT * FROM users WHERE id = {user_id}'",
      "matched_by": ["Bandit-B608", "Regex-SQLI"]
    }
  ],
  "blocking": true,
  "highest_risk": "critical",
  "security_recommendations": ["Use parameterized queries"]
}
"""
        result = SecurityDeduplicationEvaluator.evaluate(
            raw_sast_context=raw_sast,
            security_output=deduplicated_sec_output,
            threshold=0.75
        )
        assert isinstance(result, MetricScore)
        assert result.metric_name == "SecurityDeduplication"
        assert 0.0 <= result.score <= 1.0
