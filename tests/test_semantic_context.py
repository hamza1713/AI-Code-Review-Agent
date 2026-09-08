"""
Unit tests for the Semantic RAG Context Engine.

Covers the zero-dependency default path end to end: tokenization, the hashing
embedder's determinism and normalization, the cosine vector store (search +
persistence), and the engine's indexing, retrieval, re-ranking, noise filtering,
and cross-repo behavior. All tests run offline with no model download.
"""

import math
from pathlib import Path

import pytest

from code_review_agent.context_engine.semantic.embeddings import (
    HashingEmbedder,
    tokenize_code,
    get_embedder,
)
from code_review_agent.context_engine.semantic.vector_store import VectorStore, ChunkRecord, cosine
from code_review_agent.context_engine.semantic.engine import SemanticContextEngine


# ── Tokenization ─────────────────────────────────────────────────────────────
class TestTokenization:
    def test_camel_snake_and_pascal_converge(self):
        assert tokenize_code("getUserById") == ["get", "user", "by", "id"]
        assert tokenize_code("get_user_by_id") == ["get", "user", "by", "id"]
        assert tokenize_code("GetUserByID")[:3] == ["get", "user", "by"]

    def test_strips_punctuation(self):
        assert tokenize_code("db.query(sql, params)") == ["db", "query", "sql", "params"]


# ── Hashing embedder ─────────────────────────────────────────────────────────
class TestHashingEmbedder:
    def test_deterministic(self):
        e = HashingEmbedder(dimension=256)
        assert e.embed("def authenticate(user): ...") == e.embed("def authenticate(user): ...")

    def test_unit_norm(self):
        e = HashingEmbedder(dimension=256)
        v = e.embed("some meaningful code with identifiers")
        assert math.isclose(math.sqrt(sum(x * x for x in v)), 1.0, rel_tol=1e-6)

    def test_empty_is_zero_vector(self):
        e = HashingEmbedder(dimension=64)
        assert e.embed("") == [0.0] * 64

    def test_similar_code_scores_higher_than_unrelated(self):
        e = HashingEmbedder(dimension=512)
        base = e.embed("def login(username, password): return check_password(username, password)")
        similar = e.embed("def signin(username, password): return verify_password(username, password)")
        unrelated = e.embed("def render_chart(points): draw_svg_axis(points)")
        assert cosine(base, similar) > cosine(base, unrelated)

    def test_factory_defaults_to_hashing(self):
        assert get_embedder("unknown-provider").name == "hashing"
        assert get_embedder().name == "hashing"

    def test_embed_query_defaults_to_embed(self):
        e = HashingEmbedder(dimension=128)
        assert e.embed_query("lookup user by id") == e.embed("lookup user by id")

    def test_identity_encodes_name_and_dim(self):
        assert HashingEmbedder(dimension=384).identity() == "hashing:384"


# ── Vector store ─────────────────────────────────────────────────────────────
class TestVectorStore:
    def _rec(self, rec_id, repo, vec):
        return ChunkRecord(
            id=rec_id, repo=repo, file_path="a.py", qualified_name=f"a.py:{rec_id}",
            kind="function", language="python", line_start=1, line_end=3, text="x", vector=vec,
        )

    def test_add_dedups_by_id(self):
        s = VectorStore()
        assert s.add([self._rec("f1", "r", [1.0, 0.0])]) == 1
        assert s.add([self._rec("f1", "r", [1.0, 0.0])]) == 0
        assert len(s) == 1

    def test_search_orders_by_similarity(self):
        s = VectorStore()
        s.add([self._rec("near", "r", [1.0, 0.0]), self._rec("far", "r", [0.0, 1.0])])
        results = s.search([1.0, 0.0], top_k=2)
        assert results[0][0].id == "near"

    def test_filter_fn_scopes_results(self):
        s = VectorStore()
        s.add([self._rec("a", "repo1", [1.0, 0.0]), self._rec("b", "repo2", [1.0, 0.0])])
        results = s.search([1.0, 0.0], top_k=5, filter_fn=lambda r: r.repo == "repo2")
        assert [r.id for r, _ in results] == ["b"]

    def test_persistence_roundtrip(self, tmp_path):
        s = VectorStore()
        s.add([self._rec("a", "r", [0.6, 0.8])])
        path = str(tmp_path / "index.json")
        s.save(path)

        loaded = VectorStore()
        assert loaded.load(path) is True
        assert len(loaded) == 1
        assert loaded.search([0.6, 0.8], top_k=1)[0][0].id == "a"

    def test_clear_repo(self):
        s = VectorStore()
        s.add([self._rec("a", "r1", [1.0, 0.0]), self._rec("b", "r2", [1.0, 0.0])])
        assert s.clear_repo("r1") == 1
        assert s.repos == ["r2"]

    def test_load_rejects_mismatched_embedder(self, tmp_path):
        """An index built by a different embedder must be refused so the caller re-indexes."""
        s = VectorStore()
        s.add([self._rec("a", "r", [0.6, 0.8])])
        path = str(tmp_path / "index.json")
        s.save(path, embedder_id="gemini:3072")

        loaded = VectorStore()
        # Same embedder id loads fine.
        assert loaded.load(path, embedder_id="gemini:3072") is True
        # A different embedder id is rejected (returns False → triggers re-index).
        fresh = VectorStore()
        assert fresh.load(path, embedder_id="hashing:512") is False
        assert len(fresh) == 0


