"""
Programmatic pytest test suite verifying FindingOutcomeStore persistence,
ACID transactions, lifecycle updates, and time-window queries.
"""

import concurrent.futures
from pathlib import Path

import pytest
from production_fixtures import recorded_finding

from code_review_agent.calibration.outcome_store import (
    CATEGORY_ARCHITECTURE,
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
    clean_fingerprint,
    normalize_category,
    normalize_evidence_grade,
)


@pytest.fixture
def temp_db(tmp_path):
    """Provide a fresh isolated SQLite database for each test."""
    db_file = tmp_path / "test_calibration.db"
    return str(db_file)


@pytest.fixture
def store(temp_db):
    """Instantiate a clean FindingOutcomeStore with isolated database."""
    return FindingOutcomeStore(db_path=temp_db)


class TestFindingOutcomeStorePersistence:
    """Tests for SQLite persistence, table schemas, and idempotency."""

    def test_database_initialization_and_wal_mode(self, store, temp_db):
        """Verify database file creation, table existence, and WAL pragma configuration."""
        assert Path(temp_db).exists()
        conn = store._get_connection()
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode;")
        journal_mode = cursor.fetchone()[0]
        assert journal_mode.upper() == "WAL"

        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='finding_outcomes';"
        )
        assert cursor.fetchone() is not None

    def test_record_single_finding(self, store):
        """Verify recording a new review finding persists all attributes correctly."""
        record = recorded_finding(
            store,
            repo_id="owner/repo",
            fingerprint="abc12345def67890",
            category="SECURITY",
            evidence_grade="REPRODUCED",
            severity="CRITICAL",
            pr_id="42",
            outcome=PENDING,
            reason="Initial detection in SQL query",
            source="sast_scan",
        )
        assert record is not None
        assert record["repo_id"] == "owner/repo"
        assert record["fingerprint"] == "abc12345def67890"
        assert record["category"] == CATEGORY_SECURITY
        assert record["evidence_grade"] == REPRODUCED
        assert record["severity"] == "CRITICAL"
        assert record["outcome"] == PENDING
        assert record["pr_id"] == "42"

        # Verify retrieval
        fetched = store.get_finding("owner/repo", "abc12345def67890")
        assert fetched == record

    def test_category_and_grade_normalization(self, store):
        """Verify various raw category and evidence inputs map cleanly to canonical enums."""
        assert normalize_category("SQL_INJECTION") == CATEGORY_SECURITY
        assert normalize_category("code_style") == CATEGORY_QUALITY
        assert normalize_category("compliance_rule") == CATEGORY_GOVERNANCE
        assert normalize_category("circular_dependency") == CATEGORY_ARCHITECTURE
        assert normalize_category(None) == CATEGORY_QUALITY

        assert normalize_evidence_grade("empirical_original_passed") == HEURISTIC
        assert normalize_evidence_grade("heuristic_regex") == HEURISTIC
        assert normalize_evidence_grade(None) == HEURISTIC

        rec = recorded_finding(
            store,
            repo_id="corp/service",
            fingerprint="norm1234567890ab",
            category="custom_policy",
            evidence_grade="heuristic",
        )
        assert rec["category"] == CATEGORY_GOVERNANCE
        assert rec["evidence_grade"] == HEURISTIC

    def test_clean_fingerprint_normalization(self, store):
        """Verify fingerprint cleaning strips markdown backticks, hashes, and colons."""
        assert clean_fingerprint("`9a4b2f1e`") == "9a4b2f1e"
        assert clean_fingerprint("#9a4b2f1e") == "9a4b2f1e"
        assert clean_fingerprint("9a4b2f1e:") == "9a4b2f1e"
        assert clean_fingerprint('"9a4b2f1e"') == "9a4b2f1e"
        assert clean_fingerprint("  `9A4B2F1E`  ") == "9a4b2f1e"
        assert clean_fingerprint(None) == ""

        # Storing with backticks retrieves with clean hex
        rec = recorded_finding(
            store,
            repo_id="corp/service",
            fingerprint="`ab12cd34ef567890`",
        )
        assert rec["fingerprint"] == "ab12cd34ef567890"
        assert store.get_finding("corp/service", "ab12cd34ef567890") is not None
        assert store.get_finding("corp/service", "`ab12cd34ef567890`") is not None

    def test_record_findings_batch(self, store):
        """Verify atomic batch recording of multiple findings."""
        findings = [
            {
                "fingerprint": f"batch_{i:04d}",
                "category": "SECURITY",
                "severity": "HIGH",
            }
            for i in range(5)
        ]
        inserted = store.record_findings_batch("owner/batch_repo", findings, pr_id="10")
        assert inserted == 5

        listed = store.list_findings(repo_id="owner/batch_repo")
        assert len(listed) == 5

    def test_idempotent_recording_preserves_triaged_outcome(self, store):
        """Verify re-running review does not revert a CONFIRMED_REAL or FALSE_POSITIVE finding to PENDING."""
        repo = "owner/idempotent_repo"
        fp = "idem1234567890ab"

        # Initially record as PENDING
        recorded_finding(store, repo_id=repo, fingerprint=fp, outcome=PENDING)
        assert store.get_finding(repo, fp)["outcome"] == PENDING

        # Developer flags as FALSE_POSITIVE
        store.update_outcome(
            repo_id=repo, fingerprint=fp, outcome=FALSE_POSITIVE, reason="Not an issue"
        )
        assert store.get_finding(repo, fp)["outcome"] == FALSE_POSITIVE

        # Re-running review records the finding again with default PENDING
        recorded_finding(store, repo_id=repo, fingerprint=fp, outcome=PENDING)
        # Outcome MUST remain FALSE_POSITIVE
        assert store.get_finding(repo, fp)["outcome"] == FALSE_POSITIVE


