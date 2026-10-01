"""Versioned production boundaries, tested with local synthetic evidence only."""

import hashlib
import json
import sqlite3
import threading

import pytest
from release_fixture import profile, retained_release

from code_review_agent.crews.code_review_crew.crew import CodeReviewCrew
from code_review_agent.data_policy import repository_scope
from code_review_agent.llm_factory import LLMResponse
from code_review_agent.models import (
    CodeQualityJSON,
    ReviewAPIResponse,
    ReviewSecurityJSON,
    SummarizedFindingsJSON,
)
from code_review_agent.pilot import PilotStore
from code_review_agent.release import verify_bundle, verify_registry_manifest
from code_review_agent.review_contracts import ReviewInputSnapshot, SourceFile
from code_review_agent.source_snapshot import build_snapshot
from code_review_agent.tools.full_source_scan import scan_full_source

DIFF = "diff --git a/api/utils.py b/api/utils.py\n--- a/api/utils.py\n+++ b/api/utils.py\n@@ -2 +2 @@\n-    return value\n+    return eval(value)\n"
SOURCE = b"def run(value):\n    return eval(value)\n"


def test_memory_revision_change_invalidates_existing_profile(monkeypatch, tmp_path):
    from code_review_agent.learning.team_memory import TeamMemoryStore
    from code_review_agent.reviewer_profile import build_profile, same_memory_revision

    monkeypatch.setenv("TEAM_MEMORY_CACHE_DIR", str(tmp_path / "memory"))
    store = TeamMemoryStore()
    original = build_profile(team_memory=store.format_team_memory_prompt("org/repo"))
    assert same_memory_revision(original, "github:github.com/org/repo")
    store.record_accepted_suggestion(
        "org/repo", "Validate inputs", "Use parameterized queries", "query(sql, params)"
    )
    assert not same_memory_revision(original, "github:github.com/org/repo")


def test_scanners_distinguish_basenames_and_renames():
    diff = DIFF + DIFF.replace("api/utils.py", "tests/utils.py").replace(
        "eval(value)", "value"
    )
    snapshot = build_snapshot(
        diff,
        fetch=lambda path: (
            SOURCE
            if path.startswith("api/")
            else SOURCE.replace(b"eval(value)", b"value")
        ),
    )
    found = scan_full_source(snapshot, "bandit")
    assert found and all(
        f.file_path == "api/utils.py" and f.line_number == 2 for f in found
    )
    renamed = DIFF.replace("a/api/utils.py", "a/old.py")
    assert all(
        f.file_path == "api/utils.py"
        for f in scan_full_source(
            build_snapshot(renamed, fetch=lambda _: SOURCE), "ast"
        )
    )


def test_portable_collision_and_forged_snapshot_rejected():
    with pytest.raises(ValueError, match="paths|unique changed files"):
        build_snapshot(
            DIFF + DIFF.replace("api/utils.py", "API/UTILS.py"), fetch=lambda _: SOURCE
        )
    snapshot = build_snapshot(DIFF, fetch=lambda _: SOURCE)
    data = snapshot.model_dump()
    wrong = SourceFile.from_bytes(
        "api/utils.py", b"def run(value):\n    return value\n", "HEAD_BLOB"
    )
    data["files"] = [wrong.model_dump()]
    data["coverage"][0]["sha256"] = wrong.sha256
    with pytest.raises(ValueError, match="does not match diff"):
        ReviewInputSnapshot.model_validate(data)


def test_source_limit_counts_missing_files_too():
    calls = []
    diff = "".join(DIFF.replace("api/utils.py", f"f{i}.py") for i in range(101))

    def missing(path):
        calls.append(path)
        raise OSError("not available")

    snapshot = build_snapshot(diff, fetch=missing)
    assert len(calls) == 100 and len(snapshot.coverage) == 101
    assert snapshot.coverage[-1].status == "LIMIT_EXCEEDED"


def test_source_links_are_withheld_before_resolution(tmp_path, monkeypatch):
    (tmp_path / "api").mkdir()
    (tmp_path / "api/utils.py").write_bytes(SOURCE)
    from pathlib import Path

    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda p: p.name == "api" or original(p))
    assert (
        build_snapshot(DIFF, root=str(tmp_path), trusted_upload=True).coverage[0].status
        == "MISMATCH"
    )


def test_fixed_stages_run_concurrently_and_failure_withholds_synthesis(monkeypatch):
    crew = CodeReviewCrew()
    barrier = threading.Barrier(2)
    called = []

    def stage(name, inputs, schema, upstream=None):
        called.append(name)
        if name != "summarize_findings":
            barrier.wait(timeout=3)
        if name == "review_security":
            raise ValueError("invalid stage JSON")
        return CodeQualityJSON(reasoning="fixture"), LLMResponse(
            text="{}", model="ollama/test"
        )

    monkeypatch.setattr(crew, "_stage", stage)
    with pytest.raises(ValueError, match="invalid stage"):
        crew.kickoff({})
    assert set(called) == {"analyze_code_quality", "review_security"}


