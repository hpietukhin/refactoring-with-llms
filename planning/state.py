"""Project detector smells onto planner state."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from planning.ast.models import AstIndex
from planning.rules import PlanningSmell, State
from smell.smell import Smell


def state_from_smells(
    smells: Sequence[Smell],
    ast_index: AstIndex,
    repo_path: Path,
) -> State:
    """Project detector findings onto a canonical instance-level state."""
    state: list[PlanningSmell] = []
    for smell in smells:
        line = smell.location.range.start.line + 1
        element = ast_index.element_at(
            smell.file_path,
            line,
            repo_path,
        )
        state.append(
            PlanningSmell(
                element_id=(
                    element.id
                    if element is not None
                    else f"unresolved:{smell.id}"
                ),
                line=line,
                type=smell.type,
                id=smell.id,
                severity=smell.severity_score,
            )
        )
    return tuple(sorted(state))
