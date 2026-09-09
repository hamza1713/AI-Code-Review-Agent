"""
Cosine-similarity vector store for the Semantic RAG Context Engine.

A dependency-free, in-memory store with optional JSON persistence. Vectors are
stored already L2-normalized (the embedders guarantee this), so a cosine query
reduces to a dot product. Records are namespaced by `repo`, so a single store can
hold several repositories at once and searches can be scoped or left cross-repo.

For very large indexes this can be swapped for a real vector DB (Qdrant, LanceDB)
behind the same `add` / `search` surface; the engine does not depend on the backend.
"""

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Dict, List, Optional

from code_review_agent.config import logger


@dataclass
class ChunkRecord:
    """A single embedded code chunk plus its provenance metadata."""
    id: str
    repo: str
    file_path: str
    qualified_name: str
    kind: str            # function | method | class
    language: str
    line_start: int
    line_end: int
    text: str            # the chunk source used for retrieval / display
    docstring: Optional[str] = None
    vector: List[float] = field(default_factory=list)

    def payload(self) -> Dict:
        """Metadata without the (large) vector — for serialization and display."""
        d = asdict(self)
        d.pop("vector", None)
        return d


def cosine(a: List[float], b: List[float]) -> float:
    """Dot product of two vectors. With normalized inputs this is cosine similarity."""
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


class VectorStore:
    """In-memory cosine store with repo namespacing and JSON persistence."""

    def __init__(self):
        self._records: List[ChunkRecord] = []
        self._ids: set = set()

    def __len__(self) -> int:
        return len(self._records)

    @property
    def repos(self) -> List[str]:
        """Distinct repositories currently represented in the index."""
        return sorted({r.repo for r in self._records})

    def add(self, records: List[ChunkRecord]) -> int:
        """Add records, skipping any whose id is already present. Returns count added."""
        added = 0
        for rec in records:
            if rec.id in self._ids:
                continue
            self._records.append(rec)
            self._ids.add(rec.id)
            added += 1
        return added

    def clear_repo(self, repo: str) -> int:
        """Drop all records for one repo (used before re-indexing it). Returns count removed."""
        before = len(self._records)
        self._records = [r for r in self._records if r.repo != repo]
        self._ids = {r.id for r in self._records}
        return before - len(self._records)

    def search(
        self,
        query_vector: List[float],
        top_k: int = 10,
        filter_fn: Optional[Callable[[ChunkRecord], bool]] = None,
    ) -> List[tuple]:
        """
        Return the top_k (record, score) pairs by cosine similarity.

        `filter_fn` pre-filters candidates (e.g. to a language or repo, or to
        exclude chunks that are part of the PR itself).
        """
        scored: List[tuple] = []
        for rec in self._records:
            if filter_fn and not filter_fn(rec):
                continue
            scored.append((rec, cosine(query_vector, rec.vector)))
        scored.sort(key=lambda t: t[1], reverse=True)
        return scored[:top_k]

    # ── Persistence ──────────────────────────────────────────────────────────
    def save(self, path: str, embedder_id: Optional[str] = None) -> str:
        """
        Persist the full index (vectors included) to a JSON file.

        `embedder_id` (e.g. "gemini:3072") is stamped into the header so a later
        load can refuse an index built by a different embedder — otherwise a cosine
        query across mismatched dimensions silently returns nothing.
        """
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": 2,
            "embedder_id": embedder_id,
            "records": [asdict(r) for r in self._records],
        }
        with open(target, "w", encoding="utf-8") as f:
            json.dump(data, f)
        logger.info(f"💾 RAG vector store saved: {len(self._records)} chunks → {target}")
        return str(target)

    def load(self, path: str, embedder_id: Optional[str] = None) -> bool:
        """
        Load an index from disk. Returns False if the file is absent, unreadable, or
        was built by a different embedder than `embedder_id` (so the caller re-indexes
        instead of querying an incompatible index).
        """
        target = Path(path)
        if not target.exists():
            return False
        try:
            with open(target, "r", encoding="utf-8") as f:
                data = json.load(f)

            saved_id = data.get("embedder_id")
            if embedder_id is not None and saved_id is not None and saved_id != embedder_id:
                logger.info(
                    f"↻ RAG index at {target} was built with embedder '{saved_id}', "
                    f"but current embedder is '{embedder_id}'. Ignoring stale index; will re-index."
                )
                return False

            self._records = [ChunkRecord(**r) for r in data.get("records", [])]
            self._ids = {r.id for r in self._records}
            logger.info(f"📂 RAG vector store loaded: {len(self._records)} chunks from {target}")
            return True
        except Exception as e:
            logger.warning(f"Could not load RAG vector store '{target}': {e}")
            return False
