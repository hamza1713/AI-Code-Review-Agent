"""Offline blind-input prediction capture and independently labelled scoring."""

import argparse
import hashlib
import json
from collections.abc import Callable
from pathlib import Path

from code_review_agent.eval.matching import predict_verdict, score_findings
from code_review_agent.path_validation import safe_diff_path
from code_review_agent.review_contracts import ReviewInputSnapshot, SourceFile
from code_review_agent.source_snapshot import build_snapshot


def load_inputs(root: Path) -> tuple[dict, list[dict]]:
    raw_manifest = (root / "inputs.json").read_bytes()
    manifest = json.loads(raw_manifest)
    if manifest.get("schema_version") not in (1, 2) or not manifest.get(
        "corpus_version"
    ):
        raise ValueError("Unsupported corpus manifest")
    if (
        manifest["schema_version"] == 2
        and (root / "manifest.sha256").read_text(encoding="utf-8").strip()
        != hashlib.sha256(raw_manifest).hexdigest()
    ):
        raise ValueError("Frozen corpus manifest changed")
    inputs = []
    ids = set()
    for case in manifest["cases"]:
        if manifest["schema_version"] == 2 and any(
            key in case
            for key in (
                "gold",
                "labels",
                "cohort",
                "title",
                "body",
                "discussion",
                "reviewers",
                "findings",
            )
        ):
            raise ValueError("Labels or discussion leaked into frozen inputs")
        if case["id"] in ids:
            raise ValueError("Duplicate corpus case")
        ids.add(case["id"])
        data = safe_diff_path(root, case["file"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != case["sha256"]:
            raise ValueError("Corpus input hash mismatch")
        # This is the whole predictor contract: no gold, title, cohort or PR discussion.
        item = {
            "id": case["id"],
            "diff": data.decode("utf-8"),
            "sha256": case["sha256"],
        }
        if manifest["schema_version"] == 2:
            license_data = safe_diff_path(root, case["license_file"]).read_bytes()
            if hashlib.sha256(license_data).hexdigest() != case[
                "license_sha256"
            ] or case["license"] not in {"BSD-3-Clause", "Apache-2.0", "MIT"}:
                raise ValueError("Corpus license provenance mismatch")
            sources = {}
            for source in case["head_files"]:
                raw = safe_diff_path(root, source["file"]).read_bytes()
                artifact = SourceFile.from_bytes(source["path"], raw, "CORPUS")
                if artifact.path in sources or artifact.sha256 != source["sha256"]:
                    raise ValueError("Corpus source hash or identity mismatch")
                sources[artifact.path] = raw
            def fetch_corpus_source(
                path: str, bound_sources: dict[str, bytes] = sources
            ) -> bytes:
                return bound_sources[path]

            snapshot = build_snapshot(
                item["diff"],
                case["head_sha"],
                f"github:github.com/{case['source_repository']}",
                fetch=fetch_corpus_source,
            )
            if snapshot.snapshot_hash != case["input_snapshot_hash"]:
                raise ValueError("Corpus input snapshot hash mismatch")
            item["snapshot"] = snapshot
            item["input_snapshot_hash"] = snapshot.snapshot_hash
        inputs.append(item)
    if not inputs:
        raise ValueError("Empty corpus")
    return manifest, inputs


def capture_predictions(
    root: Path,
    predictor: Callable,
    mode: str,
    reviewer_profile=None,
    split: str | None = None,
) -> dict:
    if mode not in {"deterministic", "model", "combined"}:
        raise ValueError("Unknown prediction mode")
    manifest, inputs = load_inputs(root)
    if split:
        selected = {c["id"] for c in manifest["cases"] if c.get("split") == split}
        inputs = [item for item in inputs if item["id"] in selected]
        if not inputs:
            raise ValueError("Requested split is empty")
    if manifest["schema_version"] == 2 and reviewer_profile is None:
        raise ValueError("Versioned captures require a reviewer profile")
    captured = []
    for item in inputs:
        result = predictor(item.get("snapshot", item["diff"]))
        if manifest["schema_version"] == 2 and any(
            key in result for key in ("gold", "labels", "cohort", "title", "discussion")
        ):
            raise ValueError("Labels leaked into prediction capture")
        if (
            reviewer_profile
            and result.get("reviewer_profile_id", reviewer_profile.profile_id)
            != reviewer_profile.profile_id
        ):
            raise ValueError("Predictor configuration changed during capture")
        captured.append(
            {
                **result,
                "id": item["id"],
                "sha256": item["sha256"],
                "input_snapshot_hash": item.get("input_snapshot_hash"),
                "reviewer_profile_id": reviewer_profile.profile_id
                if reviewer_profile
                else None,
            }
        )
    return {
        "corpus_manifest_sha256": hashlib.sha256(
            (root / "inputs.json").read_bytes()
        ).hexdigest(),
        "schema_version": manifest["schema_version"],
        "corpus_version": manifest["corpus_version"],
        "mode": mode,
        "split": split,
        "reviewer_profile": reviewer_profile.model_dump() if reviewer_profile else None,
        "reviewer_profile_id": reviewer_profile.profile_id
        if reviewer_profile
        else None,
        "cases": captured,
    }


def score_predictions(root: Path, predictions: dict, labels: dict) -> dict:
    manifest, inputs = load_inputs(root)
    if predictions.get("split"):
        selected = {
            c["id"] for c in manifest["cases"] if c.get("split") == predictions["split"]
        }
        inputs = [item for item in inputs if item["id"] in selected]
    if manifest["schema_version"] == 2:
        from code_review_agent.review_contracts import ReviewerProfile

        manifest_hash = hashlib.sha256((root / "inputs.json").read_bytes()).hexdigest()
        if (
            predictions.get("corpus_manifest_sha256") != manifest_hash
            or labels.get("corpus_manifest_sha256") != manifest_hash
        ):
            raise ValueError("Labels or predictions use a different frozen manifest")

        profile = ReviewerProfile.model_validate(predictions.get("reviewer_profile"))
        if profile.profile_id != predictions.get("reviewer_profile_id"):
            raise ValueError("Prediction reviewer profile mismatch")
    if (
        predictions.get("corpus_version") != manifest["corpus_version"]
        or labels.get("corpus_version") != manifest["corpus_version"]
    ):
        raise ValueError("Corpus version mismatch")
    if predictions.get("mode") not in {"deterministic", "model", "combined"}:
        raise ValueError("Unknown prediction mode")
    expected_ids = {case["id"] for case in inputs}
    pred = {case["id"]: case for case in predictions["cases"]}
    gold = {case["id"]: case for case in labels["cases"]}
    if (
        set(pred) != expected_ids
        or set(gold) != expected_ids
        or len(pred) != len(predictions["cases"])
        or len(gold) != len(labels["cases"])
    ):
        raise ValueError("Missing, duplicate or unknown cases")
    details = []
    for item in inputs:
        case_id = item["id"]
        label = gold[case_id]
        if (
            len({r.strip().lower() for r in label.get("reviewers", []) if r.strip()})
            < 2
            or label.get("status") != "ADJUDICATED"
        ):
            raise ValueError("Independent adjudicated labels required")
        if not label.get("rationale") or label.get("cohort") not in {
            "clean",
            "hard_negative",
            "defect",
        }:
            raise ValueError("Label rationale and cohort required")
        if (
            pred[case_id]["sha256"] != item["sha256"]
            or label.get("sha256") != item["sha256"]
        ):
            raise ValueError("Prediction or label input hash mismatch")
        findings = pred[case_id]["findings"]
        if manifest["schema_version"] == 2 and (
            pred[case_id].get("reviewer_profile_id")
            != predictions["reviewer_profile_id"]
            or pred[case_id].get("input_snapshot_hash") != item["input_snapshot_hash"]
            or label.get("input_snapshot_hash") != item["input_snapshot_hash"]
        ):
            raise ValueError("Mixed reviewer profiles or mismatched snapshot identity")
        if label.get("disagreement") and (
            not label.get("adjudicator")
            or label["adjudicator"].strip().lower()
            in {r.strip().lower() for r in label["reviewers"]}
        ):
            raise ValueError("Disagreements require a third independent adjudicator")
        complete = pred[case_id].get("analysis_complete") is True
        score = score_findings(findings, label["findings"], tolerance=0)
        score.update(
            id=case_id,
            cohort=label["cohort"],
            emitted=len(findings),
            analysis_complete=complete,
            verdict_correct=complete
            and predict_verdict(findings) == predict_verdict(label["findings"]),
        )
        details.append(score)
    tp, fp, fn = (sum(d[key] for d in details) for key in ("tp", "fp", "fn"))
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    clean = [d for d in details if d["cohort"] == "clean"]
    return {
        "corpus_version": manifest["corpus_version"],
        "scorer_version": 2,
        "mode": predictions["mode"],
        "split": predictions.get("split"),
        "reviewer_profile_id": predictions.get("reviewer_profile_id"),
        "cases": len(details),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": precision,
        "recall": recall,
        "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None,
        "verdict_accuracy": sum(d["verdict_correct"] for d in details) / len(details),
        "precision_denominator": tp + fp,
        "recall_denominator": tp + fn,
        "false_comments_per_pr": fp / len(details),
        "incomplete_cases": sum(not d["analysis_complete"] for d in details),
        "clean_prs": len(clean),
        "clean_pr_false_alarm_rate": sum(d["fp"] > 0 for d in clean) / len(clean)
        if clean
        else None,
        "severity_emitted": {
            severity: sum(
                str(f.get("severity", "UNKNOWN")).upper() == severity
                for p in pred.values()
                for f in p["findings"]
            )
            for severity in (
                "CRITICAL",
                "HIGH",
                "MEDIUM",
                "LOW",
                "WARNING",
                "INFO",
                "UNKNOWN",
            )
        },
        "latency_seconds": {
            case_id: (p.get("telemetry") or {}).get("duration_seconds")
            for case_id, p in pred.items()
        },
        "cost_coverage_cases": sum(
            (p.get("telemetry") or {}).get("estimated_cost_usd") is not None
            for p in pred.values()
        ),
        "details": details,
    }


def main():
    parser = argparse.ArgumentParser(description="Offline blind PR evaluation")
    parser.add_argument("action", choices=["capture", "score", "label-template"])
    parser.add_argument(
        "--corpus", type=Path, default=Path("samples/evaluation/public-pr-v1")
    )
    parser.add_argument("--predictions", type=Path)
    parser.add_argument("--labels", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--mode",
        choices=["deterministic", "model", "combined"],
        default="deterministic",
    )
    parser.add_argument("--split", choices=["development", "held_out"])
    args = parser.parse_args()
    if args.action == "label-template":
        if not args.output:
            parser.error("label-template requires --output")
        if args.output.exists():
            raise ValueError("Refusing to overwrite labels")
        manifest, items = load_inputs(args.corpus)
        selected = {
            c["id"]
            for c in manifest["cases"]
            if not args.split or c.get("split") == args.split
        }
        template = {
            "corpus_manifest_sha256": hashlib.sha256(
                (args.corpus / "inputs.json").read_bytes()
            ).hexdigest(),
            "schema_version": manifest["schema_version"],
            "corpus_version": manifest["corpus_version"],
            "cases": [
                {
                    "id": i["id"],
                    "sha256": i["sha256"],
                    "input_snapshot_hash": i.get("input_snapshot_hash"),
                    "status": "UNREVIEWED",
                    "reviewers": [],
                    "cohort": None,
                    "rationale": "",
                    "disagreement": False,
                    "adjudicator": None,
                    "findings": [],
                }
                for i in items
                if i["id"] in selected
            ],
        }
        args.output.write_text(json.dumps(template, indent=2) + "\n", encoding="utf-8")
        return
    if not args.predictions:
        parser.error("capture and score require --predictions")
    if args.action == "capture":
        import os

        from code_review_agent.reviewer_profile import build_profile

        if os.getenv("RAG_ENABLED", "false").lower() not in {"false", "0", "no"}:
            raise ValueError("Evaluation requires frozen RAG-disabled context")
        profile = build_profile()

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
                analysis_mode=args.mode,
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

        if args.predictions.exists():
            raise ValueError("Refusing to overwrite frozen predictions")
        result = capture_predictions(
            args.corpus, predictor, args.mode, profile, args.split
        )
        if build_profile().profile_id != profile.profile_id:
            raise ValueError("Reviewer configuration changed during capture")
        args.predictions.parent.mkdir(parents=True, exist_ok=True)
        with args.predictions.open("x", encoding="utf-8") as output:
            output.write(json.dumps(result, indent=2) + "\n")
    else:
        if not args.labels or not args.output:
            parser.error("score requires --labels and --output")
        result = score_predictions(
            args.corpus,
            json.loads(args.predictions.read_text(encoding="utf-8")),
            json.loads(args.labels.read_text(encoding="utf-8")),
        )
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
