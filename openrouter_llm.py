"""Chat model wiring for OpenRouter, Cerebras, and local Ollama providers."""

from __future__ import annotations

import os
import re

import httpx
from langchain_core.language_models import ModelProfile
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI

from config import langgraph_config, settings
from llm_logging import EliotLLMCallback, log_http_request, log_http_response

OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"
CEREBRAS_API_BASE = "https://api.cerebras.ai/v1"

# LangChain ships capability profiles only for first-party model ids, so OpenRouter and
# Cerebras ids resolve to no profile. Callers that pick a structured-output strategy from
# the profile (the deepagents rubric grader) then fall back to ToolStrategy, which small
# models fail. Both providers accept tool calls and native json_schema responses.
OPENAI_COMPATIBLE_PROFILE: ModelProfile = {
    "structured_output": True,
    "tool_calling": True,
}


class OpenRouterAPIKeyError(RuntimeError):
    """Raised when the OpenRouter API key is not configured."""


class OpenRouterRequestError(RuntimeError):
    """Raised when OpenRouter cannot complete a model request."""


class CerebrasAPIKeyError(RuntimeError):
    """Raised when the Cerebras API key is not configured."""


def is_transient_openrouter_error(exc: BaseException) -> bool:
    """Return whether an exception represents a retryable OpenRouter failure."""
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return status_code == 408 or status_code == 429 or status_code >= 500
    match = re.search(r"""['"]code['"]\s*:\s*(\d{3})""", str(exc))
    if match is None:
        return False
    return int(match.group(1)) == 408 or int(match.group(1)) == 429 or int(match.group(1)) >= 500


def openrouter_api_key() -> str:
    """Return the OpenRouter API key from the environment."""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if api_key:
        return api_key
    raise OpenRouterAPIKeyError(
        "OpenRouter API key is required; set OPENROUTER_API_KEY in the environment "
        "(e.g. in mise.toml [env])"
    )


def cerebras_api_key() -> str:
    """Return the Cerebras API key from the environment."""
    api_key = os.environ.get("CEREBRAS_API_KEY")
    if api_key:
        return api_key
    raise CerebrasAPIKeyError(
        "Cerebras API key is required; set CEREBRAS_API_KEY in the environment "
        "(e.g. in mise.toml [env])"
    )


def configured_model_name() -> str:
    return str(langgraph_config()["model"])


def grader_model_name() -> str:
    """Return the model id used by the deep agent's rubric grader."""
    return str(langgraph_config()["grader_model"])


def chat_model(
    model_name: str | None = None,
    *,
    temperature: float | None = None,
    max_completion_tokens: int | None = None,
) -> BaseChatModel:
    """Return a chat model for the configured provider.

    - ``ollama/<model>`` → local Ollama
    - ``cerebras/<model>`` → Cerebras OpenAI-compatible API
    - otherwise → OpenRouter
    """
    raw = model_name or configured_model_name()
    name = raw.removeprefix("openrouter/")
    temp = (
        temperature
        if temperature is not None
        else float(settings["langgraph"].get("temperature", 0.0))
    )
    if name.startswith("ollama/"):
        return ChatOllama(
            model=name.removeprefix("ollama/"),
            temperature=temp,
            num_predict=max_completion_tokens,
            callbacks=[EliotLLMCallback()],
        )

    request_timeout = float(langgraph_config()["request_timeout_seconds"])
    event_hooks = {"request": [log_http_request], "response": [log_http_response]}
    client_options: dict[str, object] = {
        "temperature": temp,
        "timeout": request_timeout,
        "max_retries": int(langgraph_config()["request_max_retries"]),
        "callbacks": [EliotLLMCallback()],
        "profile": OPENAI_COMPATIBLE_PROFILE,
        "max_completion_tokens": max_completion_tokens,
        "http_client": httpx.Client(timeout=request_timeout, event_hooks=event_hooks),
        "http_async_client": httpx.AsyncClient(
            timeout=request_timeout,
            event_hooks=event_hooks,
        ),
    }
    if name.startswith("cerebras/"):
        return ChatOpenAI(
            model=name.removeprefix("cerebras/"),
            api_key=cerebras_api_key(),
            base_url=CEREBRAS_API_BASE,
            default_headers={
                "X-Cerebras-3rd-Party-Integration": "langchain",
            },
            **client_options,
        )

    reasoning_effort = str(langgraph_config()["reasoning_effort"])
    return ChatOpenAI(
        model=name,
        api_key=openrouter_api_key(),
        base_url=OPENROUTER_API_BASE,
        extra_body={"reasoning": {"effort": reasoning_effort}},
        **client_options,
    )
