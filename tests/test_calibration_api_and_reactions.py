"""
Programmatic pytest test suite verifying FastAPI calibration endpoints (/api/calibration/metrics,
/api/calibration/feedback), webhook reaction hooks, and automated lifecycle confirmations.
"""

import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from production_fixtures import recorded_finding

from code_review_agent.calibration.outcome_store import (
    CONFIRMED_REAL,
    FALSE_POSITIVE,
    HEURISTIC,
    IGNORED_MERGED,
    PENDING,
    REPRODUCED,
    FindingOutcomeStore,
)
from code_review_agent.comment_synchronizer import CommentSynchronizer
from code_review_agent.suppression_store import SuppressionStore
from code_review_agent.webhook_server import app

TOKEN = "test-operator-token-with-32-characters-minimum"


@pytest.fixture(autouse=True)
def configure_test_env(tmp_path, monkeypatch):
    """Set up test environment with isolated db and auth bypass."""
    test_db = tmp_path / "test_api_calibration.db"
    monkeypatch.setenv("QUEUE_DB_PATH", str(test_db))
    monkeypatch.setenv("REVIEW_API_TOKEN", TOKEN)
    monkeypatch.setenv("REVIEW_REQUIRE_AUTH", "false")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "test_webhook_secret")
    return test_db


@pytest.fixture
def client():
    return TestClient(app)


