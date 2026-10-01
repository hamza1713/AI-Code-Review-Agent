"""Cheap output invariants run online; no model judge or submitted code execution."""

from code_review_agent.diff_parser import DiffParser
from code_review_agent.eval.deterministic_evaluators import CodeCompilationEvaluator
from code_review_agent.models import ReviewAPIResponse


def validate_response(response: ReviewAPIResponse) -> list[str]:
    errors = []
    changed = {
        (file.target_file, line)
        for file in DiffParser.parse_diff(response.reviewed_diff or "").files
        for line, _ in DiffParser.extract_added_lines_with_numbers(file)
    }
    for comment in response.inline_comments:
        if (comment.path, comment.line) not in changed or comment.side != "RIGHT":
            errors.append("COMMENT_OUTSIDE_CHANGED_LINES")
    checks = [
        CodeCompilationEvaluator.evaluate_inline_suggestions(response.inline_comments),
        CodeCompilationEvaluator.evaluate_suggested_unit_tests(
            response.generated_unit_tests or ""
        ),
    ]
    errors.extend(error for check in checks for error in check.errors)
    if not 0 <= response.confidence_score <= 100:
        errors.append("CONFIDENCE_OUT_OF_RANGE")
    if errors:
        response.analysis_complete = False
        response.verdict = "ESCALATE"
    response.analyzer_status["response_invariants"] = (
        "FAILED" if errors else "COMPLETED"
    )
    return errors
