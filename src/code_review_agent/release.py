"""Verify an immutable release bundle before attributed advisory approval."""

import hashlib
import json
import os
from pathlib import Path
from typing import Literal
from xml.etree import ElementTree

from pydantic import BaseModel, Field, model_validator

from code_review_agent.path_validation import safe_diff_path
from code_review_agent.review_contracts import ReviewerProfile

REQUIRED_CHECKS = {
    "python-3.10",
    "python-3.11",
    "python-3.12",
    "python-3.13",
    "ruff",
    "typing",
    "dependencies",
    "secrets",
    "frontend-build",
    "frontend-lint",
    "browser",
    "sandbox-isolation",
    "image-smoke",
    "recovery-drill",
    "path-security-linux",
    "path-security-windows",
}


class ReleaseRecord(BaseModel):
    schema_version: Literal[2] = 2
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    reviewer_profile: ReviewerProfile
    reviewer_profile_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    checks: dict[str, str]
    backup_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_hashes: dict[str, str]
    evidence_reference: str = Field(min_length=1)
    approved_by: str | None = None
    approval_reference: str | None = None
    status: Literal["AWAITING_OPERATOR_APPROVAL", "APPROVED"] = (
        "AWAITING_OPERATOR_APPROVAL"
    )
    registry_manifest_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def identity(self):
        if (
            self.reviewer_profile.profile_id != self.reviewer_profile_id
            or self.reviewer_profile.application_commit != self.commit
        ):
            raise ValueError("Release reviewer profile identity mismatch")
        if not self.artifact_hashes or any(
            self.checks.get(name) != "PASS" for name in REQUIRED_CHECKS
        ):
            raise ValueError("Required release checks have not all passed")
        return self


def evidence_file(root: Path, name: str) -> Path:
    matches = [p for p in root.rglob(name) if p.is_file()]
    if len(matches) != 1:
        raise ValueError("Missing or ambiguous release evidence: " + name)
    return matches[0]


def check_evidence(root: Path, commit: str) -> None:
    for version in ("3.10", "3.11", "3.12", "3.13"):
        artifact = evidence_file(root, f"python-{version}.xml")
        marker = json.loads(
            evidence_file(root, f"python-{version}.json").read_text(encoding="utf-8")
        )
        if (
            marker.get("status") != "PASS"
            or marker.get("commit") != commit
            or marker.get("artifact_sha256")
            != hashlib.sha256(artifact.read_bytes()).hexdigest()
        ):
            raise ValueError("Python matrix result does not match commit or artifact")
    sandbox = evidence_file(root, "sandbox.xml")
    marker = json.loads(
        evidence_file(root, "sandbox-pass.json").read_text(encoding="utf-8")
    )
    if (
        marker.get("commit") != commit
        or marker.get("status") != "PASS"
        or marker.get("artifact_sha256")
        != hashlib.sha256(sandbox.read_bytes()).hexdigest()
    ):
        raise ValueError("Sandbox execution does not match the release commit")
    for file in [*root.rglob("*.xml")]:
        report = ElementTree.parse(file).getroot()
        suites = (
            [report] if report.tag == "testsuite" else list(report.iter("testsuite"))
        )
        tests = sum(int(s.attrib.get("tests", 0)) for s in suites)
        skipped = sum(int(s.attrib.get("skipped", 0)) for s in suites)
        if (
            not suites
            or tests <= skipped
            or any(
                int(s.attrib.get("failures", 0)) or int(s.attrib.get("errors", 0))
                for s in suites
            )
        ):
            raise ValueError(
                "Empty or failing test evidence: " + str(file.relative_to(root))
            )
        if file == sandbox and (skipped or tests < 4):
            raise ValueError("All four mandatory sandbox checks must execute")
    sandbox_cases = {
        case.attrib.get("name")
        for case in ElementTree.parse(sandbox).getroot().iter("testcase")
        if not any(
            case.find(tag) is not None for tag in ("skipped", "failure", "error")
        )
    }
    mandatory_cases = {
        "test_passing_tests_in_container",
        "test_failed_generated_assertion_is_not_proof",
        "test_network_host_files_and_secrets_are_isolated",
        "test_timeout_stops_container",
    }
    if not mandatory_cases.issubset(sandbox_cases):
        raise ValueError("All four named mandatory sandbox checks must execute")
    for name in (
        "static-pass.json",
        "frontend-pass.json",
        "secrets-pass.json",
        "path-security-linux.json",
        "path-security-windows.json",
    ):
        data = json.loads(evidence_file(root, name).read_text(encoding="utf-8"))
        if data.get("status") != "PASS" or data.get("commit") != commit:
            raise ValueError("Gate evidence does not match the release commit: " + name)
    smoke = json.loads(
        evidence_file(root, "image-smoke.json").read_text(encoding="utf-8")
    )
    recovery = json.loads(
        evidence_file(root, "recovery.json").read_text(encoding="utf-8")
    )
    if (
        smoke.get("status") != "PASS"
        or smoke.get("commit") != commit
        or recovery.get("status") != "PASS"
        or recovery.get("commit") != commit
    ):
        raise ValueError("Image or recovery gate failed")
    audit = json.loads(
        evidence_file(root, "python-audit.json").read_text(encoding="utf-8")
    )
    dependencies = audit.get("dependencies", []) if isinstance(audit, dict) else audit
    if (
        not isinstance(dependencies, list)
        or not dependencies
        or any(d.get("vulns") for d in dependencies)
    ):
        raise ValueError("Dependency evidence is empty or reports vulnerabilities")
    frontend_audit = json.loads(
        evidence_file(root, "frontend-audit.json").read_text(encoding="utf-8")
    )
    vulnerabilities = frontend_audit.get("metadata", {}).get("vulnerabilities")
    if (
        not isinstance(vulnerabilities, dict)
        or vulnerabilities.get("total") != 0
        or any(vulnerabilities.values())
    ):
        raise ValueError(
            "Frontend dependency evidence is missing or reports vulnerabilities"
        )


