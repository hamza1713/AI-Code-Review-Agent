"""
LLM Factory for AI Code Review Agent.
Abstracts multi-provider LLM creation (Gemini, OpenAI, Anthropic, Groq, Ollama)
using CrewAI and LiteLLM unified interfaces with graceful fallback and environment resolution.
"""

import os
from typing import Optional, Dict, Any
from crewai import LLM

from code_review_agent.config import (
    get_model_name,
    get_max_tokens,
    get_gemini_api_key,
    get_openai_api_key,
    get_anthropic_api_key,
    get_groq_api_key,
    get_ollama_base_url,
    logger,
)


class LLMFactory:
    """
    Factory class for instantiating configured LLM instances across multiple providers.
    Defaults to configured Google Gemini model with support for runtime overrides.
    """

    @classmethod
    def resolve_provider(cls, model_name: str) -> str:
        """Identify the provider based on the model name prefix or pattern."""
        model_lower = model_name.lower()
        if model_lower.startswith("gemini/") or model_lower.startswith("google/") or "gemini" in model_lower:
            return "gemini"
        elif model_lower.startswith("openai/") or model_lower.startswith("gpt-") or "gpt-" in model_lower or "o1-" in model_lower or "o3-" in model_lower:
            return "openai"
        elif model_lower.startswith("anthropic/") or model_lower.startswith("claude-") or "claude" in model_lower:
            return "anthropic"
        elif model_lower.startswith("groq/"):
            return "groq"
        elif model_lower.startswith("ollama/"):
            return "ollama"
        return "generic"

    @classmethod
    def get_api_key_for_model(cls, model_name: str) -> Optional[str]:
        """Retrieve appropriate API key based on the model provider."""
        provider = cls.resolve_provider(model_name)
        if provider == "gemini":
            return get_gemini_api_key()
        elif provider == "openai":
            key = get_openai_api_key()
            if not key:
                logger.warning(f"OPENAI_API_KEY not found in environment for model '{model_name}'.")
            return key
        elif provider == "anthropic":
            key = get_anthropic_api_key()
            if not key:
                logger.warning(f"ANTHROPIC_API_KEY not found in environment for model '{model_name}'.")
            return key
        elif provider == "groq":
            key = get_groq_api_key()
            if not key:
                logger.warning(f"GROQ_API_KEY not found in environment for model '{model_name}'.")
            return key
        elif provider == "ollama":
            return None
        return get_gemini_api_key()

    @classmethod
    def create_llm(
        cls,
        model_name: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        **kwargs: Any
    ) -> LLM:
        """
        Instantiate a configured CrewAI LLM.
        
        Args:
            model_name: Model identifier (e.g. 'gemini/gemini-2.5-flash', 'openai/gpt-4-turbo', 'anthropic/claude-3-5-sonnet-20241022').
                        Defaults to configured LLM_MODEL in .env / config.
            max_tokens: Maximum tokens in response (default: MAX_TOKENS from config).
            temperature: Sampling temperature (optional).
            **kwargs: Additional provider-specific parameters.
        """
        resolved_model = (model_name or get_model_name()).strip()
        resolved_max_tokens = max_tokens or get_max_tokens()
        api_key = cls.get_api_key_for_model(resolved_model)
        provider = cls.resolve_provider(resolved_model)

        llm_kwargs: Dict[str, Any] = {
            "model": resolved_model,
            "max_tokens": resolved_max_tokens,
        }

        if api_key:
            llm_kwargs["api_key"] = api_key

        if temperature is not None:
            llm_kwargs["temperature"] = temperature

        if provider == "ollama":
            base_url = get_ollama_base_url()
            llm_kwargs["base_url"] = base_url

        llm_kwargs.update(kwargs)

        logger.debug(f"Instantiating LLM for model: '{resolved_model}' (provider: {provider})")
        return LLM(**llm_kwargs)
