"""Log estimated model-context usage after each DeepAgents tool call."""

from __future__ import annotations

from collections.abc import Callable

from eliot import log_message
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langgraph.types import Command


class ContextUsageMiddleware(AgentMiddleware):
    """Log the context occupied by model output and every tool result."""

    def __init__(self, model: BaseChatModel, *, context_window_tokens: int) -> None:
        super().__init__()
        self._model = model
        self._context_window_tokens = context_window_tokens

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        """Run a tool and log the estimated context available to the next model call."""
        result = handler(request)
        tool_message = self._tool_message(result)
        added_tokens = self._message_tokens(tool_message) if tool_message else 0
        context_tokens = self._context_tokens(request, added_tokens)
        log_message(
            message_type="deep:context_usage",
            tool_name=request.tool_call["name"],
            tool_result_tokens=added_tokens,
            context_tokens=context_tokens,
            context_window_tokens=self._context_window_tokens,
            context_percent=round(context_tokens / self._context_window_tokens * 100, 2),
        )
        return result

    def _context_tokens(self, request: ToolCallRequest, tool_result_tokens: int) -> int:
        messages = self._messages(request)
        for message in reversed(messages):
            if not isinstance(message, AIMessage):
                continue
            input_tokens = message.usage_metadata.get("input_tokens")
            output_tokens = message.usage_metadata.get("output_tokens")
            if isinstance(input_tokens, int) and isinstance(output_tokens, int):
                return input_tokens + output_tokens + tool_result_tokens
        return sum(self._message_tokens(message) for message in messages) + tool_result_tokens

    def _messages(self, request: ToolCallRequest) -> list[BaseMessage]:
        if not isinstance(request.state, dict):
            return []
        messages = request.state.get("messages")
        if not isinstance(messages, list):
            return []
        return [message for message in messages if isinstance(message, BaseMessage)]

    def _tool_message(self, result: ToolMessage | Command) -> ToolMessage | None:
        if isinstance(result, ToolMessage):
            return result
        if not isinstance(result.update, dict):
            return None
        messages = result.update.get("messages")
        if not isinstance(messages, list):
            return None
        for message in reversed(messages):
            if isinstance(message, ToolMessage):
                return message
        return None

    def _message_tokens(self, message: BaseMessage) -> int:
        return self._model.get_num_tokens(str(message.content))


__all__ = ["ContextUsageMiddleware"]
