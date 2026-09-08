"""Pydantic AI Java smell refactoring agent."""

from __future__ import annotations

import asyncio

from eliot import log_message
from pydantic_ai import Agent, RunContext, ToolDefinition
from pydantic_ai.capabilities import ValidatedToolArgs
from pydantic_ai.capabilities.hooks import Hooks
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai_harness import CodeMode, FileSystem, Shell

from agents.java_test.analysis import run_java_test_analysis as run_java_test_analysis_impl
from agents.java_test.references import (
    find_symbol_references as find_symbol_references_impl,
)
from detection.organic import OrganicDetector
from java.metrics.ck_reports import run_ck_metrics as run_ck_metrics_impl
from openrouter_llm import (
    CEREBRAS_API_BASE,
    OPENROUTER_API_BASE,
    cerebras_api_key,
    openrouter_api_key,
)
from planning.ast.tools import find_ast_references as find_ast_references_impl
from repository.repo import Repo
from workflows.composite.nodes.detect import filter_smells_to_elements
from workflows.composite.nodes.verify import java_verification as java_verification_impl

GUIDELINES = """\
- Prefer a minimal semantic edit that removes the reported smell.
- Read the reported line range before editing.
- Every successful edit_file call automatically runs Java verification and
  ORGANIC detection.
- Do not invent unrelated members or make cosmetic-only changes.
- Do not use network search; stay inside the local repository checkout.
"""


def build_system_prompt() -> str:
    """Build the Pydantic AI system prompt for Java smell refactoring."""
    return (
        "You are an expert coding assistant operating inside a Pydantic AI coding "
        "agent harness. Your only objective is refactoring Java code to fix code "
        "smells. You will receive a rubric that defines the success criteria. Use "
        "the tools according to their execution boundary. Do not finish until every "
        "rubric criterion is satisfied or the tools show no safe refactoring is "
        "possible.\n\n"
        "After every successful edit_file call, the harness automatically runs "
        "java_verification and detects remaining smells. Its result will be "
        "provided before your next model request. Inspect that feedback and fix "
        "failures before editing again.\n\n"
        "Direct tools: read_file, edit_file, write_file, search_files, find_files, "
        "list_directory, and run_command.\n\n"
        f"Guidelines:\n{GUIDELINES}"
    )


def _build_model(model_name: str) -> Model:
    """Create an OpenAI-compatible Pydantic AI model for a configured provider."""
    name = model_name.removeprefix("openrouter/")
    if name.startswith("ollama/"):
        return OpenAIChatModel(
            name.removeprefix("ollama/"),
            provider=OpenAIProvider(
                base_url="http://localhost:11434/v1",
                api_key="ollama",
            ),
        )
    if name.startswith("cerebras/"):
        return OpenAIChatModel(
            name.removeprefix("cerebras/"),
            provider=OpenAIProvider(
                base_url=CEREBRAS_API_BASE,
                api_key=cerebras_api_key(),
            ),
        )
    return OpenAIChatModel(
        name,
        provider=OpenAIProvider(
            base_url=OPENROUTER_API_BASE,
            api_key=openrouter_api_key(),
        ),
    )


def _build_repository_tools(repo: Repo) -> FunctionToolset[None]:
    def find_symbol_references(symbol: str, dir_path: str = ".") -> str:
        """Find Java source references to an exact symbol."""
        return find_symbol_references_impl(str(repo.path), symbol, dir_path)

    def find_ast_references(symbol: str) -> str:
        """Inspect Spoon AST references and containment relations for a Java symbol."""
        return find_ast_references_impl(str(repo.path), symbol)

    return FunctionToolset(
        tools=[find_symbol_references, find_ast_references],
    ).with_metadata(code_mode=True)


