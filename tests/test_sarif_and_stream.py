import json
from code_review_agent.sarif_exporter import SarifExporter
from code_review_agent.models import SastFinding, InlineComment, RuleViolation


def test_sarif_exporter_valid_schema():
    findings = [
        SastFinding(
            rule_id="SEC-SLOPSQUAT-001",
            cwe="CWE-1357",
            description="AI-hallucinated package 'fast-jwt-super-auth' detected",
            severity="CRITICAL",
            file_path="requirements.txt",
            line_number=5,
            snippet="fast-jwt-super-auth==1.0.0",
            fix_recommendation="Verify and replace with established library like pyjwt",
            publication_status="PUBLISHED",
            blocking=True,
            evidence_grade="REPRODUCED",
            evidence_provenance={},
        ),
        SastFinding(
            rule_id="SEC-001",
            cwe="CWE-89",
            description="SQL Injection in user route",
            severity="HIGH",
            file_path="routes/user.py",
            line_number=42,
            snippet="cursor.execute(f'SELECT * FROM users WHERE id = {user_id}')",
            fix_recommendation="Use parameterized SQL query",
            publication_status="PUBLISHED",
            blocking=True,
            evidence_grade="REPRODUCED",
            evidence_provenance={},
        ),
    ]

    inline = [
        InlineComment(
            path="routes/user.py",
            line=42,
            side="RIGHT",
            severity="CRITICAL",
            comment_body="Critical SQL Injection flaw",
            why="Unsanitized string interpolation directly executes arbitrary SQL commands.",
            suggestion_code="cursor.execute('SELECT * FROM users WHERE id = %s', (user_id,))",
        )
    ]

    gov = [
        RuleViolation(
            rule_id="GOV-001",
            rule_name="no-raw-queries",
            severity="BLOCKING",
            file_path="routes/user.py",
            line_number=42,
            description="Direct cursor execute prohibited by team standards",
            suggested_fix="Use the repository pattern",
        )
    ]

    sarif = SarifExporter.build_sarif(
        sast_findings=findings,
        inline_comments=inline,
        governance_violations=gov,
        tool_name="AICodeReviewAgent",
        tool_version="3.0.0",
    )

    assert sarif["version"] == "2.1.0"
    assert sarif["$schema"] == "https://json.schemastore.org/sarif-2.1.0.json"
    run = sarif["runs"][0]
    assert run["tool"]["driver"]["name"] == "AICodeReviewAgent"
    assert run["tool"]["driver"]["semanticVersion"] == "3.0.0"

    # Verify results count: 2 SAST + 1 Gov + 1 Inline = 4 results
    assert len(run["results"]) == 4

    # Verify rule IDs
    rule_ids = [r["ruleId"] for r in run["results"]]
    assert "SEC-SLOPSQUAT-001" in rule_ids
    assert "SEC-001" in rule_ids
    assert "GOV-001" in rule_ids
    assert "AI-REVIEW-CRITICAL" in rule_ids


def test_sarif_exporter_dict_payload_compatibility():
    """Verify SARIF exporter functions when given deserialized JSON dictionaries."""
    dict_findings = [
        {
            "rule_id": "SEC-TROJAN-001",
            "cwe": "CWE-1076",
            "description": "Unicode Bidi override detected",
            "severity": "CRITICAL",
            "file_path": "auth.py",
            "line_number": 12,
            "snippet": "# \\u202e override",
            "fix_recommendation": "Remove invisible characters",
        }
    ]

    sarif = SarifExporter.build_sarif(dict_findings)
    assert sarif["version"] == "2.1.0"
    assert len(sarif["runs"][0]["results"]) == 1
    assert sarif["runs"][0]["results"][0]["ruleId"] == "SEC-TROJAN-001"
    assert sarif["runs"][0]["results"][0]["level"] == "error"
