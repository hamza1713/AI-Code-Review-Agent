"""
Unit tests for the deterministic final-synthesis reconciler.

Organized by the 7 contract rules: dedup+count, one-severity-per-defect, honest test
badges, governance production-scoping, bounded non-saturating score, CWE correction, and
the coverage/limitations note. Every report is also run through `self_check()`.
"""

from code_review_agent.models import SastFinding, RuleViolation
from code_review_agent.models import TestExecutionResult as ExecResult  # aliased: avoids pytest collecting it
from code_review_agent.synthesis.reconciler import SynthesisReconciler


def _sast(rule_id, cwe, sev, file, line, desc="", snippet="", src="regex", fix="fix it"):
    return SastFinding(
        rule_id=rule_id, cwe=cwe, description=desc or rule_id, severity=sev,
        file_path=file, line_number=line, snippet=snippet, fix_recommendation=fix, analyzer_source=src,
    )


def _gov(rule_id, sev, file, line, desc="violation", fix="fix"):
    return RuleViolation(rule_id=rule_id, rule_name=rule_id, severity=sev,
                         file_path=file, line_number=line, description=desc, suggested_fix=fix)


# ── Rule 1: dedup before counting ───────────────────────────────────────────
class TestDeduplication:
    def test_same_defect_across_analyzers_and_adjacent_lines_merges(self):
        # pickle: custom rule + Bandit B301 (call) + B403 (import, adjacent line) → ONE defect.
        findings = [
            _sast("B301", "CWE-502", "MEDIUM", "app/io.py", 20, "pickle.loads", "pickle.loads(x)", "bandit"),
            _sast("B403", "CWE-502", "LOW", "app/io.py", 18, "import pickle", "import pickle", "bandit"),
            _sast("regex-pickle", "CWE-502", "HIGH", "app/io.py", 20, "insecure deserialization", "pickle.loads(x)", "regex"),
        ]
        report = SynthesisReconciler.reconcile(sast_findings=findings)
        assert report.findings_count == 1
        assert report.findings_count == len(report.findings)
        assert len(report.findings[0].sources) >= 2
        assert report.self_check() == []

    def test_distinct_defects_not_merged(self):
        findings = [
            _sast("a", "CWE-89", "HIGH", "a.py", 1, "sqli"),
            _sast("b", "CWE-327", "MEDIUM", "b.py", 50, "md5"),
        ]
        report = SynthesisReconciler.reconcile(sast_findings=findings)
        assert report.findings_count == 2


# ── Rule 2: one severity per defect, from the rubric ────────────────────────
class TestSeverityReconciliation:
    def test_severity_from_rubric_not_analyzer_copy(self):
        # Analyzers disagree (MEDIUM/LOW/HIGH) but CWE-502 → HIGH by rubric.
        findings = [
            _sast("B301", "CWE-502", "MEDIUM", "io.py", 10),
            _sast("regex", "CWE-502", "LOW", "io.py", 10),
        ]
        report = SynthesisReconciler.reconcile(sast_findings=findings)
        assert report.findings[0].severity == "HIGH"

    def test_sqli_is_critical(self):
        report = SynthesisReconciler.reconcile(sast_findings=[_sast("x", "CWE-89", "HIGH", "a.py", 1)])
        assert report.findings[0].severity == "CRITICAL"


# ── Rule 6: CWE correction ──────────────────────────────────────────────────
class TestCweCorrection:
    def test_eval_mislabeled_as_command_injection_is_corrected(self):
        f = _sast("B307", "CWE-78", "MEDIUM", "x.py", 5, "eval(user_input)", "eval(user_input)", "bandit")
        report = SynthesisReconciler.reconcile(sast_findings=[f])
        assert report.findings[0].cwe == "CWE-95"
        assert report.findings[0].severity == "CRITICAL"


# ── Rule 5: bounded, non-saturating score ───────────────────────────────────
class TestBoundedScore:
    def test_more_defects_lower_score_without_collapsing(self):
        one = SynthesisReconciler.reconcile(sast_findings=[_sast("a", "CWE-89", "HIGH", "a.py", 1)])
        two = SynthesisReconciler.reconcile(sast_findings=[
            _sast("a", "CWE-89", "HIGH", "a.py", 1),
            _sast("b", "CWE-89", "HIGH", "b.py", 9),
        ])
        assert one.score > two.score          # non-saturating: 1 vs 2 are distinguishable
        assert two.score > 0                  # never underflows to 0

    def test_clean_pr_scores_100(self):
        report = SynthesisReconciler.reconcile()
        assert report.score == 100 and report.verdict == "APPROVE" and report.findings_count == 0

    def test_score_terms_trace_to_findings(self):
        report = SynthesisReconciler.reconcile(sast_findings=[_sast("a", "CWE-89", "HIGH", "a.py", 1)])
        # One CRITICAL defect → breakdown mentions its bucket; worst severity CRITICAL.
        assert "CRITICAL" in report.score_breakdown
        assert report.worst_severity == "CRITICAL"


