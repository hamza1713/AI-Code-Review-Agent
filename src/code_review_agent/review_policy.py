"""Conservative advisory output boundary; candidates remain in the audit response."""

import re

from code_review_agent.diff_parser import DiffParser
from code_review_agent.models import InlineComment, ReviewAPIResponse


def apply_finding_policy(response: ReviewAPIResponse) -> ReviewAPIResponse:
    """Retain all candidates, expose rejection reasons, and build grounded comments."""
    parsed = DiffParser.parse_diff(response.reviewed_diff or "")
    changed = {
        (file.target_file, line): content
        for file in parsed.files
        for line, content in DiffParser.extract_added_lines_with_numbers(file)
    }
    seen = set()
    comments = []
    for finding in response.findings:
        reasons = []
        location = (finding.file_path, finding.line_number)
        code = changed.get(location, "").strip()
        if location not in changed:
            reasons.append("NOT_CHANGED_LINE")
        if not finding.fingerprint or not re.fullmatch(
            r"[a-f0-9]{16}", finding.fingerprint
        ):
            reasons.append("INVALID_IDENTITY")
        if finding.fingerprint in seen:
            reasons.append("DUPLICATE_IDENTITY")
        seen.add(finding.fingerprint)
        snippet = finding.snippet.strip()
        if not snippet or not code or code not in snippet:
            reasons.append("UNGROUNDED_EVIDENCE")
        if (
            len(finding.description.strip()) < 20
            or len(finding.fix_recommendation.strip()) < 20
        ):
            reasons.append("NOT_ACTIONABLE")
        if finding.fix_recommendation.strip().lower() in {
            "fix this issue",
            "review this code",
            "improve code quality",
            "review and remediate the flagged code.",
        }:
            reasons.append("NOT_ACTIONABLE")
        if not response.analysis_complete:
            reasons.append("INCOMPLETE_ANALYSIS")
        if finding.evidence_grade == "REPRODUCED":
            evidence = finding.evidence_provenance
            if not (
                evidence.get("finding_fingerprint") == finding.fingerprint
                and response.analyzed_commit
                and evidence.get("commit_sha") == response.analyzed_commit
                and evidence.get("executed") is True
                and evidence.get("test_id")
                and evidence.get("status") == "REPRODUCED"
            ):
                reasons.append("UNVERIFIED_REPRODUCTION")
        finding.publication_reasons = sorted(set(reasons))
        if reasons:
            if finding.publication_status != "PUBLISHED":
                finding.publication_status = "WITHHELD"
            continue
        if finding.publication_status != "PUBLISHED":
            finding.publication_status = "ELIGIBLE"
        comments.append(
            InlineComment(
                path=finding.file_path,
                line=finding.line_number,
                fingerprint=finding.fingerprint,
                comment_body=f"[{finding.severity}] {finding.description}\n\n"
                f"Evidence ({finding.evidence_grade}): `{snippet}`\n\n"
                f"Suggested action: {finding.fix_recommendation}\n\n`{finding.fingerprint}`",
            )
        )
    response.inline_comments = comments
    response.analyzer_status["finding_policy"] = "COMPLETED"
    return response


def advisory_summary(response: ReviewAPIResponse, fingerprints: set[str]) -> str:
    """Never copy unfiltered model narrative into a provider review."""
    lines = [
        "## Advisory code review",
        "",
        f"Analyzed commit: `{response.analyzed_commit}`",
        f"Analysis: {'complete' if response.analysis_complete else 'incomplete; manual review required'}.",
        "Human reviewers retain merge authority.",
        "",
    ]
    for finding in response.findings:
        if finding.fingerprint in fingerprints and not finding.publication_reasons:
            lines.append(
                f"- `{finding.fingerprint}` [{finding.severity}] "
                f"{finding.file_path}:{finding.line_number}: {finding.description}\n"
                f"  Evidence ({finding.evidence_grade}): `{finding.snippet}`\n"
                f"  Action: {finding.fix_recommendation}"
            )
    lines.append(
        f"\n{len(fingerprints)} eligible finding(s); other candidates are retained in the analysis audit."
    )
    return "\n".join(lines)