# ── Engine (indexing + retrieval) ────────────────────────────────────────────
@pytest.fixture
def sample_repo(tmp_path):
    """A tiny repo with an auth module and an unrelated charting module."""
    (tmp_path / "auth.py").write_text(
        '''
def authenticate_user(username, password):
    """Verify a user's credentials against the store."""
    record = lookup_user(username)
    return check_password(password, record.hash)


def lookup_user(username):
    """Fetch a user record by username."""
    return db_query(username)
''',
        encoding="utf-8",
    )
    (tmp_path / "charts.py").write_text(
        '''
def render_bar_chart(points):
    """Draw a bar chart from a series of points."""
    return draw_svg(points)
''',
        encoding="utf-8",
    )
    return tmp_path


class TestSemanticEngine:
    def test_index_creates_chunks(self, sample_repo):
        engine = SemanticContextEngine(repo_roots=[str(sample_repo)])
        count = engine.index()
        assert count >= 3  # authenticate_user, lookup_user, render_bar_chart

    def test_retrieve_surfaces_related_code(self, sample_repo):
        engine = SemanticContextEngine(repo_roots=[str(sample_repo)])
        engine.index()
        # A new file that introduces a login function similar to auth.py.
        diff = (
            "diff --git a/login.py b/login.py\n"
            "--- /dev/null\n+++ b/login.py\n"
            "@@ -0,0 +1,3 @@\n"
            "+def signin(username, password):\n"
            "+    user = lookup_user(username)\n"
            "+    return check_password(password, user.hash)\n"
        )
        results = engine.retrieve(diff, top_k=5)
        assert results, "expected at least one related chunk"
        top_names = [r.record.qualified_name for r in results]
        # The auth functions should rank above the charting function.
        assert any("authenticate_user" in n or "lookup_user" in n for n in top_names)
        assert not any("render_bar_chart" in n for n in top_names[:1])

    def test_format_context_is_prompt_ready(self, sample_repo):
        engine = SemanticContextEngine(repo_roots=[str(sample_repo)])
        engine.index()
        diff = "+def signin(username, password):\n+    return lookup_user(username)\n"
        rendered = engine.format_semantic_context(diff, top_k=3)
        assert "Semantically Related Code" in rendered
        assert "auth.py" in rendered

    def test_noise_filter_excludes_the_changed_code_itself(self, sample_repo):
        """A chunk that overlaps the diff's own changed lines must not be retrieved."""
        engine = SemanticContextEngine(repo_roots=[str(sample_repo)])
        engine.index()
        # Diff edits auth.py exactly where authenticate_user lives (lines 2-5).
        diff = (
            "diff --git a/auth.py b/auth.py\n"
            "--- a/auth.py\n+++ b/auth.py\n"
            "@@ -2,4 +2,4 @@\n"
            "+def authenticate_user(username, password):\n"
            "+    record = lookup_user(username)\n"
            "+    return check_password(password, record.hash)\n"
        )
        results = engine.retrieve(diff, top_k=5)
        overlapping = [
            r for r in results
            if r.record.file_path == "auth.py"
            and r.record.line_start <= 5 and r.record.line_end >= 2
            and "authenticate_user" in r.record.qualified_name
        ]
        assert not overlapping, "changed code should be filtered out as PR-self noise"

    def test_empty_diff_returns_nothing(self, sample_repo):
        engine = SemanticContextEngine(repo_roots=[str(sample_repo)])
        engine.index()
        assert engine.retrieve("", top_k=5) == []

    def test_cross_repo_indexing(self, tmp_path, sample_repo):
        """Two repo roots index into one store and both are searchable."""
        repo2 = tmp_path / "service_b"
        repo2.mkdir()
        (repo2 / "handler.py").write_text(
            '''
def handle_login_request(username, password):
    """Entrypoint that authenticates an incoming login."""
    return authenticate_user(username, password)
''',
            encoding="utf-8",
        )
        engine = SemanticContextEngine(repo_roots=[str(sample_repo), str(repo2)])
        engine.index()
        assert set(engine.store.repos) == {sample_repo.name, "service_b"}

    def test_persist_reload_skips_reindex(self, sample_repo, tmp_path):
        path = str(tmp_path / "rag.json")
        e1 = SemanticContextEngine(repo_roots=[str(sample_repo)], persist_path=path)
        n1 = e1.index()

        e2 = SemanticContextEngine(repo_roots=[str(sample_repo)], persist_path=path)
        n2 = e2.index()  # should load from disk
        assert n1 == n2
        assert Path(path).exists()
