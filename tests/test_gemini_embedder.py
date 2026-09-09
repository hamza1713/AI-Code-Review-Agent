"""
Unit tests for GeminiEmbedder and embedding factory integration.
"""

import math
import pytest
from unittest.mock import patch

genai = pytest.importorskip("google.generativeai")

from code_review_agent.context_engine.semantic.embeddings import (
    GeminiEmbedder,
    get_embedder,
)


class TestGeminiEmbedder:
    """Test GeminiEmbedder normalization, batching, and error handling."""

    def test_missing_api_key_raises_value_error(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        with patch("code_review_agent.context_engine.semantic.embeddings.get_gemini_api_key", return_value=""):
            with pytest.raises(ValueError, match="GEMINI_API_KEY is required"):
                GeminiEmbedder(api_key=None)

    @patch("google.generativeai.configure")
    @patch("google.generativeai.embed_content")
    def test_embed_returns_normalized_vector(self, mock_embed, mock_conf):
        # Raw unnormalized vector [3.0, 4.0] -> L2 norm = 5.0 -> normalized [0.6, 0.8]
        mock_embed.return_value = {"embedding": [[3.0, 4.0]]}

        embedder = GeminiEmbedder(api_key="fake-test-key")
        vec = embedder.embed("def test(): pass")

        assert len(vec) == 2
        assert math.isclose(vec[0], 0.6, rel_tol=1e-5)
        assert math.isclose(vec[1], 0.8, rel_tol=1e-5)
        assert math.isclose(math.sqrt(sum(x * x for x in vec)), 1.0, rel_tol=1e-5)

    @patch("google.generativeai.configure")
    @patch("google.generativeai.embed_content")
    def test_embed_batch_handles_empty_list(self, mock_embed, mock_conf):
        embedder = GeminiEmbedder(api_key="fake-test-key")
        assert embedder.embed_batch([]) == []
        mock_embed.assert_not_called()

    @patch("google.generativeai.configure")
    @patch("google.generativeai.embed_content")
    def test_embed_batch_chunks_calls(self, mock_embed, mock_conf):
        mock_embed.return_value = {"embedding": [[1.0, 0.0]] * 10}
        embedder = GeminiEmbedder(api_key="fake-test-key")

        texts = [f"def func_{i}(): pass" for i in range(10)]
        res = embedder.embed_batch(texts)

        assert len(res) == 10
        mock_embed.assert_called_once()

    def test_factory_resolves_gemini_or_falls_back(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key-123")
        with patch("google.generativeai.configure"):
            embedder = get_embedder("gemini")
            assert embedder.name == "gemini"

    def test_factory_fallback_on_gemini_error(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        with patch("code_review_agent.context_engine.semantic.embeddings.get_gemini_api_key", return_value=""):
            embedder = get_embedder("gemini")
            assert embedder.name == "hashing"
