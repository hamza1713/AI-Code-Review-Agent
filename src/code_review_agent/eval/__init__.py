"""
AI Evaluation (AI EVAL) Package for Multi-Agent Code Review.
Provides deterministic checkers, G-Eval metrics, and evaluation orchestration.
"""

from .deterministic_evaluators import (
    ConfidenceMathEvaluator,
    CodeCompilationEvaluator,
    GuardrailConsistencyEvaluator,
    SarifComplianceEvaluator,
    DiffLineScopeEvaluator,
    EvaluationResult,
)
from .custom_metrics import (
    ProvenanceGroundingEvaluator,
    SecurityDeduplicationEvaluator,
    MetricScore,
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
