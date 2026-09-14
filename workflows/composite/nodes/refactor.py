"""LLM refactor and validate nodes for the composite workflow."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal, TypedDict

from eliot import log_message
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool, tool
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.managed import RemainingSteps
from langgraph.prebuilt import ToolNode

from agents.java_test.diagnostics import format_verification_feedback
from agents.java_test.tools import make_tools
from agents.java_test.file_management import ApplyPatchTool, CreatePatchTool, VerifyPatchTool
from detection.organic import OrganicDetector
from openrouter_llm import (
    OpenRouterAPIKeyError,
    OpenRouterRequestError,
    chat_model,
    configured_model_name,
    is_transient_openrouter_error,
)
from repository.repo import Repo
from smell.smell import Smell
from testing.surefire import TestRunSummary, gradle_test_command, parse_surefire_reports, run_gradle_tests
from testing.test_selection import resolve_targeted_tests
from agents.java_test.tool_logging import guarded_tool_call_logger, invoke_logged_tool
from workflows.composite.models import CompositeWorkflowState
from workflows.composite.nodes.detect import filter_smells_to_elements, resolve_smell_file

MAX_REFACTOR_AGENT_TURNS = 12
REFACTOR_RECURSION_LIMIT = 32
REMAINING_STEPS_STOP_THRESHOLD = 4


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
    patch_sequence_ok: bool
    patch_stage: str
    remaining_steps: RemainingSteps
    tests_passed: bool
    target_smell_remaining: bool


def refactor_completion_feedback(state: RefactorAgentState) -> str | None:
    """Return missing verification evidence for an attempted refactor."""
    if not state.get("java_changes_made"):
        return None
    if state.get("patch_stage") != "verified":
        return "The source edit has not passed patch verification. Fix or verify the patch."
    if not state.get("tests_passed"):
        return (
            "Maven verification has not passed. Use the structured compiler or test "
            "diagnostics, inspect the cited source context, and repair the edit."
        )
    if state.get("target_smell_remaining", True):
        return (
            "Tests pass, but the reported smell is still present or has not been "
            "rechecked. Run detect_remaining_smells and continue the refactoring."
        )
    return None


def refactor_task_for_smell(smell: Smell) -> str:
    """Build the refactor task from the complete detected-smell context."""
    location = smell.location
    source_range = location.range
    return (
        "Remove this smell:\n"
        f"type={smell.type}\n"
        f"severity={smell.severity}\n"
        f"file_path={smell.file_path}\n"
        f"location_uri={location.uri}\n"
        f"start_line={source_range.start.line + 1}\n"
        f"start_character={source_range.start.character}\n"
        f"end_line={source_range.end.line + 1}\n"
        f"end_character={source_range.end.character}\n"
        f"detected_by={smell.detected_by}\n"
        f"commit_hash={smell.commit_hash}\n"
    )


def _build_refactor_tools(repo: Repo, elements: list[str], timeout: int) -> list[BaseTool]:
    last_test_summary: TestRunSummary | None = None

    @tool
    def run_tests() -> str:
        """Run Maven tests relevant to changed files and return the result."""
        nonlocal last_test_summary
        changed = [
            path.as_posix()
            for path in repo.filter_java_edits(include_untracked=True)
        ]
        targeted = resolve_targeted_tests(repo.path, changed)
        test_args: tuple[str, ...] = ()
        if targeted:
            test_args = (f"-Dtest={','.join(targeted)}",)
            log_message(
                message_type="refactor:targeted_tests",
                tests=targeted,
            )
        last_test_summary = run_gradle_tests(
            repo, clean=False, jacoco=False, timeout=float(timeout), test_args=test_args,
        )
        command = gradle_test_command(extra=test_args)
        return format_verification_feedback(
            last_test_summary,
            repo.path,
            command,
        )

    @tool
    def read_test_reports() -> str:
        """Read Maven Surefire reports from the most recent test run."""
        if last_test_summary is not None and not last_test_summary.success:
            return (
                "The latest Maven run failed. Existing Surefire XML files may be stale. "
                "Use the structured diagnostics from run_tests as authoritative evidence."
            )
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
    create_patch_tool = next(tool for tool in tools if isinstance(tool, CreatePatchTool))
    apply_patch_tool = next(tool for tool in tools if isinstance(tool, ApplyPatchTool))
    verify_patch_tool = next(tool for tool in tools if isinstance(tool, VerifyPatchTool))
    model_tools = [
        tool
        for tool in tools
        if not isinstance(tool, (ApplyPatchTool, VerifyPatchTool))
    ]
    model = chat_model(model_name).bind_tools(model_tools)
    system = SystemMessage(
        content=(
            "You are a Java refactoring agent. Remove the reported code smell with a "
            "minimal semantic refactoring. First inspect the reported line range and use "
            "find_symbol_references to understand all affected types. Read every file "
            "you need before editing. Use read_file with the reported line range first, "
            "then expand the range when needed. Use search_text to find assignments and "
            "usages. Do not repeat identical tool calls. You may edit, move, or delete "
            "multiple Java files. "
            "Do not make cosmetic-only changes to formatting, comments, or license headers. "
            "For every source edit, execute create_patch, then apply_patch, then "
            "verify_patch in that exact order. Each stage must succeed before the "
            "next stage. After verification, run_tests and read_test_reports. Then call "
            "detect_remaining_smells. Do not finish until tests pass and the reported "
            "smell is gone, or tools show that no safe refactoring is possible."
        )
    )
    task = HumanMessage(content=refactor_task_for_smell(smell))
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

    def route_after_agent(
        state: RefactorAgentState,
    ) -> Literal["tools", "evaluate_completion", "stop"]:
        remaining_steps = state.get("remaining_steps")
        if (
            remaining_steps is not None
            and remaining_steps <= REMAINING_STEPS_STOP_THRESHOLD
        ):
            return "stop"
        turns = int(state.get("agent_turns") or 0)
        if turns >= MAX_REFACTOR_AGENT_TURNS:
            log_message(
                message_type="refactor:agent_turn_limit",
                limit=MAX_REFACTOR_AGENT_TURNS,
            )
            return "stop"
        messages = state.get("messages") or []
        if messages and isinstance(messages[-1], AIMessage) and messages[-1].tool_calls:
            return "tools"
        if refactor_completion_feedback(state) is not None:
            return "evaluate_completion"
        return "stop"

    tool_node = ToolNode(tools, wrap_tool_call=guarded_tool_call_logger())

    def run_tools(state: RefactorAgentState) -> RefactorAgentState:
        result = tool_node.invoke(state)
        messages = result.get("messages") if isinstance(result, dict) else None
        tool_messages: list[ToolMessage] = []
        if isinstance(messages, list):
            tool_messages = [
                message for message in messages if isinstance(message, ToolMessage)
            ]
        after = {path.as_posix() for path in repo.filter_java_edits(include_untracked=True)}
        updates: RefactorAgentState = {
            "messages": tool_messages,
            "java_changes_made": after != before,
        }
        target_location = (
            f"{smell.type}: {smell.file_path}:"
            f"{smell.location.range.start.line + 1}"
        )
        for message in tool_messages:
            log_message(
                message_type="refactor:tool_result",
                tool_name=message.name,
                content=str(message.content)[-2000:],
            )
            if message.name == "run_tests":
                updates["tests_passed"] = "success=True" in str(message.content)
            if message.name == "detect_remaining_smells":
                updates["target_smell_remaining"] = target_location in str(message.content)
        return updates

    def route_after_tools(
        state: RefactorAgentState,
    ) -> Literal["agent", "patch_sequence", "stop"]:
        messages = state.get("messages") or []
        latest_tool_names: list[str] = []
        for message in reversed(messages):
            if not isinstance(message, ToolMessage):
                break
            latest_tool_names.append(message.name)
        if (
            "detect_remaining_smells" in latest_tool_names
            and state.get("java_changes_made")
            and state.get("tests_passed")
            and not state.get("target_smell_remaining", True)
        ):
            log_message(
                message_type="refactor:agent_stopped_after_smell_detection",
                tools=latest_tool_names,
            )
            return "stop"
        if "create_patch" in latest_tool_names:
            return "patch_sequence"
        if state.get("patch_stage") == "verified":
            return "agent"
        return "agent"

    def apply_patch_stage(state: RefactorAgentState) -> RefactorAgentState:
        patch_path = create_patch_tool.sequence.pending_patch()
        if patch_path is None:
            return {
                "messages": [HumanMessage(content="Patch sequence failed: no pending patch.")],
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

    def verify_patch_stage(state: RefactorAgentState) -> RefactorAgentState:
        patch_path = create_patch_tool.sequence.latest_patch()
        if patch_path is None:
            return {
                "messages": [HumanMessage(content="Patch sequence failed: no created patch.")],
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

    def route_after_apply(
        state: RefactorAgentState,
    ) -> Literal["verify_patch", "agent", "stop"]:
        return "verify_patch" if state.get("patch_sequence_ok") else "agent"

    def evaluate_completion(state: RefactorAgentState) -> RefactorAgentState:
        feedback = refactor_completion_feedback(state)
        if feedback is None:
            return {}
        return {"messages": [HumanMessage(content=feedback)]}

    def stop(state: RefactorAgentState) -> RefactorAgentState:
        log_message(
            message_type="refactor:agent_stopped",
            reason="progress_or_remaining_steps",
            patch_stage=state.get("patch_stage"),
        )
        return {}

    graph = StateGraph(RefactorAgentState)
    graph.add_node("agent", agent)
    graph.add_node("tools", run_tools)
    graph.add_node("apply_patch", apply_patch_stage)
    graph.add_node("verify_patch", verify_patch_stage)
    graph.add_node("evaluate_completion", evaluate_completion)
    graph.add_node("stop", stop)
    graph.add_edge("stop", END)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges(
        "agent",
        route_after_agent,
        {
            "tools": "tools",
            "evaluate_completion": "evaluate_completion",
            "stop": "stop",
        },
    )
    graph.add_edge("evaluate_completion", "agent")
    graph.add_conditional_edges(
        "tools",
        route_after_tools,
        {"agent": "agent", "patch_sequence": "apply_patch", "stop": "stop"},
    )
    graph.add_conditional_edges(
        "apply_patch",
        route_after_apply,
        {"verify_patch": "verify_patch", "agent": "agent", "stop": "stop"},
    )
    graph.add_edge("verify_patch", "agent")
    compiled = graph.compile()

    try:
        compiled.invoke(
            {"messages": [task]},
            config={"recursion_limit": REFACTOR_RECURSION_LIMIT},
        )
    except GraphRecursionError as exc:
        log_message(message_type="refactor:recursion_limit", exception=str(exc))
        return []
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
        before = {
            path.as_posix()
            for path in repo.filter_java_edits(include_untracked=True)
        }
        if state.get("use_pydantic"):
            from agents.pydantic_deep.invoke import invoke_pydantic_deep_agent

            invoke_pydantic_deep_agent(
                repo,
                smell=smell,
                elements=list(state.get("elements") or []),
                model_name=model_name,
                timeout=int(state.get("timeout") or 120),
                case_id=str(state.get("case_id") or smell.id),
            )
            after = {
                path.as_posix()
                for path in repo.filter_java_edits(include_untracked=True)
            }
            written = sorted(after - before)
        else:
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
        if isinstance(exc, OpenRouterAPIKeyError):
            raise
        if is_transient_openrouter_error(exc):
            raise OpenRouterRequestError(str(exc)) from exc
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

    detected = detect_smells(state)
    smells = list(detected.get("smells") or [])
    after = len(smells)
    step = int(state.get("step") or 0) + 1
    no_progress = int(state.get("no_progress") or 0)
    if state.get("refactor_ok", False):
        no_progress = 0
    else:
        no_progress += 1

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
    remaining_steps = state.get("remaining_steps")
    if remaining_steps is not None and remaining_steps <= REMAINING_STEPS_STOP_THRESHOLD:
        return "end"
    if state.get("stop_reason"):
        return "end"
    if not state.get("smells"):
        return "end"
    step = int(state.get("step") or 0)
    max_steps = int(state.get("max_steps") or 5)
    if step >= max_steps:
        return "end"
    return "step_start"