# ── Rule 4: governance scoping + verdict discipline ─────────────────────────
class TestGovernanceScoping:
    def test_style_rule_in_test_file_downgraded_to_info(self):
        report = SynthesisReconciler.reconcile(
            rule_violations=[_gov("gov-no-print-statements", "WARNING", "tests/test_foo.py", 3)]
        )
        assert report.findings[0].severity == "INFO"
        assert report.verdict == "APPROVE"  # low severity never drives the verdict

    def test_style_rule_under_main_guard_downgraded(self):
        pr = '+++ b/app/cli.py\n@@ -1,0 +1,3 @@\n+if __name__ == "__main__":\n+    print("run")\n'
        report = SynthesisReconciler.reconcile(
            rule_violations=[_gov("gov-no-print-statements", "WARNING", "app/cli.py", 2)],
            pr_content=pr,
        )
        assert report.findings[0].severity == "INFO"

    def test_production_style_rule_stays_low_not_critical(self):
        report = SynthesisReconciler.reconcile(
            rule_violations=[_gov("gov-no-print-statements", "WARNING", "app/service.py", 40)]
        )
        assert report.findings[0].severity == "LOW"
        assert report.findings[0].blocking is False
        assert report.verdict == "APPROVE"

    def test_blocking_governance_escalates(self):
        report = SynthesisReconciler.reconcile(
            rule_violations=[_gov("gov-no-raw-sql", "BLOCKING", "app/db.py", 12)]
        )
        assert report.verdict == "ESCALATE"
        assert report.blocking_reasons


# ── Rule 3: honest test-evidence semantics ──────────────────────────────────
class TestTestEvidence:
    def test_passing_confirm_vuln_suite_is_negative(self):
        te = ExecResult(executed=True, status="PASSED", evidence_badge="PASSING", tests_run=1)
        src = "import pytest\n\ndef test_current():\n    with pytest.raises(Exception):\n        f()\n"
        ev = SynthesisReconciler.reconcile(
            sast_findings=[_sast("x", "CWE-89", "HIGH", "a.py", 1)],
            test_execution=te, generated_tests=src,
        ).test_evidence
        assert ev.positive is False
        assert "VULNERABILITY CONFIRMED" in ev.badge

    def test_reproduced_is_negative(self):
        te = ExecResult(executed=True, status="REPRODUCED_DEFECT", evidence_badge="REPRODUCED", failures=1)
        ev = SynthesisReconciler.reconcile(test_execution=te, generated_tests="def t(): pass").test_evidence
        assert ev.positive is False and "VULNERABILITY CONFIRMED" in ev.badge

    def test_postfix_pass_is_positive(self):
        te = ExecResult(executed=True, status="PASSED", evidence_badge="PASSING", tests_run=1)
        src = "def test_x():\n    '''POST-FIX: validates the fix'''\n    assert fixed() == 1\n"
        ev = SynthesisReconciler.reconcile(test_execution=te, generated_tests=src).test_evidence
        assert ev.positive is True and "FIX VERIFIED" in ev.badge

    def test_no_tests_is_heuristic(self):
        ev = SynthesisReconciler.reconcile().test_evidence
        assert ev.kind == "none" and "HEURISTIC" in ev.badge and ev.positive is False


# ── Rule 7 + global self-check ──────────────────────────────────────────────
class TestCoverageAndConsistency:
    def test_limitations_note_is_honest(self):
        note = SynthesisReconciler.reconcile().limitations_note
        assert "taint" in note.lower() and "not proof of safety" in note.lower()

    def test_self_check_passes_on_mixed_report(self):
        report = SynthesisReconciler.reconcile(
            sast_findings=[
                _sast("a", "CWE-89", "HIGH", "a.py", 1),
                _sast("b", "CWE-327", "LOW", "b.py", 5),
            ],
            rule_violations=[_gov("gov-no-print-statements", "WARNING", "app/x.py", 9)],
        )
        assert report.self_check() == []
        # findings_count is enumerable and consistent everywhere.
        assert report.findings_count == len(report.findings)

    def test_markdown_renders_consistently(self):
        report = SynthesisReconciler.reconcile(sast_findings=[_sast("a", "CWE-89", "HIGH", "a.py", 1)])
        md = report.to_markdown()
        assert f"Quality Score**: {report.score}/100" in md
        assert f"Findings**: {report.findings_count}" in md
        assert "Coverage & limitations" in md
