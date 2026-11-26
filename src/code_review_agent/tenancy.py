"""
Tenant context for the review job queue.

Scope today: the SQLite job queue and its HTTP endpoints are tenant-scoped.
Calibration, pilot, suppression and telemetry stores are not yet tenant-aware.

Trust model (when ``REVIEW_MULTI_TENANT=true``):
- A *tenant token* listed in ``REVIEW_TENANT_TOKENS`` is bound to exactly one
  tenant. A client-sent ``X-Tenant-ID`` header may only repeat that tenant;
  any other value is refused (``TenantAccessError`` -> HTTP 403).
- The deployment operator token (``REVIEW_API_TOKEN``) is the single trusted
  administrator and may select a tenant with ``X-Tenant-ID``.
- Webhooks never read ``X-Tenant-ID``; the tenant is the repository owner from
  the signature-verified payload.
"""

import hashlib
import hmac
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_current_tenant: ContextVar[str] = ContextVar("current_tenant", default="default")

_SAFE_TENANT_REGEX = re.compile(r"^[a-zA-Z0-9_\-:.]{1,64}$")


def normalize_tenant_id(tenant_id: str | None) -> str:
    """Normalize and validate a tenant identifier."""
    if not tenant_id:
        return "default"
    cleaned = tenant_id.strip().lower()
    if not cleaned or not _SAFE_TENANT_REGEX.match(cleaned):
        return "default"
    return cleaned


def get_current_tenant() -> str:
    """Retrieve the current active tenant identifier from context."""
    return _current_tenant.get()


def set_current_tenant(tenant_id: str | None) -> str:
    """Set the active tenant for the current thread/task context."""
    val = normalize_tenant_id(tenant_id)
    _current_tenant.set(val)
    return val


@contextmanager
def tenant_scope(tenant_id: str | None) -> Iterator[str]:
    """Execute a block within an explicit tenant isolation context."""
    norm = normalize_tenant_id(tenant_id)
    token = _current_tenant.set(norm)
    try:
        yield norm
    finally:
        _current_tenant.reset(token)


class TenantAccessError(PermissionError):
    """A caller asked for a tenant its credential is not bound to."""


def _header_tenant(headers: dict[str, Any] | None) -> str | None:
    for k, v in (headers or {}).items():
        if k.lower() == "x-tenant-id" and v:
            return normalize_tenant_id(str(v))
    return None


def tenant_for_token(token: str) -> str | None:
    """Return the tenant bound to a tenant token, or None.

    ``REVIEW_TENANT_TOKENS`` is a comma-separated list of
    ``<tenant>:<sha256 hex of token>`` pairs, so raw tenant tokens never sit in
    the server environment.
    """
    raw = os.getenv("REVIEW_TENANT_TOKENS", "")
    if not raw or not token:
        return None
    digest = hashlib.sha256(token.encode()).hexdigest()
    match = None
    for entry in raw.split(","):
        tenant, sep, expected = entry.strip().rpartition(":")
        if not sep or not tenant:
            continue
        # Compare every entry so timing does not reveal the matching position.
        if hmac.compare_digest(digest, expected.strip().lower()):
            match = normalize_tenant_id(tenant)
    return match


def resolve_tenant_from_request(
    headers: dict[str, Any] | None = None,
    repo_identifier: str | None = None,
    bound_tenant: str | None = None,
) -> str:
    """
    Resolve the tenant for a request.

    1. A credential-bound tenant wins; an ``X-Tenant-ID`` header that names a
       different tenant raises ``TenantAccessError``.
    2. Otherwise (trusted operator) an explicit ``X-Tenant-ID`` header.
    3. Repository owner namespace ('owner/repo' or 'github.com/owner/repo' -> 'owner').
    4. ``'default'``.
    """
    requested = _header_tenant(headers)
    if bound_tenant is not None:
        bound = normalize_tenant_id(bound_tenant)
        if requested is not None and requested != bound:
            raise TenantAccessError(
                "This credential is not authorized for the requested tenant."
            )
        return bound
    if requested is not None:
        return requested

    if repo_identifier:
        clean = (
            repo_identifier.replace("https://", "")
            .replace("http://", "")
            .split("?")[0]
        )
        parts = [p for p in clean.strip("/").split("/") if p]
        if parts:
            if "." in parts[0] and len(parts) >= 2:
                owner = parts[1]
            else:
                owner = parts[0]
            owner = owner.split(":")[-1]
            return normalize_tenant_id(owner)

    return "default"


def multi_tenant_enabled() -> bool:
    """Return True only when explicit multi-tenant isolation is enabled."""
    return os.getenv("REVIEW_MULTI_TENANT", "false").lower() == "true"


def resolve_request_tenant(
    headers: dict[str, Any] | None = None,
    repo_identifier: str | None = None,
    bound_tenant: str | None = None,
) -> str:
    """Resolve HTTP request tenant when multi-tenancy is enabled; otherwise 'default'."""
    if not multi_tenant_enabled():
        return "default"
    return resolve_tenant_from_request(
        headers, repo_identifier=repo_identifier, bound_tenant=bound_tenant
    )

