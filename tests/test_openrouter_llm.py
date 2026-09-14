"""Tests for chat model provider configuration."""

from __future__ import annotations

import httpx
import openrouter_llm
import pytest
from llm_logging import EliotLLMCallback


def test_chat_model_enables_high_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_chat_open_ai(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(openrouter_llm, "ChatOpenAI", fake_chat_open_ai)
    monkeypatch.setattr(openrouter_llm, "openrouter_api_key", lambda: "test-key")

    openrouter_llm.chat_model("example/model")

    assert captured["extra_body"] == {"reasoning": {"effort": "high"}}


def test_chat_model_sets_request_timeout_and_logging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_chat_open_ai(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(openrouter_llm, "ChatOpenAI", fake_chat_open_ai)
    monkeypatch.setattr(openrouter_llm, "openrouter_api_key", lambda: "test-key")

    openrouter_llm.chat_model("example/model")

    assert captured["timeout"] == 180.0
    assert captured["max_retries"] == 2
    assert captured["profile"] == {"structured_output": True, "tool_calling": True}
    callbacks = captured["callbacks"]
    assert isinstance(callbacks, list)
    assert isinstance(callbacks[0], EliotLLMCallback)
    client = captured["http_client"]
    assert isinstance(client, httpx.Client)
    assert client.event_hooks["request"] == [openrouter_llm.log_http_request]
    assert client.event_hooks["response"] == [openrouter_llm.log_http_response]


def test_chat_model_sets_max_completion_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_chat_open_ai(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(openrouter_llm, "ChatOpenAI", fake_chat_open_ai)
    monkeypatch.setattr(openrouter_llm, "openrouter_api_key", lambda: "test-key")

    openrouter_llm.chat_model("example/model", max_completion_tokens=10000)

    assert captured["max_completion_tokens"] == 10000


def test_chat_model_routes_ollama_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_chat_ollama(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(openrouter_llm, "ChatOllama", fake_chat_ollama)

    openrouter_llm.chat_model("ollama/qwen3.5:4b-mlx")

    assert captured["model"] == "qwen3.5:4b-mlx"
    assert "api_key" not in captured


def test_chat_model_routes_cerebras_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_chat_open_ai(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(openrouter_llm, "ChatOpenAI", fake_chat_open_ai)
    monkeypatch.setattr(openrouter_llm, "cerebras_api_key", lambda: "cerebras-test-key")

    openrouter_llm.chat_model("cerebras/gemma-4-31b")

    assert captured["model"] == "gemma-4-31b"
    assert captured["api_key"] == "cerebras-test-key"
    assert captured["base_url"] == openrouter_llm.CEREBRAS_API_BASE
    assert "extra_body" not in captured
