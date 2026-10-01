"""
Tests for Stratified Human Audit Sampling of REPRODUCED Findings.
Verifies stratified proportional allocation across (Category × Severity) strata,
deterministic random seeding, minimum 1 per stratum, and unaudited filtering.
"""

import pytest
from production_fixtures import recorded_finding

from code_review_agent.calibration.audit_sampler import AuditSampler
from code_review_agent.calibration.outcome_store import (
    FindingOutcomeStore,
)


@pytest.fixture
def clean_db(tmp_path, monkeypatch):
    """Provide an isolated SQLite database for audit sampling tests."""
    db_file = tmp_path / "test_audit_sampling.db"
    monkeypatch.setenv("QUEUE_DB_PATH", str(db_file))
    store = FindingOutcomeStore(db_path=str(db_file))
    return store


class TestAuditSamplerStratification:
    """Verifies stratified proportional sampling algorithm."""

    def test_empty_repo_sampling_returns_empty_list(self, clean_db):
        sampler = AuditSampler(clean_db)
        sample = sampler.sample_for_audit("empty/repo", sample_size=10)
        assert sample == []

    def test_only_reproduced_findings_are_sampled(self, clean_db):
        repo = "acme/backend"
        # Seed HEURISTIC findings
        recorded_finding(
            clean_db,
            repo,
            "h1",
            category="SECURITY",
            evidence_grade="HEURISTIC",
            severity="HIGH",
        )
        recorded_finding(
            clean_db,
            repo,
            "h2",
            category="QUALITY",
            evidence_grade="HEURISTIC",
            severity="MEDIUM",
        )

        # Seed 1 REPRODUCED finding
        recorded_finding(
            clean_db,
            repo,
            "r1",
            category="SECURITY",
            evidence_grade="REPRODUCED",
            severity="CRITICAL",
        )

        sampler = AuditSampler(clean_db)
        sample = sampler.sample_for_audit(repo, sample_size=5)
        assert len(sample) == 1
        assert sample[0]["fingerprint"] == "r1"
        assert sample[0]["evidence_grade"] == "REPRODUCED"

    def test_stratified_proportional_allocation_and_minimum_one(self, clean_db):
        repo = "acme/microservice"

        # Strata:
        # (SECURITY, HIGH): 10 findings
        # (QUALITY, MEDIUM): 10 findings
        # (ARCHITECTURE, LOW): 1 finding
        for i in range(10):
            recorded_finding(
                clean_db,
                repo,
                f"sec_{i}",
                category="SECURITY",
                evidence_grade="REPRODUCED",
                severity="HIGH",
            )
            recorded_finding(
                clean_db,
                repo,
                f"qual_{i}",
                category="QUALITY",
                evidence_grade="REPRODUCED",
                severity="MEDIUM",
            )
        recorded_finding(
            clean_db,
            repo,
            "arch_single",
            category="ARCHITECTURE",
            evidence_grade="REPRODUCED",
            severity="LOW",
        )

        sampler = AuditSampler(clean_db)
        # Sample 5 items total with seed
        sample = sampler.sample_for_audit(repo, sample_size=5, seed=42)
        assert len(sample) == 5

        sampled_fps = [s["fingerprint"] for s in sample]
        # Verify single item in sparse stratum was sampled due to minimum 1 per non-empty stratum
        assert "arch_single" in sampled_fps

        # Verify representations from other strata
        sec_sampled = [s for s in sample if s["category"] == "SECURITY"]
        qual_sampled = [s for s in sample if s["category"] == "QUALITY"]
        arch_sampled = [s for s in sample if s["category"] == "ARCHITECTURE"]

        assert len(arch_sampled) == 1
        assert len(sec_sampled) >= 1
        assert len(qual_sampled) >= 1
        assert len(sec_sampled) + len(qual_sampled) + len(arch_sampled) == 5

    def test_deterministic_seed_reproducibility(self, clean_db):
        repo = "acme/payments"
        for i in range(30):
            cat = "SECURITY" if i % 2 == 0 else "QUALITY"
            sev = "HIGH" if i % 3 == 0 else "MEDIUM"
            recorded_finding(
                clean_db,
                repo,
                f"repro_{i}",
                category=cat,
                evidence_grade="REPRODUCED",
                severity=sev,
            )

        sampler = AuditSampler(clean_db)
        sample1 = sampler.sample_for_audit(repo, sample_size=10, seed=12345)
        sample2 = sampler.sample_for_audit(repo, sample_size=10, seed=12345)
        sample3 = sampler.sample_for_audit(repo, sample_size=10, seed=99999)

        fps1 = [s["fingerprint"] for s in sample1]
        fps2 = [s["fingerprint"] for s in sample2]
        fps3 = [s["fingerprint"] for s in sample3]

        assert fps1 == fps2
        # Different seeds yield different orders/subsets
        assert fps1 != fps3

    def test_unaudited_only_filtering(self, clean_db):
        repo = "acme/auth"
        # Seed 3 findings
        recorded_finding(
            clean_db,
            repo,
            "fp_unaudited",
            category="SECURITY",
            evidence_grade="REPRODUCED",
            severity="HIGH",
        )
        recorded_finding(
            clean_db,
            repo,
            "fp_valid",
            category="SECURITY",
            evidence_grade="REPRODUCED",
            severity="HIGH",
        )
        recorded_finding(
            clean_db,
            repo,
            "fp_invalid",
            category="QUALITY",
            evidence_grade="REPRODUCED",
            severity="MEDIUM",
        )

        # Mark 2 as audited
        clean_db.record_human_audit(repo, "fp_valid", is_valid=True)
        clean_db.record_human_audit(repo, "fp_invalid", is_valid=False)

        sampler = AuditSampler(clean_db)

        # 1. Unaudited only (default)
        sample_unaudited = sampler.sample_for_audit(
            repo, sample_size=10, unaudited_only=True
        )
        assert len(sample_unaudited) == 1
        assert sample_unaudited[0]["fingerprint"] == "fp_unaudited"

        # 2. All findings (including already audited)
        sample_all = sampler.sample_for_audit(
            repo, sample_size=10, unaudited_only=False
        )
        assert len(sample_all) == 3

    def test_sample_size_exceeding_total_returns_all(self, clean_db):
        repo = "acme/small"
        recorded_finding(
            clean_db,
            repo,
            "s1",
            category="SECURITY",
            evidence_grade="REPRODUCED",
            severity="HIGH",
        )
        recorded_finding(
            clean_db,
            repo,
            "s2",
            category="QUALITY",
            evidence_grade="REPRODUCED",
            severity="LOW",
        )

        sampler = AuditSampler(clean_db)
        sample = sampler.sample_for_audit(repo, sample_size=50)
        assert len(sample) == 2
