"""
SemanticContextEngine — the RAG layer of the code review pipeline.

Pipeline (mirrors Qodo's Context Engine, built on infrastructure already in this
repo):

    index:     CodeGraphIndexer symbols  →  AST-boundary chunks  →  embeddings  →  VectorStore
    retrieve:  PR diff  →  query embedding  →  cosine search  →  re-rank  →  noise filter  →  diversify
    inject:    formatted context string handed to the crew agents

Chunking reuses the project's existing `CodeGraphIndexer`, so chunks land on real
function/method/class boundaries rather than fixed-size windows. Multiple repo roots
can be indexed into one store, giving cross-repository review context: a change in one
service can be reasoned about against callers and patterns in a sibling service.
"""

import os
import hashlib
from pathlib import Path
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Sequence

from code_review_agent.config import logger
from code_review_agent.context_engine.code_graph import CodeGraphIndexer
from code_review_agent.diff_parser import DiffParser
from code_review_agent.context_engine.semantic.embeddings import Embedder, get_embedder, tokenize_code
from code_review_agent.context_engine.semantic.vector_store import VectorStore, ChunkRecord


_EXT_LANGUAGE = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".go": "go", ".java": "java",
}

# Chunk source is capped so a giant function does not dominate an embedding or a prompt.
_MAX_CHUNK_CHARS = 2000
# Re-ranking only reshuffles a shortlist; this is how deep the shortlist goes.
_RERANK_POOL = 30
# Weight of the lexical identifier-overlap bonus added on top of cosine during re-rank.
_LEXICAL_WEIGHT = 0.15
# Max chunks surfaced from any single file, so results stay diverse across the codebase.
_MAX_PER_FILE = 2


@dataclass
class ScoredChunk:
    """A retrieved chunk with its final (post-re-rank) relevance score."""
    record: ChunkRecord
    score: float
    cosine: float
    lexical: float


