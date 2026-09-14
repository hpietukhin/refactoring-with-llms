"""Positive-only (PZ) side-effect scores for smell ordering."""

from __future__ import annotations

from planning.ast.relations import AstRelations
from planning.rules import PlanningSmell, Rule, SmellType, State


def positive_effect_score(
    action: PlanningSmell,
    state: State,
    rule_map: dict[SmellType, Rule],
    relations: AstRelations,
) -> int:
    """Count related smells that fixing ``action`` is predicted to clear.

    Negative dependencies (NZ) are ignored: their contribution is treated as 0.
    """
    rule = rule_map.get(action.type)
    if rule is None:
        return 0
    return sum(
        1
        for other in state
        if other != action
        and relations.related(other.element_id, action.element_id)
        and other.type in rule.positive
    )
