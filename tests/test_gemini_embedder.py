"""
Unit tests for GeminiEmbedder and embedding factory integration.
"""

import math
from unittest.mock import MagicMock, patch

import pytest

from code_review_agent.context_engine.semantic.embeddings import (
    GeminiEmbedder,
    get_embedder,
)

pytest.importorskip("google.genai")


class TestGeminiEmbedder:
    """Test GeminiEmbedder normalization, batching, and error handling."""

    def test_missing_api_key_raises_value_error(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        with patch(
            "code_review_agent.context_engine.semantic.embeddings.get_gemini_api_key",
            return_value="",
        ):
            with pytest.raises(ValueError, match="GEMINI_API_KEY is required"):
                GeminiEmbedder(api_key=None)

    @patch("google.genai.Client")
    def test_embed_returns_normalized_vector(self, mock_client_cls):
        # Raw unnormalized vector [3.0, 4.0] -> L2 norm = 5.0 -> normalized [0.6, 0.8]
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_response = MagicMock()
        mock_response.embeddings = [MagicMock(values=[3.0, 4.0])]
        mock_client.models.embed_content.return_value = mock_response

        embedder = GeminiEmbedder(api_key="fake-test-key")
        vec = embedder.embed("def test(): pass")

        assert len(vec) == 2
        assert math.isclose(vec[0], 0.6, rel_tol=1e-5)
        assert math.isclose(vec[1], 0.8, rel_tol=1e-5)
        assert math.isclose(math.sqrt(sum(x * x for x in vec)), 1.0, rel_tol=1e-5)

    @patch("google.genai.Client")
    def test_embed_batch_handles_empty_list(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        embedder = GeminiEmbedder(api_key="fake-test-key")
        assert embedder.embed_batch([]) == []
        mock_client.models.embed_content.assert_not_called()

    @patch("google.genai.Client")
    def test_embed_batch_chunks_calls(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_response = MagicMock()
        mock_response.embeddings = [MagicMock(values=[1.0, 0.0]) for _ in range(10)]
        mock_client.models.embed_content.return_value = mock_response

        embedder = GeminiEmbedder(api_key="fake-test-key")

        texts = [f"def func_{i}(): pass" for i in range(10)]
        res = embedder.embed_batch(texts)

        assert len(res) == 10
        mock_client.models.embed_content.assert_called_once()

    @patch("google.genai.Client")
    def test_factory_resolves_gemini_or_falls_back(self, mock_client_cls, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key-123")
        embedder = get_embedder("gemini")
        assert embedder.name == "gemini"

    def test_factory_fallback_on_gemini_error(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        with patch(
            "code_review_agent.context_engine.semantic.embeddings.get_gemini_api_key",
            return_value="",
        ):
            embedder = get_embedder("gemini")
            assert embedder.name == "hashing"
