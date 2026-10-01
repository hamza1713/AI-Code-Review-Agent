"""Assemble, verify and inspect a retained release evidence bundle."""

import argparse
import hashlib
import json
import os
from pathlib import Path

from code_review_agent.release import (
    REQUIRED_CHECKS,
    check_evidence,
    evidence_file,
    verify_bundle,
    verify_registry_manifest,
)
from code_review_agent.review_contracts import ReviewerProfile


def assemble(directory: Path, commit: str, profile_path: Path | None = None) -> dict:
    check_evidence(directory, commit)
    smoke = json.loads(evidence_file(directory, "image-smoke.json").read_text(encoding="utf-8"))
    recovery = json.loads(evidence_file(directory, "recovery.json").read_text(encoding="utf-8"))
    profile = ReviewerProfile.model_validate(
        json.loads(profile_path.read_text(encoding="utf-8"))
        if profile_path
        else smoke["reviewer_profile"]
    )
    record = {
        "schema_version": 2,
        "commit": commit,
        "image_digest": smoke["image_digest"],
        "reviewer_profile": profile.model_dump(),
        "reviewer_profile_id": profile.profile_id,
        "checks": dict.fromkeys(REQUIRED_CHECKS, "PASS"),
        "backup_sha256": recovery["backup_sha256"],
        "artifact_hashes": {
            p.relative_to(directory).as_posix(): hashlib.sha256(
                p.read_bytes()
            ).hexdigest()
            for p in sorted(directory.rglob("*"))
            if p.is_file()
        },
        "evidence_reference": os.getenv("GITHUB_SERVER_URL", "https://github.com")
        + "/"
        + os.getenv("GITHUB_REPOSITORY", "local")
        + "/actions/runs/"
        + os.getenv("GITHUB_RUN_ID", "local"),
        "approved_by": None,
        "approval_reference": None,
        "status": "AWAITING_OPERATOR_APPROVAL",
    }
    verify_bundle(record, directory)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--commit")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--record", type=Path)
    parser.add_argument("--require-approval", action="store_true")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--manifest-digest")
    args = parser.parse_args()
    if args.record:
        record = json.loads(args.record.read_text(encoding="utf-8"))
        verify_bundle(record, args.evidence, args.require_approval)
        if args.manifest:
            verify_registry_manifest(
                record, args.manifest.read_bytes(), args.manifest_digest
            )
        print(
            "Release bundle verified"
            + (
                "; registry platform manifest matches tested image"
                if args.manifest
                else ""
            )
        )
    else:
        if not args.commit or not args.output:
            parser.error("assembly requires --commit and --output")
        if args.output.resolve().is_relative_to(args.evidence.resolve()):
            parser.error("release record must be outside its evidence directory")
        args.output.write_text(
            json.dumps(assemble(args.evidence, args.commit, args.profile), indent=2)
            + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
