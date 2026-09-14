"""Profile-driven invocation entrypoint for DeepAgents experiments."""

from __future__ import annotations

from dataclasses import dataclass

from eliot import log_message

from agents.deep.agent import build_deep_agent, build_system_prompt, invoke_deep_agent
from agents.deep.paths import virtual_path
from agents.deep.profiles import DeepAgentProfile
from detection.java_version import detect_project_java_version
from detection.organic import OrganicDetector
from java.organic_feedback import OrganicFeedback
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


def build_case_task(repo: Repo, smells: tuple[Smell, ...]) -> str:
    """Build a whole-case task with all detected smells."""
    smell_details = []
    for smell in smells:
        source_range = smell.location.range
        smell_details.append(
            "\n".join(
                (
                    f"type={smell.type}",
                    f"severity={smell.severity}",
                    f"file_path={virtual_path(repo.path, smell.file_path)}",
                    f"start_line={source_range.start.line + 1}",
                    f"start_character={source_range.start.character}",
                    f"end_line={source_range.end.line + 1}",
                    f"end_character={source_range.end.character}",
                    f"detected_by={smell.detected_by}",
                    f"advice_path={smell.advice.as_posix()}",
                )
            )
        )
    return (
        "Refactor all of these code smells:\n\n"
        f"{'\n\n'.join(smell_details)}\n\n"
        "Create a todo for every listed smell. Process todos one at a time. Read the "
        "reported source range, make the minimal semantic edit, and inspect automatic "
        "verification before continuing. Do not delegate work. Finish only after every "
        "todo is completed or no safe refactoring is possible."
    )


def remaining_target_smells(repo: Repo, elements: list[str]) -> tuple[Smell, ...]:
    """Return current ORGANIC findings that are inside the case scope."""
    smells = OrganicDetector().detect(repo.path)
    return tuple(filter_smells_to_elements(smells, elements))


def remaining_target_feedback(
    repo: Repo,
    elements: list[str],
) -> tuple[OrganicFeedback, ...]:
    """Return filtered ORGANIC findings with agent-facing detector context."""
    feedback = OrganicDetector().detect_feedback(repo.path)
    filtered_smells = set(
        filter_smells_to_elements(
            [item.smell for item in feedback],
            elements,
        )
    )
    return tuple(item for item in feedback if item.smell in filtered_smells)


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
    java_source: str | None = None,
) -> DeepCaseResult:
    """Run one persistent DeepAgents session for all smells in a case."""
    log_message(
        message_type="deep:case_start",
        case_id=case_id,
        elements=elements,
        repo_path=str(repo.path),
        profile=profile.name,
    )
    initial_smells = remaining_target_smells(repo, elements)
    if not initial_smells:
        return DeepCaseResult("", (), "smells_cleared", 0)
    detected_version = detect_project_java_version(repo.path)
    source_level = java_source or (
        detected_version.source if detected_version is not None else None
    )
    task = build_case_task(repo, initial_smells)
    log_message(
        message_type="deep:prompt",
        case_id=case_id,
        profile=profile.name,
        system_prompt=build_system_prompt(
            profile.system_instructions,
            java_source=source_level,
        ),
        task_prompt=task,
    )
    agent = build_deep_agent(
        repo,
        elements=elements,
        model_name=model_name,
        timeout=timeout,
        system_instructions=profile.system_instructions,
        java_source=source_level,
        max_model_calls=profile.max_model_calls,
    )
    result = invoke_deep_agent(agent, case_id=case_id, task=task)
    content = _last_message_content(result)
    remaining_smells = remaining_target_smells(repo, elements)
    smells_fixed = max(0, len(initial_smells) - len(remaining_smells))
    stop_reason = "smells_cleared" if not remaining_smells else "agent_stopped"
    return DeepCaseResult(content, remaining_smells, stop_reason, smells_fixed)


__all__ = [
    "DeepCaseResult",
    "build_case_task",
    "invoke_deep_case_agent",
    "remaining_target_feedback",
    "remaining_target_smells",
]
