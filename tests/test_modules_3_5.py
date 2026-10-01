"""Advisory delivery, output quality, and honest scoring regressions."""

import json
from contextlib import contextmanager
from unittest.mock import Mock

import pytest

from code_review_agent.eval.custom_metrics import ProvenanceGroundingEvaluator
from code_review_agent.eval.matching import predict_verdict, score_findings
from code_review_agent.eval.runtime import validate_response
from code_review_agent.models import InlineComment, ReviewAPIResponse, SastFinding
from code_review_agent.review_policy import advisory_summary, apply_finding_policy

DIFF = "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -0,0 +1,1 @@\n+result = eval(user_input)\n"


def response(**kwargs):
    return ReviewAPIResponse(
        verdict="REQUEST CHANGES",
        confidence_score=50,
        summary="Review",
        full_report="DO NOT PUBLISH WEAK NARRATIVE",
        reviewed_diff=DIFF,
        findings=[
            SastFinding(
                rule_id="eval",
                severity="HIGH",
                file_path="src/a.py",
                line_number=1,
                snippet="result = eval(user_input)",
                description="Untrusted input is executed as Python code.",
                fix_recommendation="Parse the input as structured JSON rather than executing it.",
            )
        ],
        **kwargs,
    )


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("line_number", 2, "NOT_CHANGED_LINE"),
        ("file_path", "a.py", "NOT_CHANGED_LINE"),
        ("snippet", "imaginary code", "UNGROUNDED_EVIDENCE"),
        ("snippet", "", "UNGROUNDED_EVIDENCE"),
        ("description", "Issue", "NOT_ACTIONABLE"),
        ("fix_recommendation", "Fix this issue", "NOT_ACTIONABLE"),
        (
            "fix_recommendation",
            "Review and remediate the flagged code.",
            "NOT_ACTIONABLE",
        ),
        ("fingerprint", "fake", "INVALID_IDENTITY"),
    ],
)
def test_weak_candidate_retained_but_never_published(field, value, reason):
    review = response()
    setattr(review.findings[0], field, value)
    apply_finding_policy(review)
    assert reason in review.findings[0].publication_reasons
    assert review.findings[0].publication_status == "WITHHELD"
    assert not review.inline_comments
    assert "DO NOT PUBLISH" not in advisory_summary(review, set())


def test_grounded_candidate_and_duplicate_audit():
    review = response()
    review.findings.append(review.findings[0].model_copy(deep=True))
    apply_finding_policy(review)
    assert len(review.inline_comments) == 1
    assert review.findings[0].publication_status == "ELIGIBLE"
    assert review.findings[1].publication_reasons == ["DUPLICATE_IDENTITY"]
    summary = advisory_summary(review, {review.findings[0].fingerprint})
    assert summary.count("Untrusted input is executed") == 1


@pytest.mark.parametrize(
    "verdict,complete",
    [("APPROVE", True), ("REQUEST CHANGES", True), ("ESCALATE", False)],
)
def test_provider_boundary_comment_only_no_merge_effects(
    monkeypatch, tmp_path, queued_claim, verdict, complete
):
    from code_review_agent.platform.base import PlatformPRIdentifier, PlatformPRMetadata
    from code_review_agent.webhook_queue import execute_platform_review

    review = response(analysis_complete=complete)
    from code_review_agent.reviewer_profile import build_profile

    reviewer = build_profile()
    review.reviewer_profile = reviewer
    review.reviewer_profile_id = reviewer.profile_id
    from code_review_agent.source_snapshot import build_snapshot

    review.input_snapshot_hash = build_snapshot(
        DIFF, "a" * 40, fetch=lambda _: b"result = eval(user_input)\n"
    ).snapshot_hash
    review.verdict = verdict
    client = Mock()
    client.fetch_pull_request_diff.return_value = DIFF
    client.fetch_source_file.return_value = b"result = eval(user_input)\n"
    client.fetch_pull_request_metadata.return_value = PlatformPRMetadata(
        head_sha="a" * 40, head_ref="feature"
    )
    client.list_pull_request_review_comments.return_value = []
    client.post_pull_request_review.return_value = {"id": 123}
    ident = PlatformPRIdentifier(
        platform="github",
        owner_or_project="org",
        repo_or_slug="repo",
        pr_id=1,
        raw_identifier="org/repo#1",
    )
    monkeypatch.setattr(
        "code_review_agent.platform.factory.get_platform_client",
        lambda _: (client, ident),
    )

    @contextmanager
    def checkout(*args, **kwargs):
        yield str(tmp_path)

    monkeypatch.setattr(
        "code_review_agent.bot.checkout.temporary_pr_checkout", checkout
    )
    monkeypatch.setattr(
        "code_review_agent.review_service.ReviewService.execute_review",
        lambda **_: review,
    )
    claim = queued_claim("org/repo#1")
    result = execute_platform_review({}, "org/repo#1", **claim)
    execute_platform_review({}, "org/repo#1", **claim)
    assert client.post_pull_request_review.call_count == int(complete)
    if complete:
        sent = client.post_pull_request_review.call_args.kwargs
        assert sent["event"] == "COMMENT" and sent["commit_id"] == "a" * 40
        assert "DO NOT PUBLISH" not in sent["body"]
        assert result["verdict"] == verdict
    client.set_commit_status.assert_not_called()
    client.create_or_update_check_run.assert_not_called()