def test_synthesis_cannot_invent_finding_at_existing_location(monkeypatch):
    from code_review_agent.models import InlineComment

    crew = CodeReviewCrew()
    comment = InlineComment(
        path="api/utils.py", line=2, severity="HIGH", comment_body="Existing evidence"
    )
    quality = CodeQualityJSON(reasoning="fixture", inline_suggestions=[comment])
    security = ReviewSecurityJSON(blocking=False, highest_risk="none")
    summary = SummarizedFindingsJSON(
        confidence=80,
        findings="fixture",
        inline_comments=[
            comment.model_copy(update={"comment_body": "Invented evidence"})
        ],
    )

    def stage(name, *args):
        return {
            "analyze_code_quality": quality,
            "review_security": security,
            "summarize_findings": summary,
        }[name], LLMResponse(text="{}", model="ollama/test")

    monkeypatch.setattr(crew, "_stage", stage)
    with pytest.raises(ValueError, match="unsupported finding"):
        crew.kickoff({})


def test_context_consent_propagates_to_simple_executor(monkeypatch):
    from code_review_agent.llm_factory import ProviderLLM
    from code_review_agent.main import PRCodeReviewFlow

    seen = []

    def call(self, *args):
        from code_review_agent.data_policy import _repository

        seen.append(_repository.get())
        return LLMResponse(text="{}", model="ollama/test")

    monkeypatch.setattr(ProviderLLM, "call_response", call)
    flow = PRCodeReviewFlow()
    monkeypatch.setattr(flow, "_get_llm", lambda: ProviderLLM("ollama/test", ""))
    with repository_scope("github:github.com/org/repo"):
        flow._call_llm_with_timeout("fixture", 1)
    assert seen == ["github:github.com/org/repo"]


def test_profiles_separate_evidence_and_input_conflicts(tmp_path):
    store = PilotStore(str(tmp_path / "pilot.db"))
    reviewer = profile()
    response = ReviewAPIResponse(
        verdict="APPROVE",
        confidence_score=0,
        summary="fixture",
        analyzed_commit="a" * 40,
        reviewer_profile=reviewer,
        reviewer_profile_id=reviewer.profile_id,
        input_snapshot_hash="b" * 64,
    )
    first = store.record("org/repo", "1", response, "SHADOW")
    other = reviewer.model_copy(update={"model": "ollama/other"})
    response.reviewer_profile, response.reviewer_profile_id = other, other.profile_id
    second = store.record("org/repo", "1", response, "SHADOW")
    assert (
        first != second
        and store.report("org/repo")["reviewer_profile_id"] == other.profile_id
    )
    assert not store.report("org/repo", reviewer.profile_id)["shadow_gate"]
    response.input_snapshot_hash = "c" * 64
    with pytest.raises(ValueError, match="different input"):
        store.record("org/repo", "1", response, "SHADOW")


def test_backup_first_migration_preserves_labels_and_receipts(tmp_path):
    path = tmp_path / "pilot.db"
    store = PilotStore(str(path))
    response = ReviewAPIResponse(
        verdict="APPROVE",
        confidence_score=0,
        summary="fixture",
        analyzed_commit="a" * 40,
    )
    run = store.record("org/repo", "1", response, "SHADOW")
    for reviewer in ("alice", "bob"):
        store.label(run, reviewer, "CLEAN", {}, "fixture")
    store.control("org/repo", "SHADOW", "operator", "fixture")
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "CREATE TABLE old_runs AS SELECT run_id,repository,pr,head,mode,complete,snapshot,created_at FROM pilot_runs"
        )
        conn.execute("DROP TABLE pilot_runs")
        conn.execute("ALTER TABLE old_runs RENAME TO pilot_runs")
        conn.execute("UPDATE schema_versions SET version=1 WHERE component='pilot'")
        conn.execute("UPDATE pilot_controls SET mode='ADVISORY'")
        conn.execute(
            "INSERT INTO pilot_publications VALUES (?,?,?,?,?)",
            (run, "fixture", '{"id":"receipt"}', "a" * 40, 1),
        )
    migrated = PilotStore(str(path))
    with migrated.connect() as conn:
        assert conn.execute(
            "SELECT run_id,reviewer_profile_id FROM pilot_runs"
        ).fetchone()[:] == (run, "LEGACY")
        assert conn.execute("SELECT COUNT(*) FROM pilot_labels").fetchone()[0] == 2
        assert (
            conn.execute("SELECT receipt FROM pilot_publications").fetchone()[0]
            == '{"id":"receipt"}'
        )
        assert conn.execute("SELECT mode FROM pilot_controls").fetchone()[0] == "PAUSED"
        assert (
            conn.execute(
                "SELECT actor FROM pilot_control_events ORDER BY created_at DESC"
            ).fetchone()[0]
            == "system:pilot-v2-migration"
        )
    assert (
        list((tmp_path / "backups").glob("*.db"))
        and not migrated.report("org/repo")["shadow_gate"]
    )


