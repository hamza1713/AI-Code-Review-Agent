"""Consent-checked, bounded HTTP transports. No implicit model substitutions."""

import json
import os
import threading
import time
from concurrent.futures import Future
from contextvars import copy_context
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from code_review_agent.config import (
    get_anthropic_api_key,
    get_gemini_api_key,
    get_groq_api_key,
    get_max_tokens,
    get_model_name,
    get_ollama_base_url,
    get_openai_api_key,
)
from code_review_agent.data_policy import production, require_model_approval


class LLMRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    messages: list[dict[str, str]]
    timeout_seconds: float = Field(default=60, gt=0, le=60)


class TokenUsage(BaseModel):
    model_config = ConfigDict(frozen=True)
    prompt_tokens: int = Field(ge=0, strict=True)
    completion_tokens: int = Field(ge=0, strict=True)
    total_tokens: int = Field(ge=0, strict=True)


class LLMResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    text: str
    model: str
    provider_model: str | None = None
    model_revision: str | None = None
    request_id: str | None = None
    finish_status: Literal["COMPLETED"] = "COMPLETED"
    usage: TokenUsage | None = None


class ProviderOutputError(ValueError):
    """Output was refused, truncated, empty, or malformed."""


class LLMProvider(Protocol):
    model: str

    def call_response(
        self,
        messages: LLMRequest | str | list[dict[str, str]],
        timeout_seconds: float | None = None,
    ) -> LLMResponse: ...


