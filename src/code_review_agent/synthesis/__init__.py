"""
Deterministic final-synthesis stage.

Reconciles raw analyzer findings (regex scanner, Bandit/SAST, governance engine) plus
generated-test results into ONE coherent, internally consistent report — deduplicated,
one severity per defect, honest test-evidence semantics, production-scoped governance,
corrected CWEs, and a bounded non-saturating quality score whose every term traces to a
listed finding. See `reconciler.SynthesisReconciler`.
"""

from .reconciler import (
    SynthesisReconciler,
    ReconciledReport,
    ReconciledFinding,
    TestEvidence,
)

__all__ = [
    "SynthesisReconciler",
    "ReconciledReport",
    "ReconciledFinding",
    "TestEvidence",
]
