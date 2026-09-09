"""
Evaluation Runner & Orchestrator for AI Code Review Agent.
Executes both deterministic invariants and custom LLM evaluation rubrics,
producing a structured EvaluationReport with pass/fail gates.
"""

from typing import Dict, List, Any, Optional
from pydantic import BaseModel, Field

from code_review_agent.models import (
    ReviewState,
)
from code_review_agent.eval.deterministic_evaluators import (
    ConfidenceMathEvaluator,
    CodeCompilationEvaluator,
    GuardrailConsistencyEvaluator,
    SarifComplianceEvaluator,
    DiffLineScopeEvaluator,
    EvaluationResult,
)
from code_review_agent.eval.custom_metrics import (
    ProvenanceGroundingEvaluator,
    SecurityDeduplicationEvaluator,
    MetricScore,
)


class EvaluationReport(BaseModel):
    """Aggregated evaluation report across all deterministic and LLM metrics."""
    overall_passed: bool
    total_evaluators_run: int
    passed_count: int
    failed_count: int
    deterministic_score: float = Field(..., description="Average score across deterministic checks (0.0 - 1.0)")
    llm_eval_score: Optional[float] = Field(default=None, description="Average score across LLM-as-a-judge metrics")
    results: List[EvaluationResult] = Field(default_factory=list)
    llm_metrics: List[MetricScore] = Field(default_factory=list)
    summary_text: str = ""


class EvalRunner:
    """Orchestrates comprehensive evaluation of a completed code review."""

    @classmethod
    def evaluate_review(
        cls,
        state: ReviewState,
        include_llm_judge: bool = False,
        sarif_dict: Optional[Dict[str, Any]] = None,
    ) -> EvaluationReport:
        """Run all applicable evaluation checks against the flow's final state."""
        results: List[EvaluationResult] = []
        llm_metrics: List[MetricScore] = []

        summary = state.summarized_findings
        sec_output = state.security_review
        quality_output = state.quality_review
        parsed_pr = state.parsed_pr

        # 1. Deterministic Checks
        if summary:
            # Confidence Math Arithmetic Check
            results.append(ConfidenceMathEvaluator.evaluate_breakdown_arithmetic(summary))

            # Suggestion Code AST Compilation Check
            results.append(CodeCompilationEvaluator.evaluate_inline_suggestions(summary.inline_comments))

            # Suggested Unit Tests AST Compilation Check
            results.append(CodeCompilationEvaluator.evaluate_suggested_unit_tests(summary.suggested_unit_tests))

            # Diff Line Scope Check
            results.append(DiffLineScopeEvaluator.evaluate_line_scope(summary.inline_comments, parsed_pr))

        if sec_output:
            # Guardrail Invariant Check
            results.append(GuardrailConsistencyEvaluator.evaluate_security_output(sec_output))

        if sarif_dict:
            # SARIF Schema Compliance Check
            results.append(SarifComplianceEvaluator.evaluate_sarif_dict(sarif_dict))

        # 2. LLM-as-a-Judge Checks (Optional / Offline Gate)
        if include_llm_judge and summary:
            # Provenance Grounding
            upstream_context = f"QUALITY:\n{quality_output}\n\nSECURITY:\n{sec_output}\n\nRULES:\n{state.rules_context}"
            prov_score = ProvenanceGroundingEvaluator.evaluate(
                context=upstream_context,
                actual_output=summary.model_dump_json(indent=2)
            )
            llm_metrics.append(prov_score)

            # Security Deduplication
            if sec_output and state.sast_context:
                dedup_score = SecurityDeduplicationEvaluator.evaluate(
                    raw_sast_context=state.sast_context,
                    security_output=sec_output.model_dump_json(indent=2)
                )
                llm_metrics.append(dedup_score)

        # 3. Aggregation & Scoring
        passed_count = sum(1 for r in results if r.passed)
        failed_count = len(results) - passed_count
        det_score = (sum(r.score for r in results) / len(results)) if results else 1.0

        llm_score = None
        if llm_metrics:
            llm_score = sum(m.score for m in llm_metrics) / len(llm_metrics)
            all_llm_passed = all(m.passed for m in llm_metrics)
        else:
            all_llm_passed = True

        overall_passed = (failed_count == 0) and all_llm_passed

        summary_text = (
            f"Evaluation Report: {'PASSED ✅' if overall_passed else 'FAILED ❌'} | "
            f"Deterministic: {passed_count}/{len(results)} passed (Score: {det_score*100:.1f}%)"
        )
        if llm_score is not None:
            summary_text += f" | LLM Judge Score: {llm_score*100:.1f}%"

        return EvaluationReport(
            overall_passed=overall_passed,
            total_evaluators_run=len(results) + len(llm_metrics),
            passed_count=passed_count + sum(1 for m in llm_metrics if m.passed),
            failed_count=failed_count + sum(1 for m in llm_metrics if not m.passed),
            deterministic_score=round(det_score, 4),
            llm_eval_score=round(llm_score, 4) if llm_score is not None else None,
            results=results,
            llm_metrics=llm_metrics,
            summary_text=summary_text
        )
