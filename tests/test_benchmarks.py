"""
Unit tests for deterministic benchmark suite and metrics calculation.
"""

import pytest

from code_review_agent.benchmarks import BenchmarkMetric, BenchmarkRunner


class TestBenchmarkSuite:
    """Validate benchmark suite execution and metric computations."""

    def test_load_manifest_success(self):
        runner = BenchmarkRunner()
        manifest = runner.load_manifest()
        assert len(manifest) >= 12
        ids = [item["id"] for item in manifest]
        assert "SEC-001" in ids
        assert "QUAL-001" in ids
        assert "GOV-001" in ids

    @pytest.mark.slow
    def test_deterministic_sast_benchmark_metrics(self):
        runner = BenchmarkRunner()
        # Evaluate Security, Governance, and Complex scenarios
        metrics = runner.run_sast_benchmark(
            categories=["SECURITY", "GOVERNANCE", "COMPLEX"]
        )

        assert isinstance(metrics, BenchmarkMetric)
        assert metrics.total_test_cases >= 5
        # Every emitted hit is either TP or FP; every label is either TP or FN.
        assert metrics.true_positives + metrics.false_positives == sum(
            case["detected_findings_count"] for case in metrics.details
        )
        assert metrics.true_positives + metrics.false_negatives == sum(
            case["expected_issues_count"] for case in metrics.details
        )
        assert metrics.precision == round(
            metrics.true_positives / (metrics.true_positives + metrics.false_positives),
            4,
        )
        # NOTE: this is a regex+AST scan (no LLM call) so it is "fast" relative to a
        # full crew review, but BanditRunner shells out a fresh `python -m bandit`
        # subprocess per case (see tools/bandit_runner.py) — interpreter cold-start
        # cost is host-dependent: sub-second on a clean CI runner, but measured at
        # 125-150s on a dev machine with heavy site-packages / AV process scanning.
        # 240s gives real headroom above that observed range without being a no-op.
        assert metrics.duration_seconds < 240.0

    @pytest.mark.slow
    def test_deterministic_full_benchmark_metrics(self):
        runner = BenchmarkRunner()
        metrics = runner.run_deterministic_benchmark()

        assert isinstance(metrics, BenchmarkMetric)
        assert metrics.total_test_cases >= 12
        assert metrics.verdict_accuracy == round(
            sum(case["verdict_correct"] for case in metrics.details)
            / len(metrics.details),
            4,
        )
        assert "SECURITY" in metrics.category_breakdown
        assert "QUALITY" in metrics.category_breakdown

    @pytest.mark.slow
    def test_format_summary_table(self):
        runner = BenchmarkRunner()
        metrics = runner.run_sast_benchmark()
        table_md = BenchmarkRunner.format_summary_table(metrics)

        assert "# 🎯 Model & Code Review Benchmark Results" in table_md
        assert "Precision" in table_md
        assert "Recall" in table_md
        assert "F1-Score" in table_md
        assert "SEC-001" in table_md
