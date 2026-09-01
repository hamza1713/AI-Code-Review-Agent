"""
Unit tests for deterministic benchmark suite and metrics calculation.
"""

import pytest
from pathlib import Path
from code_review_agent.benchmarks import BenchmarkRunner, BenchmarkMetric


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

    def test_deterministic_sast_benchmark_metrics(self):
        runner = BenchmarkRunner()
        # Evaluate Security, Governance, and Complex scenarios
        metrics = runner.run_sast_benchmark(categories=["SECURITY", "GOVERNANCE", "COMPLEX"])

        assert isinstance(metrics, BenchmarkMetric)
        assert metrics.total_test_cases >= 5
        # High precision and recall on canonical benchmark cases
        assert metrics.precision >= 0.8
        assert metrics.recall >= 0.8
        assert metrics.f1_score >= 0.8
        assert metrics.verdict_accuracy >= 0.85
        assert metrics.duration_seconds < 30.0  # Fast deterministic check

    def test_deterministic_full_benchmark_metrics(self):
        runner = BenchmarkRunner()
        metrics = runner.run_deterministic_benchmark()

        assert isinstance(metrics, BenchmarkMetric)
        assert metrics.total_test_cases == 12
        # All 12 test cases derive accurate verdicts (APPROVE vs REQUEST CHANGES)
        assert metrics.verdict_accuracy >= 0.90
        assert "SECURITY" in metrics.category_breakdown
        assert "QUALITY" in metrics.category_breakdown

    def test_format_summary_table(self):
        runner = BenchmarkRunner()
        metrics = runner.run_sast_benchmark()
        table_md = BenchmarkRunner.format_summary_table(metrics)

        assert "# 🎯 Model & Code Review Benchmark Results" in table_md
        assert "Precision" in table_md
        assert "Recall" in table_md
        assert "F1-Score" in table_md
        assert "SEC-001" in table_md