def test_output_failure_marks_incomplete():
    review = response()
    review.inline_comments = [
        InlineComment(path="a.py", line=1, comment_body="Wrong file")
    ]
    assert validate_response(review) == ["COMMENT_OUTSIDE_CHANGED_LINES"]
    assert review.verdict == "ESCALATE" and not review.analysis_complete
    apply_finding_policy(review)
    assert not review.inline_comments


@pytest.mark.parametrize(
    "reply",
    [
        None,
        "not json",
        '{"score": 2, "reasoning":"bad"}',
        '{"score":true,"reasoning":"bad"}',
        '{"score":0.9}',
    ],
)
def test_judge_unavailable_is_not_pass(monkeypatch, reply):
    llm = Mock()
    if reply is None:
        llm.call.side_effect = TimeoutError()
    else:
        llm.call.return_value = reply
    monkeypatch.setattr(
        "code_review_agent.eval.custom_metrics.LLMFactory.create_llm", lambda: llm
    )
    result = ProvenanceGroundingEvaluator.evaluate("context", "output")
    assert result.status == "UNAVAILABLE" and result.score is None and not result.passed


@pytest.mark.parametrize("score,status", [(0.9, "PASS"), (0.2, "FAIL")])
def test_judge_valid_result(monkeypatch, score, status):
    llm = Mock()
    llm.call.return_value = json.dumps(
        {"score": score, "reasoning": "Rubric evaluated."}
    )
    monkeypatch.setattr(
        "code_review_agent.eval.custom_metrics.LLMFactory.create_llm", lambda: llm
    )
    assert ProvenanceGroundingEvaluator.evaluate("context", "output").status == status


def test_every_unmatched_prediction_counts_including_duplicate_and_wrong_path():
    finding = {
        "file_path": "src/a.py",
        "line_number": 1,
        "rule_id": "eval",
        "cwe": "CWE-95",
    }
    gold = dict(finding)
    predictions = [
        finding,
        dict(finding),
        dict(finding, file_path="other/a.py"),
        dict(finding, line_number=9),
    ]
    scored = score_findings(predictions, [gold])
    assert (scored["tp"], scored["fp"], scored["fn"]) == (1, 3, 0)
    assert predict_verdict([]) == "APPROVE"
    assert predict_verdict(predictions) == "REQUEST CHANGES"


def test_missing_gold_location_rejected():
    with pytest.raises(ValueError):
        score_findings([], [{"cwe": "CWE-95"}])


def test_scanner_statuses_and_ruff_wiring(monkeypatch):
    from code_review_agent.source_snapshot import build_snapshot
    from code_review_agent.tools.ruff_tool import RuffRunner
    from code_review_agent.tools.scan_report import run_scanners

    monkeypatch.setattr(
        "code_review_agent.tools.bandit_runner.BanditRunner.is_available", lambda: False
    )
    monkeypatch.setattr(
        "code_review_agent.tools.semgrep_runner.SemgrepRunner.is_available",
        lambda: False,
    )
    monkeypatch.setattr(RuffRunner, "is_available", lambda: True)
    snapshot = build_snapshot(DIFF, fetch=lambda _: b"result = eval(user_input)\n")
    report = run_scanners(DIFF, snapshot)
    assert report.statuses["ruff"] == "COMPLETED"
    assert report.statuses["bandit"] == "FAILED_UNAVAILABLE"
    assert report.statuses["semgrep"] == "SKIPPED_OPTIONAL_UNAVAILABLE"
    assert any(
        f.rule_id == "ruff-F821" and f.snippet == "result = eval(user_input)"
        for f in report.findings
    )
    monkeypatch.setattr(
        "code_review_agent.tools.full_source_scan.subprocess.run",
        Mock(side_effect=TimeoutError()),
    )
    assert run_scanners(DIFF, snapshot).statuses["ruff"] == "FAILED"


def test_reproduction_claim_requires_finding_specific_committed_evidence():
    review = response()
    review.analyzed_commit = "head"
    finding = review.findings[0]
    finding.evidence_grade = "REPRODUCED"
    apply_finding_policy(review)
    assert "UNVERIFIED_REPRODUCTION" in finding.publication_reasons
    finding.evidence_provenance = {
        "finding_fingerprint": finding.fingerprint,
        "commit_sha": "head",
        "test_id": "test_input",
        "executed": True,
        "status": "REPRODUCED",
    }
    apply_finding_policy(review)
    assert not finding.publication_reasons
    finding.evidence_provenance["commit_sha"] = "other-head"
    apply_finding_policy(review)
    assert not review.inline_comments


