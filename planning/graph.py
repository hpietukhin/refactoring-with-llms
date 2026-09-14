"""Expand planner states through dependency rules."""

from __future__ import annotations

from planning.ast.relations import AstRelations
from planning.rules import Rule, SmellType, State
from planning.transition import Transition


def transitions(
    state: State,
    rule_map: dict[SmellType, Rule],
    relations: AstRelations,
) -> tuple[Transition, ...]:
    """Return deterministic AST-scoped transitions from ``state``."""
    result: list[Transition] = []
    for action in state:
        if rule := rule_map.get(action.type):
            result.append(
                Transition(
                    action=action,
                    state=rule.apply(state, action, relations),
                )
            )
    return tuple(result)
