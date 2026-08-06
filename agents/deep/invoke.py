"""Profile-driven invocation entrypoint for DeepAgents experiments."""

from __future__ import annotations

from dataclasses import dataclass

from eliot import log_message

from agents.deep.agent import build_deep_agent, build_system_prompt, invoke_deep_agent
from agents.deep.paths import virtual_path
from agents.deep.profiles import DeepAgentProfile
from detection.organic import OrganicDetector
from planning.planner import pick_next_smell
from repository.repo import Repo
from smell.smell import Smell
from workflows.composite.nodes.detect import filter_smells_to_elements


@dataclass(frozen=True, slots=True)
class DeepCaseResult:
    """Outcome of one profile-driven DeepAgents experiment."""

    content: str
    remaining_smells: tuple[Smell, ...]
    stop_reason: str
    steps: int


def build_smell_task(repo: Repo, smell: Smell) -> str:
    """Build a one-smell DeepAgents task using virtual filesystem paths."""
    source_range = smell.location.range
    return (
        "Refactor exactly this selected code smell:\n"
        f"type={smell.type}\n"
        f"severity={smell.severity}\n"
        f"file_path={virtual_path(repo.path, smell.file_path)}\n"
        f"start_line={source_range.start.line + 1}\n"
        f"start_character={source_range.start.character}\n"
        f"end_line={source_range.end.line + 1}\n"
        f"end_character={source_range.end.character}\n"
        f"detected_by={smell.detected_by}\n\n"
        "Read the reported source range, make the minimal semantic edit needed for "
        "this smell, and inspect the automatic verification feedback. Do not plan or "
        "work on other smells. Do not delegate work. Finish after this selected smell "
        "is removed or no safe refactoring is possible."
    )


def _remaining_smells(repo: Repo, elements: list[str]) -> tuple[Smell, ...]:
    """Return current ORGANIC findings that are inside the case scope."""
    smells = OrganicDetector().detect(repo.path)
    return tuple(filter_smells_to_elements(smells, elements))


def _last_message_content(result: object) -> str:
    """Extract the final model message content from an agent state."""
    messages = result.get("messages") if isinstance(result, dict) else None
    if isinstance(messages, list) and messages:
        return str(getattr(messages[-1], "content", messages[-1]))
    return ""


def invoke_deep_case_agent(
    repo: Repo,
    *,
    elements: list[str],
    model_name: str,
    timeout: int,
    case_id: str,
    profile: DeepAgentProfile,
) -> DeepCaseResult:
    """Run the supported sequential DeepAgents profile for one case."""
    if profile.mode != "sequential":
        raise ValueError(f"DeepAgents profile {profile.name!r} is not implemented")
    log_message(
        message_type="deep:case_start",
        case_id=case_id,
        elements=elements,
        repo_path=str(repo.path),
        profile=profile.name,
    )
    content = ""
    for step in range(1, profile.max_smell_iterations + 1):
        smells = _remaining_smells(repo, elements)
        selected = pick_next_smell(smells, repo.path)
        if selected is None:
            stop_reason = "smells_cleared" if not smells else "planner_stopped"
            return DeepCaseResult(content, smells, stop_reason, step - 1)
        log_message(
            message_type="deep:smell_selected",
            case_id=case_id,
            profile=profile.name,
            step=step,
            smell=selected.to_dict(),
        )
        task = build_smell_task(repo, selected)
        log_message(
            message_type="deep:prompt",
            case_id=case_id,
            profile=profile.name,
            step=step,
            system_prompt=build_system_prompt(profile.system_instructions),
            task_prompt=task,
        )
        agent = build_deep_agent(
            repo,
            elements=elements,
            model_name=model_name,
            timeout=timeout,
            system_instructions=profile.system_instructions,
            max_completion_tokens=profile.max_completion_tokens,
            max_model_calls=profile.max_model_calls,
        )
        result = invoke_deep_agent(
            agent,
            case_id=f"{case_id}:step:{step}",
            task=task,
        )
        content = _last_message_content(result)
        log_message(
            message_type="deep:smell_done",
            case_id=case_id,
            profile=profile.name,
            step=step,
            smell_id=selected.id,
            content=content[-1000:],
        )
    smells = _remaining_smells(repo, elements)
    return DeepCaseResult(content, smells, "iteration_limit", profile.max_smell_iterations)


__all__ = ["DeepCaseResult", "build_smell_task", "invoke_deep_case_agent"]
