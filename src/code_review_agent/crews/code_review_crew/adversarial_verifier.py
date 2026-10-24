"""
Cross-Model Adversarial Verifier.
Subject primary reviewer findings to an independent adversarial red-team verification pass.
Correlates model consensus to distinguish high-confidence exploits from single-model hallucinations.
"""

import json
import os
from typing import Any

from code_review_agent.config import logger
from code_review_agent.llm_factory import LLMFactory, LLMProvider
from code_review_agent.models import SastFinding


class AdversarialVerifier:
    """
    Evaluates primary review findings through an adversarial skeptic perspective
    to eliminate hallucinations and measure cross-model consensus.
    """

    def __init__(
        self,
        verifier_model: str | None = None,
        llm: LLMProvider | None = None,
    ):
        self.verifier_model_name = (
            verifier_model
            or os.getenv("SECONDARY_REVIEW_MODEL")
            or os.getenv("REVIEW_MODEL", "gemini-2.5-pro")
        )
        self._llm = llm

    def _get_llm(self) -> LLMProvider:
        if self._llm is None:
            try:
                self._llm = LLMFactory.create_llm(
                    model_name=self.verifier_model_name
                )
            except Exception as e:
                logger.debug(
                    f"Could not initialize secondary verifier model '{self.verifier_model_name}': {e}. "
                    "Falling back to default LLM."
                )
                self._llm = LLMFactory.create_llm()
        return self._llm

    def verify_findings(
        self,
        raw_diff: str,
        findings: list[SastFinding],
        timeout_seconds: float = 30.0,
    ) -> list[SastFinding]:
        """
        Verify findings through adversarial evaluation.
        Updates each finding's evidence_provenance with consensus telemetry.
        """
        if not findings:
            return []

        # Only verify High/Critical security and quality findings to preserve token budget
        critical_findings = [
            f for f in findings if f.severity in ("CRITICAL", "HIGH")
        ]
        if not critical_findings:
            return findings

        prompt = self._build_adversarial_prompt(raw_diff, critical_findings)
        try:
            llm = self._get_llm()
            resp = llm.call_response(
                [{"role": "user", "content": prompt}],
                timeout_seconds=timeout_seconds,
            )
            parsed_evaluations = self._parse_verifier_response(resp.text)
        except Exception as e:
            logger.debug(
                f"Adversarial verifier call failed or timed out: {e}; retaining original findings."
            )
            return findings

        # Correlate evaluations with findings
        for f in findings:
            eval_data = parsed_evaluations.get(
                f.rule_id
            ) or parsed_evaluations.get(f.name)
            if eval_data:
                verdict = eval_data.get("verdict", "AGREED").upper()
                reason = eval_data.get("reason", "")
                if verdict == "REFUTED":
                    f.evidence_provenance["consensus"] = "ADVERSARIAL_DIVERGENCE"
                    f.evidence_provenance["verifier_counter_argument"] = reason
                    f.evidence_provenance["verifier_model"] = (
                        self.verifier_model_name
                    )
                else:
                    f.evidence_provenance["consensus"] = "CONSENSUS_AGREED"
                    f.evidence_provenance["verifier_model"] = (
                        self.verifier_model_name
                    )
            else:
                f.evidence_provenance["consensus"] = "SINGLE_MODEL"

        return findings

    def _build_adversarial_prompt(
        self, raw_diff: str, findings: list[SastFinding]
    ) -> str:
        findings_summary = []
        for f in findings:
            findings_summary.append(
                f"- ID: {f.rule_id}\n"
                f"  Rule: {f.name}\n"
                f"  Location: {f.file_path}:L{f.line_number}\n"
                f"  Description: {f.description}\n"
                f"  Snippet: {f.snippet}"
            )

        return (
            "You are an Adversarial Security Reviewer and Defense Skeptic.\n"
            "Your job is to challenge the findings below and identify FALSE POSITIVES or HALLUCINATIONS.\n"
            "For each finding, determine if it is a real vulnerability (AGREED) or if it is safe/unreachable/sanitized (REFUTED).\n\n"
            "Diff Snippet:\n"
            f"```\n{raw_diff[:8000]}\n```\n\n"
            "Findings to Verify:\n"
            + "\n".join(findings_summary)
            + "\n\n"
            "Return JSON only in this exact format:\n"
            "{\n"
            '  "evaluations": [\n'
            '    {"rule_id": "<ID>", "verdict": "AGREED" | "REFUTED", "reason": "<brief justification>"}\n'
            "  ]\n"
            "}"
        )

    def _parse_verifier_response(self, text: str) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        try:
            data = json.loads(cleaned)
            evals = data.get("evaluations", [])
            for item in evals:
                rule_id = item.get("rule_id")
                if rule_id:
                    result[rule_id] = item
        except Exception:
            pass

        return result