class ProviderLLM:
    def __init__(
        self,
        model: str,
        api_key: str | None,
        max_tokens: int = 4096,
        temperature: float | None = None,
        timeout: float = 60,
        base_url: str | None = None,
    ):
        self.model, self.max_tokens, self.temperature = model, max_tokens, temperature
        self.api_key = SecretStr(api_key or "")
        self.timeout = min(60.0, float(timeout))
        self.provider = LLMFactory.resolve_provider(model)
        self.base_url = base_url
        if max_tokens <= 0 or (temperature is not None and not 0 <= temperature <= 2):
            raise ValueError("Invalid generation settings")

    def call(self, messages: Any, *args: Any, **kwargs: Any) -> str:
        return self.call_response(messages, kwargs.pop("timeout_seconds", None)).text

    def call_response(
        self, messages: Any, timeout_seconds: float | None = None
    ) -> LLMResponse:
        budget = min(timeout_seconds or self.timeout, self.timeout)
        if isinstance(messages, LLMRequest):
            budget = min(budget, messages.timeout_seconds)
        budget = min(
            budget, float(os.getenv("REVIEW_WORKER_DEADLINE", "inf")) - time.time()
        )
        if budget <= 0:
            raise TimeoutError("Model request deadline expired")
        deadline = time.monotonic() + budget
        result: Future[LLMResponse] = Future()

        def invoke():
            try:
                result.set_result(
                    self._call_response(messages, timeout_seconds, deadline)
                )
            except BaseException as error:
                result.set_exception(error)

        # HTTPX read timeouts bound individual reads. Bound the whole call even
        # when a provider stalls between chunks; cleanup cannot hold the worker open.
        context = copy_context()
        threading.Thread(target=context.run, args=(invoke,), daemon=True).start()
        try:
            return result.result(timeout=max(0, deadline - time.monotonic()))
        except TimeoutError as error:
            raise TimeoutError("Model request deadline expired") from error

    def _call_response(
        self, messages: Any, timeout_seconds: float | None, deadline: float
    ) -> LLMResponse:
        if isinstance(messages, LLMRequest):
            timeout_seconds = min(
                timeout_seconds or messages.timeout_seconds, messages.timeout_seconds
            )
            messages = messages.messages
        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]
        request = LLMRequest(
            messages=messages,
            timeout_seconds=min(timeout_seconds or self.timeout, self.timeout),
        )
        revision = None
        for attempt in range(2):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Model request deadline expired")
            url, headers, body = self._request(request.messages)
            with httpx.Client(
                timeout=httpx.Timeout(remaining, connect=min(10, remaining)),
                follow_redirects=False,
            ) as client:
                if production() and self.provider == "ollama":
                    pinned = os.getenv("REVIEW_OLLAMA_MODEL_DIGEST")
                    if not pinned:
                        raise PermissionError(
                            "Production Ollama requires a pinned model digest"
                        )
                    require_model_approval(self.model)
                    with client.stream(
                        "GET", url.rsplit("/", 1)[0] + "/tags", headers=headers
                    ) as tags:
                        if tags.status_code != 200:
                            raise ProviderOutputError(
                                "Ollama model identity unavailable"
                            )
                        raw_tags = bytearray()
                        for chunk in tags.iter_bytes():
                            raw_tags.extend(chunk)
                            if len(raw_tags) > 4 * 1024 * 1024:
                                raise ProviderOutputError(
                                    "Ollama identity exceeds size limit"
                                )
                            if time.monotonic() >= deadline:
                                raise TimeoutError(
                                    "Model identity verification exceeded deadline"
                                )
                    try:
                        models = json.loads(raw_tags)["models"]
                        if not isinstance(models, list) or any(
                            not isinstance(m, dict) for m in models
                        ):
                            raise ValueError("Invalid model list")
                    except (ValueError, KeyError, TypeError) as error:
                        raise ProviderOutputError(
                            "Invalid Ollama model identity"
                        ) from error
                    name = self.model.split("/", 1)[-1]
                    matches = [
                        m
                        for m in models
                        if m.get("name") == name or m.get("model") == name
                    ]
                    if len(matches) != 1 or matches[0].get("digest") != pinned:
                        raise ProviderOutputError(
                            "Ollama model digest differs from approved reviewer"
                        )
                    revision = pinned
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(
                            "Model identity verification exceeded deadline"
                        )
                    client.timeout = httpx.Timeout(
                        remaining, connect=min(10, remaining)
                    )
                require_model_approval(self.model)
                with client.stream("POST", url, headers=headers, json=body) as response:
                    if attempt == 0 and (
                        response.status_code == 429 or 500 <= response.status_code < 600
                    ):
                        try:
                            delay = min(
                                5.0,
                                max(
                                    0.0, float(response.headers.get("retry-after", "1"))
                                ),
                            )
                        except ValueError:
                            delay = 1.0
                        if time.monotonic() + delay >= deadline:
                            raise TimeoutError("Retry exceeds request deadline")
                        time.sleep(delay)
                        continue
                    if response.status_code != 200:
                        raise RuntimeError(
                            f"Model provider returned HTTP {response.status_code}"
                        )
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data) > 4 * 1024 * 1024:
                            raise ProviderOutputError("Model output exceeds size limit")
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Model request deadline expired")
                    try:
                        result = self._response(
                            json.loads(data), response.headers.get("x-request-id")
                        )
                        return result.model_copy(update={"model_revision": revision})
                    except (
                        KeyError,
                        TypeError,
                        IndexError,
                        ValueError,
                        AttributeError,
                    ) as error:
                        raise ProviderOutputError(
                            "Invalid or incomplete model output"
                        ) from error
        raise RuntimeError("Model retry exhausted")

    def _request(
        self, messages: list[dict[str, str]]
    ) -> tuple[str, dict[str, str], dict]:
        model, key = self.model.split("/", 1)[-1], self.api_key.get_secret_value()
        if self.provider != "ollama" and not key:
            raise PermissionError("Model provider credential is missing")
        headers = {"Content-Type": "application/json"}
        system = "\n".join(
            m["content"] for m in messages if m["role"] in {"system", "developer"}
        )
        conversation = [m for m in messages if m["role"] not in {"system", "developer"}]
        body: dict[str, Any]
        if self.provider == "gemini":
            body = {
                "contents": [
                    {
                        "role": "model" if m["role"] == "assistant" else "user",
                        "parts": [{"text": m["content"]}],
                    }
                    for m in conversation
                ],
                "generationConfig": {"maxOutputTokens": self.max_tokens},
            }
            if system:
                body["systemInstruction"] = {"parts": [{"text": system}]}
            if self.temperature is not None:
                body["generationConfig"]["temperature"] = self.temperature
            headers["x-goog-api-key"] = key
            return (
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers,
                body,
            )
        if self.provider == "anthropic":
            headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
            body = {
                "model": model,
                "max_tokens": self.max_tokens,
                "messages": conversation,
            }
            if system:
                body["system"] = system
            if self.temperature is not None:
                body["temperature"] = self.temperature
            return "https://api.anthropic.com/v1/messages", headers, body
        if self.provider == "ollama":
            body = {
                "model": model,
                "messages": messages,
                "stream": False,
                "options": {"num_predict": self.max_tokens},
            }
            if self.temperature is not None:
                body["options"]["temperature"] = self.temperature
            if key:
                headers["Authorization"] = f"Bearer {key}"
            return (
                (self.base_url or get_ollama_base_url()).rstrip("/") + "/api/chat",
                headers,
                body,
            )
        if self.provider not in {"openai", "groq"}:
            raise ValueError("Unsupported provider; use an explicit provider prefix")
        headers["Authorization"] = f"Bearer {key}"
        body = {"model": model, "messages": messages, "stream": False}
        body["max_completion_tokens" if self.provider == "openai" else "max_tokens"] = (
            self.max_tokens
        )
        if self.provider == "openai":
            body["store"] = False
        if self.temperature is not None:
            body["temperature"] = self.temperature
        return (
            (
                "https://api.openai.com/v1/chat/completions"
                if self.provider == "openai"
                else "https://api.groq.com/openai/v1/chat/completions"
            ),
            headers,
            body,
        )

    def _response(self, payload: dict, request_id: str | None) -> LLMResponse:
        usage = None
        if self.provider == "gemini":
            candidate = payload["candidates"][0]
            if candidate.get("finishReason") != "STOP" or payload.get(
                "promptFeedback", {}
            ).get("blockReason"):
                raise ProviderOutputError("Gemini did not complete")
            text = "".join(
                p["text"] for p in candidate["content"]["parts"] if not p.get("thought")
            )
            u = payload.get("usageMetadata")
            if u and "promptTokenCount" in u and "candidatesTokenCount" in u:
                usage = TokenUsage(
                    prompt_tokens=u["promptTokenCount"],
                    completion_tokens=u["candidatesTokenCount"]
                    + u.get("thoughtsTokenCount", 0),
                    total_tokens=u["totalTokenCount"],
                )
        elif self.provider == "anthropic":
            if payload.get("stop_reason") != "end_turn":
                raise ProviderOutputError("Claude did not complete")
            text = "".join(p["text"] for p in payload["content"] if p["type"] == "text")
            u = payload.get("usage")
            if u and "input_tokens" in u and "output_tokens" in u:
                prompt = (
                    u["input_tokens"]
                    + u.get("cache_creation_input_tokens", 0)
                    + u.get("cache_read_input_tokens", 0)
                )
                usage = TokenUsage(
                    prompt_tokens=prompt,
                    completion_tokens=u["output_tokens"],
                    total_tokens=prompt + u["output_tokens"],
                )
        elif self.provider == "ollama":
            if payload.get("done") is not True or payload.get("done_reason") != "stop":
                raise ProviderOutputError("Ollama did not complete")
            text = payload["message"]["content"]
            if "prompt_eval_count" in payload and "eval_count" in payload:
                usage = TokenUsage(
                    prompt_tokens=payload["prompt_eval_count"],
                    completion_tokens=payload["eval_count"],
                    total_tokens=payload["prompt_eval_count"] + payload["eval_count"],
                )
        else:
            choice = payload["choices"][0]
            if (
                choice.get("finish_reason") != "stop"
                or choice["message"].get("refusal")
                or choice["message"].get("tool_calls")
            ):
                raise ProviderOutputError("Completion was refused or incomplete")
            text = choice["message"]["content"]
            u = payload.get("usage")
            if u and all(
                k in u for k in ("prompt_tokens", "completion_tokens", "total_tokens")
            ):
                usage = TokenUsage(
                    **{
                        k: u[k]
                        for k in ("prompt_tokens", "completion_tokens", "total_tokens")
                    }
                )
        if not isinstance(text, str) or not text.strip():
            raise ProviderOutputError("Empty model output")
        provider_model = payload.get("model") or payload.get("modelVersion")
        expected = os.getenv("REVIEW_PROVIDER_MODEL_ID") or self.model.split("/", 1)[-1]
        if production() and (
            not isinstance(provider_model, str)
            or provider_model.removeprefix("models/")
            != expected.removeprefix("models/")
        ):
            raise ProviderOutputError(
                "Provider model identity differs from approved reviewer"
            )
        return LLMResponse(
            text=text,
            model=self.model,
            provider_model=provider_model,
            request_id=request_id or payload.get("id") or payload.get("responseId"),
            usage=usage,
        )


