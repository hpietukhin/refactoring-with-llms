"""LLM refactor and validate nodes for the composite workflow."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal, TypedDict

from eliot import log_message
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool, tool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from agents.java_test.tools import make_tools
from detection.organic import OrganicDetector
from openrouter_llm import chat_model, configured_model_name
from repository.repo import Repo
from smell.smell import Smell
from testing.surefire import parse_surefire_reports, run_maven_tests
from workflows.composite.models import CompositeWorkflowState
from workflows.composite.nodes.detect import filter_smells_to_elements, resolve_smell_file

MAX_REFACTOR_AGENT_TURNS = 12


def _selected_smell(state: CompositeWorkflowState) -> Smell | None:
    selected_id = state.get("selected_smell_id")
    if not selected_id:
        return None
    for smell in state.get("smells") or []:
        if smell.id == selected_id:
            return smell
    return None


class RefactorAgentState(TypedDict, total=False):
    """Messages exchanged by the Java refactoring agent and its tools."""

    messages: Annotated[list[BaseMessage], add_messages]
    java_changes_made: bool
    agent_turns: int


def _build_refactor_tools(repo: Repo, elements: list[str], timeout: int) -> list[BaseTool]:
    @tool
    def run_tests() -> str:
        """Run the complete Maven test suite and return its result."""
        summary = run_maven_tests(repo, clean=False, jacoco=False, timeout=float(timeout))
        return (
            f"success={summary.success} exit_code={summary.exit_code}\n"
            f"stdout:\n{summary.stdout[-4000:]}\n"
            f"stderr:\n{summary.stderr[-4000:]}"
        )

    @tool
    def read_test_reports() -> str:
        """Read Maven Surefire reports from the most recent test run."""
        reports = parse_surefire_reports(repo.path)
        if not reports:
            return "No Maven Surefire reports found."
        return "\n".join(
            f"{report.name}: {report.status} {report.error_message or ''}".strip()
            for report in reports
        )

    @tool
    def detect_remaining_smells() -> str:
        """Run ORGANIC and return remaining smells for the requested elements."""
        smells = OrganicDetector().detect(repo.path)
        filtered = filter_smells_to_elements(smells, elements)
        if not filtered:
            return "No remaining target smells."
        return "\n".join(
            f"{item.type}: {item.file_path}:{item.location.range.start.line + 1}"
            for item in filtered
        )

    return [*make_tools(repo), run_tests, read_test_reports, detect_remaining_smells]


def _run_refactor_agent(
    repo: Repo,
    smell: Smell,
    *,
    elements: list[str],
    model_name: str,
    timeout: int,
) -> list[str]:
    tools = _build_refactor_tools(repo, elements, timeout)
    model = chat_model(model_name).bind_tools(tools)
    system = SystemMessage(
        content=(
            "You are a Java refactoring agent. Remove the reported code smell with a "
            "minimal semantic refactoring. First inspect the target and use "
            "find_symbol_references to understand all affected types. Read every file "
            "you need before editing. You may edit, move, or delete multiple Java files. "
            "Do not make cosmetic-only changes to formatting, comments, or license headers. "
            "After edits, run_tests and read_test_reports. Then call "
            "detect_remaining_smells. Do not finish until tests pass and the reported "
            "smell is gone, or tools show that no safe refactoring is possible."
        )
    )
    task = HumanMessage(
        content=(
            f"Remove this smell:\n"
            f"type={smell.type}\n"
            f"severity={smell.severity}\n"
            f"location={smell.file_path}:{smell.location.range.start.line + 1}\n"
            f"target symbol={Path(smell.file_path).stem}\n"
        )
    )
    before = {path.as_posix() for path in repo.filter_java_edits(include_untracked=True)}

    def agent(state: RefactorAgentState) -> RefactorAgentState:
        response = model.invoke([system, *state["messages"]])
        turns = int(state.get("agent_turns") or 0) + 1
        if isinstance(response, AIMessage):
            tool_names = [
                str(call.get("name"))
                for call in response.tool_calls
                if isinstance(call, dict) and isinstance(call.get("name"), str)
            ]
            log_message(
                message_type="refactor:agent_response",
                tool_calls=tool_names,
                content=str(response.content)[-1000:],
            )
        return {"messages": [response], "agent_turns": turns}

    def route_after_agent(state: RefactorAgentState) -> Literal["tools", "end"]:
        turns = int(state.get("agent_turns") or 0)
        if turns >= MAX_REFACTOR_AGENT_TURNS:
            log_message(
                message_type="refactor:agent_turn_limit",
                limit=MAX_REFACTOR_AGENT_TURNS,
            )
            return "end"
        messages = state.get("messages") or []
        if messages and isinstance(messages[-1], AIMessage) and messages[-1].tool_calls:
            return "tools"
        return "end"

    tool_node = ToolNode(tools)

    def run_tools(state: RefactorAgentState) -> RefactorAgentState:
        result = tool_node.invoke(state)
        messages = result.get("messages") if isinstance(result, dict) else None
        tool_messages: list[ToolMessage] = []
        if isinstance(messages, list):
            tool_messages = [
                message for message in messages if isinstance(message, ToolMessage)
            ]
        for message in tool_messages:
            log_message(
                message_type="refactor:tool_result",
                tool_name=message.name,
                content=str(message.content)[-2000:],
            )
        after = {path.as_posix() for path in repo.filter_java_edits(include_untracked=True)}
        return {
            "messages": tool_messages,
            "java_changes_made": after != before,
        }

    def route_after_tools(state: RefactorAgentState) -> Literal["agent", "end"]:
        messages = state.get("messages") or []
        latest_tool_names: list[str] = []
        for message in reversed(messages):
            if not isinstance(message, ToolMessage):
                break
            latest_tool_names.append(message.name)
        if "detect_remaining_smells" in latest_tool_names and state.get("java_changes_made"):
            log_message(
                message_type="refactor:agent_stopped_after_smell_detection",
                tools=latest_tool_names,
            )
            return "end"
        return "agent"

    graph = StateGraph(RefactorAgentState)
    graph.add_node("agent", agent)
    graph.add_node("tools", run_tools)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges(
        "agent",
        route_after_agent,
        {"tools": "tools", "end": END},
    )
    graph.add_conditional_edges(
        "tools",
        route_after_tools,
        {"agent": "agent", "end": END},
    )
    compiled = graph.compile()

    compiled.invoke(
        {"messages": [task]},
        config={"recursion_limit": 32},
    )
    after = {path.as_posix() for path in repo.filter_java_edits(include_untracked=True)}
    return sorted(after - before)


def refactor_smell(state: CompositeWorkflowState) -> CompositeWorkflowState:
    """Ask the LLM for a structured Java rewrite of the selected smell."""
    repo_path_raw = state.get("repo_path")
    if not repo_path_raw:
        raise ValueError("refactor_smell requires state['repo_path']")
    smell = _selected_smell(state)
    if smell is None:
        return {"refactor_ok": False, "stop_reason": state.get("stop_reason") or "no_smells"}

    repo_path = Path(repo_path_raw)
    target = resolve_smell_file(repo_path, smell)
    if target is None or not target.is_file():
        log_message(message_type="refactor:smell_file_missing", smell_id=smell.id)
        retries = int(state.get("retries_used") or 0)
        return {"refactor_ok": False, "retries_used": retries + 1}

    model_name = state.get("model") or configured_model_name()
    try:
        repo = Repo(repo_path)
        written = _run_refactor_agent(
            repo,
            smell,
            elements=list(state.get("elements") or []),
            model_name=model_name,
            timeout=int(state.get("timeout") or 120),
        )
        if not written:
            raise RuntimeError("Refactor agent completed without Java source changes")
    except Exception as exc:  # noqa: BLE001 - record failure in state
        log_message(message_type="refactor:failed", exception=str(exc))
        retries = int(state.get("retries_used") or 0)
        return {"refactor_ok": False, "retries_used": retries + 1}

    log_message(message_type="refactor:wrote", files=written)
    retries = int(state.get("retries_used") or 0)
    return {"refactor_ok": True, "retries_used": retries + 1}


def validate_refactor(state: CompositeWorkflowState) -> CompositeWorkflowState:
    """Rollback the worktree when the refactor node failed."""
    if state.get("refactor_ok", False):
        return {}
    repo_path = state.get("repo_path")
    if not repo_path:
        return {"tests_failed": True}
    try:
        repo = Repo(repo_path)
        repo.git_repo.git.reset("--hard")
        repo.git_repo.git.clean("-fd")
    except Exception as exc:  # noqa: BLE001
        log_message(message_type="refactor:rollback_failed", exception=str(exc))
    return {"tests_failed": True}


def replan_after_action(state: CompositeWorkflowState) -> CompositeWorkflowState:
    """Re-detect smells and advance the step counter after a verified action."""
    from workflows.composite.nodes.detect import detect_smells

    if state.get("tests_failed") and not state.get("refactor_ok", False):
        step = int(state.get("step") or 0)
        return {
            "step": step + 1,
            "no_progress": int(state.get("no_progress") or 0) + 1,
            "stop_reason": state.get("stop_reason") or "refactor_failed",
        }

    before = len(state.get("smells") or [])
    detected = detect_smells(state)
    smells = list(detected.get("smells") or [])
    after = len(smells)
    step = int(state.get("step") or 0) + 1
    no_progress = int(state.get("no_progress") or 0)
    if after >= before:
        no_progress += 1
    else:
        no_progress = 0

    stop_reason: str | None = None
    max_steps = int(state.get("max_steps") or 5)
    if after == 0:
        stop_reason = "smells_cleared"
    elif step >= max_steps:
        stop_reason = "max_steps"
    elif no_progress >= 2:
        stop_reason = "no_progress"

    return {
        "smells": smells,
        "step": step,
        "no_progress": no_progress,
        "selected_smell_id": None,
        "stop_reason": stop_reason,
        "tests_failed": False,
        "refactor_ok": False,
    }


def continue_after_replan(state: CompositeWorkflowState) -> str:
    """Loop back for another step or end the workflow."""
    if state.get("stop_reason"):
        return "end"
    if not state.get("smells"):
        return "end"
    step = int(state.get("step") or 0)
    max_steps = int(state.get("max_steps") or 5)
    if step >= max_steps:
        return "end"
    return "step_start"
