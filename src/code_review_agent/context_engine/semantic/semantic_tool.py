"""
SemanticContextTool — CrewAI BaseTool exposing the RAG Context Engine to agents.

Mirrors the ergonomics of `CodebaseContextTool`: it is repo_root-scoped and indexes
lazily on first use, so constructing the tool is cheap and the (more expensive)
embedding pass only runs when an agent actually queries it.
"""

from typing import List, Optional, Type

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from code_review_agent.context_engine.semantic.engine import SemanticContextEngine


class SemanticContextInput(BaseModel):
    """Input parameters for SemanticContextTool."""
    query_type: str = Field(
        ...,
        description=(
            "'retrieve_for_diff' (find code across the codebase relevant to a PR diff) "
            "or 'search_similar' (free-text/code query for similar definitions)."
        ),
    )
    target: str = Field(
        ...,
        description="A raw diff (for retrieve_for_diff) or a query string / code snippet (for search_similar).",
    )


class SemanticContextTool(BaseTool):
    """Retrieval-augmented codebase context: semantically related code for a change."""

    name: str = "Semantic Codebase Retrieval (RAG)"
    description: str = (
        "Retrieve semantically related code from across the indexed repository (or "
        "repositories) — similar functions, existing patterns, and cross-service "
        "definitions that do not appear in the diff. Use it to check whether a change "
        "is consistent with conventions already in the codebase and to surface "
        "cross-file or cross-repo impact the raw diff cannot show."
    )
    args_schema: Type[BaseModel] = SemanticContextInput

    _engine: Optional[SemanticContextEngine] = None
    _repo_roots: Optional[List[str]] = None
    _persist_path: Optional[str] = None

    def __init__(
        self,
        repo_root: Optional[str] = None,
        repo_roots: Optional[List[str]] = None,
        persist_path: Optional[str] = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        roots = repo_roots or ([repo_root] if repo_root else ["."])
        self._repo_roots = roots
        self._persist_path = persist_path
        self._engine = None

    def _get_engine(self) -> SemanticContextEngine:
        """Lazily construct and index the engine on first query."""
        if self._engine is None:
            self._engine = SemanticContextEngine(
                repo_roots=self._repo_roots,
                persist_path=self._persist_path,
            )
            self._engine.index()
        return self._engine

    def _run(self, query_type: str, target: str) -> str:
        engine = self._get_engine()
        query_type = (query_type or "").strip().lower()

        if query_type in ("retrieve_for_diff", "diff", "retrieve"):
            return engine.format_semantic_context(target, top_k=8)

        if query_type in ("search_similar", "search", "similar"):
            # Reuse retrieve() by treating the query as a single added line.
            synthetic_diff = "\n".join(f"+{line}" for line in target.splitlines()) or f"+{target}"
            return engine.format_semantic_context(synthetic_diff, top_k=8)

        return (
            f"Unknown query_type '{query_type}'. "
            f"Use 'retrieve_for_diff' or 'search_similar'."
        )
