"""Synthetic retained evidence, exclusively for release rejection tests."""

import hashlib
import json
from pathlib import Path

from code_review_agent.release import REQUIRED_CHECKS
from code_review_agent.reviewer_profile import build_profile


def profile():
    from code_review_agent.learning.team_memory import TeamMemoryStore

    return build_profile(
        team_memory=TeamMemoryStore().format_team_memory_prompt("org/repo")
    ).model_copy(update={"application_commit": "a" * 40})


def retained_release(root: Path) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    reviewer = profile()
    commit = "a" * 40

    def write(name, data):
        (root / name).write_text(json.dumps(data), encoding="utf-8")

    for name in [*(f"python-{v}" for v in ("3.10", "3.11", "3.12", "3.13")), "sandbox"]:
        raw = b'<testsuites><testsuite tests="4" failures="0" errors="0" skipped="0"/></testsuites>'
        if name == "sandbox":
            cases = [
                "test_passing_tests_in_container",
                "test_failed_generated_assertion_is_not_proof",
                "test_network_host_files_and_secrets_are_isolated",
                "test_timeout_stops_container",
            ]
            raw = (
                '<testsuites><testsuite tests="4" failures="0" errors="0" skipped="0">'
                + "".join(f'<testcase name="{case}"/>' for case in cases)
                + "</testsuite></testsuites>"
            ).encode()
        (root / (name + ".xml")).write_bytes(raw)
        write(
            (name if name != "sandbox" else "sandbox-pass") + ".json",
            {
                "status": "PASS",
                "commit": commit,
                "artifact_sha256": hashlib.sha256(raw).hexdigest(),
            },
        )
    for name in (
        "static-pass",
        "frontend-pass",
        "secrets-pass",
        "path-security-linux",
        "path-security-windows",
    ):
        write(name + ".json", {"status": "PASS", "commit": commit})
    write(
        "image-smoke.json",
        {
            "status": "PASS",
            "commit": commit,
            "image_digest": "sha256:" + "b" * 64,
            "reviewer_profile": reviewer.model_dump(),
        },
    )
    write(
        "recovery.json", {"status": "PASS", "commit": commit, "backup_sha256": "c" * 64}
    )
    write(
        "python-audit.json",
        {"dependencies": [{"name": "example", "version": "1", "vulns": []}]},
    )
    write("frontend-audit.json", {"metadata": {"vulnerabilities": {"total": 0}}})
    return {
        "schema_version": 2,
        "commit": commit,
        "image_digest": "sha256:" + "b" * 64,
        "reviewer_profile": reviewer.model_dump(),
        "reviewer_profile_id": reviewer.profile_id,
        "approved_by": "operator",
        "status": "APPROVED",
        "approval_reference": "synthetic fixture only",
        "backup_sha256": "c" * 64,
        "evidence_reference": "synthetic fixture only",
        "checks": dict.fromkeys(REQUIRED_CHECKS, "PASS"),
        "artifact_hashes": {
            p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*")
            if p.is_file()
        },
    }