def _build_validation_tools(
    repo: Repo,
    elements: list[str],
    timeout: int,
    model_name: str,
) -> FunctionToolset[None]:
    def run_java_test_analysis() -> dict[str, object]:
        """Detect Maven and run the full test suite without LLM repair."""
        return run_java_test_analysis_impl(
            str(repo.path),
            clean=False,
            timeout=timeout,
            code_agent_model=model_name,
            code_agent_max_attempts=0,
        )

    def java_verification() -> dict[str, object]:
        """Run Maven tests and collect CK metrics when tests pass."""
        return java_verification_impl(
            {
                "repo_path": str(repo.path),
                "timeout": timeout,
                "model": model_name,
            }
        )

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

    def run_ck_metrics() -> dict[str, float | int | bool | None]:
        """Collect CK metrics for the current checkout."""
        summary = run_ck_metrics_impl(repo, timeout=float(timeout))
        totals = summary.totals
        return {
            "success": summary.success,
            "exit_code": summary.exit_code,
            "classes": totals.classes,
            "mean_cbo": totals.mean_cbo,
            "mean_lcom": totals.mean_lcom,
            "mean_wmc": totals.mean_wmc,
            "total_loc": totals.total_loc,
        }

    return FunctionToolset(
        tools=[
            run_java_test_analysis,
            java_verification,
            detect_remaining_smells,
            run_ck_metrics,
        ],
    ).with_metadata(code_mode=True)


def _build_verification_hooks(
    repo: Repo,
    elements: list[str],
    timeout: int,
    model_name: str,
) -> Hooks:
    """Create hooks that verify every successful filesystem edit."""
    hooks = Hooks()

    @hooks.on.after_tool_execute(tools=["edit_file"])
    async def verify_after_edit(
        ctx: RunContext[None],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: ValidatedToolArgs,
        result: object,
    ) -> object:
        del call, tool_def, args
        verification = await asyncio.to_thread(
            java_verification_impl,
            {
                "repo_path": str(repo.path),
                "timeout": timeout,
                "model": model_name,
            },
        )
        smells = await asyncio.to_thread(OrganicDetector().detect, repo.path)
        remaining = filter_smells_to_elements(smells, elements)
        smell_report = (
            "\n".join(
                f"{item.type}: {item.file_path}:"
                f"{item.location.range.start.line + 1}"
                for item in remaining
            )
            if remaining
            else "No remaining target smells."
        )
        tests_failed = verification.get("tests_failed", True)
        log_message(
            message_type="pydantic_deep:automatic_verification",
            tests_failed=tests_failed,
            remaining_smells=len(remaining),
        )
        ctx.enqueue(
            "Automatic verification after edit_file:\n"
            f"tests_failed={tests_failed}\n"
            f"remaining_smells:\n{smell_report}"
        )
        return result

    return hooks


def build_pydantic_deep_agent(
    repo: Repo,
    *,
    elements: list[str],
    model_name: str,
    timeout: int,
) -> Agent[None, str]:
    """Create the isolated Pydantic AI refactoring agent."""
    repository_tools = _build_repository_tools(repo)
    validation_tools = _build_validation_tools(repo, elements, timeout, model_name)
    return Agent(
        _build_model(model_name),
        name="pydantic_java_smell_refactor",
        instructions=build_system_prompt(),
        retries=5,
        toolsets=[repository_tools, validation_tools],
        capabilities=[
            _build_verification_hooks(repo, elements, timeout, model_name),
            FileSystem(repo.path),
            Shell(
                repo.path,
                allowed_commands=("git", "mvn"),
                default_timeout=float(timeout),
            ),
            CodeMode(tools={"code_mode": True}),
        ],
    )


def log_evaluation_metrics(repo: Repo, timeout: int) -> None:
    """Collect and log CK metrics after a completed agent evaluation."""
    try:
        summary = run_ck_metrics_impl(repo, timeout=float(timeout))
    except (OSError, RuntimeError) as exc:
        log_message(message_type="pydantic_deep:ck_failed", exception=str(exc))
        return
    totals = summary.totals
    log_message(
        message_type="pydantic_deep:ck",
        success=summary.success,
        exit_code=summary.exit_code,
        classes=totals.classes,
        mean_cbo=totals.mean_cbo,
        mean_lcom=totals.mean_lcom,
        mean_wmc=totals.mean_wmc,
        total_loc=totals.total_loc,
    )


__all__ = [
    "GUIDELINES",
    "build_pydantic_deep_agent",
    "build_system_prompt",
    "log_evaluation_metrics",
]
