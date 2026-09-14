"""Structured logging for agent tool execution."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from eliot import log_message
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

ToolCallHandler = Callable[
    [ToolCallRequest],
    ToolMessage | Command,
]


@dataclass(slots=True)
class ToolCallRepeatGuard:
    """Block consecutive identical tool calls before execution."""

    max_consecutive: int = 1
    _last_fingerprint: str | None = None
    _consecutive: int = 0

    def is_blocked(self, tool_name: str, arguments: dict[str, object]) -> bool:
        """Record a tool call and report whether it exceeds the repeat limit."""
        fingerprint = json.dumps(
            {"name": tool_name, "arguments": arguments},
            sort_keys=True,
            separators=(",", ":"),
        )
        if fingerprint == self._last_fingerprint:
            self._consecutive += 1
        else:
            self._last_fingerprint = fingerprint
            self._consecutive = 1
        return self._consecutive > self.max_consecutive


def log_tool_call(
    request: ToolCallRequest,
    handler: ToolCallHandler,
) -> ToolMessage | Command:
    """Log one ToolNode call before and after its execution."""
    started = time.monotonic()
    tool_call = request.tool_call
    tool_name = str(tool_call["name"])
    tool_call_id = str(tool_call["id"])
    log_message(
        message_type="tool:start",
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        args=str(tool_call.get("args", {})),
    )
    try:
        result = handler(request)
    except Exception as exc:  # noqa: BLE001 - preserve tool error semantics
        log_message(
            message_type="tool:error",
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            exception=str(exc),
            duration_ms=round((time.monotonic() - started) * 1000, 2),
        )
        raise
    log_message(
        message_type="tool:done",
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        result=str(result)[-2000:],
        duration_ms=round((time.monotonic() - started) * 1000, 2),
    )
    return result


def guarded_tool_call_logger() -> Callable[
    [ToolCallRequest, ToolCallHandler],
    ToolMessage | Command,
]:
    """Create per-run logging middleware with duplicate-call protection."""
    guard = ToolCallRepeatGuard()

    def invoke(
        request: ToolCallRequest,
        handler: ToolCallHandler,
    ) -> ToolMessage | Command:
        tool_call = request.tool_call
        tool_name = str(tool_call["name"])
        raw_arguments = tool_call.get("args", {})
        arguments = raw_arguments if isinstance(raw_arguments, dict) else {}
        if guard.is_blocked(tool_name, arguments):
            log_message(
                message_type="tool:duplicate_blocked",
                tool_name=tool_name,
                args=str(arguments),
            )
            return ToolMessage(
                content=(
                    "Duplicate tool call blocked. The same tool and arguments were "
                    "used immediately before. Use search_text, expand the line range, "
                    "or choose a different action."
                ),
                tool_call_id=str(tool_call["id"]),
                name=tool_name,
            )
        return log_tool_call(request, handler)

    return invoke


def invoke_logged_tool(
    tool: BaseTool,
    tool_name: str,
    arguments: dict[str, str],
) -> str:
    """Invoke a graph-owned tool and log its result."""
    started = time.monotonic()
    log_message(
        message_type="tool:start",
        tool_name=tool_name,
        tool_call_id=f"graph:{tool_name}",
        args=str(arguments),
    )
    try:
        result = cast(str, tool.invoke(arguments))
    except Exception as exc:  # noqa: BLE001 - preserve tool error semantics
        log_message(
            message_type="tool:error",
            tool_name=tool_name,
            tool_call_id=f"graph:{tool_name}",
            exception=str(exc),
            duration_ms=round((time.monotonic() - started) * 1000, 2),
        )
        raise
    log_message(
        message_type="tool:done",
        tool_name=tool_name,
        tool_call_id=f"graph:{tool_name}",
        result=result[-2000:],
        duration_ms=round((time.monotonic() - started) * 1000, 2),
    )
    return result
