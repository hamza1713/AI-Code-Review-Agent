"""
Pluggable embedding providers for the Semantic RAG Context Engine.

The default `HashingEmbedder` is a pure-standard-library feature-hashing embedder:
it tokenizes source into identifier sub-words plus character n-grams and hashes them
into a fixed-dimension, L2-normalized vector. It has no external dependencies, is
fully deterministic (identical input → identical vector), and gives lexically
similar code a high cosine similarity — enough for a functional retrieval baseline
and for tests that must run offline.

For production-grade semantic retrieval, set RAG_EMBEDDER=sentence-transformers and
RAG_EMBEDDING_MODEL to a code-aware model; the factory imports it lazily and falls
back to hashing (with a warning) if the dependency or model is unavailable.
"""

import os
import re
import math
import hashlib
from abc import ABC, abstractmethod
from typing import List, Optional

from code_review_agent.config import get_gemini_api_key, logger


# camelCase / PascalCase boundary → split "parseDiffContent" into parts
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_WORD = re.compile(r"[^A-Za-z0-9]+")


def tokenize_code(text: str) -> List[str]:
    """
    Break source text into lowercased sub-word tokens.

    Splits on non-word characters and on camelCase boundaries, then lowercases,
    so `getUserById`, `get_user_by_id`, and `GetUserByID` all yield the same
    token stream and therefore embed near each other.
    """
    tokens: List[str] = []
    for raw in _NON_WORD.split(text):
        if not raw:
            continue
        for part in _CAMEL_BOUNDARY.split(raw):
            part = part.strip().lower()
            if part:
                tokens.append(part)
    return tokens


class Embedder(ABC):
    """Abstract embedding provider. Implementations return L2-normalized vectors."""

    dimension: int = 0
    name: str = "abstract"

    @abstractmethod
    def embed(self, text: str) -> List[float]:
        """Embed a single string into an L2-normalized vector."""
        raise NotImplementedError

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """Embed many strings. Providers with a native batch API should override."""
        return [self.embed(t) for t in texts]

    def embed_query(self, text: str) -> List[float]:
        """
        Embed a retrieval *query* (the PR change), as opposed to an indexed document.
        Providers with asymmetric query/document encoders should override; the default
        treats a query like any other text.
        """
        return self.embed(text)

    def identity(self) -> str:
        """Stable id of this embedder (name + dimension) for index-provenance checks."""
        return f"{self.name}:{self.dimension}"


