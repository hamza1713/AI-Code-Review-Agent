"""
Unit tests for AdversarialVerifier.
Verifies cross-model consensus and adversarial divergence reporting.
"""

from unittest.mock import MagicMock

from code_review_agent.crews.code_review_crew.adversarial_verifier import (
    AdversarialVerifier,
)
from code_review_agent.llm_factory import LLMResponse
from code_review_agent.models import SastFinding


class TestAdversarialVerifier:
    """Test suite for adversarial red-team verification and consensus telemetry."""

    def test_verifies_agreement_on_valid_vulnerability(self):
        mock_llm = MagicMock()
        mock_llm.call_response.return_value = LLMResponse(
            text="""{
  "evaluations": [
    {
      "rule_id": "SEC-001",
      "verdict": "AGREED",
      "reason": "Direct f-string concatenation into cursor.execute is an exploitable SQL injection."
    }
  ]
}""",
            model="claude-3-7-sonnet",
        )

        finding = SastFinding(
            rule_id="SEC-001",
            cwe="CWE-89",
            category="SECURITY",
            name="SQL Injection",
            description="Raw SQL formatted with user input",
            severity="CRITICAL",
            file_path="app/db.py",
            line_number=10,
            snippet="cursor.execute(f'SELECT * FROM users WHERE id = {user_id}')",
            fix_recommendation="Use parameterized query",
            analyzer_source="semgrep",
        )

        verifier = AdversarialVerifier(
            verifier_model="claude-3-7-sonnet", llm=mock_llm
        )
        verified = verifier.verify_findings(
            raw_diff="dummy diff", findings=[finding]
        )

        assert len(verified) == 1
        res = verified[0]
        assert res.evidence_provenance.get("consensus") == "CONSENSUS_AGREED"
        assert (
            res.evidence_provenance.get("verifier_model") == "claude-3-7-sonnet"
        )

    def test_verifies_adversarial_divergence_on_false_positive(self):
        mock_llm = MagicMock()
        mock_llm.call_response.return_value = LLMResponse(
            text="""{
  "evaluations": [
    {
      "rule_id": "SEC-006",
      "verdict": "REFUTED",
      "reason": "Path is safe: int(file_id) is cast before path construction, making traversal impossible."
    }
  ]
}""",
            model="gpt-4o",
        )

        finding = SastFinding(
            rule_id="SEC-006",
            cwe="CWE-22",
            category="SECURITY",
            name="Path Traversal",
            description="Potential path traversal",
            severity="HIGH",
            file_path="app/files.py",
            line_number=25,
            snippet="path = f'/tmp/{int(file_id)}.txt'",
            fix_recommendation="Validate path",
            analyzer_source="heuristic",
        )

        verifier = AdversarialVerifier(verifier_model="gpt-4o", llm=mock_llm)
        verified = verifier.verify_findings(
            raw_diff="dummy diff", findings=[finding]
        )

        assert len(verified) == 1
        res = verified[0]
        assert (
            res.evidence_provenance.get("consensus") == "ADVERSARIAL_DIVERGENCE"
        )
        assert (
            "int(file_id)"
            in res.evidence_provenance.get("verifier_counter_argument", "")
        )
