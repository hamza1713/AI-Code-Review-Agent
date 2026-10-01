"""
Programmatic pytest test suite verifying RepoCalibrationEngine mathematical calculations,
false-positive rates, Cried-Wolf indices, and Markdown table/trust footer generation.
"""

import time

import pytest
from production_fixtures import recorded_finding

from code_review_agent.calibration.engine import (
    RepoCalibrationEngine,
    compute_slice_metrics,
)
from code_review_agent.calibration.outcome_store import (
    CATEGORY_GOVERNANCE,
    CATEGORY_QUALITY,
    CATEGORY_SECURITY,
    CONFIRMED_REAL,
    FALSE_POSITIVE,
    HEURISTIC,
    IGNORED_MERGED,
    PENDING,
    REPRODUCED,
    FindingOutcomeStore,
)


@pytest.fixture
def temp_db(tmp_path):
    return str(tmp_path / "test_engine.db")


@pytest.fixture
def store(temp_db):
    return FindingOutcomeStore(db_path=temp_db)


@pytest.fixture
def engine(store):
    return RepoCalibrationEngine(store=store)


class TestMathematicalCalculations:
    """Verifies FPR = FP / (TP + FP) and Cried-Wolf formulas across edge cases."""

    def test_empty_findings_zero_division_guard(self):
        """When 0 findings exist, FPR and CWI must cleanly return 0.0."""
        slice_m = compute_slice_metrics([])
        assert slice_m.total == 0
        assert slice_m.confirmed_real == 0
        assert slice_m.false_positives == 0
        assert slice_m.false_positive_rate == 0.0
        assert slice_m.cried_wolf_index == 0.0
        assert slice_m.noise_burden_rate == 0.0

    def test_only_pending_or_ignored_zero_division_guard(self):
        """When findings exist but none are triaged (TP+FP == 0), FPR must be 0.0."""
        findings = [
            {"outcome": PENDING},
            {"outcome": PENDING},
            {"outcome": IGNORED_MERGED},
        ]
        slice_m = compute_slice_metrics(findings)
        assert slice_m.total == 3
        assert slice_m.pending == 2
        assert slice_m.ignored_merged == 1
        assert slice_m.false_positive_rate == 0.0
        assert slice_m.cried_wolf_index == 0.0
        # Noise burden is 1 ignored out of 3 total = 0.3333
        assert slice_m.noise_burden_rate == pytest.approx(0.3333, abs=1e-4)

    def test_pure_true_positives(self):
        """All triaged findings are genuine defects: FPR = 0.0."""
        findings = [{"outcome": CONFIRMED_REAL} for _ in range(8)]
        slice_m = compute_slice_metrics(findings)
        assert slice_m.confirmed_real == 8
        assert slice_m.false_positives == 0
        assert slice_m.false_positive_rate == 0.0
        assert slice_m.cried_wolf_index == 0.0

    def test_pure_false_positives(self):
        """All triaged findings are false alarms: FPR = 1.0 (100%)."""
        findings = [{"outcome": FALSE_POSITIVE} for _ in range(5)]
        slice_m = compute_slice_metrics(findings)
        assert slice_m.confirmed_real == 0
        assert slice_m.false_positives == 5
        assert slice_m.false_positive_rate == 1.0
        assert slice_m.cried_wolf_index == 1.0

    def test_mixed_triaged_ratio(self):
        """7 TP and 3 FP: FPR = 3 / (7 + 3) = 0.30 (30%)."""
        findings = (
            [{"outcome": CONFIRMED_REAL} for _ in range(7)]
            + [{"outcome": FALSE_POSITIVE} for _ in range(3)]
            + [{"outcome": IGNORED_MERGED} for _ in range(2)]
            + [{"outcome": PENDING} for _ in range(1)]
        )
        slice_m = compute_slice_metrics(findings)
        assert slice_m.total == 13
        assert slice_m.confirmed_real == 7
        assert slice_m.false_positives == 3
        assert slice_m.ignored_merged == 2
        assert slice_m.pending == 1
        assert slice_m.false_positive_rate == 0.3
        assert slice_m.cried_wolf_index == 0.3
        # Noise burden = (3 FP + 2 Ignored) / 13 = 5/13 = ~0.3846
        assert slice_m.noise_burden_rate == pytest.approx(5 / 13, abs=1e-4)


