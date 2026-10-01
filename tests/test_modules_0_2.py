"""Security and durability acceptance tests for production modules 0 through 2."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from code_review_agent.diff_parser import DiffParser
from code_review_agent.models import FileDiff, ParsedPR, ReviewAPIResponse
from code_review_agent.path_validation import (
    UnsafeDiffPathError,
    safe_diff_path,
    validate_diff_path,
)
from code_review_agent.publication import ReconciliationRequired, publish
from code_review_agent.review_executor import ReviewWorkerError
from code_review_agent.tools.bandit_runner import BanditRunner
from code_review_agent.tools.ruff_tool import RuffRunner
from code_review_agent.tools.semgrep_runner import SemgrepRunner
from code_review_agent.webhook_queue import WebhookJobQueue, WebhookWorker

BAD_PATHS = [
    "../outside.py",
    "src/../../outside.py",
    "/tmp/outside.py",
    r"C:\temp\outside.py",
    "C:outside.py",
    r"\\server\share\outside.py",
    r"\outside.py",
    "//server/share/outside.py",
    r"src\..\outside.py",
    "src/NUL.py",
    "CON/file.py",
    "src/COM1.py",
    "src/LPT¹.py",
    "src/file.py:stream",
    "src/file.py.",
    "src/file.py ",
    "src/./file.py",
    "src//file.py",
    ".",
    "",
    "src/evil\x00.py",
]


def patch(path="src/app.py"):
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-old = 1\n+new = 2\n"


@pytest.mark.parametrize("filename", BAD_PATHS)
def test_portable_paths_rejected(filename, tmp_path):
    with pytest.raises(UnsafeDiffPathError):
        safe_diff_path(tmp_path, filename)


@pytest.mark.parametrize("filename", BAD_PATHS[:16])
def test_diff_ingestion_rejects_paths_before_any_tool(filename):
    with pytest.raises(UnsafeDiffPathError):
        DiffParser.parse_diff(patch(filename))


@pytest.mark.parametrize(
    "filename", ["src/app.py", "docs/read me.md", "src/日本語.py", r"src\nested\app.py"]
)
def test_legitimate_paths_are_preserved(filename, tmp_path):
    normalized = validate_diff_path(filename)
    assert safe_diff_path(tmp_path, filename) == tmp_path / normalized
    assert DiffParser.parse_diff(patch(filename)).files[0].target_file == normalized


@pytest.mark.parametrize(
    "metadata",
    [
        "diff --git a/../outside.py b/safe.py\n--- a/safe.py\n+++ b/safe.py",
        "diff --git a/safe.py b/safe.py\nrename from ../outside.py",
        "--- /absolute.py\n+++ b/safe.py",
        "diff --git /absolute.py /absolute.py",
        'diff --git "a/..\\057outside.py" "b/safe.py"',
    ],
)
def test_all_metadata_paths_checked_even_when_overridden(metadata):
    with pytest.raises(UnsafeDiffPathError):
        DiffParser.parse_diff(metadata + "\n@@ -0,0 +1 @@\n+value=1\n")


def test_null_sentinel_and_quoted_unicode_survive_chunking():
    new = "diff --git a/added.py b/added.py\nnew file mode 100644\n--- /dev/null\n+++ b/added.py\n@@ -0,0 +1,2 @@\n+x=1\n+y=2\n"
    removed = "diff --git a/deleted.py b/deleted.py\ndeleted file mode 100644\n--- a/deleted.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x=1\n"
    assert DiffParser.parse_diff(new).files[0].is_new_file
    assert DiffParser.parse_diff(removed).files[0].is_deleted_file
    assert DiffParser.chunk_diff_adaptively(new + removed, max_tokens=15)
    quoted = 'diff --git "a/\\346\\227\\245.py" "b/\\346\\227\\245.py"\n+value=1\n'
    assert DiffParser.parse_diff(quoted).files[0].target_file == "日.py"


def test_symlink_escape_is_rejected(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (root / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip(
            "Creating symlinks requires Windows developer mode or appropriate privileges"
        )
    with pytest.raises(UnsafeDiffPathError):
        safe_diff_path(root, "linked/write.py")
    assert not (outside / "write.py").exists()


@pytest.mark.parametrize("runner", [BanditRunner, RuffRunner, SemgrepRunner])
def test_scanners_revalidate_before_writing_even_if_parser_is_bypassed(
    runner, tmp_path, monkeypatch
):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("preserve me")
    forged = ParsedPR(
        files=[
            FileDiff(
                source_file="../outside.py",
                target_file="../outside.py",
                raw_patch="+overwritten=True",
                hunks=[],
            )
        ],
        files_changed=1,
        total_added=1,
        total_deleted=0,
    )
    monkeypatch.setattr(DiffParser, "parse_diff", lambda _: forged)
    monkeypatch.setattr(runner, "is_available", lambda: True)

    @contextmanager
    def workspace(**kwargs):
        yield str(root)

    monkeypatch.setattr("tempfile.TemporaryDirectory", workspace)
    subprocess = Mock()
    monkeypatch.setattr("subprocess.run", subprocess)
    with pytest.raises(UnsafeDiffPathError):
        runner.scan_diff("untrusted patch")
    assert outside.read_text() == "preserve me"
    subprocess.assert_not_called()


@pytest.mark.parametrize("endpoint", ["/api/review", "/api/review/stream"])
@pytest.mark.parametrize("prefer", ["", "respond-async"])
def test_browser_rejects_unsafe_diff_with_422(endpoint, prefer, monkeypatch):
    from code_review_agent import webhook_server as server

    executor = Mock()
    monkeypatch.setattr(server, "execute_bounded", executor)
    response = TestClient(server.app).post(
        endpoint, json={"raw_diff": patch("../outside.py")}, headers={"Prefer": prefer}
    )
    assert response.status_code == 422
    assert server.queue.list_jobs() == []
    executor.assert_not_called()


def test_invalid_worker_input_is_terminal(monkeypatch):
    queue = WebhookJobQueue()
    jid = queue.enqueue("org/repo#1", {}, job_kind="browser")
    job = queue.claim_next_job()

    def rejected(*args, **kwargs):
        raise ReviewWorkerError("Unsafe diff path; review refused.", status=422)

    monkeypatch.setattr("code_review_agent.review_executor.run_job", rejected)
    worker = WebhookWorker(queue)
    worker._semaphore.acquire()
    worker._process_claimed_job(job)
    assert queue.get_job(jid)["status"] == "FAILED"
    assert queue.claim_next_job() is None
    assert (
        queue._get_connection()
        .execute("SELECT COUNT(*) FROM external_operations_log")
        .fetchone()[0]
        == 0
    )
    worker.stop()


@pytest.mark.parametrize(
    "credentials",
    [(None, None), ("missing", None), (None, "token"), ("missing", "token")],
)
def test_provider_write_without_active_claim_never_creates_intent(credentials):
    queue = WebhookJobQueue()
    send = Mock()
    with pytest.raises(PermissionError):
        publish(
            queue,
            job_id=credentials[0],
            claim_token=credentials[1],
            key="op",
            target="org/repo#1",
            payload={},
            send=send,
        )
    send.assert_not_called()
    assert queue.get_external_operation("op") is None


@pytest.mark.parametrize(
    "receipt",
    [
        None,
        True,
        {},
        {"id": True},
        {"id": -1},
        {"id": ""},
        {"id": 1, "error": "failed"},
    ],
)
def test_provider_must_return_actual_receipt(receipt, queued_claim):
    queue = WebhookJobQueue()
    claim = queued_claim()
    with pytest.raises(ReconciliationRequired):
        publish(
            queue,
            **claim,
            key="receipt",
            target="org/repo#1",
            payload={},
            send=lambda _: receipt,
        )
    assert queue.get_external_operation("receipt")["status"] == "PENDING"


def test_crash_reclaim_reconciles_original_intent_without_resending(queued_claim):
    queue = WebhookJobQueue()
    claim = queued_claim()
    receipts = []
    sends = []

    def accepted(marker):
        assert queue.get_external_operation("stable")["status"] == "PENDING"
        sends.append(marker)
        receipts.append({"id": 71, "body": marker, "user": {"login": "review-bot"}})
        raise httpx.ReadTimeout("accepted but response was lost")

    args = {
        "key": "stable",
        "target": "org/repo#1",
        "payload": {"body": "original"},
        "send": accepted,
        "search": lambda: receipts,
    }
    with pytest.raises(ReconciliationRequired):
        publish(queue, **claim, **args)
    with queue._get_connection() as conn:
        conn.execute(
            "UPDATE webhook_jobs SET lease_expires_at=0 WHERE job_id=?",
            (claim["job_id"],),
        )
    restarted = WebhookJobQueue(str(queue.db_path))
    new = restarted.claim_next_job()
    assert new["claim_token"] != claim["claim_token"]
    with pytest.raises(PermissionError):
        publish(queue, **claim, **args)
    args["payload"] = {"body": "new analysis"}
    assert (
        publish(
            restarted, job_id=new["job_id"], claim_token=new["claim_token"], **args
        )["id"]
        == 71
    )
    assert len(sends) == 1
    assert restarted.get_external_operation("stable")["result"]["intent"] == {
        "body": "original"
    }


def test_concurrent_publication_creates_one_remote_write(queued_claim):
    queue = WebhookJobQueue()
    claim = queued_claim()
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def send(marker):
        calls.append(marker)
        entered.set()
        assert release.wait(5)
        return {"id": 1}

    args = dict(
        **claim,
        key="concurrent",
        target="org/repo#1",
        payload={},
        send=send,
        search=list,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(publish, WebhookJobQueue(str(queue.db_path)), **args)
        assert entered.wait(5)
        try:
            with pytest.raises(ReconciliationRequired):
                publish(WebhookJobQueue(str(queue.db_path)), **args)
        finally:
            release.set()
        assert first.result(timeout=5)["id"] == 1
    assert len(calls) == 1


def test_cancelled_writer_cannot_acknowledge_or_retry(queued_claim):
    queue = WebhookJobQueue()
    claim = queued_claim()
    calls = []

    def send(marker):
        calls.append(marker)
        queue.cancel_job(claim["job_id"])
        return {"id": 1}

    args = dict(**claim, key="cancelled", target="org/repo#1", payload={}, send=send)
    with pytest.raises(PermissionError):
        publish(queue, **args)
    assert queue.get_external_operation("cancelled")["status"] == "PENDING"
    with pytest.raises(PermissionError):
        publish(queue, **args)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "matches",
    [[], [{"id": 1, "body": "wrong marker", "user": {"login": "review-bot"}}]],
)
def test_uncertain_write_is_not_replayed_when_lookup_cannot_confirm(
    matches, queued_claim
):
    queue = WebhookJobQueue()
    claim = queued_claim()
    sends = Mock(side_effect=httpx.ReadTimeout("lost"))
    args = dict(
        **claim,
        key="unknown",
        target="org/repo#1",
        payload={},
        send=sends,
        search=lambda: matches,
    )
    with pytest.raises(ReconciliationRequired):
        publish(queue, **args)
    with pytest.raises(ReconciliationRequired):
        publish(queue, **args)
    assert sends.call_count == 1


def test_duplicate_webhook_delivery_only_enqueues_once(
    monkeypatch, offline_repository_client
):
    from code_review_agent import webhook_server as server

    monkeypatch.setattr(server, "verify_github_signature", lambda *a: True)
    dispatch = Mock()
    monkeypatch.setattr(server.CommandRouter, "dispatch", dispatch)
    payload = {
        "action": "created",
        "issue": {
            "number": 1,
            "pull_request": {"html_url": "https://github.com/org/repo/pull/1"},
        },
        "comment": {
            "id": 9,
            "body": "/help",
            "user": {"login": "alice", "type": "User"},
        },
    }
    client = TestClient(server.app)
    for _ in range(2):
        assert (
            client.post(
                "/webhook/github",
                json=payload,
                headers={
                    "X-GitHub-Event": "issue_comment",
                    "X-GitHub-Delivery": "same-delivery",
                },
            ).status_code
            == 200
        )
    assert len(server.queue.list_jobs()) == 1
    dispatch.assert_not_called()


def test_direct_flow_does_not_publish(monkeypatch):
    from code_review_agent.github_client import GitHubClient
    from code_review_agent.main import PRCodeReviewFlow

    post = Mock()
    monkeypatch.setattr(GitHubClient, "post_pull_request_review", post)
    monkeypatch.setattr(
        "code_review_agent.config.get_github_token", lambda: "configured"
    )
    flow = PRCodeReviewFlow()
    flow.state.pr_url = "https://github.com/org/repo/pull/1"
    flow.make_final_decision()
    assert not flow.state.github_review_submitted
    post.assert_not_called()


def test_reconciliation_cannot_affirm_unrelated_operations(queued_claim):
    from code_review_agent import webhook_server as server

    queue = server.queue
    first = queued_claim()
    second = queued_claim()

    def uncertain(marker):
        raise httpx.ReadTimeout("lost")

    for claim, key in [(first, "first"), (second, "second")]:
        with pytest.raises(ReconciliationRequired):
            publish(
                queue,
                **claim,
                key=key,
                target="org/repo#1",
                payload={"body": key},
                send=uncertain,
            )
        queue.fail_job(
            claim["job_id"],
            "RECONCILIATION_REQUIRED",
            claim_token=claim["claim_token"],
            state="RECONCILIATION_REQUIRED",
        )
    client = TestClient(server.app)
    url = f"/jobs/{first['job_id']}/reconcile"
    assert (
        client.post(
            url, json={"operation_key": "second", "receipt": {"id": 71}}
        ).status_code
        == 404
    )
    assert client.post(url, json={"operation_key": "first"}).status_code == 422
    assert client.post(url, json={"action": "retry"}).status_code == 200
    assert queue.get_external_operation("first")["status"] == "PENDING"
    assert queue.get_external_operation("second")["status"] == "PENDING"
    retry = queue.claim_next_job()
    queue.fail_job(
        retry["job_id"],
        "RECONCILIATION_REQUIRED",
        claim_token=retry["claim_token"],
        state="RECONCILIATION_REQUIRED",
    )
    res = client.post(url, json={"operation_key": "first", "receipt": {"id": 71}})
    assert res.status_code == 200 and res.json()["job_status"] == "RETRYING"
    assert queue.get_external_operation("first")["result"]["intent"] == {
        "body": "first"
    }
    assert queue.get_external_operation("second")["status"] == "PENDING"


def test_job_head_is_bound_across_retries(queued_claim):
    queue = WebhookJobQueue()
    claim = queued_claim()
    assert queue.bind_job_head(**claim, head_sha="original") == "original"
    assert queue.bind_job_head(**claim, head_sha="different") == "original"
    payload = (
        queue._get_connection()
        .execute(
            "SELECT payload_json FROM webhook_jobs WHERE job_id=?", (claim["job_id"],)
        )
        .fetchone()[0]
    )
    assert json.loads(payload)["_analyzed_head"] == "original"


def test_review_recovery_retains_head_and_skips_acknowledged_writes(
    queued_claim, tmp_path, monkeypatch
):
    from code_review_agent.platform.base import PlatformPRIdentifier, PlatformPRMetadata
    from code_review_agent.webhook_queue import execute_platform_review

    target = "https://github.com/org/repo/pull/1"
    claim = queued_claim(target)
    client = Mock()
    metadata = PlatformPRMetadata(head_sha="a" * 40, head_ref="feature")
    client.fetch_pull_request_metadata.return_value = metadata
    client.fetch_pull_request_diff.return_value = patch()
    client.list_pull_request_review_comments.return_value = []
    client.create_or_update_check_run.return_value = {"id": 92}
    client.set_commit_status.return_value = {"id": 93}
    receipts = []

    def accepted(**kwargs):
        receipts.append(
            {"id": 91, "body": kwargs["body"], "user": {"login": "review-bot"}}
        )
        raise httpx.ReadTimeout("response lost")

    client.post_pull_request_review.side_effect = accepted
    client.list_pull_request_reviews.side_effect = lambda *args: receipts
    identifier = PlatformPRIdentifier(
        platform="github",
        owner_or_project="org",
        repo_or_slug="repo",
        pr_id=1,
        raw_identifier=target,
    )
    monkeypatch.setattr(
        "code_review_agent.platform.factory.get_platform_client",
        lambda _: (client, identifier),
    )

    @contextmanager
    def checkout(*args, **kwargs):
        yield str(tmp_path)

    monkeypatch.setattr(
        "code_review_agent.bot.checkout.temporary_pr_checkout", checkout
    )
    from production_fixtures import versioned_response

    review = Mock(
        return_value=versioned_response(
            ReviewAPIResponse(
                verdict="APPROVE",
                confidence_score=90,
                summary="Complete",
                full_report="Original report",
            )
        )
    )
    monkeypatch.setattr(
        "code_review_agent.review_service.ReviewService.execute_review", review
    )
    with pytest.raises(ReconciliationRequired):
        execute_platform_review({}, target, **claim)
    metadata.head_sha = "b" * 40
    with pytest.raises(ReconciliationRequired):
        execute_platform_review({}, target, **claim)
    assert client.post_pull_request_review.call_count == 1
    client.create_or_update_check_run.assert_not_called()
    metadata.head_sha = "a" * 40
    assert (
        execute_platform_review({}, target, **claim)["analyzer_status"]["publication"]
        == "PUBLISHED"
    )
    assert (
        execute_platform_review({}, target, **claim)["full_report"] == "Original report"
    )
    alias_claim = queued_claim("org/repo#1")
    assert (
        execute_platform_review({}, "org/repo#1", **alias_claim)["full_report"]
        == "Original report"
    )
    assert client.post_pull_request_review.call_count == 1
    client.create_or_update_check_run.assert_not_called()
    client.set_commit_status.assert_not_called()
    review.assert_called_once()


def test_queue_schema_upgrade_preserves_existing_jobs(tmp_path):
    import sqlite3

    database = tmp_path / "upgrade.db"
    with sqlite3.connect(database) as conn:
        conn.execute(
            "CREATE TABLE schema_versions (component TEXT PRIMARY KEY, version INTEGER NOT NULL)"
        )
        conn.execute("INSERT INTO schema_versions VALUES ('queue', 1)")
        conn.execute("""CREATE TABLE webhook_jobs (job_id TEXT PRIMARY KEY, pr_identifier TEXT NOT NULL,
            payload_json TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            max_retries INTEGER NOT NULL DEFAULT 3, last_error TEXT, created_at REAL NOT NULL,
            updated_at REAL NOT NULL, locked_at REAL, review_result_json TEXT)""")
        conn.execute(
            "INSERT INTO webhook_jobs VALUES ('saved','org/repo#1','{}','QUEUED',0,3,NULL,1,1,NULL,NULL)"
        )
    queue = WebhookJobQueue(str(database))
    assert queue.get_job("saved")["status"] == "QUEUED"
    assert (
        queue._get_connection()
        .execute("SELECT version FROM schema_versions WHERE component='queue'")
        .fetchone()[0]
        == 2
    )
    assert WebhookJobQueue(str(database)).get_job("saved")["status"] == "QUEUED"


def test_uncertain_remediation_is_not_replayed_after_head_changes(
    queued_claim, monkeypatch
):
    from code_review_agent import webhook_server as server
    from code_review_agent.models import SastFinding
    from code_review_agent.platform.base import PlatformPRIdentifier, PlatformPRMetadata
    from code_review_agent.remediator import AutoRemediator

    target = "https://github.com/org/repo/pull/1"
    queue = WebhookJobQueue()
    claim = queued_claim(target)
    client = Mock()
    meta = PlatformPRMetadata(
        head_sha="original-head", head_ref="feature", base_ref="main"
    )
    client.fetch_pull_request_metadata.return_value = meta
    client.fetch_pull_request_diff.return_value = patch()
    client.fetch_file_content.return_value = 'print("old")\n'
    client.commit_file_change.side_effect = httpx.ReadTimeout(
        "accepted commit; response lost"
    )
    identifier = PlatformPRIdentifier(
        platform="github",
        owner_or_project="org",
        repo_or_slug="repo",
        pr_id=1,
        raw_identifier=target,
    )
    monkeypatch.setattr(
        "code_review_agent.platform.factory.get_platform_client",
        lambda _: (client, identifier),
    )
    monkeypatch.setattr(
        "code_review_agent.remediator.get_platform_client",
        lambda _: (client, identifier),
    )
    finding = SastFinding(
        rule_id="PRINT",
        description="Debug output",
        severity="LOW",
        file_path="src/app.py",
        line_number=1,
        snippet='print("old")',
        fix_recommendation="Use logging",
    )
    monkeypatch.setattr(AutoRemediator, "find_target_finding", lambda *a, **kw: finding)
    args = {
        "pr_identifier": target,
        "fingerprint": finding.fingerprint,
        "client": client,
        "replacement_code": 'logger.info("old")',
        "bypass_policy": True,
    }
    with pytest.raises(ReconciliationRequired):
        AutoRemediator.apply_remediation(**args, **claim)
    key = (
        queue._get_connection()
        .execute(
            "SELECT operation_key FROM external_operation_jobs WHERE job_id=?",
            (claim["job_id"],),
        )
        .fetchone()[0]
    )
    meta.head_sha = "commit-was-accepted"
    with pytest.raises(ReconciliationRequired):
        AutoRemediator.apply_remediation(**args, **claim)
    assert client.commit_file_change.call_count == 1
    queue.fail_job(
        claim["job_id"],
        "RECONCILIATION_REQUIRED",
        claim_token=claim["claim_token"],
        state="RECONCILIATION_REQUIRED",
    )
    response = TestClient(server.app).post(
        f"/jobs/{claim['job_id']}/reconcile",
        json={"operation_key": key, "receipt": {"sha": "commit-was-accepted"}},
    )
    assert response.status_code == 200
    resumed = queue.claim_next_job()
    result = AutoRemediator.apply_remediation(
        **args, job_id=resumed["job_id"], claim_token=resumed["claim_token"]
    )
    assert result["commit_sha"] == "commit-was-accepted"
    assert client.commit_file_change.call_count == 1
    manifest = AutoRemediator.get_remediation_manifest(result["remediation_id"])
    assert manifest["expected_old_text"] == 'print("old")'
    assert manifest["commit_sha_after"] == "commit-was-accepted"


def test_zip_validation_checks_all_members_before_writing(tmp_path):
    import io
    import zipfile

    from code_review_agent.review_service import ReviewService

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("safe.py", "value=1")
        output.writestr("../outside.py", "value=2")
    with pytest.raises(UnsafeDiffPathError):
        ReviewService.validate_and_extract_zip(archive.getvalue(), tmp_path)
    assert not (tmp_path / "safe.py").exists()


def test_sandbox_rejects_unsafe_workspace_before_docker_launch(monkeypatch):
    from code_review_agent.sandbox.test_runner import SandboxTestRunner

    monkeypatch.setenv("REVIEW_SANDBOX_IMAGE", "sha256:" + "a" * 64)
    monkeypatch.setattr("shutil.which", lambda _: "docker")
    launch = Mock()
    monkeypatch.setattr("subprocess.Popen", launch)
    result = SandboxTestRunner.run_tests(
        "def test_ok(): assert True", pr_content=patch("../outside.py")
    )
    assert not result.executed and result.status == "ERROR"
    launch.assert_not_called()


def test_cancelled_write_receipt_can_be_recorded_without_resuming_job(queued_claim):
    from code_review_agent import webhook_server as server

    queue = server.queue
    claim = queued_claim()

    def accepted(marker):
        queue.cancel_job(claim["job_id"])
        return {"id": 71}

    with pytest.raises(PermissionError):
        publish(
            queue,
            **claim,
            key="cancelled-write",
            target="org/repo#1",
            payload={},
            send=accepted,
        )
    client = TestClient(server.app)
    url = f"/jobs/{claim['job_id']}/reconcile"
    assert client.post(url, json={"action": "retry"}).status_code == 409
    result = client.post(
        url, json={"operation_key": "cancelled-write", "receipt": {"id": 71}}
    )
    assert result.status_code == 200 and result.json()["job_status"] == "CANCELLED"
    assert queue.get_external_operation("cancelled-write")["status"] == "SUCCESS"
    assert queue.claim_next_job() is None
