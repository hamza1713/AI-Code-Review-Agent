"""Provider wire contracts and failure behavior, with no live model calls."""

import json
import threading
from unittest.mock import Mock

import httpx
import pytest

from code_review_agent.llm_factory import ProviderLLM, ProviderOutputError

PAYLOADS = {
    "gemini": {
        "candidates": [
            {"finishReason": "STOP", "content": {"parts": [{"text": "{}"}]}}
        ],
        "usageMetadata": {
            "promptTokenCount": 2,
            "candidatesTokenCount": 3,
            "totalTokenCount": 5,
        },
    },
    "openai": {
        "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
    },
    "groq": {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]},
    "anthropic": {
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "{}"}],
        "usage": {"input_tokens": 2, "output_tokens": 3},
    },
    "ollama": {
        "done": True,
        "done_reason": "stop",
        "message": {"content": "{}"},
        "prompt_eval_count": 2,
        "eval_count": 3,
    },
}


def install_transport(monkeypatch, callback):
    original = httpx.Client
    monkeypatch.setattr(
        "code_review_agent.llm_factory.httpx.Client",
        lambda **kwargs: original(transport=httpx.MockTransport(callback), **kwargs),
    )


@pytest.mark.parametrize("provider", PAYLOADS)
def test_wire_contract_and_usage(monkeypatch, provider):
    sent = []

    def respond(request):
        sent.append(request)
        return httpx.Response(
            200, json=PAYLOADS[provider], headers={"x-request-id": "request-1"}
        )

    install_transport(monkeypatch, respond)
    result = ProviderLLM(f"{provider}/exact-model", "test-key").call_response("review")
    assert result.text == "{}" and result.model == f"{provider}/exact-model"
    assert result.request_id == "request-1"
    assert (result.usage.total_tokens if result.usage else None) == (
        None if provider == "groq" else 5
    )
    body = json.loads(sent[0].content)
    assert "tools" not in body
    if provider in {"openai", "groq", "ollama"}:
        assert body["stream"] is False
    if provider == "openai":
        assert body["store"] is False


@pytest.mark.parametrize(
    "payload",
    [
        {"choices": []},
        {"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]},
        {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": "{}", "refusal": "refused"},
                }
            ]
        },
        {"choices": [{"finish_reason": "stop", "message": {"content": ""}}]},
    ],
)
def test_incomplete_never_accepted(monkeypatch, payload):
    install_transport(monkeypatch, lambda request: httpx.Response(200, json=payload))
    with pytest.raises(ProviderOutputError):
        ProviderLLM("openai/test", "test-key").call("review")


def test_consent_checked_before_retry(monkeypatch):
    approval = Mock(side_effect=[{}, PermissionError("expired")])
    monkeypatch.setattr(
        "code_review_agent.llm_factory.require_model_approval", approval
    )
    sent = Mock(return_value=httpx.Response(503, headers={"retry-after": "0"}))
    install_transport(monkeypatch, sent)
    with pytest.raises(PermissionError):
        ProviderLLM("openai/test", "test-key").call("review")
    assert approval.call_count == 2 and sent.call_count == 1


def test_expired_worker_deadline_prevents_call(monkeypatch):
    monkeypatch.setenv("REVIEW_WORKER_DEADLINE", "1")
    with pytest.raises(TimeoutError):
        ProviderLLM("openai/test", "test-key").call("review")


def test_retry_cannot_exceed_call_deadline(monkeypatch):
    install_transport(
        monkeypatch, lambda request: httpx.Response(429, headers={"retry-after": "60"})
    )
    with pytest.raises(TimeoutError):
        ProviderLLM("openai/test", "test-key", timeout=0.01).call("review")


@pytest.mark.parametrize("provider", PAYLOADS)
def test_production_rejects_missing_or_changed_exact_model(monkeypatch, provider):
    monkeypatch.setattr("code_review_agent.llm_factory.production", lambda: True)
    monkeypatch.setattr(
        "code_review_agent.llm_factory.require_model_approval", lambda _: {}
    )
    monkeypatch.setenv("REVIEW_PROVIDER_MODEL_ID", "approved-model")
    monkeypatch.setenv("REVIEW_OLLAMA_MODEL_DIGEST", "sha256:" + "a" * 64)

    def respond(request):
        if request.url.path.endswith("/tags"):
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"name": "approved-model", "digest": "sha256:" + "a" * 64}
                    ]
                },
            )
        return httpx.Response(200, json=PAYLOADS[provider])

    install_transport(monkeypatch, respond)
    with pytest.raises(ProviderOutputError):
        ProviderLLM(f"{provider}/approved-model", "test-key").call("review")


@pytest.mark.parametrize(
    "identity",
    [b"{}", b'{"models": [1]}', b"invalid", b"x" * (4 * 1024 * 1024 + 1)],
    ids=["missing-models", "malformed-models", "invalid-json", "oversized"],
)
def test_ollama_identity_is_bounded_and_validated(monkeypatch, identity):
    monkeypatch.setattr("code_review_agent.llm_factory.production", lambda: True)
    monkeypatch.setattr(
        "code_review_agent.llm_factory.require_model_approval", lambda _: {}
    )
    monkeypatch.setenv("REVIEW_OLLAMA_MODEL_DIGEST", "sha256:" + "a" * 64)
    sent = []

    def respond(request):
        sent.append(request.method)
        return httpx.Response(200, content=identity)

    install_transport(monkeypatch, respond)
    with pytest.raises(ProviderOutputError):
        ProviderLLM("ollama/approved-model", None).call("review")
    assert sent == ["GET"]


def test_ollama_pinned_digest_and_post_both_require_approval(monkeypatch):
    monkeypatch.setattr("code_review_agent.llm_factory.production", lambda: True)
    approval = Mock(return_value={})
    monkeypatch.setattr(
        "code_review_agent.llm_factory.require_model_approval", approval
    )
    digest = "sha256:" + "a" * 64
    monkeypatch.setenv("REVIEW_OLLAMA_MODEL_DIGEST", digest)
    monkeypatch.setenv("REVIEW_PROVIDER_MODEL_ID", "approved-model")

    def respond(request):
        if request.method == "GET":
            return httpx.Response(
                200, json={"models": [{"name": "approved-model", "digest": digest}]}
            )
        return httpx.Response(
            200, json={**PAYLOADS["ollama"], "model": "approved-model"}
        )

    install_transport(monkeypatch, respond)
    result = ProviderLLM("ollama/approved-model", None).call_response("review")
    assert approval.call_count == 2 and result.model_revision == digest


def test_stalled_transport_cannot_hold_the_caller_open(monkeypatch):
    release = threading.Event()
    finished = threading.Event()

    def respond(request):
        release.wait(timeout=5)
        finished.set()
        return httpx.Response(200, json=PAYLOADS["openai"])

    install_transport(monkeypatch, respond)
    try:
        with pytest.raises(TimeoutError, match="deadline"):
            ProviderLLM("openai/test", "test-key", timeout=0.05).call("review")
        assert not finished.is_set()
    finally:
        release.set()
