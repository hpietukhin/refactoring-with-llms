"""OpenRouter LangChain chat model wiring."""

from __future__ import annotations

import os

from langchain_openai import ChatOpenAI

from config import langgraph_config, openrouter_config, settings

OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"


def openrouter_api_key() -> str:
    """Return the OpenRouter API key from config or the environment."""
    configured = openrouter_config().get("api_key")
    if isinstance(configured, str) and configured:
        return configured
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if api_key:
        return api_key
    raise RuntimeError(
        "OpenRouter API key is required; set [openrouter].api_key in config.toml "
        "or OPENROUTER_API_KEY in the environment"
    )


def configured_model_name() -> str:
    return str(langgraph_config()["model"])


def chat_model(model_name: str | None = None, *, temperature: float | None = None) -> ChatOpenAI:
    name = (model_name or configured_model_name()).removeprefix("openrouter/")
    temp = (
        temperature
        if temperature is not None
        else float(settings["langgraph"].get("temperature", 0.0))
    )
    return ChatOpenAI(
        model=name,
        temperature=temp,
        api_key=openrouter_api_key(),
        base_url=OPENROUTER_API_BASE,
    )