class SemanticContextEngine:
    """Indexes one or more repositories and retrieves semantically relevant code for a diff."""

    def __init__(
        self,
        repo_roots: Optional[Sequence[str]] = None,
        embedder: Optional[Embedder] = None,
        persist_path: Optional[str] = None,
    ):
        if repo_roots is None:
            repo_roots = ["."]
        elif isinstance(repo_roots, (str, Path)):
            repo_roots = [str(repo_roots)]
        self.repo_roots = [str(Path(r).resolve()) for r in repo_roots]

        self.embedder = embedder or get_embedder()
        self.store = VectorStore()
        self.persist_path = persist_path
        self._indexed = False

    # ── Indexing ─────────────────────────────────────────────────────────────
    def index(self, max_files_per_repo: int = 500, force: bool = False) -> int:
        """
        Build (or load) the semantic index for all configured repositories.

        Returns the number of chunks in the store. If `persist_path` is set and a
        saved index exists, it is loaded instead of re-embedding, unless `force`.
        """
        if self._indexed and not force:
            return len(self.store)

        if self.persist_path and not force and self.store.load(self.persist_path, embedder_id=self.embedder.identity()):
            self._indexed = True
            return len(self.store)

        total_added = 0
        for repo_root in self.repo_roots:
            repo_name = Path(repo_root).name or repo_root
            try:
                added = self._index_repo(repo_root, repo_name, max_files_per_repo)
                total_added += added
            except Exception as e:
                logger.warning(f"RAG indexing failed for repo '{repo_root}': {e}")

        self._indexed = True
        logger.info(
            f"🧠 Semantic Context Engine indexed {total_added} chunk(s) across "
            f"{len(self.repo_roots)} repo(s) using the '{self.embedder.name}' embedder."
        )
        if self.persist_path:
            try:
                self.store.save(self.persist_path, embedder_id=self.embedder.identity())
            except Exception as e:
                logger.warning(f"Could not persist RAG index: {e}")
        return len(self.store)

    def _index_repo(self, repo_root: str, repo_name: str, max_files: int) -> int:
        """Chunk a single repository along AST boundaries and embed each chunk."""
        indexer = CodeGraphIndexer(repo_root=repo_root)
        indexer.index_repository(max_files=max_files)

        # A small cache of file contents so we read each source file at most once.
        file_cache: Dict[str, List[str]] = {}
        records: List[ChunkRecord] = []
        texts: List[str] = []

        for sym in indexer.symbols.values():
            if sym.kind not in ("function", "method", "class"):
                continue
            lines = self._read_file_lines(repo_root, sym.file_path, file_cache)
            if lines is None:
                continue

            snippet = self._slice_source(lines, sym.line_start, sym.line_end)
            if not snippet.strip():
                continue

            ext = Path(sym.file_path).suffix.lower()
            language = _EXT_LANGUAGE.get(ext, "unknown")

            # The text used for both embedding and retrieval display: a compact,
            # signal-rich header (qualified name + docstring) followed by the body.
            header = f"{sym.kind} {sym.qualified_name}"
            if sym.docstring:
                header += f"\n\"\"\"{sym.docstring.strip()}\"\"\""
            chunk_text = f"{header}\n{snippet}"[:_MAX_CHUNK_CHARS]

            chunk_id = hashlib.blake2b(
                f"{repo_name}:{sym.qualified_name}:{sym.line_start}".encode("utf-8"),
                digest_size=12,
            ).hexdigest()

            records.append(ChunkRecord(
                id=chunk_id,
                repo=repo_name,
                file_path=sym.file_path,
                qualified_name=sym.qualified_name or f"{sym.file_path}:{sym.name}",
                kind=sym.kind,
                language=language,
                line_start=sym.line_start,
                line_end=sym.line_end,
                text=chunk_text,
                docstring=sym.docstring,
            ))
            texts.append(chunk_text)

        if not records:
            return 0

        vectors = self.embedder.embed_batch(texts)
        for rec, vec in zip(records, vectors):
            rec.vector = vec

        self.store.clear_repo(repo_name)
        return self.store.add(records)

    @staticmethod
    def _read_file_lines(repo_root: str, rel_path: str, cache: Dict[str, List[str]]) -> Optional[List[str]]:
        """Read and cache a source file's lines; returns None if unreadable."""
        if rel_path in cache:
            return cache[rel_path]
        abs_path = Path(repo_root) / rel_path
        try:
            with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.read().splitlines()
        except Exception:
            cache[rel_path] = None  # type: ignore[assignment]
            return None
        cache[rel_path] = lines
        return lines

    @staticmethod
    def _slice_source(lines: List[str], line_start: int, line_end: int) -> str:
        """Extract [line_start, line_end] (1-indexed, inclusive), clamped to the file."""
        start = max(1, line_start)
        end = min(len(lines), max(start, line_end))
        return "\n".join(lines[start - 1:end])

    # ── Retrieval ────────────────────────────────────────────────────────────
    def retrieve(self, diff: str, top_k: int = 8, repo: Optional[str] = None) -> List[ScoredChunk]:
        """
        Retrieve the most relevant chunks for a PR diff.

        Applies, in order: cosine search over a shortlist, a lexical re-rank against
        the diff's changed identifiers, a noise filter that drops the very code being
        changed and off-language chunks, and per-file diversification.
        """
        if not self._indexed:
            self.index()
        if len(self.store) == 0 or not diff or not diff.strip():
            return []

        changed_files, changed_line_map, diff_languages, query_text = self._analyze_diff(diff)
        if not query_text.strip():
            return []

        query_vec = self.embedder.embed_query(query_text)
        query_tokens: Set[str] = set(tokenize_code(query_text))

        def _keep(rec: ChunkRecord) -> bool:
            # Optional hard scope to a single repo.
            if repo and rec.repo != repo:
                return False
            # Language filter — only when the diff has recognizable languages.
            if diff_languages and rec.language not in diff_languages and rec.language != "unknown":
                return False
            # Dedup vs the PR: drop a chunk that IS (overlaps) the changed code itself.
            if rec.file_path in changed_files:
                changed_lines = changed_line_map.get(rec.file_path, set())
                if any(rec.line_start <= ln <= rec.line_end for ln in changed_lines):
                    return False
            return True

        # Cosine shortlist, then lexical re-rank on that pool only (cheap).
        pool = self.store.search(query_vec, top_k=_RERANK_POOL, filter_fn=_keep)
        rescored: List[ScoredChunk] = []
        for rec, cos in pool:
            overlap = self._lexical_overlap(query_tokens, rec.text)
            rescored.append(ScoredChunk(
                record=rec,
                score=cos + _LEXICAL_WEIGHT * overlap,
                cosine=cos,
                lexical=overlap,
            ))
        rescored.sort(key=lambda s: s.score, reverse=True)

        # Diversify: cap chunks per file so one file cannot fill the whole result.
        diversified: List[ScoredChunk] = []
        per_file: Dict[str, int] = {}
        for sc in rescored:
            key = f"{sc.record.repo}:{sc.record.file_path}"
            if per_file.get(key, 0) >= _MAX_PER_FILE:
                continue
            per_file[key] = per_file.get(key, 0) + 1
            diversified.append(sc)
            if len(diversified) >= top_k:
                break
        return diversified

    def format_semantic_context(self, diff: str, top_k: int = 8, repo: Optional[str] = None) -> str:
        """Render retrieved context as a prompt-ready block for the crew agents."""
        results = self.retrieve(diff, top_k=top_k, repo=repo)
        if not results:
            return "No semantically related code found in the indexed repositories."

        cross_repo = len({r.record.repo for r in results}) > 1
        header = "🧠 Semantically Related Code (retrieved from the indexed codebase"
        header += ", spanning multiple repositories):" if cross_repo else "):"
        lines = [header]
        for sc in results:
            rec = sc.record
            loc = f"{rec.repo}/{rec.file_path}:L{rec.line_start}-L{rec.line_end}"
            doc = f" — {rec.docstring.strip().splitlines()[0]}" if rec.docstring else ""
            lines.append(
                f"- `{rec.qualified_name}` ({rec.kind}, {rec.language}) "
                f"[relevance {sc.score:.2f}] in `{loc}`{doc}"
            )
        lines.append(
            "\nUse these related definitions to reason about consistency with existing "
            "patterns, cross-file/cross-service impact, and whether the change matches "
            "conventions already established in the codebase."
        )
        return "\n".join(lines)

    # ── Diff analysis helpers ────────────────────────────────────────────────
    @staticmethod
    def _analyze_diff(diff: str):
        """
        Extract from a unified diff: the set of changed files, a map of changed line
        numbers per file, the languages involved, and a query text built from the
        added lines (the code the reviewer is actually introducing).
        """
        changed_files: Set[str] = set()
        changed_line_map: Dict[str, Set[int]] = {}
        languages: Set[str] = set()
        added_text_parts: List[str] = []

        try:
            parsed = DiffParser.parse_diff(diff)
            for file_diff in parsed.files:
                path = file_diff.target_file
                changed_files.add(path)
                ext = Path(path).suffix.lower()
                lang = _EXT_LANGUAGE.get(ext)
                if lang:
                    languages.add(lang)
                added = DiffParser.extract_added_lines_with_numbers(file_diff)
                changed_line_map.setdefault(path, set())
                for line_no, text in added:
                    changed_line_map[path].add(line_no)
                    added_text_parts.append(text)
        except Exception as e:
            logger.debug(f"RAG diff analysis fell back to raw parsing: {e}")

        # Fallback: if structured parsing yielded nothing, scrape '+' lines directly.
        if not added_text_parts:
            for line in diff.splitlines():
                if line.startswith("+") and not line.startswith("+++"):
                    added_text_parts.append(line[1:])

        return changed_files, changed_line_map, languages, "\n".join(added_text_parts)

    @staticmethod
    def _lexical_overlap(query_tokens: Set[str], chunk_text: str) -> float:
        """Jaccard overlap between the query's identifiers and a chunk's identifiers."""
        if not query_tokens:
            return 0.0
        chunk_tokens = set(tokenize_code(chunk_text))
        if not chunk_tokens:
            return 0.0
        inter = len(query_tokens & chunk_tokens)
        union = len(query_tokens | chunk_tokens)
        return inter / union if union else 0.0