def verify_bundle(
    record: dict, evidence_dir: str | Path, require_approval: bool = False
) -> ReleaseRecord:
    release = ReleaseRecord.model_validate(record)
    root = Path(evidence_dir).resolve()
    for name, expected in release.artifact_hashes.items():
        path = safe_diff_path(root, name)
        if (
            not path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest() != expected
        ):
            raise ValueError("Release artifact hash mismatch: " + name)
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    if actual != set(release.artifact_hashes):
        raise ValueError("Release evidence manifest is incomplete")
    check_evidence(root, release.commit)
    smoke = json.loads(
        evidence_file(root, "image-smoke.json").read_text(encoding="utf-8")
    )
    recovery = json.loads(
        evidence_file(root, "recovery.json").read_text(encoding="utf-8")
    )
    tested_profile = ReviewerProfile.model_validate(smoke.get("reviewer_profile"))
    if (
        tested_profile.application_commit != release.commit
        or tested_profile.prompt_hashes != release.reviewer_profile.prompt_hashes
        or tested_profile.scanner_versions != release.reviewer_profile.scanner_versions
        or tested_profile.configuration.get("source_tree_sha256")
        != release.reviewer_profile.configuration.get("source_tree_sha256")
    ):
        raise ValueError("Reviewer implementation differs from the tested image")
    if (
        smoke.get("image_digest") != release.image_digest
        or recovery.get("backup_sha256") != release.backup_sha256
    ):
        raise ValueError("Release image or backup differs from retained evidence")
    if require_approval and (
        release.status != "APPROVED"
        or not release.approved_by
        or not release.approved_by.strip()
        or not release.approval_reference
        or not release.approval_reference.strip()
    ):
        raise ValueError("Release requires attributed operator approval")
    if release.registry_manifest_digest:
        verify_registry_manifest(
            record,
            evidence_file(root, "registry-manifest.json").read_bytes(),
            release.registry_manifest_digest,
        )
    return release


def validate_release(record: dict, evidence_dir: str | Path | None = None) -> None:
    directory = evidence_dir or os.getenv("REVIEW_RELEASE_EVIDENCE_DIR")
    if not directory:
        raise ValueError("Release approval requires retained evidence bytes")
    verify_bundle(record, directory, require_approval=True)


def verify_registry_manifest(record: dict, raw_manifest: bytes, digest: str) -> None:
    if "sha256:" + hashlib.sha256(raw_manifest).hexdigest() != digest:
        raise ValueError("Registry manifest digest mismatch")
    manifest = json.loads(raw_manifest)
    if manifest.get("config", {}).get("digest") != record.get("image_digest"):
        raise ValueError(
            "Registry manifest does not resolve to the smoke-tested image; provide its platform manifest"
        )
