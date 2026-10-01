"""
Deterministic Model & Code Review Benchmarking Suite.
Measures Recall, Precision, F1-Score, and Verdict Accuracy against ground-truth
curated pull request diffs across Security, Quality, Governance, and Architecture domains.
"""

import json
import sys
import time
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    try:
        getattr(sys.stdout, "reconfigure", lambda **kw: None)(
            encoding="utf-8", errors="replace"
        )
        getattr(sys.stderr, "reconfigure", lambda **kw: None)(
            encoding="utf-8", errors="replace"
        )
    except Exception:
        pass

from pydantic import BaseModel, Field

from code_review_agent.config import logger
from code_review_agent.governance import RulesEngine
from code_review_agent.tools import SastEngine


class BenchmarkMetric(BaseModel):
    """Calculated precision, recall, and accuracy metrics for a benchmark run."""

    total_test_cases: int
    true_positives: int
    false_positives: int
    false_negatives: int
    true_negatives: int
    precision: float
    recall: float
    f1_score: float
    verdict_accuracy: float
    duration_seconds: float
    category_breakdown: dict[str, dict[str, Any]] = Field(default_factory=dict)
    details: list[dict[str, Any]] = Field(default_factory=list)


class BenchmarkRunner:
    """Executes deterministic evaluations against benchmark PR datasets."""

    def __init__(self, benchmarks_dir: str | None = None):
        if benchmarks_dir:
            self.benchmarks_dir = Path(benchmarks_dir).resolve()
        else:
            # Default to samples/benchmarks
            self.benchmarks_dir = (
                Path(__file__).resolve().parent.parent.parent / "samples" / "benchmarks"
            )
        self.rules_engine = RulesEngine()

    def load_manifest(self) -> list[dict[str, Any]]:
        """Load benchmark manifest file containing ground truth definitions."""
        manifest_file = self.benchmarks_dir / "manifest.json"
        if not manifest_file.exists():
            raise FileNotFoundError(f"Benchmark manifest not found at: {manifest_file}")
        with open(manifest_file, encoding="utf-8") as f:
            return json.load(f)

    def run_deterministic_benchmark(
        self, categories: list[str] | None = None
    ) -> BenchmarkMetric:
        """
        Run multi-engine deterministic benchmark (SAST + Governance + Ruff) against
        ground truth without requiring external LLM API calls.
        Ultra-fast, deterministic, zero-cost regression gate.
        """
        manifest = self.load_manifest()
        if categories:
            cat_set = {c.upper() for c in categories}
            manifest = [
                item for item in manifest if item.get("category", "").upper() in cat_set
            ]

        start_time = time.time()

        tp = 0
        fp = 0
        fn = 0
        tn = 0
        correct_verdicts = 0
        details: list[dict[str, Any]] = []
        category_stats: dict[str, dict[str, int]] = {}

        for item in manifest:
            category = item.get("category", "GENERAL")
            if category not in category_stats:
                category_stats[category] = {
                    "total": 0,
                    "tp": 0,
                    "fp": 0,
                    "fn": 0,
                    "correct_verdicts": 0,
                }
            category_stats[category]["total"] += 1

            diff_file = self.benchmarks_dir / item["file"]
            if not diff_file.exists():
                raise FileNotFoundError(f"Benchmark diff file missing: {diff_file}")

            diff_content = diff_file.read_text(encoding="utf-8")

            # 1. Multi-Engine Scans
            sast_findings = SastEngine.scan_diff(diff_content)
            rule_violations = self.rules_engine.evaluate_diff(diff_content)

            from code_review_agent.eval.matching import predict_verdict, score_findings

            # Score exactly what the scanner emitted; never forgive extra or duplicate hits.
            all_detected_issues = [f.model_dump() for f in sast_findings]
            all_detected_issues.extend(
                {
                    "rule_id": v.rule_id,
                    "cwe": "",
                    "file_path": v.file_path,
                    "line_number": v.line_number,
                    "severity": v.severity,
                }
                for v in rule_violations
            )
            derived_verdict = predict_verdict(all_detected_issues)
            expected_issues = item.get("expected_issues", [])
            score = score_findings(all_detected_issues, expected_issues, tolerance=0)
            case_tp, case_fp, case_fn = score["tp"], score["fp"], score["fn"]
            case_tn = int(not expected_issues and not all_detected_issues)

            tp += case_tp
            fp += case_fp
            fn += case_fn
            tn += case_tn

            category_stats[category]["tp"] += case_tp
            category_stats[category]["fp"] += case_fp
            category_stats[category]["fn"] += case_fn

            expected_verdict = item.get("expected_verdict", "APPROVE")
            verdict_correct = derived_verdict == expected_verdict
            if verdict_correct:
                correct_verdicts += 1
                category_stats[category]["correct_verdicts"] += 1

            details.append(
                {
                    "id": item["id"],
                    "name": item["name"],
                    "category": category,
                    "expected_issues_count": len(expected_issues),
                    "detected_findings_count": len(all_detected_issues),
                    "tp": case_tp,
                    "fp": case_fp,
                    "fn": case_fn,
                    "derived_verdict": derived_verdict,
                    "expected_verdict": expected_verdict,
                    "verdict_correct": verdict_correct,
                }
            )

        duration = time.time() - start_time
        precision = (tp / (tp + fp)) if (tp + fp) > 0 else 0.0
        recall = (tp / (tp + fn)) if (tp + fn) > 0 else 0.0
        f1 = (
            (2 * precision * recall / (precision + recall))
            if (precision + recall) > 0
            else 0.0
        )
        accuracy = (correct_verdicts / len(manifest)) if manifest else 0.0

        # Calculate category breakdowns
        category_breakdown = {}
        for cat, stats in category_stats.items():
            cat_tp, cat_fp, cat_fn = stats["tp"], stats["fp"], stats["fn"]
            cat_prec = (cat_tp / (cat_tp + cat_fp)) if (cat_tp + cat_fp) > 0 else 0.0
            cat_rec = (cat_tp / (cat_tp + cat_fn)) if (cat_tp + cat_fn) > 0 else 0.0
            cat_f1 = (
                (2 * cat_prec * cat_rec / (cat_prec + cat_rec))
                if (cat_prec + cat_rec) > 0
                else 0.0
            )
            cat_acc = (
                (stats["correct_verdicts"] / stats["total"])
                if stats["total"] > 0
                else 0.0
            )
            category_breakdown[cat] = {
                "total_cases": stats["total"],
                "precision": round(cat_prec, 4),
                "recall": round(cat_rec, 4),
                "f1_score": round(cat_f1, 4),
                "verdict_accuracy": round(cat_acc, 4),
            }

        return BenchmarkMetric(
            total_test_cases=len(manifest),
            true_positives=tp,
            false_positives=fp,
            false_negatives=fn,
            true_negatives=tn,
            precision=round(precision, 4),
            recall=round(recall, 4),
            f1_score=round(f1, 4),
            verdict_accuracy=round(accuracy, 4),
            duration_seconds=round(duration, 4),
            category_breakdown=category_breakdown,
            details=details,
        )

    # Alias for backwards compatibility
    def run_sast_benchmark(
        self, categories: list[str] | None = None
    ) -> BenchmarkMetric:
        return self.run_deterministic_benchmark(categories=categories)

    @staticmethod
    def format_summary_table(metric: BenchmarkMetric) -> str:
        """Format metrics as a Markdown summary table."""
        lines = [
            "# 🎯 Model & Code Review Benchmark Results",
            "",
            "| Overall Metric | Value |",
            "| :--- | :--- |",
            f"| **Total Test Cases** | {metric.total_test_cases} |",
            f"| **True Positives (TP)** | {metric.true_positives} |",
            f"| **False Positives (FP)** | {metric.false_positives} |",
            f"| **False Negatives (FN)** | {metric.false_negatives} |",
            f"| **Overall Precision** | {metric.precision * 100:.1f}% |",
            f"| **Overall Recall** | {metric.recall * 100:.1f}% |",
            f"| **Overall F1-Score** | {metric.f1_score * 100:.1f}% |",
            f"| **Verdict Accuracy** | {metric.verdict_accuracy * 100:.1f}% |",
            f"| **Execution Duration** | {metric.duration_seconds:.3f}s |",
            "",
            "### 📊 Breakdown by Domain Category",
            "",
            "| Category | Cases | Precision | Recall | F1-Score | Verdict Accuracy |",
            "| :--- | :--- | :--- | :--- | :--- | :--- |",
        ]

        for cat, stats in metric.category_breakdown.items():
            lines.append(
                f"| **{cat}** | {stats['total_cases']} | {stats['precision'] * 100:.1f}% | "
                f"{stats['recall'] * 100:.1f}% | {stats['f1_score'] * 100:.1f}% | {stats['verdict_accuracy'] * 100:.1f}% |"
            )

        lines.extend(
            [
                "",
                "### 📋 Detailed Case Breakdown",
                "",
                "| ID | Test Case | Category | Expected | Detected | TP | FP | FN | Verdict Match |",
                "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
            ]
        )

        for d in metric.details:
            match_icon = "✅" if d["verdict_correct"] else "❌"
            lines.append(
                f"| {d['id']} | {d['name']} | {d.get('category', 'N/A')} | {d['expected_issues_count']} | "
                f"{d['detected_findings_count']} | {d['tp']} | {d['fp']} | {d['fn']} | {match_icon} {d['derived_verdict']} |"
            )

        return "\n".join(lines)

    @classmethod
    def generate_markdown_report(
        cls, metric: BenchmarkMetric, output_path: str | None = None
    ) -> str:
        """
        Generate a comprehensive, publication-ready benchmark evaluation report
        with LaTeX formulas, confusion matrix, Qodo comparison, and Shields.io badges.
        """
        lines = [
            "# Deterministic scanner regression report",
            "",
            "Scorer version: 2. Synthetic fixtures; not a product quality study.",
            "Full path, exact target line, and rule ID or CWE define a match. One-to-one matching; every unmatched emitted hit is an FP.",
            "Predicted verdict uses emitted output only. Gold labels never enter prediction.",
            "TP/FP/FN count findings; TN counts clean cases, so these are not a PR confusion matrix.",
            "Zero-denominator precision/recall are reported as 0, not evidence of perfect performance.",
            "",
            cls.format_summary_table(metric),
            "",
            "Reproduce: `python -m code_review_agent.benchmarks --publish`",
        ]

        report_content = "\n".join(lines)
        if output_path:
            out_file = Path(output_path)
            out_file.write_text(report_content, encoding="utf-8")
            logger.info(f"📄 Published benchmark report to {out_file}")

        return report_content

    @classmethod
    def publish_report(
        cls,
        metrics: BenchmarkMetric | None = None,
        output_file: str | None = None,
        output_path: Any | None = None,
        repo_root: str | None = None,
    ) -> tuple[str, BenchmarkMetric]:
        """Run benchmarks (or use provided metrics) and publish output markdown report."""
        if output_path:
            target_path = Path(output_path)
        else:
            filename = output_file or "BENCHMARK_REPORT.md"
            root = Path(repo_root) if repo_root else Path.cwd()
            target_path = root / filename

        target_path.parent.mkdir(parents=True, exist_ok=True)
        if metrics is None:
            runner = cls()
            metrics = runner.run_deterministic_benchmark()

        report_md = cls.generate_markdown_report(metrics, output_path=str(target_path))
        return report_md, metrics


def main():
    """CLI entrypoint for running and publishing the benchmark suite."""
    import argparse

    parser = argparse.ArgumentParser(description="AI Code Review Agent Benchmark Suite")
    parser.add_argument(
        "--publish", action="store_true", help="Generate and save BENCHMARK_REPORT.md"
    )
    parser.add_argument(
        "--output",
        default="BENCHMARK_REPORT.md",
        help="Output file path for benchmark report",
    )
    args = parser.parse_args()

    runner = BenchmarkRunner()
    metrics = runner.run_deterministic_benchmark()

    if args.publish:
        report_md, _ = BenchmarkRunner.publish_report(
            metrics=metrics, output_file=args.output
        )
        print(f"✅ Benchmark report successfully published to {args.output}!")
        print(
            f"Overall F1: {metrics.f1_score * 100:.1f}% | Accuracy: {metrics.verdict_accuracy * 100:.1f}%"
        )
    else:
        print(BenchmarkRunner.format_summary_table(metrics))


if __name__ == "__main__":
    main()
