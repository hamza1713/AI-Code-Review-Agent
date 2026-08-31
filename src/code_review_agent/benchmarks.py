"""
Deterministic Model & Code Review Benchmarking Suite.
Measures Recall, Precision, F1-Score, and Verdict Accuracy against ground-truth
curated pull request diffs without requiring external LLM-as-a-judge API costs.
"""

import json
import time
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple
from pydantic import BaseModel, Field

from code_review_agent.config import logger
from code_review_agent.tools import SastEngine
from code_review_agent.diff_parser import DiffParser


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
    details: List[Dict[str, Any]] = Field(default_factory=list)


class BenchmarkRunner:
    """Executes deterministic evaluations against benchmark PR datasets."""

    def __init__(self, benchmarks_dir: Optional[str] = None):
        if benchmarks_dir:
            self.benchmarks_dir = Path(benchmarks_dir).resolve()
        else:
            # Default to samples/benchmarks
            self.benchmarks_dir = Path(__file__).resolve().parent.parent.parent / "samples" / "benchmarks"

    def load_manifest(self) -> List[Dict[str, Any]]:
        """Load benchmark manifest file containing ground truth definitions."""
        manifest_file = self.benchmarks_dir / "manifest.json"
        if not manifest_file.exists():
            raise FileNotFoundError(f"Benchmark manifest not found at: {manifest_file}")
        with open(manifest_file, "r", encoding="utf-8") as f:
            return json.load(f)

    def run_sast_benchmark(self) -> BenchmarkMetric:
        """
        Run static pattern / SAST benchmark against ground truth without LLM calls.
        Ultra-fast, deterministic, zero-cost verification.
        """
        manifest = self.load_manifest()
        start_time = time.time()

        tp = 0
        fp = 0
        fn = 0
        tn = 0
        correct_verdicts = 0
        details: List[Dict[str, Any]] = []

        for item in manifest:
            diff_file = self.benchmarks_dir / item["file"]
            if not diff_file.exists():
                logger.warning(f"Benchmark diff file missing: {diff_file}")
                continue

            diff_content = diff_file.read_text(encoding="utf-8")
            findings = SastEngine.scan_diff(diff_content)
            expected_issues = item.get("expected_issues", [])

            # Check if expected issues were detected
            matched_expected = 0
            for exp in expected_issues:
                exp_cwe = exp.get("cwe", "").upper()
                keywords = [k.lower() for k in exp.get("keywords", [])]

                matched = False
                for f in findings:
                    f_cwe = (f.cwe or "").upper()
                    f_desc = f.description.lower()
                    f_rule = f.rule_id.lower()

                    if exp_cwe and exp_cwe in f_cwe:
                        matched = True
                        break
                    if any(kw in f_desc or kw in f_rule for kw in keywords):
                        matched = True
                        break

                if matched:
                    matched_expected += 1

            # Track which findings matched known expected issues
            matched_finding_indices = set()
            for f_idx, f in enumerate(findings):
                f_cwe = (f.cwe or "").upper()
                f_desc = f.description.lower()
                f_rule = f.rule_id.lower()
                f_snippet = (f.snippet or "").lower()
                for exp in expected_issues:
                    exp_cwe = exp.get("cwe", "").upper()
                    keywords = [k.lower() for k in exp.get("keywords", [])]
                    if (exp_cwe and (exp_cwe in f_cwe or exp_cwe in f_desc)) or any(kw in f_desc or kw in f_rule or kw in f_snippet for kw in keywords):
                        matched_finding_indices.add(f_idx)
                        break

            # Compute TP, FP, FN, TN for this test case
            case_tp = matched_expected
            case_fn = len(expected_issues) - matched_expected
            case_fp = len(findings) - len(matched_finding_indices)
            case_tn = 1 if (len(expected_issues) == 0 and len(findings) == 0) else 0

            tp += case_tp
            fp += case_fp
            fn += case_fn
            tn += case_tn


            # Verdict evaluation
            has_critical = any(f.severity in ("CRITICAL", "HIGH") for f in findings)
            derived_verdict = "REQUEST CHANGES" if (has_critical or len(findings) > 0) else "APPROVE"
            expected_verdict = item.get("expected_verdict", "APPROVE")
            verdict_correct = derived_verdict == expected_verdict
            if verdict_correct:
                correct_verdicts += 1

            details.append({
                "id": item["id"],
                "name": item["name"],
                "expected_issues_count": len(expected_issues),
                "detected_findings_count": len(findings),
                "tp": case_tp,
                "fp": case_fp,
                "fn": case_fn,
                "derived_verdict": derived_verdict,
                "expected_verdict": expected_verdict,
                "verdict_correct": verdict_correct
            })

        duration = time.time() - start_time
        precision = (tp / (tp + fp)) if (tp + fp) > 0 else 1.0
        recall = (tp / (tp + fn)) if (tp + fn) > 0 else 1.0
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 1.0
        accuracy = (correct_verdicts / len(manifest)) if manifest else 0.0

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
            details=details
        )

    @staticmethod
    def format_summary_table(metric: BenchmarkMetric) -> str:
        """Format metrics as a Markdown summary table."""
        lines = [
            "# 🎯 Model & Code Review Benchmark Results",
            "",
            "| Metric | Value |",
            "| :--- | :--- |",
            f"| **Total Test Cases** | {metric.total_test_cases} |",
            f"| **True Positives (TP)** | {metric.true_positives} |",
            f"| **False Positives (FP)** | {metric.false_positives} |",
            f"| **False Negatives (FN)** | {metric.false_negatives} |",
            f"| **Precision** | {metric.precision * 100:.1f}% |",
            f"| **Recall** | {metric.recall * 100:.1f}% |",
            f"| **F1-Score** | {metric.f1_score * 100:.1f}% |",
            f"| **Verdict Accuracy** | {metric.verdict_accuracy * 100:.1f}% |",
            f"| **Execution Duration** | {metric.duration_seconds:.3f}s |",
            "",
            "### Detailed Case Breakdown",
            "",
            "| ID | Test Case | Expected | Detected | TP | FP | FN | Verdict Match |",
            "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |"
        ]

        for d in metric.details:
            match_icon = "✅" if d["verdict_correct"] else "❌"
            lines.append(
                f"| {d['id']} | {d['name']} | {d['expected_issues_count']} | "
                f"{d['detected_findings_count']} | {d['tp']} | {d['fp']} | {d['fn']} | {match_icon} {d['derived_verdict']} |"
            )

        return "\n".join(lines)
