"""
Public PR Corpus V2 Benchmark Runner.
Executes formal evaluation across frozen public PR benchmark cases in samples/evaluation/public-pr-v2
according to EVALUATION_PROTOCOL_V2.

Captures blind predictions under frozen context (RAG disabled), saves predictions to evaluation-runs/,
and scores against adjudicated ground-truth labels if available.
"""

# ruff: noqa: E402
import argparse
import json
import os
import sys
import time
from pathlib import Path

# Add src to sys.path
src_dir = Path(__file__).resolve().parent.parent / "src"
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

from code_review_agent.eval.corpus import (
    capture_predictions,
    score_predictions,
)
from code_review_agent.review_contracts import ReviewInputSnapshot
from code_review_agent.reviewer_profile import build_profile
from code_review_agent.source_snapshot import build_snapshot


def run_evaluation(
    split: str = "development",
    mode: str = "deterministic",
    corpus_dir: str = "samples/evaluation/public-pr-v2",
    output_dir: str = "evaluation-runs",
    strict_gates: bool = False,
) -> dict:
    corpus_path = Path(corpus_dir).resolve()
    if not corpus_path.exists():
        raise FileNotFoundError(f"Corpus path not found: {corpus_path}")

    # Ensure RAG is disabled for frozen evaluation runs
    os.environ["RAG_ENABLED"] = "false"
    out_dir = Path(output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    profile = build_profile()
    splits_to_run = ["development", "held_out"] if split == "all" else [split]

    all_scores = {}

    for s in splits_to_run:
        label_file = corpus_path / "labeling" / f"{s}-adjudication.json"
        has_labels = label_file.exists()
        labels = json.loads(label_file.read_text(encoding="utf-8")) if has_labels else None

        def predictor(value):
            from code_review_agent.review_service import ReviewService

            snapshot = (
                value
                if isinstance(value, ReviewInputSnapshot)
                else build_snapshot(value)
            )
            result = ReviewService.execute_review(
                raw_diff=snapshot.diff,
                input_snapshot=snapshot.model_dump(),
                analysis_mode=mode,
                evaluation_mode=True,
                bypass_limits=True,
            )
            return {
                "reviewer_profile_id": result.reviewer_profile_id,
                "findings": [f.model_dump() for f in result.findings],
                "analysis_complete": result.analysis_complete,
                "analyzer_status": result.analyzer_status,
                "telemetry": result.telemetry.model_dump() if result.telemetry else {},
            }

        print("\n=======================================================")
        print(f"🚀 Running Public PR Corpus V2 Benchmark [Split: {s}, Mode: {mode}]")
        print("=======================================================")

        start_time = time.time()
        predictions = capture_predictions(
            corpus_path,
            predictor,
            mode=mode,
            reviewer_profile=profile,
            split=s,
        )
        elapsed = time.time() - start_time
        num_cases = len(predictions["cases"])
        print(f"✅ Prediction capture finished in {elapsed:.2f}s ({num_cases} PR cases).")

        # Save predictions
        pred_path = out_dir / f"{s}-{mode}-predictions.json"
        pred_path.write_text(json.dumps(predictions, indent=2) + "\n", encoding="utf-8")
        print(f"💾 Captured predictions saved to: {pred_path}")

        # Check if adjudication labels are finalized or unreviewed templates
        is_adjudicated = False
        if labels and "cases" in labels and labels["cases"]:
            first_case = labels["cases"][0]
            if first_case.get("status") == "ADJUDICATED" and len(first_case.get("reviewers", [])) >= 2:
                is_adjudicated = True

        if is_adjudicated:
            scores = score_predictions(
                corpus_path,
                predictions,
                labels,
            )
            all_scores[s] = scores

            p = (scores.get("precision") or 0.0) * 100
            r = (scores.get("recall") or 0.0) * 100
            f1 = (scores.get("f1") or 0.0) * 100
            far = (scores.get("clean_pr_false_alarm_rate") or 0.0) * 100
            acc = (scores.get("verdict_accuracy") or 0.0) * 100

            print("\n📊 Formal Evaluation Results:")
            print(f"  • Total Cases Evaluated : {scores['cases']}")
            print(f"  • True Positives (TP)   : {scores['tp']}")
            print(f"  • False Positives (FP)  : {scores['fp']}")
            print(f"  • False Negatives (FN)  : {scores['fn']}")
            print(f"  • Precision             : {p:.1f}%")
            print(f"  • Recall                : {r:.1f}%")
            print(f"  • F1 Score              : {f1:.1f}%")
            print(f"  • Clean PR False Alarms : {far:.1f}% ({scores.get('clean_prs', 0)} clean control PRs)")
            print(f"  • Verdict Accuracy      : {acc:.1f}%")

            if strict_gates:
                if f1 < 75.0:
                    print(f"❌ Gate failure: F1 score {f1:.1f}% < 75.0% threshold")
                    sys.exit(1)
                if far > 15.0:
                    print(f"❌ Gate failure: Clean PR false alarm rate {far:.1f}% > 15.0% threshold")
                    sys.exit(1)
        else:
            print("\nℹ️  Adjudication Status: Templates are in UNREVIEWED state awaiting independent human labeling")
            print("    (per EVALUATION_PROTOCOL_V2: Tooling never synthesizes fake human reviewer identities).")
            # Compute empirical runtime telemetry from predictions
            total_findings = sum(len(c.get("findings", [])) for c in predictions["cases"])
            complete_cases = sum(1 for c in predictions["cases"] if c.get("analysis_complete") is True)
            print(f"  • Captured Cases        : {num_cases}")
            print(f"  • Analysis Complete     : {complete_cases}/{num_cases} ({(complete_cases/num_cases)*100:.1f}%)")
            print(f"  • Total Sourced Findings: {total_findings}")
            print(f"  • Mean Latency Per PR   : {elapsed/num_cases:.2f}s")
            all_scores[s] = {
                "status": "CAPTURED_AWAITING_INDEPENDENT_LABELS",
                "cases_captured": num_cases,
                "analysis_complete_count": complete_cases,
                "total_findings_sourced": total_findings,
                "duration_seconds": elapsed,
                "mean_latency_seconds": elapsed / num_cases if num_cases else 0,
            }

    report_path = out_dir / "v2-formal-benchmark-report.json"
    report_data = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "corpus_version": "v2",
        "eval_protocol": "EVALUATION_PROTOCOL_V2",
        "mode": mode,
        "splits": all_scores,
    }
    report_path.write_text(json.dumps(report_data, indent=2) + "\n", encoding="utf-8")
    print(f"\n📁 Saved formal benchmark report to: {report_path}")
    return report_data


def main():
    parser = argparse.ArgumentParser(description="Public PR Corpus V2 Benchmark Runner")
    parser.add_argument(
        "--split",
        choices=["development", "held_out", "all"],
        default="development",
        help="Corpus split to evaluate (default: development)",
    )
    parser.add_argument(
        "--mode",
        choices=["deterministic", "model", "combined"],
        default="deterministic",
        help="Analysis mode (default: deterministic)",
    )
    parser.add_argument(
        "--corpus",
        default="samples/evaluation/public-pr-v2",
        help="Path to frozen public PR corpus v2 directory",
    )
    parser.add_argument(
        "--output-dir",
        default="evaluation-runs",
        help="Directory to save benchmark reports",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit with non-zero status if quality gates fail",
    )
    args = parser.parse_args()

    run_evaluation(
        split=args.split,
        mode=args.mode,
        corpus_dir=args.corpus,
        output_dir=args.output_dir,
        strict_gates=args.strict,
    )


if __name__ == "__main__":
    main()
