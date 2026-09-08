"""
Unit tests for benchmark reporting, publishing, CLI integration, and API endpoints.
"""

from unittest.mock import patch
from starlette.testclient import TestClient

from code_review_agent.benchmarks import BenchmarkRunner, BenchmarkMetric
from code_review_agent.bot.command_router import CommandRouter
from code_review_agent.webhook_server import app


def _create_sample_metric() -> BenchmarkMetric:
    return BenchmarkMetric(
        total_test_cases=14,
        true_positives=16,
        false_positives=3,
        false_negatives=3,
        true_negatives=14,
        precision=0.8421,
        recall=0.8421,
        f1_score=0.8421,
        verdict_accuracy=1.0,
        duration_seconds=12.9,
        category_breakdown={
            "SECURITY": {
                "total_cases": 4,
                "passed_cases": 4,
                "precision": 0.88,
                "recall": 0.88,
                "f1_score": 0.88,
                "verdict_accuracy": 1.0,
            },
            "GOVERNANCE": {
                "total_cases": 4,
                "passed_cases": 4,
                "precision": 1.0,
                "recall": 1.0,
                "f1_score": 1.0,
                "verdict_accuracy": 1.0,
            },
            "QUALITY": {
                "total_cases": 3,
                "passed_cases": 3,
                "precision": 0.75,
                "recall": 0.75,
                "f1_score": 0.75,
                "verdict_accuracy": 1.0,
            },
            "COMPLEX": {
                "total_cases": 3,
                "passed_cases": 3,
                "precision": 0.75,
                "recall": 0.75,
                "f1_score": 0.75,
                "verdict_accuracy": 1.0,
            },
        },
        details=[
            {
                "id": "SEC-001",
                "name": "Raw SQL Injection via String Concatenation",
                "category": "SECURITY",
                "expected_issues_count": 1,
                "detected_findings_count": 1,
                "tp": 1,
                "fp": 0,
                "fn": 0,
                "verdict_correct": True,
            }
        ]
    )


class TestBenchmarkPublisher:
    """Test generating and publishing benchmark reports."""

    def test_generate_markdown_report_structure(self):
        metric = _create_sample_metric()
        report_md = BenchmarkRunner.generate_markdown_report(metric)

        # Badges
        assert "https://img.shields.io/badge/Benchmark_F1-84.2%25-brightgreen" in report_md
        assert "https://img.shields.io/badge/Verdict_Accuracy-100.0%25-success" in report_md
        assert "https://img.shields.io/badge/Deterministic_Grounding-Compiler--Grade-blue" in report_md

        # Headings
        assert "Code Review Ground-Truth Benchmark Report" in report_md

        # Confusion matrix
        assert "True Positive (TP)" in report_md
        assert "False Positive (FP)" in report_md

        # Positioning section — honestly scoped, NOT a head-to-head advantage claim.
        assert "Positioning vs Qodo Merge" in report_md
        assert "60.1%" in report_md  # Qodo reference F1, cited for context
        assert "84.2%" in report_md  # Our agent F1 on our own suite
        assert "not directly comparable" in report_md
        # The misleading subtracted-advantage claim must be gone.
        assert "+24.1%" not in report_md

        # Case Breakdown
        assert "SEC-001" in report_md
        assert "Raw SQL Injection" in report_md

    def test_publish_report_writes_file(self, tmp_path):
        metric = _create_sample_metric()
        target_path = tmp_path / "SUBDIR" / "BENCHMARK_REPORT.md"

        report_md, published_metric = BenchmarkRunner.publish_report(metrics=metric, output_path=target_path)
        assert target_path.exists()
        assert published_metric.f1_score == metric.f1_score
        content = target_path.read_text(encoding="utf-8")
        assert "Code Review Ground-Truth Benchmark Report" in content
        assert "84.2%" in content


class TestBotBenchmarkCommand:
    """Test /benchmark bot slash command integration."""

    @patch.object(BenchmarkRunner, "run_deterministic_benchmark")
    def test_handle_benchmark_command(self, mock_run):
        mock_run.return_value = _create_sample_metric()

        result = CommandRouter.dispatch("/benchmark --fast", auto_post=False)
        assert result.command == "benchmark"
        assert result.status == "SUCCESS"
        assert "Ground-Truth Benchmark Results" in result.response_markdown
        assert "84.2%" in result.response_markdown
        assert "100.0%" in result.response_markdown
        assert "16" in result.response_markdown


class TestWebhookBenchmarkAPI:
    """Test webhook REST API endpoint for benchmark metrics."""

    @patch.object(BenchmarkRunner, "run_deterministic_benchmark")
    def test_api_benchmark_metrics_endpoint(self, mock_run):
        mock_run.return_value = _create_sample_metric()
        client = TestClient(app)

        response = client.get("/api/benchmark/metrics")
        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "success"
        assert data["metrics"]["total_test_cases"] == 14
        assert data["metrics"]["f1_score"] == 0.8421
        assert data["metrics"]["verdict_accuracy"] == 1.0
        assert "SECURITY" in data["metrics"]["category_breakdown"]