class TestCategoryAndEvidenceGradeAggregation:
    """Verifies per-repository aggregation broken down by Category and Evidence Grade."""

    def test_category_breakdown(self, store, engine):
        repo = "acme/backend"
        now = time.time()

        # Security: 4 TP, 0 FP (FPR = 0%)
        for i in range(4):
            recorded_finding(
                store,
                repo,
                f"sec_{i}",
                category=CATEGORY_SECURITY,
                outcome=CONFIRMED_REAL,
                created_at=now,
            )

        # Quality: 2 TP, 2 FP (FPR = 50%)
        for i in range(2):
            recorded_finding(
                store,
                repo,
                f"qual_tp_{i}",
                category=CATEGORY_QUALITY,
                outcome=CONFIRMED_REAL,
                created_at=now,
            )
            recorded_finding(
                store,
                repo,
                f"qual_fp_{i}",
                category=CATEGORY_QUALITY,
                outcome=FALSE_POSITIVE,
                created_at=now,
            )

        # Governance: 1 TP, 0 FP
        recorded_finding(
            store,
            repo,
            "gov_1",
            category=CATEGORY_GOVERNANCE,
            outcome=CONFIRMED_REAL,
            created_at=now,
        )

        metrics = engine.get_metrics(repo_id=repo, window_days=30)
        assert metrics.total_findings == 9
        assert metrics.confirmed_real == 7
        assert metrics.false_positives == 2
        assert metrics.false_positive_rate == pytest.approx(2 / 9, abs=1e-4)

        sec_m = metrics.by_category[CATEGORY_SECURITY]
        assert sec_m.total == 4
        assert sec_m.false_positive_rate == 0.0

        qual_m = metrics.by_category[CATEGORY_QUALITY]
        assert qual_m.total == 4
        assert qual_m.false_positive_rate == 0.50

        gov_m = metrics.by_category[CATEGORY_GOVERNANCE]
        assert gov_m.total == 1
        assert gov_m.false_positive_rate == 0.0

    def test_evidence_grade_breakdown(self, store, engine):
        """Empirically reproduced defects should have distinctly lower FPR than heuristics."""
        repo = "acme/evidence_repo"
        now = time.time()

        # REPRODUCED (sandbox proven): 5 TP, 0 FP -> FPR 0%
        for i in range(5):
            recorded_finding(
                store,
                repo,
                f"rep_{i}",
                evidence_grade=REPRODUCED,
                outcome=CONFIRMED_REAL,
                created_at=now,
            )

        # HEURISTIC (regex/pattern): 3 TP, 3 FP -> FPR 50%
        for i in range(3):
            recorded_finding(
                store,
                repo,
                f"heur_tp_{i}",
                evidence_grade=HEURISTIC,
                outcome=CONFIRMED_REAL,
                created_at=now,
            )
            recorded_finding(
                store,
                repo,
                f"heur_fp_{i}",
                evidence_grade=HEURISTIC,
                outcome=FALSE_POSITIVE,
                created_at=now,
            )

        metrics = engine.get_metrics(repo_id=repo, window_days=30)

        rep_m = metrics.by_evidence_grade[REPRODUCED]
        assert rep_m.total == 5
        assert rep_m.confirmed_real == 5
        assert rep_m.false_positives == 0
        assert rep_m.false_positive_rate == 0.0

        heur_m = metrics.by_evidence_grade[HEURISTIC]
        assert heur_m.total == 6
        assert heur_m.confirmed_real == 3
        assert heur_m.false_positives == 3
        assert heur_m.false_positive_rate == 0.5


class TestMarkdownReportAndTrustFooter:
    """Verifies generation of Markdown tables, Cried-Wolf cards, and review trust footers."""

    def test_generate_markdown_report_formatting(self, store, engine):
        repo = "corp/web-app"
        now = time.time()

        recorded_finding(
            store,
            repo,
            "sec1",
            category=CATEGORY_SECURITY,
            evidence_grade=REPRODUCED,
            outcome=CONFIRMED_REAL,
            created_at=now,
        )
        recorded_finding(
            store,
            repo,
            "qual1",
            category=CATEGORY_QUALITY,
            evidence_grade=HEURISTIC,
            outcome=FALSE_POSITIVE,
            created_at=now,
        )

        metrics = engine.get_metrics(repo_id=repo, window_days=30)
        report = engine.generate_markdown_report(metrics)

        assert "## 🐺 Repository Calibration & Cried-Wolf Telemetry" in report
        assert "`corp/web-app`" in report
        assert "False-Positive Rate (FPR)" in report
        assert "50.0%" in report
        assert "| Category | Total |" in report
        assert "`SECURITY`" in report
        assert "`QUALITY`" in report
        assert "| `REPRODUCED` |" in report
        assert "| `HEURISTIC` |" in report
        assert "/fp <fingerprint>" in report
        assert "`/confirm <fingerprint>`" in report

    def test_generate_trust_footer_with_triaged_findings(self, store, engine):
        repo = "corp/api-gateway"
        now = time.time()

        recorded_finding(
            store,
            repo,
            "f1",
            evidence_grade=REPRODUCED,
            outcome=CONFIRMED_REAL,
            created_at=now,
        )
        recorded_finding(
            store,
            repo,
            "f2",
            evidence_grade=HEURISTIC,
            outcome=FALSE_POSITIVE,
            created_at=now,
        )
        recorded_finding(
            store,
            repo,
            "f3",
            evidence_grade=HEURISTIC,
            outcome=CONFIRMED_REAL,
            created_at=now,
        )

        footer = engine.generate_trust_footer(repo_id=repo, window_days=30)
        assert "📊 **Repository Trust & Calibration (`corp/api-gateway`)" in footer
        assert "Historical False-Positive Rate:" in footer
        assert "33.3%" in footer  # 1 FP out of 3 triaged = 33.3%
        assert "Reproduced: **0.0%**" in footer
        assert "Heuristic: **50.0%**" in footer
        assert "`/fp <fingerprint>`" in footer

    def test_generate_trust_footer_empty_repo_graceful_message(self, store, engine):
        repo = "corp/brand-new-repo"
        footer = engine.generate_trust_footer(repo_id=repo, window_days=30)
        assert "No triaged review findings recorded yet" in footer
        assert "Calibrate with `/fp <fingerprint>`" in footer
