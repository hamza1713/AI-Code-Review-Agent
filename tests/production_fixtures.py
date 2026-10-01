"""Explicit publication/evidence fixtures for calibration and queue transition tests."""


def recorded_finding(store, *args, **kwargs):
    # Tests requesting reproduced evidence provide a finding-specific recorded result.
    fingerprint = kwargs.get("fingerprint", args[1] if len(args) > 1 else "")
    if kwargs.get("evidence_grade") == "REPRODUCED":
        kwargs.setdefault("head_sha", "fixture-reviewed-commit")
        kwargs.setdefault(
            "evidence_provenance",
            {
                "finding_fingerprint": fingerprint,
                "commit_sha": kwargs["head_sha"],
                "executed": True,
                "test_id": "fixture-test",
                "status": "REPRODUCED",
            },
        )
    kwargs.setdefault("provider_review_id", "fixture-provider-receipt")
    return store.record_finding(*args, **kwargs)


def active_claim(queue, job_id):
    return (
        queue._get_connection()
        .execute("SELECT claim_token FROM webhook_jobs WHERE job_id=?", (job_id,))
        .fetchone()[0]
    )


def versioned_response(response):
    """Server identity for synthetic publication fixtures, never pilot evidence."""
    import hashlib

    from code_review_agent.reviewer_profile import build_profile

    reviewer = build_profile()
    response.reviewer_profile = reviewer
    response.reviewer_profile_id = reviewer.profile_id
    response.input_snapshot_hash = hashlib.sha256(
        (response.reviewed_diff or "").encode()
    ).hexdigest()
    return response
