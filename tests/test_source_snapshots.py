"""Full-file scanner context and exact mapping regressions."""

import subprocess

import pytest

from code_review_agent.source_snapshot import build_snapshot
from code_review_agent.tools.full_source_scan import scan_full_source
from code_review_agent.tools.scan_report import run_scanners

DIFF = "diff --git a/api/utils.py b/api/utils.py\n--- a/api/utils.py\n+++ b/api/utils.py\n@@ -3,2 +3,2 @@\n     def execute(self, value):\n-        return value\n+        return eval(value)\n"
SOURCE = b"import os\nclass Service:\n    def execute(self, value):\n        return eval(value)\n"


def test_full_file_context_and_changed_line_filter():
    snapshot = build_snapshot(DIFF, "a" * 40, fetch=lambda path: SOURCE)
    assert snapshot.coverage[0].status == "AVAILABLE"
    report = run_scanners(DIFF, snapshot)
    assert report.statuses["ast"] == "COMPLETED"
    assert report.statuses["bandit"] == "COMPLETED"
    assert report.statuses["ruff"] == "COMPLETED"
    assert all(
        f.file_path == "api/utils.py" and f.line_number == 4 for f in report.findings
    )
    assert any("B307" in f.rule_id for f in report.findings)


def test_missing_or_wrong_source_is_incomplete():
    missing = build_snapshot(DIFF)
    wrong = build_snapshot(DIFF, "a" * 40, fetch=lambda path: b"wrong\n")
    assert missing.coverage[0].status == "MISSING"
    assert wrong.coverage[0].status == "MISMATCH"
    assert run_scanners(DIFF, missing).statuses["bandit"] == "FAILED"


def test_unverified_checkout_is_not_used(tmp_path):
    (tmp_path / "api").mkdir()
    (tmp_path / "api/utils.py").write_bytes(SOURCE)
    assert build_snapshot(DIFF, "a" * 40, root=str(tmp_path)).files == ()


@pytest.mark.parametrize(
    "status", [" M api/utils.py\n", "?? helpers.py\n", "!! plugin.py\n"]
)
def test_matching_head_with_dirty_context_is_not_verified(
    monkeypatch, tmp_path, status
):
    from code_review_agent.source_snapshot import verified_checkout

    outputs = iter(
        [
            subprocess.CompletedProcess([], 0, "a" * 40 + "\n", ""),
            subprocess.CompletedProcess([], 0, status, ""),
        ]
    )
    monkeypatch.setattr(
        "code_review_agent.source_snapshot.subprocess.run",
        lambda *args, **kwargs: next(outputs),
    )
    assert not verified_checkout(str(tmp_path), "a" * 40)


def test_oversize_source_and_snapshot_mismatch():
    snapshot = build_snapshot(DIFF, fetch=lambda path: b"x" * (2 * 1024 * 1024 + 1))
    assert snapshot.coverage[0].status == "LIMIT_EXCEEDED"
    with pytest.raises(ValueError, match="mismatch"):
        run_scanners(DIFF + "\n", snapshot)


def test_unknown_scanner_path_is_rejected(monkeypatch):
    snapshot = build_snapshot(DIFF, fetch=lambda path: SOURCE)
    monkeypatch.setattr(
        "code_review_agent.tools.full_source_scan.subprocess.run",
        lambda *a, **kw: subprocess.CompletedProcess(
            a, 1, '[{"filename":"/unknown/utils.py"}]', ""
        ),
    )
    with pytest.raises(ValueError, match="UNKNOWN_SCANNER_PATH"):
        scan_full_source(snapshot, "ruff")