class LLMFactory:
    @classmethod
    def resolve_provider(cls, model_name: str) -> str:
        name = model_name.lower()
        prefix = name.split("/", 1)[0]
        if prefix in {"gemini", "google", "openai", "anthropic", "groq", "ollama"}:
            return "gemini" if prefix == "google" else prefix
        if name.startswith("gemini"):
            return "gemini"
        if name.startswith(("gpt-", "o1", "o3", "o4")):
            return "openai"
        if name.startswith("claude"):
            return "anthropic"
        return "generic"

    @classmethod
    def get_api_key_for_model(cls, model_name: str) -> str | None:
        getters = {
            "gemini": get_gemini_api_key,
            "openai": get_openai_api_key,
            "anthropic": get_anthropic_api_key,
            "groq": get_groq_api_key,
            "ollama": lambda: os.getenv("OLLAMA_API_KEY"),
        }
        getter = getters.get(cls.resolve_provider(model_name))
        return getter() if getter else None

    @classmethod
    def create_llm(
        cls,
        model_name: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        **kwargs: Any,
    ) -> ProviderLLM:
        model = (model_name or get_model_name()).strip()
        require_model_approval(model)
        if production() and kwargs:
            raise PermissionError(
                "Production endpoint overrides require separate approval"
            )
        if set(kwargs) - {"timeout", "base_url"}:
            raise ValueError("Unsupported model settings")
        return ProviderLLM(
            model,
            cls.get_api_key_for_model(model),
            max_tokens or get_max_tokens(),
            temperature,
            kwargs.get("timeout", float(os.getenv("LLM_REQUEST_TIMEOUT", "60"))),
            kwargs.get("base_url"),
        )