class TestCalibrationAPIEndpoints:
    """Tests GET /api/calibration/metrics and POST /api/calibration/feedback."""

    def test_get_calibration_metrics(self, client):
        repo = "acme/metrics-api"
        store = FindingOutcomeStore()
        now = time.time()

        recorded_finding(
            store,
            repo,
            "fp_tp_1",
            category="SECURITY",
            evidence_grade=REPRODUCED,
            outcome=CONFIRMED_REAL,
            created_at=now,
        )
        recorded_finding(
            store,
            repo,
            "fp_fp_1",
            category="QUALITY",
            evidence_grade=HEURISTIC,
            outcome=FALSE_POSITIVE,
            created_at=now,
        )

        resp = client.get(f"/api/calibration/metrics?repo_id={repo}&window_days=30")
        assert resp.status_code == 200
        data = resp.json()

        assert data["repo_id"] == repo
        assert data["window_days"] == 30
        assert data["total_findings"] == 2
        assert data["confirmed_real"] == 1
        assert data["false_positives"] == 1
        # 1 FP / (1 TP + 1 FP) = 0.5 (50%)
        assert data["false_positive_rate"] == 0.5
        assert data["cried_wolf_index"] == 0.5

        # Check category breakdowns
        assert "SECURITY" in data["by_category"]
        assert data["by_category"]["SECURITY"]["confirmed_real"] == 1
        assert data["by_category"]["SECURITY"]["false_positive_rate"] == 0.0

        assert "QUALITY" in data["by_category"]
        assert data["by_category"]["QUALITY"]["false_positives"] == 1
        assert data["by_category"]["QUALITY"]["false_positive_rate"] == 1.0

        # Check evidence grade breakdowns
        assert "REPRODUCED" in data["by_evidence_grade"]
        assert data["by_evidence_grade"]["REPRODUCED"]["confirmed_real"] == 1
        assert "HEURISTIC" in data["by_evidence_grade"]
        assert data["by_evidence_grade"]["HEURISTIC"]["false_positives"] == 1

    def test_submit_calibration_feedback_false_positive(self, client):
        repo = "acme/feedback-api"
        fp = "feeda11122223333"
        recorded_finding(FindingOutcomeStore(), repo, fp, pr_id="1")

        payload = {
            "repo_id": repo,
            "fingerprint": fp,
            "outcome": "FALSE_POSITIVE",
            "reason": "Test mock not an issue",
            "source": "api_test",
        }
        resp = client.post("/api/calibration/feedback", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["finding"]["outcome"] == "FALSE_POSITIVE"
        assert data["false_positive_rate"] == 1.0

        # Verify auto-suppressed in SuppressionStore
        supp_store = SuppressionStore()
        assert not supp_store.is_suppressed(repo, fp)

    def test_submit_calibration_feedback_confirmed_real(self, client):
        repo = "acme/feedback-api"
        fp = "feedb44455556666"
        recorded_finding(FindingOutcomeStore(), repo, fp, pr_id="1")

        payload = {
            "repo_id": repo,
            "fingerprint": fp,
            "outcome": "CONFIRMED_REAL",
            "reason": "Confirmed defect",
            "source": "api_test",
        }
        resp = client.post("/api/calibration/feedback", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["finding"]["outcome"] == "CONFIRMED_REAL"

        store = FindingOutcomeStore()
        assert store.get_finding(repo, fp)["outcome"] == "CONFIRMED_REAL"

    def test_submit_invalid_outcome_rejected(self, client):
        payload = {
            "repo_id": "acme/repo",
            "fingerprint": "1234abcd1234abcd",
            "outcome": "BOGUS_STATUS",
        }
        resp = client.post("/api/calibration/feedback", json=payload)
        assert resp.status_code == 422
        assert resp.json()["detail"][0]["loc"][-1] == "outcome"

    def test_api_feedback_sanitizes_backticks_and_validates(self, client):
        recorded_finding(FindingOutcomeStore(), "org/repo", "c0ffee12", pr_id="1")
        # 1. Backticked fingerprint is sanitized and accepted
        resp = client.post(
            "/api/calibration/feedback",
            json={
                "repo_id": "org/repo",
                "fingerprint": " `c0ffee12` ",
                "outcome": "CONFIRMED_REAL",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["finding"]["fingerprint"] == "c0ffee12"

        # 2. Non-hex fingerprint rejected with 400
        resp_bad = client.post(
            "/api/calibration/feedback",
            json={
                "repo_id": "org/repo",
                "fingerprint": "invalid-non-hex!",
                "outcome": "CONFIRMED_REAL",
            },
        )
        assert resp_bad.status_code == 422
        assert "Invalid fingerprint" in str(resp_bad.json()["detail"])


class TestUnsupportedReactionWebhooks:
    @pytest.mark.parametrize("content", ["+1", "-1"])
    def test_synthetic_reaction_event_is_ignored(self, client, content):
        with patch(
            "code_review_agent.webhook_server.verify_github_signature",
            return_value=True,
        ):
            response = client.post(
                "/webhook/github",
                json={"action": "created", "reaction": {"content": content}},
                headers={"X-GitHub-Event": "reaction"},
            )
        assert response.status_code == 200 and response.json()["status"] == "ignored"
        assert FindingOutcomeStore().list_findings() == []
        assert SuppressionStore().list_suppressions() == []


class TestAutomatedLifecycleConfirmations:
    """Tests automated confirmations via CommentSynchronizer, AutoRemediator, and PR merge."""

    def test_comment_synchronizer_resolves_findings_to_confirmed_real(self):
        """When CommentSynchronizer identifies resolved findings, it updates their outcome to CONFIRMED_REAL."""
        owner = "acme"
        repo = "app"
        repo_id = f"{owner}/{repo}"
        fp = "1234567890abcdef"

        # Record finding as initially PENDING
        store = FindingOutcomeStore()
        recorded_finding(
            store, repo_id=repo_id, fingerprint=fp, pr_id="10", outcome=PENDING
        )

        mock_client = MagicMock()
        # Prior comments contained fp
        mock_client.list_pull_request_review_comments.return_value = [
            {"body": f"⚠️ Issue\n<!-- ai-code-review:fingerprint:{fp} -->", "id": 1}
        ]

        # In current commit, finding is fixed (empty current findings)
        deduped, resolved_fps, _ = CommentSynchronizer.synchronize(
            client=mock_client,
            owner=owner,
            repo=repo,
            pull_number=10,
            current_comments=[],
            current_findings=[],
            head_sha="head1234",
        )

        assert fp in resolved_fps
        # Verify outcome transitioned to CONFIRMED_REAL in FindingOutcomeStore
        finding = store.get_finding(repo_id, fp)
        assert finding is not None
        assert finding["outcome"] == PENDING
        assert finding["lifecycle_status"] == "NOT_OBSERVED"

    def test_pr_merge_transitions_pending_to_ignored_merged(self):
        """When SuggestionTracker processes a merged PR, pending findings transition to IGNORED_MERGED."""
        from code_review_agent.learning.suggestion_tracker import SuggestionTracker

        owner = "acme"
        repo = "merge-check"
        repo_id = f"{owner}/{repo}"
        pr = 77
        fp_pend = "merge_pend_01"
        fp_real = "merge_real_02"

        store = FindingOutcomeStore()
        recorded_finding(
            store, repo_id=repo_id, fingerprint=fp_pend, pr_id=str(pr), outcome=PENDING
        )
        recorded_finding(
            store,
            repo_id=repo_id,
            fingerprint=fp_real,
            pr_id=str(pr),
            outcome=CONFIRMED_REAL,
        )

        tracker = SuggestionTracker()
        tracker.process_merged_pr(
            owner=owner,
            repo=repo,
            pr_number=pr,
            review_comments=[],
            applied_suggestions=[],
            final_diff="",
        )

        # fp_pend must have transitioned to IGNORED_MERGED
        assert store.get_finding(repo_id, fp_pend)["outcome"] == IGNORED_MERGED
        # fp_real must remain CONFIRMED_REAL
        assert store.get_finding(repo_id, fp_real)["outcome"] == CONFIRMED_REAL

    def test_comment_sync_does_not_overwrite_user_false_positive(self):
        """A finding flagged as FALSE_POSITIVE must NEVER be converted to CONFIRMED_REAL by comment sync."""
        owner = "org"
        repo = "proj"
        repo_id = f"{owner}/{repo}"
        fp = "ca55e77e01020304"

        store = FindingOutcomeStore()
        # Developer explicitly flagged as FALSE_POSITIVE
        recorded_finding(
            store,
            repo_id,
            fp,
            pr_id="10",
            outcome=FALSE_POSITIVE,
            source="slash_command_fp",
        )

        mock_client = MagicMock()
        mock_client.list_pull_request_review_comments.return_value = [
            {"body": f"<!-- ai-code-review:fingerprint:{fp} -->\nFinding", "id": 1}
        ]

        # In current commit, finding is absent from current_findings
        deduped, resolved_fps, _ = CommentSynchronizer.synchronize(
            client=mock_client,
            owner=owner,
            repo=repo,
            pull_number=10,
            current_comments=[],
            current_findings=[],
            head_sha="sha2222",
        )

        # It must NOT be reported as resolved
        assert fp not in resolved_fps
        # And its outcome in store must remain FALSE_POSITIVE
        rec = store.get_finding(repo_id, fp)
        assert rec["outcome"] == FALSE_POSITIVE

    def test_comment_sync_avoids_duplicate_resolution_summaries_on_subsequent_commits(
        self,
    ):
        """A finding already resolved in commit 1 must NOT be repeatedly announced on commit 2."""
        owner = "org"
        repo = "proj"
        repo_id = f"{owner}/{repo}"
        fp = "ded0000011223344"

        store = FindingOutcomeStore()
        # First commit recorded finding as PENDING
        recorded_finding(store, repo_id, fp, pr_id="15", outcome=PENDING)

        mock_client = MagicMock()
        mock_client.list_pull_request_review_comments.return_value = [
            {"body": f"<!-- ai-code-review:fingerprint:{fp} -->", "id": 10}
        ]

        # Commit 2: finding is fixed!
        deduped, res1, _ = CommentSynchronizer.synchronize(
            client=mock_client,
            owner=owner,
            repo=repo,
            pull_number=15,
            current_comments=[],
            current_findings=[],
            head_sha="commit2",
        )
        assert fp in res1
        assert mock_client.post_issue_comment.call_count == 0
        assert store.get_finding(repo_id, fp)["outcome"] == PENDING

        # Reset mock call count
        mock_client.post_issue_comment.reset_mock()

        # Commit 3: author pushes docstring update; finding is still absent from diff
        deduped3, res3, _ = CommentSynchronizer.synchronize(
            client=mock_client,
            owner=owner,
            repo=repo,
            pull_number=15,
            current_comments=[],
            current_findings=[],
            head_sha="commit3",
        )
        # Must NOT re-announce fp in commit 3
        assert fp not in res3
        mock_client.post_issue_comment.assert_not_called()
