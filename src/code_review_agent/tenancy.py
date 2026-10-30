"""
Multi-Tenant Context and Isolation Engine.
Enforces tenant namespacing across webhooks, API requests, SQLite/PostgreSQL
job queues, finding suppressions, and telemetry stores.
"""

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


def resolve_tenant_from_request(
    headers: dict[str, Any] | None = None,
    repo_identifier: str | None = None,
) -> str:
    """
    Resolve tenant from incoming HTTP headers or repository path.
    Order of precedence:
    1. Explicit 'X-Tenant-ID' or 'x-tenant-id' header
    2. Repository owner namespace (e.g., 'owner/repo' -> 'owner', 'github.com/owner/repo' -> 'owner')
    3. Default tenant ('default')
    """
    headers = headers or {}
    for k, v in headers.items():
        if k.lower() == "x-tenant-id" and v:
            return normalize_tenant_id(str(v))

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


class TenantConfig:
    """Enterprise policy configuration per tenant."""

    def __init__(
        self,
        tenant_id: str,
        max_concurrent_reviews: int = 3,
        rate_limit_per_minute: int = 60,
        zdr_enforced: bool = True,
    ):
        self.tenant_id = normalize_tenant_id(tenant_id)
        self.max_concurrent_reviews = max_concurrent_reviews
        self.rate_limit_per_minute = rate_limit_per_minute
        self.zdr_enforced = zdr_enforced
