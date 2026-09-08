"""Bounded LangGraph repair agent: file tools + a dedicated Gradle test node."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal, TypedDict

from eliot import log_message
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.managed import RemainingSteps
from langgraph.prebuilt import ToolNode

from agents.java_test.diagnostics import format_verification_feedback
from agents.java_test.models import RepairResult, TestRunSummary
from agents.java_test.tool_logging import guarded_tool_call_logger, invoke_logged_tool
from agents.java_test.tools import MUTATING_TOOL_NAMES, make_tools
from config import settings
from agents.java_test.file_management import ApplyPatchTool, CreatePatchTool, VerifyPatchTool
from openrouter_llm import OpenRouterAPIKeyError, chat_model
from repository.repo import Repo
from testing.surefire import gradle_test_command, run_gradle_tests

REMAINING_STEPS_STOP_THRESHOLD = 4


class RepairState(TypedDict, total=False):
    """Messages plus verification status for one repair run."""

    messages: Annotated[list[BaseMessage], add_messages]
    tests_passed: bool
    attempt: int
    max_attempts: int
    verification_command: str
    timeout: int
    patch_sequence_ok: bool
    patch_stage: str
    remaining_steps: RemainingSteps
    stop_reason: str


def _message_text(message: object) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, list):
        return "\n".join(str(part) for part in content)
    return str(content)


def _last_tool_names(messages: list[BaseMessage]) -> set[str]:
    for message in reversed(messages):
        tool_calls = getattr(message, "tool_calls", None)
        if isinstance(tool_calls, list) and tool_calls:
            names: set[str] = set()
            for call in tool_calls:
                if isinstance(call, dict):
                    name = call.get("name")
                else:
                    name = getattr(call, "name", None)
                if isinstance(name, str):
                    names.add(name)
            return names
    return set()


def build_repair_graph(
    repo: Repo,
    *,
    model_name: str,
    step_limit: int,
) -> object:
    """Build agent → tools → (optional) Maven test → agent loop."""
    temperature = float(settings["langgraph"].get("temperature", 0.0))
    model = chat_model(model_name, temperature=temperature)
    tools = make_tools(repo)
    create_patch_tool = next(tool for tool in tools if isinstance(tool, CreatePatchTool))
    apply_patch_tool = next(tool for tool in tools if isinstance(tool, ApplyPatchTool))
    verify_patch_tool = next(tool for tool in tools if isinstance(tool, VerifyPatchTool))
    model_tools = [
        tool
        for tool in tools
        if not isinstance(tool, (ApplyPatchTool, VerifyPatchTool))
    ]
    model_with_tools = model.bind_tools(model_tools)
    system = SystemMessage(
        content=(
            "You are a minimal Java build repair agent. Fix only verification failures. "
            "Use file tools to read and patch sources under the project root. "
            "Compiler diagnostics include the failing source line and nearby code. "
            "Use search_text to find other assignments or usages before editing. "
            "Do not repeat an identical tool call; expand the range or change strategy. "
            "Do not refactor unrelated code. "
            "For every source edit, execute create_patch, then apply_patch, then "
            "verify_patch in that exact order. Each stage must succeed before the "
            "next stage. Maven tests run automatically after apply_patch/delete/"
            "move/copy; "
            "wait for those results instead of asking to run tests yourself."
        )
    )

    def agent(state: RepairState) -> RepairState:
        response = model_with_tools.invoke([system, *state["messages"]])
        return {"messages": [response]}

    def run_tests(state: RepairState) -> RepairState:
        timeout = float(state.get("timeout") or 300)
        command = state.get("verification_command") or gradle_test_command()
        summary = run_gradle_tests(repo, clean=False, jacoco=False, timeout=timeout)
        attempt = int(state.get("attempt") or 0) + 1
        return {
            "messages": [
                HumanMessage(
                    content=format_verification_feedback(summary, repo.path, command)
                )
            ],
            "tests_passed": summary.success,
            "attempt": attempt,
        }

    def apply_patch_stage(state: RepairState) -> RepairState:
        patch_path = create_patch_tool.sequence.pending_patch()
        if patch_path is None:
            return {
                "messages": [
                    HumanMessage(content="Patch sequence failed: no pending patch exists.")
                ],
                "patch_sequence_ok": False,
                "patch_stage": "failed",
            }
        relative_patch = patch_path.relative_to(repo.path).as_posix()
        applied = invoke_logged_tool(
            apply_patch_tool,
            "apply_patch",
            {"patch_path": relative_patch},
        )
        if not apply_patch_tool.sequence.is_applied(patch_path):
            return {
                "messages": [HumanMessage(content=str(applied))],
                "patch_sequence_ok": False,
                "patch_stage": "failed",
            }
        return {
            "messages": [HumanMessage(content=str(applied))],
            "patch_sequence_ok": True,
            "patch_stage": "applied",
        }

    def verify_patch_stage(state: RepairState) -> RepairState:
        patch_path = create_patch_tool.sequence.latest_patch()
        if patch_path is None:
            return {
                "messages": [
                    HumanMessage(content="Patch sequence failed: no created patch exists.")
                ],
                "patch_sequence_ok": False,
                "patch_stage": "failed",
            }
        relative_patch = patch_path.relative_to(repo.path).as_posix()
        verified = invoke_logged_tool(
            verify_patch_tool,
            "verify_patch",
            {"patch_path": relative_patch},
        )
        return {
            "messages": [HumanMessage(content=str(verified))],
            "patch_sequence_ok": verify_patch_tool.sequence.is_verified(patch_path),
            "patch_stage": (
                "verified"
                if verify_patch_tool.sequence.is_verified(patch_path)
                else "failed"
            ),
        }

    def stop(state: RepairState) -> RepairState:
        return {"stop_reason": state.get("stop_reason") or "agent_stopped"}

    tool_node = ToolNode(tools, wrap_tool_call=guarded_tool_call_logger())

    def run_tools(state: RepairState) -> RepairState:
        result = tool_node.invoke(state)
        messages = result.get("messages") if isinstance(result, dict) else None
        tool_messages = messages if isinstance(messages, list) else []
        return {"messages": tool_messages}

    def route_after_agent(state: RepairState) -> Literal["tools", "stop"]:
        remaining_steps = state.get("remaining_steps")
        if (
            remaining_steps is not None
            and remaining_steps <= REMAINING_STEPS_STOP_THRESHOLD
        ):
            return "stop"
        messages = state.get("messages") or []
        if messages and getattr(messages[-1], "tool_calls", None):
            return "tools"
        return "stop"

    def route_after_tools(
        state: RepairState,
    ) -> Literal["test", "apply_patch", "agent", "stop"]:
        if "create_patch" in _last_tool_names(state["messages"]):
            return "apply_patch"
        if _last_tool_names(state["messages"]) & MUTATING_TOOL_NAMES:
            return "test"
        if state.get("patch_stage") == "verified":
            return "agent"
        return "agent"

    def route_after_apply(state: RepairState) -> Literal["verify_patch", "agent", "stop"]:
        return "verify_patch" if state.get("patch_sequence_ok") else "agent"

    def route_after_verify(state: RepairState) -> Literal["test", "agent", "stop"]:
        return "test" if state.get("patch_sequence_ok") else "agent"

    def route_after_test(state: RepairState) -> Literal["agent", "stop"]:
        if state.get("tests_passed"):
            return "stop"
        max_attempts = int(state.get("max_attempts") or 1)
        if int(state.get("attempt") or 0) >= max_attempts:
            return "stop"
        return "agent"

    graph = StateGraph(RepairState)
    graph.add_node("agent", agent)
    graph.add_node("tools", run_tools)
    graph.add_node("apply_patch", apply_patch_stage)
    graph.add_node("verify_patch", verify_patch_stage)
    graph.add_node("test", run_tests)
    graph.add_node("stop", stop)
    graph.add_edge("stop", END)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges(
        "agent",
        route_after_agent,
        {"tools": "tools", "stop": "stop"},
    )
    graph.add_conditional_edges(
        "tools",
        route_after_tools,
        {"test": "test", "apply_patch": "apply_patch", "agent": "agent", "stop": "stop"},
    )
    graph.add_conditional_edges(
        "apply_patch",
        route_after_apply,
        {"verify_patch": "verify_patch", "agent": "agent", "stop": "stop"},
    )
    graph.add_conditional_edges(
        "verify_patch",
        route_after_verify,
        {"test": "test", "agent": "agent", "stop": "stop"},
    )
    graph.add_conditional_edges(
        "test",
        route_after_test,
        {"agent": "agent", "stop": "stop"},
    )
    return graph.compile()


def repair_checkout(
    repo: Repo,
    summary: TestRunSummary,
    *,
    verification_command: str,
    model_name: str,
    step_limit: int,
    timeout: int,
    max_attempts: int | None = None,
) -> dict[str, object]:
    """Run one bounded repair attempt with file tools and a Maven test node."""
    before = {path.as_posix() for path in repo.filter_java_edits(include_untracked=True)}
    attempts = max_attempts if max_attempts is not None else max(1, step_limit)
    try:
        graph = build_repair_graph(repo, model_name=model_name, step_limit=step_limit)
        task = (
            f"{format_verification_feedback(summary, repo.path, verification_command)}\n"
            "Fix the build/test failure with minimal file edits. "
            "Tests will run automatically after you change files."
        )
        result = graph.invoke(  # type: ignore[attr-defined]
            {
                "messages": [HumanMessage(content=task)],
                "tests_passed": False,
                "attempt": 0,
                "max_attempts": attempts,
                "verification_command": verification_command,
                "timeout": timeout,
            },
            config={
                "recursion_limit": max(16, step_limit * 4),
                "configurable": {"thread_id": uuid.uuid4().hex},
            },
        )
    except GraphRecursionError as exc:
        log_message(message_type="repair:recursion_limit", exception=str(exc))
        return RepairResult(
            attempted=True,
            applied=False,
            error="repair graph reached its recursion limit",
            model=model_name,
        ).to_dict()
    except Exception as exc:  # noqa: BLE001 - surface repair failure to caller
        log_message(message_type="repair:failed", exception=str(exc))
        if isinstance(exc, OpenRouterAPIKeyError):
            raise
        return RepairResult(
            attempted=True,
            applied=False,
            error=str(exc),
            model=model_name,
        ).to_dict()

    messages = result.get("messages", []) if isinstance(result, dict) else []
    output = "\n".join(_message_text(message) for message in messages[-6:])
    tool_calls = sum(
        len(calls)
        for message in messages
        if isinstance((calls := getattr(message, "tool_calls", None)), list)
    )
    after = {path.as_posix() for path in repo.filter_java_edits(include_untracked=True)}
    changed = sorted(after - before) or sorted(
        path.as_posix() for path in repo.filter_java_edits(include_untracked=True)
    )
    applied = bool(changed) or tool_calls > 0
    tests_passed = bool(result.get("tests_passed")) if isinstance(result, dict) else False
    return RepairResult(
        attempted=True,
        applied=applied,
        changed_files=changed,
        steps=len(messages),
        tool_calls=tool_calls,
        stopped_early=not tests_passed,
        output_excerpt=output[-2000:],
        model=model_name,
    ).to_dict()