@pytest.mark.parametrize(
    "mutation", ["tamper", "matrix", "sandbox", "image", "profile", "basename"]
)
def test_release_rejects_modified_missing_or_mixed_evidence(tmp_path, mutation):
    record = retained_release(tmp_path)
    verify_bundle(record, tmp_path, True)
    if mutation == "tamper":
        (tmp_path / "python-3.12.xml").write_text("changed", encoding="utf-8")
    elif mutation == "matrix":
        del record["artifact_hashes"]["python-3.13.xml"]
    elif mutation == "sandbox":
        (tmp_path / "sandbox.xml").write_text(
            '<testsuite tests="4" failures="0" errors="0" skipped="1"/>',
            encoding="utf-8",
        )
        sha = hashlib.sha256((tmp_path / "sandbox.xml").read_bytes()).hexdigest()
        (tmp_path / "sandbox-pass.json").write_text(
            json.dumps({"status": "PASS", "commit": "a" * 40, "artifact_sha256": sha}),
            encoding="utf-8",
        )
        for name in ("sandbox.xml", "sandbox-pass.json"):
            record["artifact_hashes"][name] = hashlib.sha256(
                (tmp_path / name).read_bytes()
            ).hexdigest()
    elif mutation == "image":
        record["image_digest"] = "sha256:" + "f" * 64
    elif mutation == "profile":
        changed = profile().model_copy(
            update={"prompt_hashes": {"different": "f" * 64}}
        )
        record.update(
            reviewer_profile=changed.model_dump(),
            reviewer_profile_id=changed.profile_id,
        )
    else:
        (tmp_path / "duplicate").mkdir()
        (tmp_path / "duplicate/python-3.12.xml").write_bytes(
            (tmp_path / "python-3.12.xml").read_bytes()
        )
        record["artifact_hashes"]["duplicate/python-3.12.xml"] = hashlib.sha256(
            (tmp_path / "python-3.12.xml").read_bytes()
        ).hexdigest()
    with pytest.raises(ValueError):
        verify_bundle(record, tmp_path, True)


def test_registry_manifest_is_bound_to_tested_config(tmp_path):
    record = retained_release(tmp_path)
    raw = json.dumps(
        {"schemaVersion": 2, "config": {"digest": record["image_digest"]}}
    ).encode()
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    verify_registry_manifest(record, raw, digest)
    with pytest.raises(ValueError):
        verify_registry_manifest(record, raw, "sha256:" + "0" * 64)
    with pytest.raises(ValueError):
        verify_registry_manifest({**record, "image_digest": "different"}, raw, digest)


def test_deterministic_comparison_uses_production_reconciliation_without_models(
    monkeypatch,
):
    from code_review_agent.review_service import ReviewService
    from code_review_agent.reviewer_profile import build_profile

    monkeypatch.setattr(
        "code_review_agent.llm_factory.LLMFactory.create_llm",
        lambda *a, **k: pytest.fail("Deterministic capture must not call a model"),
    )
    result = ReviewService.execute_review(
        raw_diff=DIFF,
        input_snapshot=build_snapshot(
            DIFF, "a" * 40, fetch=lambda _: SOURCE
        ).model_dump(),
        analysis_mode="deterministic",
        evaluation_mode=True,
        bypass_limits=True,
    )
    assert result.reviewer_profile_id == build_profile().profile_id
    assert result.analyzer_status["review"] == "SKIPPED_COMPARISON_DETERMINISTIC"
    assert result.analysis_complete
    assert len({(f.file_path, f.line_number, f.cwe) for f in result.findings}) == len(
        result.findings
    )


def test_release_rejects_replacement_sandbox_cases(tmp_path):
    record = retained_release(tmp_path)
    sandbox = tmp_path / "sandbox.xml"
    raw = sandbox.read_bytes().replace(
        b"test_timeout_stops_container", b"unrelated_passing_test"
    )
    sandbox.write_bytes(raw)
    marker = tmp_path / "sandbox-pass.json"
    data = json.loads(marker.read_text(encoding="utf-8"))
    data["artifact_sha256"] = hashlib.sha256(raw).hexdigest()
    marker.write_text(json.dumps(data), encoding="utf-8")
    record["artifact_hashes"]["sandbox.xml"] = hashlib.sha256(raw).hexdigest()
    record["artifact_hashes"]["sandbox-pass.json"] = hashlib.sha256(
        marker.read_bytes()
    ).hexdigest()
    with pytest.raises(ValueError, match="named mandatory"):
        verify_bundle(record, tmp_path)
