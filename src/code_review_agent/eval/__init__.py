"""
AI Evaluation (AI EVAL) Package for Multi-Agent Code Review.
Provides deterministic checkers, G-Eval metrics, and evaluation orchestration.
"""

from .custom_metrics import (
    MetricScore,
    ProvenanceGroundingEvaluator,
    SecurityDeduplicationEvaluator,
)
from .deterministic_evaluators import (
    CodeCompilationEvaluator,
    ConfidenceMathEvaluator,
    DiffLineScopeEvaluator,
    EvaluationResult,
    GuardrailConsistencyEvaluator,
    SarifComplianceEvaluator,
)
from .eval_runner import EvalRunner, EvaluationReport

__all__ = [
    "ConfidenceMathEvaluator",
    "CodeCompilationEvaluator",
    "GuardrailConsistencyEvaluator",
    "SarifComplianceEvaluator",
    "DiffLineScopeEvaluator",
    "EvaluationResult",
    "ProvenanceGroundingEvaluator",
    "SecurityDeduplicationEvaluator",
    "MetricScore",
    "EvalRunner",
    "EvaluationReport",
]