class TestOutcomeLifecycleAndUpdates:
    """Tests for lifecycle transitions and developer feedback updates."""

    def test_update_to_all_valid_outcomes(self, store):
        """Verify updating through all recognized outcome states."""
        repo = "acme/api"
        fp = "life1234567890ab"

        recorded_finding(store, repo_id=repo, fingerprint=fp)
        for outcome in [CONFIRMED_REAL, FALSE_POSITIVE, IGNORED_MERGED, PENDING]:
            res = store.update_outcome(repo_id=repo, fingerprint=fp, outcome=outcome)
            assert res["outcome"] == outcome
            assert store.get_finding(repo, fp)["outcome"] == outcome

    def test_update_invalid_outcome_raises_error(self, store):
        """Verify invalid outcome strings are rejected with ValueError."""
        repo = "acme/api"
        fp = "inv1234567890abc"
        recorded_finding(store, repo_id=repo, fingerprint=fp)

        with pytest.raises(ValueError, match="Invalid outcome"):
            store.update_outcome(repo_id=repo, fingerprint=fp, outcome="INVALID_STATUS")

    def test_feedback_rejects_unknown_occurrences(self, store):
        from code_review_agent.calibration.persistence import FindingNotFoundError

        with pytest.raises(FindingNotFoundError):
            store.update_outcome("acme/api", "brand_new_fp1234", outcome=FALSE_POSITIVE)
        assert store.get_finding("acme/api", "brand_new_fp1234") is None

    def test_mark_pr_pending_as_ignored_on_merge(self, store):
        """Verify PR merge transitions only PENDING findings to IGNORED_MERGED."""
        repo = "acme/merge_repo"
        pr = "99"

        # Finding 1: PENDING
        recorded_finding(
            store,
            repo_id=repo,
            fingerprint="fp_pend_00000001",
            pr_id=pr,
            outcome=PENDING,
        )
        # Finding 2: CONFIRMED_REAL
        recorded_finding(
            store,
            repo_id=repo,
            fingerprint="fp_conf_00000002",
            pr_id=pr,
            outcome=CONFIRMED_REAL,
        )
        # Finding 3: FALSE_POSITIVE
        recorded_finding(
            store,
            repo_id=repo,
            fingerprint="fp_fpos_00000003",
            pr_id=pr,
            outcome=FALSE_POSITIVE,
        )

        affected = store.mark_pr_pending_as_ignored(repo_id=repo, pr_id=pr)
        assert affected == 1

        f1 = store.get_finding(repo, "fp_pend_00000001")
        f2 = store.get_finding(repo, "fp_conf_00000002")
        f3 = store.get_finding(repo, "fp_fpos_00000003")

        assert f1["outcome"] == IGNORED_MERGED
        assert f2["outcome"] == CONFIRMED_REAL
        assert f3["outcome"] == FALSE_POSITIVE


