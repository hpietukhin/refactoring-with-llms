"""Tests for the isolated Pydantic AI refactoring agent."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic_ai.models.test import TestModel

from agents.pydantic_deep.agent import build_pydantic_deep_agent, build_system_prompt
from agents.pydantic_deep.invoke import (
    build_rubric,
    write_agent_log,
    write_error_log,
)
from repository.repo import Repo
from smell.smell import Smell


def test_build_rubric_mentions_pydantic_validation_tools() -> None:
    smell = Smell.at(
        "Class Data Should Be Private",
        "src/Main.java",
        12,
        end_line=15,
        severity="MEDIUM",
        detected_by="ORGANIC",
    )

    rubric = build_rubric(smell)

    assert "java_verification" in rubric
    assert "detect_remaining_smells" in rubric
    assert "type=Class Data Should Be Private" in rubric
    assert "start_line=12" in rubric
    assert "end_line=15" in rubric


def test_system_prompt_disables_network_search() -> None:
    prompt = build_system_prompt()

    assert "Pydantic AI" in prompt
    assert "network search" in prompt
    assert "automatically runs java_verification" in prompt
    assert "await asyncio.gather" not in prompt


def test_write_agent_log_uses_case_id_filename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agents.pydantic_deep.invoke.PYDANTIC_LOG_DIR", tmp_path)

    path = write_agent_log("Tap4j:4413ab35b400", b'["message"]')

    assert path == tmp_path / "Tap4j_4413ab35b400.json"
    assert path.read_bytes() == b'["message"]'


def test_write_error_log_includes_tool_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agents.pydantic_deep.invoke.PYDANTIC_LOG_DIR", tmp_path)
    events = [
        {
            "event": "tool_retry",
            "tool": "edit_file",
            "content": "old_text was not found",
        }
    ]

    path = write_error_log(
        "Tap4j:4413ab35b400",
        RuntimeError("tool retries exhausted"),
        events,
    )

    assert path == tmp_path / "Tap4j_4413ab35b400.errors.jsonl"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["error"] == "tool retries exhausted"
    assert payload["events"] == events


def test_build_agent_registers_code_mode_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from git import Repo as GitRepo

    GitRepo.init(tmp_path)

    model = TestModel(call_tools=[])

    def fake_build_model(model_name: str) -> TestModel:
        del model_name
        return model

    monkeypatch.setattr(
        "agents.pydantic_deep.agent._build_model",
        fake_build_model,
    )

    agent = build_pydantic_deep_agent(
        Repo(tmp_path),
        elements=[],
        model_name="test-model",
        timeout=30,
    )

    assert agent.name == "pydantic_java_smell_refactor"
    result = agent.run_sync("Verify the available tools.")
    assert result.output == "success (no tool calls)"
    tools = model.last_model_request_parameters.function_tools
    assert {tool.name for tool in tools} == {
        "check_command",
        "create_directory",
        "edit_file",
        "file_info",
        "find_files",
        "list_directory",
        "read_file",
        "run_code",
        "run_command",
        "search_files",
        "start_command",
        "stop_command",
        "write_file",
    }
    run_code = next(tool for tool in tools if tool.name == "run_code")
    assert "java_verification" in run_code.description
    assert "detect_remaining_smells" in run_code.description
