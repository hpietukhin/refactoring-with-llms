"""DeepAgents-backed Java smell refactoring with automatic verification."""

from __future__ import annotations

from eliot import log_message
from langchain.messages import HumanMessage
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.memory import InMemorySaver

from agents.java_test.analysis import run_java_test_analysis as run_java_test_analysis_impl
from agents.java_test.diagnostics import format_verification_feedback
from agents.deep.context_usage import ContextUsageMiddleware
from agents.deep.paths import virtual_path
from agents.deep.verification import VerificationMiddleware
from detection.organic import OrganicDetector
from openrouter_llm import chat_model
from repository.repo import Repo
from testing.surefire import run_maven_tests
from testing.test_selection import resolve_targeted_tests
from workflows.composite.nodes.detect import filter_smells_to_elements

GUIDELINES = """\
- Prefer a minimal edit that removes the reported smell.
- Read the reported line range before editing.
- Inspect automatic verification feedback after every source edit.
- Do not invent unrelated members or make cosmetic-only changes.
- Do not use network search; stay inside the local repository checkout.
"""

CONTEXT_WINDOW_TOKENS = 262_144


TOOL_WORKFLOW = """\
Use tools in this order:
1. Read the reported source range with `read_file`.
2. If more context is necessary, use `glob` to locate files or `grep` to locate
   text, then read only the relevant ranges.
3. Make one minimal source change with `edit_file` or `write_file`.
4. Inspect the automatic verification appended to that tool result before making
   another change.

After every successful source edit, middleware automatically runs Maven
verification and detects remaining smells. Do not call `run_tests`,
`run_java_test_analysis`, or `detect_remaining_smells` by default. Call one
only if the automatic feedback is missing or insufficient to diagnose a problem.

Do not call a tool that is absent from the tool schemas. In particular,
`run_shell` is not available, `execute` cannot run with this filesystem backend,
and this task must not use `task` delegation.
"""


def build_system_prompt(system_instructions: str) -> str:
    """Build the deep-agent system prompt for Java smell refactoring.

    Tool schemas provide individual tool descriptions; this prompt specifies their
    required workflow.
    """
    return (
        "You are an expert coding assistant operating inside deepagents, a coding "
        "agent harness. Your only objective is refactoring Java code to fix code "
        "smells. Automatic verification runs after each source edit. You must ensure "
        "that all tests pass and that every targeted code smell is fixed.\n\n"
        f"Guidelines:\n{GUIDELINES}\n"
        f"Tool workflow:\n{TOOL_WORKFLOW}\n"
        f"Profile instructions:\n{system_instructions}"
    )


def _build_validation_tools(
    repo: Repo,
    elements: list[str],
    timeout: int,
    *,
    model_name: str,
) -> list[BaseTool]:
    @tool
    def run_tests() -> str:
        """Run Maven tests relevant to changed Java files and return diagnostics."""
        changed = [
            path.as_posix()
            for path in repo.filter_java_edits(include_untracked=True)
        ]
        targeted = resolve_targeted_tests(repo.path, changed)
        test_args: tuple[str, ...] = ()
        if targeted:
            test_args = (f"-Dtest={','.join(targeted)}",)
            log_message(message_type="deep:targeted_tests", tests=targeted)
        summary = run_maven_tests(
            repo,
            clean=False,
            jacoco=False,
            timeout=float(timeout),
            test_args=test_args,
        )
        command = f"mvn test -Dtest={','.join(targeted)}" if targeted else "mvn test"
        return format_verification_feedback(summary, repo.path, command)

    @tool
    def run_java_test_analysis() -> str:
        """Run full Maven verification (detect build, run tests; no nested repair)."""
        result = run_java_test_analysis_impl(
            str(repo.path),
            clean=False,
            timeout=timeout,
            code_agent_model=model_name,
            code_agent_max_attempts=0,
        )
        summary = result.get("summary")
        error = result.get("error")
        if error:
            return f"run_java_test_analysis failed: {error}"
        success = getattr(summary, "success", None)
        exit_code = getattr(summary, "exit_code", None)
        return (
            f"run_java_test_analysis success={success} exit_code={exit_code} "
            f"command={result.get('command')}"
        )

    @tool
    def detect_remaining_smells() -> str:
        """Run ORGANIC and return remaining smells for the requested elements."""
        smells = OrganicDetector().detect(repo.path)
        filtered = filter_smells_to_elements(smells, elements)
        if not filtered:
            return "No remaining target smells."
        return "\n".join(
            f"{item.type}: {virtual_path(repo.path, item.file_path)}:"
            f"{item.location.range.start.line + 1}"
            for item in filtered
        )

    return [run_tests, run_java_test_analysis, detect_remaining_smells]


def build_deep_agent(
    repo: Repo,
    *,
    elements: list[str],
    model_name: str,
    timeout: int,
    system_instructions: str,
    max_completion_tokens: int,
    max_model_calls: int = 20,
) -> object:
    """Create a deep agent with deterministic verification after source edits."""
    verification = VerificationMiddleware(repo, elements=elements, timeout=timeout)
    return _create_deep_agent(
        repo,
        elements=elements,
        model_name=model_name,
        timeout=timeout,
        max_model_calls=max_model_calls,
        max_completion_tokens=max_completion_tokens,
        system_instructions=system_instructions,
        verification=verification,
    )


def _create_deep_agent(
    repo: Repo,
    *,
    elements: list[str],
    model_name: str,
    timeout: int,
    max_model_calls: int,
    max_completion_tokens: int,
    system_instructions: str,
    verification: VerificationMiddleware,
) -> object:
    """Create a deep agent with a caller-owned verification middleware."""
    from deepagents import create_deep_agent
    from deepagents.backends import FilesystemBackend

    validation_tools = _build_validation_tools(
        repo,
        elements,
        timeout,
        model_name=model_name,
    )
    model = chat_model(model_name, max_completion_tokens=max_completion_tokens)
    context_usage = ContextUsageMiddleware(
        model,
        context_window_tokens=CONTEXT_WINDOW_TOKENS,
    )
    return create_deep_agent(
        model=model,
        tools=validation_tools,
        system_prompt=build_system_prompt(system_instructions),
        middleware=[
            context_usage,
            verification,
            ModelCallLimitMiddleware(run_limit=max_model_calls, exit_behavior="end"),
            ToolCallLimitMiddleware(run_limit=max_model_calls * 3, exit_behavior="end"),
        ],
        backend=FilesystemBackend(root_dir=str(repo.path)),
        checkpointer=InMemorySaver(),
    )


def invoke_deep_agent(agent: object, *, case_id: str, task: str) -> object:
    """Invoke the deep agent and stream its model and tool events."""
    inputs = {"messages": [HumanMessage(content=task)]}
    config = {"configurable": {"thread_id": case_id, "case_id": case_id}}
    stream = agent.stream(  # type: ignore[attr-defined]
        inputs,
        config=config,
        stream_mode=("messages", "updates"),
    )
    for event in stream:
        log_message(message_type="deep:stream", event=str(event)[-2000:])
        if isinstance(event, tuple) and len(event) == 2 and event[0] == "updates":
            print(f"[deep] {event[1]}", flush=True)
        elif isinstance(event, dict):
            print(f"[deep] {event}", flush=True)
    state = agent.get_state(config)  # type: ignore[attr-defined]
    return state.values


__all__ = [
    "CONTEXT_WINDOW_TOKENS",
    "GUIDELINES",
    "TOOL_WORKFLOW",
    "build_deep_agent",
    "build_system_prompt",
    "invoke_deep_agent",
]
