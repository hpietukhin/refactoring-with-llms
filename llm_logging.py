"""Eliot instrumentation for chat model traffic."""

from __future__ import annotations

import json
import time
from uuid import UUID

import httpx
from eliot import log_message
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage
from langchain_core.outputs import LLMResult

RATE_LIMIT_HEADERS = (
    "retry-after",
    "x-ratelimit-limit-requests-minute",
    "x-ratelimit-remaining-requests-minute",
    "x-ratelimit-limit-tokens-minute",
    "x-ratelimit-remaining-tokens-minute",
)


def log_http_request(request: httpx.Request) -> None:
    """Log one provider HTTP request; SDK retries appear as repeated events."""
    log_message(
        message_type="llm:http_request",
        method=request.method,
        url=str(request.url),
        body_bytes=int(request.headers.get("content-length") or 0),
    )


def log_http_response(response: httpx.Response) -> None:
    """Log provider HTTP status, request id, and rate-limit headers."""
    limits = {
        name.replace("-", "_"): response.headers[name]
        for name in RATE_LIMIT_HEADERS
        if name in response.headers
    }
    log_message(
        message_type="llm:http_response",
        url=str(response.request.url),
        status_code=response.status_code,
        request_id=response.headers.get("x-request-id"),
        cf_ray=response.headers.get("cf-ray"),
        **limits,
    )


def tool_schema_names(schemas: list[object]) -> list[str]:
    """Return the tool names carried by OpenAI-style tool schemas."""
    names: list[str] = []
    for schema in schemas:
        if not isinstance(schema, dict):
            continue
        function = schema.get("function")
        if isinstance(function, dict):
            names.append(str(function.get("name", "?")))
            continue
        names.append(str(schema.get("name", "?")))
    return names


class EliotLLMCallback(BaseCallbackHandler):
    """Log every chat model request, response, and failure to Eliot."""

    def __init__(self) -> None:
        self._started: dict[UUID, float] = {}

    def on_chat_model_start(
        self,
        serialized: dict[str, object],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        metadata: dict[str, object] | None = None,
        **kwargs: object,
    ) -> None:
        """Log prompt shape, size, and bound tools before the request goes out."""
        del serialized
        self._started[run_id] = time.monotonic()
        prompt = messages[0] if messages else []
        params = kwargs.get("invocation_params")
        invocation = params if isinstance(params, dict) else {}
        tools = invocation.get("tools")
        schemas = tools if isinstance(tools, list) else []
        model = invocation.get("model") or (metadata or {}).get("ls_model_name")
        log_message(
            message_type="llm:request",
            run_id=str(run_id),
            parent_run_id=str(parent_run_id) if parent_run_id else None,
            model=str(model or ""),
            message_count=len(prompt),
            message_shape=[
                f"{message.type}:{len(str(message.content))}" for message in prompt
            ],
            prompt_chars=sum(len(str(message.content)) for message in prompt),
            tool_count=len(schemas),
            tool_names=tool_schema_names(schemas),
            tool_schema_chars=len(json.dumps(schemas, default=str)),
            last_message_tail=str(prompt[-1].content)[-500:] if prompt else "",
        )

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: object,
    ) -> None:
        """Log token usage, finish reason, and requested tool calls."""
        del parent_run_id, kwargs
        duration_ms = self._duration_ms(run_id)
        llm_output = response.llm_output if isinstance(response.llm_output, dict) else {}
        raw_usage = llm_output.get("token_usage")
        usage = raw_usage if isinstance(raw_usage, dict) else {}
        candidates = response.generations[0] if response.generations else []
        generation = candidates[0] if candidates else None
        info = getattr(generation, "generation_info", None) or {}
        message = getattr(generation, "message", None)
        tool_calls = getattr(message, "tool_calls", None) or []
        content = str(getattr(message, "content", ""))
        log_message(
            message_type="llm:response",
            run_id=str(run_id),
            duration_ms=duration_ms,
            model=str(llm_output.get("model_name") or ""),
            finish_reason=str(info.get("finish_reason") or ""),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
            content_chars=len(content),
            content_tail=content[-500:],
            tool_calls=[
                f"{call.get('name')}({json.dumps(call.get('args'), default=str)[:300]})"
                for call in tool_calls
            ],
        )

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: object,
    ) -> None:
        """Log the failure that ended a chat model request."""
        del parent_run_id, kwargs
        log_message(
            message_type="llm:error",
            run_id=str(run_id),
            duration_ms=self._duration_ms(run_id),
            exception=type(error).__name__,
            message=str(error)[:1000],
            status_code=getattr(error, "status_code", None),
            request_id=getattr(error, "request_id", None),
        )

    def _duration_ms(self, run_id: UUID) -> float | None:
        started = self._started.pop(run_id, None)
        if started is None:
            return None
        return round((time.monotonic() - started) * 1000, 2)
