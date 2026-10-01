"""Server-owned authorization policy shared by HTTP and queued operations."""

import os
from urllib.parse import urlsplit


def development_access() -> bool:
    return (
        os.getenv("APP_ENV", "production").lower() == "development"
        and os.getenv("REVIEW_REQUIRE_AUTH", "true").lower() == "false"
    )


def validate_configuration() -> None:
    if not development_access() and len(os.getenv("REVIEW_API_TOKEN", "")) < 32:
        raise RuntimeError("REVIEW_API_TOKEN must contain at least 32 characters.")


def validate_target(platform: str, host: str, repository: str) -> None:
    """Identifiers are namespaced by provider and host; empty production lists deny."""
    host = host.lower().rstrip(".")
    defaults = {
        "github": "github.com",
        "gitlab": "gitlab.com",
        "bitbucket": "bitbucket.org",
    }
    allowed_hosts = {
        h.strip().lower()
        for h in os.getenv("REVIEW_ALLOWED_HOSTS", ",".join(defaults.values())).split(
            ","
        )
        if h.strip()
    }
    if platform not in defaults or host not in allowed_hosts:
        raise PermissionError("Repository provider or host is not allowed.")
    allowed = {
        r.strip().lower()
        for r in os.getenv("REVIEW_ALLOWED_REPOSITORIES", "").split(",")
        if r.strip()
    }
    key = f"{platform}:{host}/{repository}".lower()
    repo_lower = repository.lower()
    if (
        "*" in allowed
        or key in allowed
        or repo_lower in allowed
        or f"{host}/{repo_lower}" in allowed
        or (development_access() and not allowed)
    ):
        return
    raise PermissionError("Repository is not enabled for this deployment.")


def validate_pr_target(identifier: str):
    from code_review_agent.platform.factory import get_platform_client

    if "://" in identifier:
        url = urlsplit(identifier)
        if (
            url.scheme != "https"
            or url.username
            or url.password
            or url.port not in (None, 443)
        ):
            raise PermissionError(
                "Only HTTPS provider targets without credentials are allowed"
            )
    client, ident = get_platform_client(identifier)
    host = urlsplit(identifier).hostname if "://" in identifier else None
    host = host or {
        "github": "github.com",
        "gitlab": "gitlab.com",
        "bitbucket": "bitbucket.org",
    }.get(ident.platform, "")
    validate_target(
        ident.platform, host, f"{ident.owner_or_project}/{ident.repo_or_slug}"
    )
    return client, ident, host


def effective_command(command: str, args: str = "") -> str:
    return (
        args.split()[0].lower() if command == "review" and args.split() else command
    ).replace("_", "-")


PRIVILEGED_COMMANDS = {"apply", "suppress", "unsuppress"}
FEEDBACK_COMMANDS = {"fp", "confirm", "audit-confirm", "audit-reject"}
READ_COMMANDS = {
    "help",
    "calibration",
    "cried-wolf",
    "fp-rate",
    "fpr",
    "audit",
    "audit-sample",
}


def authorize_actor(
    client, owner: str, repo: str, actor: str, command: str, platform: str = "github"
) -> bool:
    if not actor or actor.endswith("[bot]"):
        return False
    if command in READ_COMMANDS:
        return True
    if platform == "github":
        try:
            role = client.get_repository_permission(owner, repo, actor)
        except Exception:
            return False
        if command in PRIVILEGED_COMMANDS:
            return role in {"maintain", "admin"}
        return role in {"write", "maintain", "admin"}
    setting = (
        "BOT_MAINTAINER_USERS"
        if command in PRIVILEGED_COMMANDS
        else "BOT_ALLOWED_USERS"
    )
    allowed = {
        u.strip().lower() for u in os.getenv(setting, "").split(",") if u.strip()
    }
    return actor.lower() in allowed


def require_remediation_enabled() -> None:
    if (
        os.getenv("REVIEW_ENABLE_REMEDIATION", "false").lower() != "true"
        and not development_access()
    ):
        raise PermissionError("Remediation is disabled on this deployment.")
