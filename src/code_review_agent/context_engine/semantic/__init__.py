"""
Semantic RAG Context Engine.

Adds retrieval-augmented context to the code review pipeline: the repository
(or several repositories) is chunked along AST symbol boundaries, embedded, and
stored in a cosine-similarity vector index. For each PR diff the engine retrieves
the most relevant code elsewhere in the codebase — the semantic layer that lets an
agent reason about a change against patterns and callers it cannot see in the diff.

Design goals mirror the rest of the project:
  - Deterministic-first & zero-dependency by default. The default HashingEmbedder
    uses only the standard library, so the whole pipeline indexes, retrieves, and
    is testable without downloading a model or hitting an API.
  - Pluggable. Swap in a real code-embedding model (sentence-transformers, or a
    hosted embeddings API) via RAG_EMBEDDER without touching call sites.
  - Graceful degradation. A missing optional model falls back to the hashing
    embedder with a warning rather than failing the review.

Public surface:
  SemanticContextEngine   — index repositories, retrieve/format context for a diff
  SemanticContextTool     — CrewAI BaseTool wrapper for agents
  get_embedder            — embedder factory
  VectorStore             — cosine-similarity store with disk persistence
"""

from .embeddings import get_embedder, Embedder, HashingEmbedder
from .vector_store import VectorStore, ChunkRecord
from .engine import SemanticContextEngine, ScoredChunk
from .semantic_tool import SemanticContextTool

__all__ = [
    "SemanticContextEngine",
    "ScoredChunk",
    "SemanticContextTool",
    "get_embedder",
    "Embedder",
    "HashingEmbedder",
    "VectorStore",
    "ChunkRecord",
]