class TestACIDTransactions:
    """Tests guaranteeing ACID atomicity and rollback on exceptions."""

    def test_transaction_commits_on_success(self, store):
        """Verify explicit transaction block commits mutations when no error occurs."""
        repo = "acid/test_repo"
        fp = "acid_commit_0001"

        with store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO finding_outcomes (
                    repo_id, fingerprint, category, evidence_grade,
                    severity, outcome, created_at, updated_at, finding_id, last_seen_at
                ) VALUES (?, ?, 'SECURITY', 'REPRODUCED', 'HIGH', 'CONFIRMED_REAL', 1000.0, 1000.0, 'acid-fixture', 1000.0)
            """,
                (repo, fp),
            )

        fetched = store.get_finding(repo, fp)
        assert fetched is not None
        assert fetched["outcome"] == CONFIRMED_REAL

    def test_transaction_rolls_back_on_exception(self, store):
        """Verify explicit transaction block automatically rolls back when an exception is raised."""
        repo = "acid/test_repo"
        fp1 = "acid_rollback_01"
        fp2 = "acid_rollback_02"

        # Record fp1 successfully first
        recorded_finding(store, repo_id=repo, fingerprint=fp1, outcome=PENDING)
        assert store.get_finding(repo, fp1) is not None

        # Attempt to insert fp2 and mutate fp1 in a failing transaction
        with pytest.raises(RuntimeError, match="Simulated crash during write"):
            with store.transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO finding_outcomes (
                        repo_id, fingerprint, category, evidence_grade,
                        severity, outcome, created_at, updated_at, finding_id, last_seen_at
                    ) VALUES (?, ?, 'QUALITY', 'HEURISTIC', 'LOW', 'PENDING', 2000.0, 2000.0, 'acid-fixture-2', 2000.0)
                """,
                    (repo, fp2),
                )
                # Mutate fp1
                conn.execute(
                    """
                    UPDATE finding_outcomes SET outcome = 'FALSE_POSITIVE' WHERE fingerprint = ?
                """,
                    (fp1,),
                )
                # Trigger failure
                raise RuntimeError("Simulated crash during write")

        # fp2 must NOT exist
        assert store.get_finding(repo, fp2) is None
        # fp1 mutation must have rolled back to PENDING
        assert store.get_finding(repo, fp1)["outcome"] == PENDING

    def test_nested_transactions_savepoint_success(self, store):
        """Verify nested store.transaction() blocks succeed using savepoints without crashing."""
        repo = "nest/repo"
        fp1 = "nested_0001"
        fp2 = "nested_0002"

        with store.transaction():
            # record_finding internally opens its own with self.transaction()
            recorded_finding(store, repo_id=repo, fingerprint=fp1, outcome=PENDING)
            with store.transaction():
                recorded_finding(store, repo_id=repo, fingerprint=fp2, outcome=PENDING)
                store.update_outcome(
                    repo_id=repo, fingerprint=fp2, outcome=CONFIRMED_REAL
                )

        f1 = store.get_finding(repo, fp1)
        f2 = store.get_finding(repo, fp2)
        assert f1 is not None and f1["outcome"] == PENDING
        assert f2 is not None and f2["outcome"] == CONFIRMED_REAL

    def test_nested_transaction_savepoint_rollback(self, store):
        """Verify inner transaction rollback preserves outer transaction mutations."""
        repo = "nest/rollback_repo"
        fp_outer = "outer_success"
        fp_inner = "inner_failure"

        with store.transaction():
            recorded_finding(store, repo_id=repo, fingerprint=fp_outer, outcome=PENDING)
            try:
                with store.transaction():
                    recorded_finding(
                        store, repo_id=repo, fingerprint=fp_inner, outcome=PENDING
                    )
                    raise ValueError("Simulated inner error")
            except ValueError:
                pass  # Inner rolled back, outer continues

            store.update_outcome(
                repo_id=repo, fingerprint=fp_outer, outcome=CONFIRMED_REAL
            )

        # Outer must exist and be committed
        assert store.get_finding(repo, fp_outer)["outcome"] == CONFIRMED_REAL
        # Inner must have rolled back
        assert store.get_finding(repo, fp_inner) is None

    def test_high_concurrency_stress_writes(self, temp_db):
        """Verify 60 concurrent threads simultaneously writing to FindingOutcomeStore without lock failures."""
        num_threads = 60
        store = FindingOutcomeStore(db_path=temp_db)

        def worker(thread_idx: int):
            t_store = FindingOutcomeStore(db_path=temp_db)
            repo = f"stress/repo_{thread_idx % 3}"
            fp = f"stress_fp_{thread_idx:04d}"
            t_store.record_finding(repo_id=repo, fingerprint=fp, outcome=PENDING)
            t_store.update_outcome(
                repo_id=repo,
                fingerprint=fp,
                outcome=CONFIRMED_REAL,
                source=f"thread_{thread_idx}",
            )
            return fp

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(worker, i) for i in range(num_threads)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        assert len(results) == num_threads
        all_findings = store.list_findings()
        assert len(all_findings) == num_threads
        assert all(f["outcome"] == CONFIRMED_REAL for f in all_findings)


