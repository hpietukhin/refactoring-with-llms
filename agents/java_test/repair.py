"""Bounded LangGraph repair agent: file tools + dedicated Maven test node."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal, TypedDict

from eliot import log_message
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition

from agents.java_test.models import RepairResult, TestRunSummary
from agents.java_test.tools import MUTATING_TOOL_NAMES, make_tools
from config import settings
from openrouter_llm import chat_model
from repository.repo import Repo
from testing.surefire import run_maven_tests

PROMPT_OUTPUT_EXCERPT_CHARS = 8000
TEST_OUTPUT_TAIL_CHARS = 4000


class RepairState(TypedDict, total=False):
    """Messages plus verification status for one repair run."""

    messages: Annotated[list[BaseMessage], add_messages]
    tests_passed: bool
    attempt: int
    max_attempts: int
    verification_command: str
    timeout: int


def _message_text(message: object) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, list):
        return "\n".join(str(part) for part in content)
    return str(content)


def _failure_excerpt(summary: TestRunSummary) -> str:
    parts: list[str] = []
    if summary.stderr:
        parts.append(summary.stderr[-PROMPT_OUTPUT_EXCERPT_CHARS:])
    if summary.stdout:
        parts.append(summary.stdout[-PROMPT_OUTPUT_EXCERPT_CHARS:])
    for test in summary.tests:
        if test.status in {"FAIL", "ERROR"}:
            parts.append(
                f"{test.name}: {test.error_message or ''} {test.failure_trace or ''}"[:1000]
            )
    return "\n".join(parts)[:PROMPT_OUTPUT_EXCERPT_CHARS]


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


def _format_test_message(summary: TestRunSummary, verification_command: str) -> str:
    failed = [test.name for test in summary.tests if test.status in {"FAIL", "ERROR"}]
    tail = ((summary.stdout or "") + "\n" + (summary.stderr or ""))[-TEST_OUTPUT_TAIL_CHARS:]
    return (
        f"Maven test results after your edits.\n"
        f"command={verification_command}\n"
        f"success={summary.success} exit_code={summary.exit_code} "
        f"passed={summary.counts.passed} failed={summary.counts.failed} "
        f"errors={summary.counts.errors}\n"
        f"failed_tests={failed[:20]}\n"
        f"output_tail:\n{tail}"
    )


def build_repair_graph(
    repo: Repo,
    *,
    model_name: str,
    step_limit: int,
) -> object:
    """Build agent → tools → (optional) Maven test → agent loop."""
    del step_limit
    temperature = float(settings["langgraph"].get("temperature", 0.0))
    model = chat_model(model_name, temperature=temperature)
    tools = make_tools(repo)
    model_with_tools = model.bind_tools(tools)
    system = SystemMessage(
        content=(
            "You are a minimal Java build repair agent. Fix only verification failures. "
            "Use file tools to read and edit sources under the project root. "
            "Do not refactor unrelated code. "
            "Maven tests run automatically after write/delete/move/copy; "
            "wait for those results instead of asking to run tests yourself."
        )
    )

    def agent(state: RepairState) -> RepairState:
        response = model_with_tools.invoke([system, *state["messages"]])
        return {"messages": [response]}

    def run_tests(state: RepairState) -> RepairState:
        timeout = float(state.get("timeout") or 300)
        command = state.get("verification_command") or "mvn test"
        summary = run_maven_tests(repo, clean=False, jacoco=False, timeout=timeout)
        attempt = int(state.get("attempt") or 0) + 1
        return {
            "messages": [HumanMessage(content=_format_test_message(summary, command))],
            "tests_passed": summary.success,
            "attempt": attempt,
        }

    def route_after_tools(state: RepairState) -> Literal["test", "agent"]:
        if _last_tool_names(state["messages"]) & MUTATING_TOOL_NAMES:
            return "test"
        return "agent"

    def route_after_test(state: RepairState) -> Literal["agent", "end"]:
        if state.get("tests_passed"):
            return "end"
        max_attempts = int(state.get("max_attempts") or 1)
        if int(state.get("attempt") or 0) >= max_attempts:
            return "end"
        return "agent"

    graph = StateGraph(RepairState)
    graph.add_node("agent", agent)
    graph.add_node("tools", ToolNode(tools))
    graph.add_node("test", run_tests)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges(
        "agent",
        tools_condition,
        {"tools": "tools", END: END},
    )
    graph.add_conditional_edges(
        "tools",
        route_after_tools,
        {"test": "test", "agent": "agent"},
    )
    graph.add_conditional_edges(
        "test",
        route_after_test,
        {"agent": "agent", "end": END},
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
            f"Maven verification failed with exit_code={summary.exit_code}.\n"
            f"Verification command: {verification_command}\n"
            f"Failure excerpt:\n{_failure_excerpt(summary)}\n"
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
    except Exception as exc:  # noqa: BLE001 - surface repair failure to caller
        log_message(message_type="repair:failed", exception=str(exc))
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
