"""Composite planner node backed by ``planning/`` PlannerProtocol selection."""

from __future__ import annotations

from eliot import log_message

from planning.planner import pick_next_smell
from workflows.composite.models import CompositeWorkflowState


def planner(state: CompositeWorkflowState) -> CompositeWorkflowState:
    """Select the next smell via planning rules; severity is not consulted."""
    smells = list(state.get("smells") or [])
    repo_path = state.get("repo_path")
    if not repo_path:
        raise ValueError("planner requires state['repo_path']")
    selected = pick_next_smell(smells, repo_path)
    if selected is None:
        log_message(message_type="planner:no_smells")
        return {"selected_smell_id": None, "stop_reason": "no_smells"}
    log_message(
        message_type="planner:selected",
        smell_id=selected.id[:12],
        smell_type=selected.type,
    )
    return {"selected_smell_id": selected.id, "stop_reason": None}


def action_selected(state: CompositeWorkflowState) -> CompositeWorkflowState:
    """Pass-through marker after planner selection."""
    return {}


def action_route(state: CompositeWorkflowState) -> str:
    """Skip refactor when the planner found nothing to fix."""
    if state.get("selected_smell_id"):
        return "refactor_smell"
    return "end"


def step_start(state: CompositeWorkflowState) -> CompositeWorkflowState:
    """Begin a refactoring step."""
    step = int(state.get("step") or 0)
    return {"step": step, "retries_used": 0, "tests_failed": False, "refactor_ok": False}