class HashingEmbedder(Embedder):
    """
    Zero-dependency feature-hashing embedder.

    Uses signed feature hashing (the "hashing trick") over code sub-word tokens and
    character 3-grams, so no vocabulary needs to be learned or stored. Deterministic
    and offline. This is the default so the engine works with no model download.
    """

    name = "hashing"

    def __init__(self, dimension: int = 512, char_ngrams: int = 3):
        self.dimension = dimension
        self.char_ngrams = char_ngrams

    def _bucket(self, feature: str) -> tuple:
        """Map a feature string to (index, sign) using a stable content hash."""
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        h = int.from_bytes(digest, "big")
        index = h % self.dimension
        sign = 1.0 if (h // self.dimension) % 2 == 0 else -1.0
        return index, sign

    def embed(self, text: str) -> List[float]:
        vec = [0.0] * self.dimension
        if not text:
            return vec

        tokens = tokenize_code(text)
        # Whole-token features (weighted higher — identifiers carry the most signal)
        for tok in tokens:
            idx, sign = self._bucket(f"tok:{tok}")
            vec[idx] += 2.0 * sign

        # Character n-gram features over the joined token stream — robustness to
        # near-miss identifiers (e.g. "authenticate" vs "authenticated").
        joined = " ".join(tokens)
        n = self.char_ngrams
        for i in range(len(joined) - n + 1):
            gram = joined[i:i + n]
            if gram.strip():
                idx, sign = self._bucket(f"ng:{gram}")
                vec[idx] += sign

        # L2 normalize so cosine similarity reduces to a dot product downstream.
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec


class SentenceTransformerEmbedder(Embedder):
    """
    Optional local neural embedder backed by sentence-transformers.

    Lazily loads the model on first use. Intended for a code-aware embedding model
    (set RAG_EMBEDDING_MODEL). Not imported unless explicitly selected, so the base
    install stays lightweight.
    """

    name = "sentence-transformers"

    def __init__(self, model_name: str):
        from sentence_transformers import SentenceTransformer  # noqa: PLC0415 (lazy by design)

        self._model = SentenceTransformer(model_name)
        self.dimension = int(self._model.get_sentence_embedding_dimension())
        self._model_name = model_name

    def embed(self, text: str) -> List[float]:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        vecs = self._model.encode(
            texts,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return [v.tolist() for v in vecs]


class GeminiEmbedder(Embedder):
    """
    Embedding provider backed by Google Gemini embedding models.
    Produces L2-normalized embeddings via Google Generative AI.
    """

    name = "gemini"
    dimension = 3072

    def __init__(self, model_name: Optional[str] = None, api_key: Optional[str] = None):
        import google.generativeai as genai  # noqa: PLC0415

        key = api_key or get_gemini_api_key()
        if not key:
            raise ValueError("GEMINI_API_KEY is required for GeminiEmbedder.")
        genai.configure(api_key=key)
        self._genai = genai
        self.model_name = model_name or os.getenv("RAG_EMBEDDING_MODEL", "models/gemini-embedding-001")

    def embed(self, text: str) -> List[float]:
        return self._embed_batch([text], task_type="retrieval_document")[0]

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """Embed indexed documents (repository chunks)."""
        return self._embed_batch(texts, task_type="retrieval_document")

    def embed_query(self, text: str) -> List[float]:
        """Embed a retrieval query (the PR change) — Gemini scores these better with
        the dedicated 'retrieval_query' task type than as a document."""
        return self._embed_batch([text], task_type="retrieval_query")[0]

    def _embed_batch(self, texts: List[str], task_type: str) -> List[List[float]]:
        if not texts:
            return []

        results: List[List[float]] = []
        chunk_size = 50
        for i in range(0, len(texts), chunk_size):
            batch = texts[i:i + chunk_size]
            response = self._genai.embed_content(
                model=self.model_name,
                content=batch,
                task_type=task_type,
            )
            raw_embeddings = response.get("embedding", [])
            if raw_embeddings and isinstance(raw_embeddings[0], (float, int)):
                raw_embeddings = [raw_embeddings]

            for emb in raw_embeddings:
                norm = math.sqrt(sum(x * x for x in emb))
                if norm > 0:
                    emb = [x / norm for x in emb]
                results.append(emb)

        return results


def get_embedder(name: Optional[str] = None, dimension: Optional[int] = None) -> Embedder:
    """
    Resolve an embedding provider.

    Selection order: explicit `name` arg → RAG_EMBEDDER env var → "hashing".
    Any failure to construct an optional provider falls back to HashingEmbedder so
    a review is never blocked by a missing model.
    """
    choice = (name or os.getenv("RAG_EMBEDDER", "hashing")).strip().lower()
    dim = dimension or int(os.getenv("RAG_EMBEDDING_DIM", "512"))

    if choice in ("gemini", "google", "gemini-embedding"):
        model_name = os.getenv("RAG_EMBEDDING_MODEL", "models/gemini-embedding-001")
        try:
            embedder = GeminiEmbedder(model_name=model_name)
            logger.info(f"🧠 RAG embedder: Google Gemini ({model_name}, dim={embedder.dimension}).")
            return embedder
        except Exception as e:
            logger.warning(
                f"Could not load Gemini embedder ({e}); falling back to zero-dependency HashingEmbedder."
            )
            return HashingEmbedder(dimension=dim)

    if choice in ("sentence-transformers", "sentence_transformers", "st", "local"):
        model_name = os.getenv("RAG_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
        try:
            embedder = SentenceTransformerEmbedder(model_name)
            logger.info(f"🧠 RAG embedder: sentence-transformers ({model_name}, dim={embedder.dimension}).")
            return embedder
        except Exception as e:
            logger.warning(
                f"Could not load sentence-transformers model '{model_name}' ({e}); "
                f"falling back to zero-dependency HashingEmbedder."
            )
            return HashingEmbedder(dimension=dim)

    if choice in ("hashing", "hash", "default", "auto", ""):
        return HashingEmbedder(dimension=dim)

    logger.warning(f"Unknown RAG_EMBEDDER '{choice}'; using HashingEmbedder.")
    return HashingEmbedder(dimension=dim)

