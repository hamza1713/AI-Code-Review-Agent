"""Explicit operator approval for repository code sent to model providers."""

import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

_repository: ContextVar[str] = ContextVar("review_repository", default="local-upload")


def production() -> bool:
    return os.getenv("APP_ENV", "production").lower() == "production"


@contextmanager
def repository_scope(repository: str):
    token = _repository.set(repository.lower())
    try:
        yield
    finally:
        _repository.reset(token)


def require_model_approval(model: str) -> dict:
    """Fail closed before construction/calling; development never grants production consent."""
    if not production():
        return {}
    try:
        policy = json.loads(
            Path(os.environ["REVIEW_DATA_POLICY"]).read_text(encoding="utf-8")
        )
        if not isinstance(policy, dict) or not isinstance(
            policy.get("expires_at"), str
        ):
            raise ValueError("Invalid approval schema")
        if any(
            not isinstance(policy.get(key), list)
            or not all(isinstance(value, str) for value in policy[key])
            for key in ("models", "repositories")
        ):
            raise ValueError("Approval models and repositories must be explicit lists")
        expiry = datetime.fromisoformat(policy["expires_at"].replace("Z", "+00:00"))
        if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
            raise ValueError("Expired approval")
        if not policy.get("approved_by") or not policy.get("approval_reference"):
            raise ValueError("Missing approval attribution")
        if (
            model not in policy["models"]
            or _repository.get() not in policy["repositories"]
        ):
            raise ValueError("Unapproved model or repository")
        if not policy.get("provider_terms_reference") or not policy.get(
            "retention_policy"
        ):
            raise ValueError("Missing transfer/retention terms")
    except (KeyError, TypeError, ValueError, OSError) as error:
        raise PermissionError(
            "Repository code transfer requires a current REVIEW_DATA_POLICY approval"
        ) from error
    return policy
