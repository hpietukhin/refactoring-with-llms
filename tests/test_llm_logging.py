"""Tests for Eliot instrumentation of chat model traffic."""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import httpx
import pytest
from eliot import add_destinations, remove_destination
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from llm_logging import EliotLLMCallback, log_http_request, log_http_response


@pytest.fixture
def eliot_messages() -> Iterator[list[dict[str, object]]]:
    messages: list[dict[str, object]] = []
    destination = messages.append
    add_destinations(destination)
    yield messages
    remove_destination(destination)


def test_chat_model_start_logs_prompt_shape_and_tools(
    eliot_messages: list[dict[str, object]],
) -> None:
    callback = EliotLLMCallback()
    run_id = uuid4()
    callback.on_chat_model_start(
        {},
        [[SystemMessage(content="be brief"), HumanMessage(content="fix the smell")]],
        run_id=run_id,
        invocation_params={
            "model": "gemma-4-31b",
            "tools": [{"type": "function", "function": {"name": "read_file"}}],
        },
    )

    logged = eliot_messages[-1]
    assert logged["message_type"] == "llm:request"
    assert logged["model"] == "gemma-4-31b"
    assert logged["message_count"] == 2
    assert logged["prompt_chars"] == len("be brief") + len("fix the smell")
    assert logged["tool_names"] == ["read_file"]
    assert logged["last_message_tail"] == "fix the smell"


def test_llm_end_logs_usage_and_tool_calls(
    eliot_messages: list[dict[str, object]],
) -> None:
    callback = EliotLLMCallback()
    run_id = uuid4()
    callback.on_chat_model_start({}, [[HumanMessage(content="hi")]], run_id=run_id)
    message = AIMessage(
        content="",
        tool_calls=[
            {"name": "read_file", "args": {"file_path": "Main.java"}, "id": "call-1"}
        ],
    )
    callback.on_llm_end(
        LLMResult(
            generations=[
                [ChatGeneration(message=message, generation_info={"finish_reason": "tool_calls"})]
            ],
            llm_output={
                "model_name": "gemma-4-31b",
                "token_usage": {
                    "prompt_tokens": 82,
                    "completion_tokens": 19,
                    "total_tokens": 101,
                },
            },
        ),
        run_id=run_id,
    )

    logged = eliot_messages[-1]
    assert logged["message_type"] == "llm:response"
    assert logged["finish_reason"] == "tool_calls"
    assert logged["prompt_tokens"] == 82
    assert logged["total_tokens"] == 101
    assert logged["duration_ms"] is not None
    assert "read_file(" in str(logged["tool_calls"])


def test_llm_error_logs_status_code(eliot_messages: list[dict[str, object]]) -> None:
    class QuotaError(RuntimeError):
        status_code = 429

    callback = EliotLLMCallback()
    callback.on_llm_error(QuotaError("token quota exceeded"), run_id=uuid4())

    logged = eliot_messages[-1]
    assert logged["message_type"] == "llm:error"
    assert logged["status_code"] == 429
    assert logged["exception"] == "QuotaError"


def test_http_hooks_log_request_and_rate_limits(
    eliot_messages: list[dict[str, object]],
) -> None:
    request = httpx.Request(
        "POST",
        "https://api.cerebras.ai/v1/chat/completions",
        json={"model": "gemma-4-31b"},
    )
    log_http_request(request)
    logged_request = eliot_messages[-1]
    assert logged_request["message_type"] == "llm:http_request"
    assert logged_request["body_bytes"] > 0

    log_http_response(
        httpx.Response(
            429,
            headers={
                "retry-after": "59",
                "x-ratelimit-remaining-tokens-minute": "0",
                "cf-ray": "a25d0d13a974a3c3-BTS",
            },
            request=request,
        )
    )
    logged_response = eliot_messages[-1]
    assert logged_response["message_type"] == "llm:http_response"
    assert logged_response["status_code"] == 429
    assert logged_response["retry_after"] == "59"
    assert logged_response["x_ratelimit_remaining_tokens_minute"] == "0"
    assert logged_response["cf_ray"] == "a25d0d13a974a3c3-BTS"
