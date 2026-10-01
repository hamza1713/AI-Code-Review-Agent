"""Server-generated reviewer identity
output-affecting changes require new evidence."""

import hashlib
import os
import platform
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from code_review_agent.config import get_max_tokens, get_model_name, get_ollama_base_url
from code_review_agent.review_contracts import ReviewerProfile


def build_profile(
    repo_root: str | None = None, team_memory: str = ""
) -> ReviewerProfile:
    package = Path(__file__).parent
    prompts = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (package / "crews/code_review_crew/config").glob("*.yaml")
    }
    prompts["flow"] = hashlib.sha256((package / "main.py").read_bytes()).hexdigest()
    scanners = {}
    for name in ("bandit", "ruff", "semgrep", "pydantic"):
        try:
            scanners[name] = version(name)
        except PackageNotFoundError:
            scanners[name] = "UNAVAILABLE"
    source_hash = hashlib.sha256()
    for file in sorted(package.rglob("*.py")):
        source_hash.update(file.relative_to(package).as_posix().encode())
        source_hash.update(file.read_bytes())
    rules = (
        Path(repo_root) / ".code-review.yaml"
        if repo_root
        else Path(__file__).resolve().parents[2] / ".code-review.yaml"
    )
    rules_hash = (
        hashlib.sha256(rules.read_bytes()).hexdigest() if rules.is_file() else "NONE"
    )
    return ReviewerProfile(
        application_commit=os.getenv("REVIEW_DEPLOYED_COMMIT")
        or os.getenv("REVIEW_SOURCE_COMMIT")
        or os.getenv("GITHUB_SHA")
        or "unreleased-working-tree",
        model=get_model_name(),
        generation_settings={"max_tokens": get_max_tokens(), "temperature": None},
        prompt_hashes=prompts,
        scanner_versions=scanners,
        configuration={
            "python_runtime": platform.python_version(),
            "pricing_sha256": hashlib.sha256(
                os.getenv("REVIEW_MODEL_PRICING_JSON", "{}").encode()
            ).hexdigest(),
            "expected_provider_model": os.getenv("REVIEW_PROVIDER_MODEL_ID")
            or get_model_name().split("/", 1)[-1],
            "ollama_model_digest": os.getenv("REVIEW_OLLAMA_MODEL_DIGEST"),
            "source_tree_sha256": source_hash.hexdigest(),
            "rules_sha256": rules_hash,
            "team_memory_sha256": hashlib.sha256(team_memory.encode()).hexdigest(),
            "rag_enabled": os.getenv("RAG_ENABLED", "false"),
            "rag_model": os.getenv("RAG_EMBEDDING_MODEL", ""),
            "rag_embedder": os.getenv("RAG_EMBEDDER", "hashing"),
            "rag_dimension": os.getenv("RAG_EMBEDDING_DIM", "512"),
            "rag_extra_roots_sha256": hashlib.sha256(
                os.getenv("RAG_REPO_ROOTS", "").encode()
            ).hexdigest(),
            "ollama_endpoint_sha256": hashlib.sha256(
                get_ollama_base_url().encode()
            ).hexdigest(),
            "experimental_tests": os.getenv(
                "REVIEW_ENABLE_EXPERIMENTAL_TESTS", "false"
            ),
            "review_pipeline": "fixed-stages-v1",
            "scanner_settings": "full-source-isolated-v1",
        },
    )


def same_implementation(profile: ReviewerProfile) -> bool:
    current = build_profile()
    return (
        current.application_commit == profile.application_commit
        and current.model == profile.model
        and current.generation_settings == profile.generation_settings
        and current.prompt_hashes == profile.prompt_hashes
        and current.scanner_versions == profile.scanner_versions
        and {
            k: v
            for k, v in current.configuration.items()
            if k not in {"rules_sha256", "team_memory_sha256"}
        }
        == {
            k: v
            for k, v in profile.configuration.items()
            if k not in {"rules_sha256", "team_memory_sha256"}
        }
    )


def same_memory_revision(profile: ReviewerProfile, repository: str) -> bool:
    from code_review_agent.learning.team_memory import TeamMemoryStore

    repo_id = repository.split("/", 1)[-1] if ":" in repository else repository
    current = TeamMemoryStore().format_team_memory_prompt(repo_id)
    return hashlib.sha256(current.encode()).hexdigest() == profile.configuration.get(
        "team_memory_sha256"
    )
