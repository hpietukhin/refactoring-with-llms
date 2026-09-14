"""State and step models for the composite LangGraph workflow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict

from langgraph.managed import RemainingSteps

from smell.smell import Smell


class CompositeWorkflowState(TypedDict, total=False):
    """State for the composite refactoring workflow graph."""

    project: str
    repo_url: str
    current_commit: str
    previous_commit: str | None
    repo_path: str
    elements: list[str]
    smells: list[Smell]
    selected_smell_id: str | None
    step: int
    max_steps: int
    max_retries: int
    retries_used: int
    tests_failed: bool
    refactor_ok: bool
    stop_reason: str | None
    step_logs: list[StepLog]
    no_progress: int
    remaining_steps: RemainingSteps
    model: str
    timeout: int
    case_id: str
    use_pydantic: bool
    ck_classes: int | None
    ck_mean_cbo: float | None
    ck_mean_lcom: float | None
    ck_mean_wmc: float | None
    ck_total_loc: float | None


@dataclass(slots=True)
class StepLog:
    """One executed refactoring step."""

    step: int
    smell_count_before: int
    smell_count_after: int
    action_smell_id: str
    compile_passed: bool
    tests_passed: bool
    execution_ok: bool
    stop_reason: str | None = None
