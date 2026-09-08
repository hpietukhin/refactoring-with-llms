"""Greedy next-smell selection by positive dependency count (NZ ignored)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from planning.ast.models import AstIndex
from planning.ast.relations import AstRelations
from planning.ast.runner import GradleRunner
from planning.organic_rules import organic_rules
from planning.state import state_from_smells
from planning.rules import Rule, SmellType
from planning.scoring import positive_effect_score
from repository.repo import Repo
from smell.smell import Smell


@dataclass(frozen=True, slots=True)
class GreedyPlanner:
    """Always pick the remaining smell with the highest PZ score.

    Score is the number of AST-related smells whose types appear in the
    rule's positive set. Negative effects are not counted (NZ = 0).
    """

    rule_map: dict[SmellType, Rule] | None = None
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
            else GradleRunner(Repo(repo_path)).inspect()
        )
        state = state_from_smells(smells, index, repo_path)
        relations = AstRelations.from_index(index)
        selected = min(
            state,
            key=lambda smell: (
                -positive_effect_score(
                    smell,
                    state,
                    active_rules,
                    relations,
                ),
                smell.id,
            ),
        )
        for smell in smells:
            if smell.id == selected.id:
                return smell
        raise RuntimeError(
            f"Planned smell is absent from detector state: {selected.id}"
        )
