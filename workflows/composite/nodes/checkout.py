"""Repository checkout for the composite workflow."""

from __future__ import annotations

from eliot import log_message

from config import langgraph_model, settings
from repository.repo import Repo
from workflows.composite.models import CompositeWorkflowState


def _require_str(state: CompositeWorkflowState, key: str) -> str:
    value = state.get(key, "")
    if not isinstance(value, str) or not value:
        raise ValueError(f"repo_checkout requires state[{key!r}]")
    return value


def workflow_start(state: CompositeWorkflowState) -> CompositeWorkflowState:
    """Seed defaults from config when the caller omitted them."""
    lg = settings["langgraph"]
    defaults: CompositeWorkflowState = {
        "max_steps": int(lg.get("max_steps", 5)),
        "step": 0,
        "retries_used": 0,
        "max_retries": 1,
        "no_progress": 0,
        "step_logs": [],
        "timeout": int(lg.get("timeout_seconds", 120)),
        "model": langgraph_model(),
        "repo_path": str(settings["git"]["repo_path"]),
    }
    return {key: value for key, value in defaults.items() if key not in state}


def repo_checkout(state: CompositeWorkflowState) -> CompositeWorkflowState:
    """Clone the case repository when needed and move it to the start commit."""
    current_commit = _require_str(state, "current_commit")
    repo_url = _require_str(state, "repo_url")
    repo = Repo.for_experiment(repo_url, current_commit)
    log_message(
        message_type="repo_checkout:ready",
        commit=current_commit[:12],
        repo_path=str(repo.path),
    )
    return {
        "current_commit": current_commit,
        "previous_commit": state.get("previous_commit"),
        "repo_path": str(repo.path),
    }