def test_ruff_preserves_original_target_lines(monkeypatch):
    from pathlib import Path

    from code_review_agent.tools.ruff_tool import RuffRunner

    diff = DIFF.replace("+1,1", "+80,1")
    monkeypatch.setattr(RuffRunner, "is_available", lambda: True)

    def process(command, **kwargs):
        root = Path(command[-1])
        file = root / "src/a.py"
        assert file.read_text().splitlines()[79] == "result = eval(user_input)"
        return Mock(
            returncode=1,
            stdout=json.dumps(
                [
                    {
                        "code": "F821",
                        "message": "Undefined name",
                        "filename": str(file),
                        "location": {"row": 80, "column": 1},
                        "fix": None,
                    }
                ]
            ),
        )

    monkeypatch.setattr("code_review_agent.tools.ruff_tool.subprocess.run", process)
    findings = RuffRunner.scan_diff(diff, strict=True)
    assert findings[0].line_number == 80 and findings[0].file_path == "src/a.py"


def test_benchmark_prediction_never_uses_gold(monkeypatch, tmp_path):
    from code_review_agent.benchmarks import BenchmarkRunner

    (tmp_path / "input.diff").write_text(DIFF)
    manifest = [
        {
            "id": "blind",
            "name": "Blind input",
            "category": "SECURITY",
            "file": "input.diff",
            "expected_issues": [],
            "expected_verdict": "APPROVE",
        }
    ]
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(
        "code_review_agent.benchmarks.SastEngine.scan_diff", lambda _: []
    )
    runner = BenchmarkRunner(str(tmp_path))
    monkeypatch.setattr(runner.rules_engine, "evaluate_diff", lambda _: [])
    first = runner.run_deterministic_benchmark()
    manifest[0]["expected_issues"] = [
        {"file_path": "src/a.py", "line_number": 1, "cwe": "CWE-95"}
    ]
    manifest[0]["expected_verdict"] = "REQUEST CHANGES"
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    second = runner.run_deterministic_benchmark()
    assert (
        first.details[0]["derived_verdict"]
        == second.details[0]["derived_verdict"]
        == "APPROVE"
    )
    assert second.false_negatives == 1 and second.verdict_accuracy == 0


def test_blind_corpus_integrity_label_gate_and_denominators(tmp_path):
    import hashlib

    from code_review_agent.eval.corpus import capture_predictions, score_predictions

    digest = hashlib.sha256(DIFF.encode()).hexdigest()
    (tmp_path / "input.diff").write_text(DIFF, encoding="utf-8", newline="")
    (tmp_path / "inputs.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "corpus_version": "test-v1",
                "cases": [
                    {
                        "id": "test",
                        "file": "input.diff",
                        "sha256": digest,
                        "cohort": "SECRET_GOLD_COHORT",
                    }
                ],
            }
        )
    )
    predictor = Mock(return_value={"findings": [], "analysis_complete": True})
    predictions = capture_predictions(tmp_path, predictor, "deterministic")
    predictor.assert_called_once_with(DIFF)
    labels = {
        "corpus_version": "test-v1",
        "cases": [
            {
                "id": "test",
                "sha256": digest,
                "reviewers": ["human-a", "human-b"],
                "status": "ADJUDICATED",
                "rationale": "Reviewed clean diff.",
                "cohort": "clean",
                "findings": [],
            }
        ],
    }
    scores = score_predictions(tmp_path, predictions, labels)
    assert scores["precision"] is None and scores["recall"] is None
    assert scores["precision_denominator"] == 0 and scores["cases"] == 1
    labels["cases"][0]["reviewers"] = ["human-a"]
    with pytest.raises(ValueError, match="Independent"):
        score_predictions(tmp_path, predictions, labels)
    (tmp_path / "input.diff").write_text("tampered")
    with pytest.raises(ValueError, match="hash mismatch"):
        capture_predictions(tmp_path, predictor, "deterministic")


def test_production_never_executes_generated_code(monkeypatch):
    from code_review_agent.main import PRCodeReviewFlow
    from code_review_agent.sandbox.test_runner import SandboxTestRunner

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("REVIEW_ENABLE_EXPERIMENTAL_TESTS", "true")
    runner = Mock()
    monkeypatch.setattr(SandboxTestRunner, "run_tests", runner)
    flow = PRCodeReviewFlow()
    flow.state.generated_unit_tests = "def test_ok(): assert True"
    flow.make_final_decision()
    runner.assert_not_called()
    assert flow.state.analyzer_status["sandbox"] == "SKIPPED_DISABLED"
    assert flow.state.analyzer_status["self_healing"] == "SKIPPED_DISABLED"