class TestTimeWindowAndFilteringQueries:
    """Tests querying rolling 30-day and custom time-window metrics."""

    def test_filter_by_time_window(self, store):
        """Verify list_findings correctly bounds results by start_time and end_time."""
        repo = "time/test_repo"
        t0 = 1_000_000.0

        recorded_finding(store, repo_id=repo, fingerprint="fp_old", created_at=t0 - 100)
        recorded_finding(
            store, repo_id=repo, fingerprint="fp_in_window_1", created_at=t0 + 50
        )
        recorded_finding(
            store, repo_id=repo, fingerprint="fp_in_window_2", created_at=t0 + 150
        )
        recorded_finding(
            store, repo_id=repo, fingerprint="fp_future", created_at=t0 + 500
        )

        results = store.list_findings(repo_id=repo, start_time=t0, end_time=t0 + 200)
        fps = {r["fingerprint"] for r in results}
        assert fps == {"fp_in_window_1", "fp_in_window_2"}

    def test_filter_by_dimensions(self, store):
        """Verify filtering by category, evidence grade, and outcome."""
        repo = "dim/test_repo"
        recorded_finding(
            store,
            repo_id=repo,
            fingerprint="f1",
            category="SECURITY",
            evidence_grade="REPRODUCED",
            outcome=CONFIRMED_REAL,
        )
        recorded_finding(
            store,
            repo_id=repo,
            fingerprint="f2",
            category="SECURITY",
            evidence_grade="HEURISTIC",
            outcome=FALSE_POSITIVE,
        )
        recorded_finding(
            store,
            repo_id=repo,
            fingerprint="f3",
            category="QUALITY",
            evidence_grade="HEURISTIC",
            outcome=PENDING,
        )

        sec_findings = store.list_findings(repo_id=repo, category="SECURITY")
        assert len(sec_findings) == 2

        rep_findings = store.list_findings(repo_id=repo, evidence_grade="REPRODUCED")
        assert len(rep_findings) == 1
        assert rep_findings[0]["fingerprint"] == "f1"

        fp_findings = store.list_findings(repo_id=repo, outcome=FALSE_POSITIVE)
        assert len(fp_findings) == 1
        assert fp_findings[0]["fingerprint"] == "f2"
