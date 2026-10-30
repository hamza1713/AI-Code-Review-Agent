"""
Enterprise Key & Secrets Management Interface.
Provides isolated per-tenant credential resolution with support for
environment fallback, encrypted vault backends, and token rotation.
"""

import os

from code_review_agent.tenancy import get_current_tenant, normalize_tenant_id


class SecretsVault:
    """
    Secrets vault provider.
    In local/development environments, resolves from process environment variables.
    In enterprise deployments, fetches credentials dynamically per tenant.
    """

    _tenant_secrets_store: dict[str, dict[str, str]] = {}

    @classmethod
    def register_tenant_secrets(cls, tenant_id: str, secrets: dict[str, str]) -> None:
        """Register encrypted secrets for a specific tenant (used in multi-tenant mode or tests)."""
        tid = normalize_tenant_id(tenant_id)
        if tid not in cls._tenant_secrets_store:
            cls._tenant_secrets_store[tid] = {}
        cls._tenant_secrets_store[tid].update(secrets)

    @classmethod
    def clear_tenant_secrets(cls, tenant_id: str | None = None) -> None:
        """Clear registered tenant secrets."""
        if tenant_id:
            cls._tenant_secrets_store.pop(normalize_tenant_id(tenant_id), None)
        else:
            cls._tenant_secrets_store.clear()

    @classmethod
    def get_secret(
        cls,
        secret_key: str,
        tenant_id: str | None = None,
        default: str | None = None,
    ) -> str | None:
        """
        Resolve a secret for a given tenant.
        Checks registered tenant vault store first, then tenant-prefixed env vars,
        and finally falls back to generic environment variable.
        """
        tid = normalize_tenant_id(tenant_id or get_current_tenant())
        if tid in cls._tenant_secrets_store and secret_key in cls._tenant_secrets_store[tid]:
            return cls._tenant_secrets_store[tid][secret_key]

        # Specific tenant prefixed env var e.g. TENANT_CORP_A_GITHUB_TOKEN
        tenant_env_key = f"TENANT_{tid.upper().replace('-', '_').replace(':', '_')}_{secret_key}"
        tenant_val = os.getenv(tenant_env_key)
        if tenant_val:
            return tenant_val

        # Generic environment variable fallback
        env_val = os.getenv(secret_key)
        if env_val:
            return env_val

        return default

    @classmethod
    def get_git_token(
        cls,
        platform: str = "github",
        tenant_id: str | None = None,
    ) -> str:
        """Resolve platform-specific git token for tenant."""
        plat = platform.lower()
        key_map = {
            "github": "GITHUB_TOKEN",
            "gitlab": "GITLAB_TOKEN",
            "bitbucket": "BITBUCKET_TOKEN",
        }
        secret_key = key_map.get(plat, "GITHUB_TOKEN")
        token = cls.get_secret(secret_key, tenant_id=tenant_id)
        if not token and plat == "github":
            from code_review_agent.config import get_github_token

            token = get_github_token()
        return token or ""

    @classmethod
    def get_tenant_git_token(
        cls,
        tenant_id: str | None = None,
        platform: str = "github",
        repo: str | None = None,
    ) -> str:
        """Alias for get_git_token with explicit tenant parameter."""
        return cls.get_git_token(platform=platform, tenant_id=tenant_id)
