"""Select the next smell with native-order or dependency-aware planning."""

from __future__ import annotations

import heapq
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from eliot import log_message

from config import planning_config
from planning.ast.models import AstIndex
from planning.ast.relations import AstRelations
from planning.ast.runner import MavenRunner
from planning.graph import transitions
from planning.organic_rules import organic_rules
from planning.rules import PlanningSmell, Rule, SmellType, State
from repository.repo import Repo
from smell.smell import Smell


def smell_burden(state: State) -> int:
    """Return the severity-weighted smell burden of ``state``."""
    return sum(smell.severity for smell in state)


def best_first_smell_plan(
    start: State,
    rule_map: dict[SmellType, Rule],
    relations: AstRelations,
    max_expansions: int,
) -> list[PlanningSmell]:
    """Return the action path reaching the lowest smell burden with BeFS."""
    if max_expansions <= 0:
        raise ValueError("max_expansions must be positive")

    empty_path: tuple[PlanningSmell, ...] = ()
    queue: list[
        tuple[
            int,
            int,
            tuple[PlanningSmell, ...],
            State,
        ]
    ] = [(smell_burden(start), 0, empty_path, start)]
    visited: set[State] = set()
    best_path = empty_path
    best_rank = (smell_burden(start), 0, empty_path)
    expanded = 0
    reached_empty_state = False

    while queue and expanded < max_expansions:
        _, _, path, state = heapq.heappop(queue)
        if state in visited:
            continue
        visited.add(state)
        expanded += 1

        rank = (smell_burden(state), len(path), path)
        if rank < best_rank:
            best_rank = rank
            best_path = path
        if not state:
            reached_empty_state = True
            break

        for transition in transitions(state, rule_map, relations):
            if transition.state in visited:
                continue
            next_path = (*path, transition.action)
            heapq.heappush(
                queue,
                (
                    smell_burden(transition.state),
                    len(next_path),
                    next_path,
                    transition.state,
                ),
            )

    if queue and not reached_empty_state:
        log_message(
            message_type="planner:search_exhausted",
            max_expansions=max_expansions,
            visited_states=len(visited),
            initial_smell_count=len(start),
            best_burden=best_rank[0],
        )
    return list(best_path)


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


@runtime_checkable
class PlannerProtocol(Protocol):
    """Select the next concrete smell to resolve."""

    def plan_next_smell(
        self,
        smells: Sequence[Smell],
        repo_path: Path,
    ) -> Smell | None:
        """Return the next smell, or ``None`` when no improving plan exists."""
        ...


@dataclass(frozen=True, slots=True)
class PriorityPlanner:
    """Best-first planner over direct AST smell dependencies."""

    rule_map: dict[SmellType, Rule] | None = None
    max_expansions: int = 10_000
    ast_index: AstIndex | None = None

    def plan_next_smell(
        self,
        smells: Sequence[Smell],
        repo_path: Path,
    ) -> Smell | None:
        if not smells:
            return None
        active_rules = (
            self.rule_map if self.rule_map is not None else organic_rules
        )
        index = (
            self.ast_index
            if self.ast_index is not None
            else MavenRunner(Repo(repo_path)).inspect()
        )
        path = best_first_smell_plan(
            state_from_smells(smells, index, repo_path),
            active_rules,
            AstRelations.from_index(index),
            self.max_expansions,
        )
        if not path:
            return None
        selected_id = path[0].id
        for smell in smells:
            if smell.id == selected_id:
                return smell
        raise RuntimeError(f"Planned smell is absent from detector state: {selected_id}")


@dataclass(frozen=True, slots=True)
class NoPriorityPlanner:
    """Use the detector's native order without dependency rules."""

    def plan_next_smell(
        self,
        smells: Sequence[Smell],
        repo_path: Path,
    ) -> Smell | None:
        return smells[0] if smells else None


def planner_from_config() -> PlannerProtocol:
    """Build the planner named by ``[planning].planner``."""
    name = str(planning_config()["planner"])
    if name == "priority":
        return PriorityPlanner()
    if name == "no_priority":
        return NoPriorityPlanner()
    raise ValueError(
        f"Unsupported planning.planner={name!r}; expected 'priority' or 'no_priority'"
    )


def pick_next_smell(
    smells: Sequence[Smell],
    repo_path: str | Path,
    *,
    planner: PlannerProtocol | None = None,
) -> Smell | None:
    """Select the next concrete smell with the configured planner."""
    active = planner if planner is not None else planner_from_config()
    return active.plan_next_smell(smells, Path(repo_path).expanduser().resolve())
