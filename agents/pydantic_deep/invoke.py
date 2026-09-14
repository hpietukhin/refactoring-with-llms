"""Invocation entrypoint for the Pydantic AI refactoring agent."""

from __future__ import annotations

from collections.abc import AsyncIterable
import json
from pathlib import Path

from eliot import log_message
from pydantic_ai import (
    AgentStreamEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    RetryPromptPart,
    RunContext,
    TextPartDelta,
    ToolReturnPart,
)
from pydantic_ai.usage import UsageLimits

from agents.pydantic_deep.agent import (
    build_pydantic_deep_agent,
    log_evaluation_metrics,
)
from config import ROOT
from repository.repo import Repo
from smell.smell import Smell
from workflows.composite.nodes.refactor import refactor_task_for_smell

PYDANTIC_LOG_DIR = ROOT / "data" / "pydantic"


def build_rubric(smell: Smell) -> str:
    """Build the success criteria supplied with one refactoring task."""
    start_line = smell.location.range.start.line + 1
    end_line = smell.location.range.end.line + 1
    return (
        "- All tests pass when java_verification reports tests_failed=False.\n"
        f"- Targeted code smell is fixed: type={smell.type} "
        f"file_path={smell.file_path} start_line={start_line} end_line={end_line}.\n"
        "- detect_remaining_smells no longer reports that smell at the same location."
    )


def write_agent_log(case_id: str, messages: bytes) -> Path:
    """Write one Pydantic AI message history under the project log directory."""
    safe_case_id = "".join(
        character if character.isalnum() or character in "-._" else "_"
        for character in case_id
    )
    path = PYDANTIC_LOG_DIR / f"{safe_case_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(messages)
    return path


def write_error_log(
    case_id: str,
    error: BaseException,
    events: list[dict[str, str]],
) -> Path:
    """Append one failed Pydantic AI run and its tool events."""
    safe_case_id = "".join(
        character if character.isalnum() or character in "-._" else "_"
        for character in case_id
    )
    path = PYDANTIC_LOG_DIR / f"{safe_case_id}.errors.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "error_type": type(error).__name__,
        "error": str(error),
        "events": events,
    }
    with path.open("a", encoding="utf-8") as log_file:
        log_file.write(json.dumps(payload) + "\n")
    return path


async def stream_to_stdout(
    ctx: RunContext[None],
    events: AsyncIterable[AgentStreamEvent],
    event_log: list[dict[str, str]],
) -> None:
    """Print agent text and function-tool activity as the run progresses."""
    del ctx
    async for event in events:
        match event:
            case PartDeltaEvent(delta=TextPartDelta(content_delta=content)):
                print(content, end="", flush=True)
            case FunctionToolCallEvent(part=part):
                print(f"\n[tool] {part.tool_name}({part.args})", flush=True)
                event_log.append(
                    {
                        "event": "tool_call",
                        "tool": part.tool_name,
                        "content": str(part.args),
                    }
                )
            case FunctionToolResultEvent(
                part=ToolReturnPart(tool_name=tool_name),
                content=content,
            ):
                print(f"\n[tool result] {tool_name}: {content}", flush=True)
                event_log.append(
                    {
                        "event": "tool_result",
                        "tool": tool_name,
                        "content": str(content),
                    }
                )
            case FunctionToolResultEvent(
                part=RetryPromptPart(tool_name=tool_name, content=content),
            ):
                print(f"\n[tool retry] {tool_name}: {content}", flush=True)
                event_log.append(
                    {
                        "event": "tool_retry",
                        "tool": tool_name or "",
                        "content": str(content),
                    }
                )


def invoke_pydantic_deep_agent(
    repo: Repo,
    *,
    smell: Smell,
    elements: list[str],
    model_name: str,
    timeout: int,
    case_id: str,
    max_iterations: int = 50,
) -> str:
    """Run the agent for one case and return its final response."""
    agent = build_pydantic_deep_agent(
        repo,
        elements=elements,
        model_name=model_name,
        timeout=timeout,
    )
    prompt = f"{refactor_task_for_smell(smell)}\nRubric:\n{build_rubric(smell)}"
    event_log: list[dict[str, str]] = []

    async def stream_events(
        ctx: RunContext[None],
        events: AsyncIterable[AgentStreamEvent],
    ) -> None:
        await stream_to_stdout(ctx, events, event_log)

    try:
        result = agent.run_sync(
            prompt,
            conversation_id=case_id,
            usage_limits=UsageLimits(request_limit=max_iterations),
            event_stream_handler=stream_events,
        )
    except BaseException as exc:
        log_path = write_error_log(case_id, exc, event_log)
        log_message(
            message_type="pydantic_deep:refactor_failed",
            case_id=case_id,
            exception=str(exc),
            log_path=str(log_path),
        )
        raise
    print()
    log_path = write_agent_log(case_id, result.all_messages_json())
    log_evaluation_metrics(repo, timeout)
    log_message(
        message_type="pydantic_deep:refactor_done",
        case_id=case_id,
        content=result.output[-1000:],
        log_path=str(log_path),
    )
    return result.output


__all__ = [
    "build_rubric",
    "invoke_pydantic_deep_agent",
    "stream_to_stdout",
    "write_agent_log",
    "write_error_log",
]
