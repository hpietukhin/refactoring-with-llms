"""Tests for the DeepAgents refactor entrypoints."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import ValidationError

import agents.deep.invoke as deep_invoke
import agents.deep.profiles as deep_profiles
from agents.deep.agent import (
    _build_validation_tools,
    build_system_prompt,
    invoke_deep_agent,
)
from agents.deep.context_usage import ContextUsageMiddleware
from agents.deep.invoke import DeepCaseResult, build_case_task
from agents.deep.paths import virtual_path
from agents.deep.profiles import DeepAgentProfile, load_deep_agent_profile
from smell.smell import Smell


class StreamingAgent:
    """Minimal DeepAgents stream double."""

    def __init__(self) -> None:
        self.inputs: object | None = None

    def stream(
        self,
        inputs: object,
        *,
        config: object,
        stream_mode: tuple[str, str],
    ) -> Iterator[object]:
        del config, stream_mode
        self.inputs = inputs
        yield "updates", {"tools": "edited /src/Main.java"}
        yield {"tools": "verified /src/Main.java"}

    def get_state(self, config: object) -> SimpleNamespace:
        del config
        return SimpleNamespace(values={"messages": []})


class TokenCounter:
    """Minimal model double for context-token estimates."""

    def get_num_tokens(self, text: str) -> int:
        """Use character count as a deterministic token estimate."""
        return len(text)


def test_build_system_prompt_includes_tool_workflow() -> None:
    prompt = build_system_prompt("Handle one smell.", java_source="1.6")
    assert "deepagents" in prompt
    assert "Automatic verification" in prompt
    assert "Guidelines:" in prompt
    assert "Tool workflow:" in prompt
    assert "read_smell_advice" in prompt
    assert "read_file" in prompt
    assert "edit_file" in prompt
    assert "Handle one smell." in prompt
    assert "Java source at level 1.6" in prompt


def test_context_usage_logs_after_a_tool_call(monkeypatch: pytest.MonkeyPatch) -> None:
    import agents.deep.context_usage as context_usage

    events: list[dict[str, object]] = []
    middleware = ContextUsageMiddleware(  # type: ignore[arg-type]
        TokenCounter(),
        context_window_tokens=1_000,
    )
    request = SimpleNamespace(
        tool_call={"name": "read_file", "args": {}, "id": "call-id"},
        state={
            "messages": [
                AIMessage(
                    content="",
                    usage_metadata={
                        "input_tokens": 100,
                        "output_tokens": 10,
                        "total_tokens": 110,
                    },
                )
            ]
        },
        runtime=None,
    )
    monkeypatch.setattr(context_usage, "log_message", lambda **kwargs: events.append(kwargs))

    middleware.wrap_tool_call(
        request,  # type: ignore[arg-type]
        lambda request: ToolMessage(content="result", tool_call_id="call-id"),
    )

    assert events == [
        {
            "message_type": "deep:context_usage",
            "tool_name": "read_file",
            "tool_result_tokens": 6,
            "context_tokens": 116,
            "context_window_tokens": 1_000,
            "context_percent": 11.6,
        }
    ]


def test_invoke_deep_agent_streams_whole_case_task(capsys: pytest.CaptureFixture[str]) -> None:
    agent = StreamingAgent()

    result = invoke_deep_agent(
        agent,
        case_id="case-id",
        task="Refactor all smells in /src/Main.java.",
    )

    assert result == {"messages": []}
    output = capsys.readouterr().out
    assert "[deep] {'tools': 'edited /src/Main.java'}" in output
    assert "[deep] {'tools': 'verified /src/Main.java'}" in output
    assert agent.inputs is not None
    assert "Refactor all smells in /src/Main.java." in str(agent.inputs)


def test_build_case_task_lists_all_selected_smells() -> None:
    smell = Smell.at(
        "Long Method",
        "/tmp/repo/src/Main.java",
        12,
        severity="HIGH",
        detected_by="ORGANIC",
    )
    repo = SimpleNamespace(path=Path("/tmp/repo"))
    task = build_case_task(repo, (smell,))  # type: ignore[arg-type]

    assert "type=Long Method" in task
    assert "file_path=/src/Main.java" in task
    assert "start_line=12" in task
    assert "advice_path=agents/deep/smells/long-method.md" in task
    assert "Create a todo for every listed smell." in task


def test_read_smell_advice_rejects_unknown_paths() -> None:
    repo = SimpleNamespace(path=Path("/tmp/repo"))
    tools = _build_validation_tools(repo, [], 30, model_name="model")  # type: ignore[arg-type]
    read_smell_advice = next(tool for tool in tools if tool.name == "read_smell_advice")

    advice = read_smell_advice.invoke(
        {"advice_path": "agents/deep/smells/long-method.md"}
    )

    assert "Long Method" in advice
    assert read_smell_advice.invoke({"advice_path": "../pyproject.toml"}) == (
        "Unknown smell advice path: ../pyproject.toml"
    )


def test_virtual_path_is_relative_to_deep_agent_repository() -> None:
    repo_path = Path("/tmp/project")

    assert (
        virtual_path(repo_path, "/tmp/project/src/test/java/ExampleTest.java")
        == "/src/test/java/ExampleTest.java"
    )


def test_load_without_planning_profile() -> None:
    profile = load_deep_agent_profile("without-planning")

    assert profile.mode == "sequential"
    assert profile.max_model_calls == 60


def test_load_deep_agent_profile_rejects_unknown_name() -> None:
    with pytest.raises(ValueError, match="Unknown DeepAgents profile"):
        load_deep_agent_profile("missing")


def test_load_deep_agent_profile_rejects_invalid_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "invalid.yaml").write_text(
        "name: invalid\nmode: sequential\nmax_model_calls: 0\n"
        "system_instructions: test\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(deep_profiles, "PROFILE_DIRECTORY", tmp_path)

    with pytest.raises(ValidationError, match="greater than 0"):
        load_deep_agent_profile("invalid")


def test_deep_agent_uses_one_session_for_all_case_smells(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smell = Smell.at(
        "Long Method",
        "/tmp/repo/src/Main.java",
        12,
        severity="HIGH",
        detected_by="ORGANIC",
    )
    profile = DeepAgentProfile(
        name="without-planning",
        mode="sequential",
        max_model_calls=3,
        system_instructions="Create todos.",
    )
    repo = SimpleNamespace(path=Path("/tmp/repo"))
    calls: list[object] = []
    events: list[dict[str, object]] = []
    findings = iter([(smell,), ()])

    monkeypatch.setattr(deep_invoke, "log_message", lambda **kwargs: events.append(kwargs))
    monkeypatch.setattr(
        deep_invoke,
        "remaining_target_smells",
        lambda repo, elements: next(findings),
    )
    monkeypatch.setattr(
        deep_invoke,
        "build_deep_agent",
        lambda repo, **kwargs: calls.append({"repo": repo, **kwargs}) or object(),
    )
    monkeypatch.setattr(
        deep_invoke,
        "invoke_deep_agent",
        lambda agent, *, case_id, task: calls.append((case_id, task))
        or {"messages": [SimpleNamespace(content="fixed")]},
    )

    result = deep_invoke.invoke_deep_case_agent(
        repo,  # type: ignore[arg-type]
        elements=["example.Main"],
        model_name="model",
        timeout=60,
        case_id="case",
        profile=profile,
    )

    assert result == DeepCaseResult("fixed", (), "smells_cleared", 1)
    assert calls[0] == {
        "repo": repo,
        "elements": ["example.Main"],
        "model_name": "model",
        "timeout": 60,
        "system_instructions": "Create todos.",
        "java_source": None,
        "max_model_calls": 3,
    }
    assert calls[1] == ("case", build_case_task(repo, (smell,)))
    prompt_event = next(event for event in events if event["message_type"] == "deep:prompt")
    assert prompt_event["system_prompt"] == build_system_prompt("Create todos.")
    assert prompt_event["task_prompt"] == build_case_task(repo, (smell,))


