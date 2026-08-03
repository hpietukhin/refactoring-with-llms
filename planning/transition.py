"""Possible actions between planner states."""

from __future__ import annotations

from dataclasses import dataclass

from planning.rules import PlanningSmell, State


@dataclass(frozen=True, slots=True)
class Transition:
    """A state reached by resolving one smell instance."""

    action: PlanningSmell
    state: State
