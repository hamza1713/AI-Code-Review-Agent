"""
Unit tests for LLMFactory provider resolution and instantiation.
"""

import pytest
from unittest.mock import patch
from code_review_agent.llm_factory import LLMFactory


class TestLLMFactory:
    """Validate multi-provider resolution and factory creation."""

    def test_provider_resolution(self):
        assert LLMFactory.resolve_provider("gemini/gemini-2.5-flash") == "gemini"
        assert LLMFactory.resolve_provider("google/gemini-1.5-pro") == "gemini"
        assert LLMFactory.resolve_provider("openai/gpt-4-turbo") == "openai"
        assert LLMFactory.resolve_provider("gpt-4o") == "openai"
        assert LLMFactory.resolve_provider("anthropic/claude-3-5-sonnet-20241022") == "anthropic"
        assert LLMFactory.resolve_provider("claude-3-haiku") == "anthropic"
        assert LLMFactory.resolve_provider("groq/llama-3.3-70b") == "groq"
        assert LLMFactory.resolve_provider("ollama/codellama") == "ollama"

    @patch("code_review_agent.llm_factory.get_gemini_api_key", return_value="fake_gemini_key")
    def test_create_llm_default_gemini(self, mock_key):
        try:
            llm = LLMFactory.create_llm(model_name="gemini/gemini-2.5-flash")
            assert "gemini" in llm.model.lower()
            assert llm is not None
        except ImportError as e:
            pytest.skip(f"Gemini native provider not installed: {e}")

    @patch("code_review_agent.llm_factory.get_openai_api_key", return_value="fake_openai_key")
    def test_create_llm_openai(self, mock_key):
        try:
            llm = LLMFactory.create_llm(model_name="openai/gpt-4-turbo")
            assert "openai" in llm.model.lower() or "gpt-4" in llm.model.lower()
            assert llm is not None
        except ImportError as e:
            pytest.skip(f"OpenAI native provider not installed: {e}")

    @patch("code_review_agent.llm_factory.get_anthropic_api_key", return_value="fake_anthropic_key")
    def test_create_llm_anthropic(self, mock_key):
        try:
            llm = LLMFactory.create_llm(model_name="anthropic/claude-3-5-sonnet-20241022")
            assert "anthropic" in llm.model.lower() or "claude" in llm.model.lower()
            assert llm is not None
        except ImportError as e:
            pytest.skip(f"Anthropic native provider not installed: {e}")

