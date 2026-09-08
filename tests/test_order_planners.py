"""Tests for greedy PZ and topological smell planners."""

from __future__ import annotations

from pathlib import Path

from planning.ast.models import AstElement, AstIndex
from planning.ast.relations import AstRelations
from planning.greedy import GreedyPlanner
from planning.rules import (
    FEATURE_ENVY,
    LAZY_CLASS,
    LONG_METHOD,
    SPECULATIVE_GENERALITY,
    rules,
)
from planning.scoring import positive_effect_score
from planning.state import state_from_smells
from planning.topo import TopoPlanner, topological_smell_order
from smell.smell import Smell

REPO_PATH = Path(".").resolve()
AST_INDEX = AstIndex(
    elements=[
        AstElement(
            id="a.A",
            kind="class",
            file="a/A.java",
            startLine=1,
            endLine=100,
        ),
        AstElement(
            id="a.A#work()",
            kind="method",
            file="a/A.java",
            startLine=1,
            endLine=20,
        ),
        AstElement(
            id="b.B",
            kind="class",
            file="b/B.java",
            startLine=1,
            endLine=100,
        ),
        AstElement(
            id="b.B#work()",
            kind="method",
            file="b/B.java",
            startLine=1,
            endLine=20,
        ),
    ],
    edges=[],
    unresolvedCalls=0,
)
AST_RELATIONS = AstRelations.from_index(AST_INDEX)


def test_positive_effect_score_ignores_unrelated_and_negatives() -> None:
    long_method = Smell.at(
        LONG_METHOD,
        "a/A.java",
        1,
        severity="HIGH",
        detected_by="ORGANIC",
    )
    local_envy = Smell.at(
        FEATURE_ENVY,
        "a/A.java",
        2,
        severity="MEDIUM",
        detected_by="ORGANIC",
    )
    remote_envy = Smell.at(
        FEATURE_ENVY,
        "b/B.java",
        2,
        severity="MEDIUM",
        detected_by="ORGANIC",
    )
    state = state_from_smells(
        [long_method, local_envy, remote_envy],
        AST_INDEX,
        REPO_PATH,
    )
    action = next(smell for smell in state if smell.id == long_method.id)

    assert positive_effect_score(action, state, rules, AST_RELATIONS) == 1


def test_greedy_picks_highest_positive_side_effect() -> None:
    lazy = Smell.at(
        LAZY_CLASS,
        "a/A.java",
        1,
        severity="MEDIUM",
        detected_by="ORGANIC",
    )
    speculative = Smell.at(
        SPECULATIVE_GENERALITY,
        "a/A.java",
        2,
        severity="MEDIUM",
        detected_by="ORGANIC",
    )

    selected = GreedyPlanner(ast_index=AST_INDEX).plan_next_smell(
        [lazy, speculative],
        REPO_PATH,
    )

    assert selected == speculative


def test_topo_orders_positive_predecessor_first() -> None:
    lazy = Smell.at(
        LAZY_CLASS,
        "a/A.java",
        1,
        severity="MEDIUM",
        detected_by="ORGANIC",
    )
    speculative = Smell.at(
        SPECULATIVE_GENERALITY,
        "a/A.java",
        2,
        severity="MEDIUM",
        detected_by="ORGANIC",
    )
    state = state_from_smells([lazy, speculative], AST_INDEX, REPO_PATH)

    order = topological_smell_order(state, rules, AST_RELATIONS)

    assert [smell.id for smell in order] == [speculative.id, lazy.id]


def test_topo_keeps_unrelated_smells_in_separate_components() -> None:
    left = Smell.at(
        LAZY_CLASS,
        "a/A.java",
        1,
        severity="MEDIUM",
        detected_by="ORGANIC",
    )
    right = Smell.at(
        LAZY_CLASS,
        "b/B.java",
        1,
        severity="MEDIUM",
        detected_by="ORGANIC",
    )
    state = state_from_smells([left, right], AST_INDEX, REPO_PATH)

    order = topological_smell_order(state, rules, AST_RELATIONS)

    assert {smell.id for smell in order} == {left.id, right.id}
    assert len(order) == 2


def test_topo_breaks_cycles_via_condensation() -> None:
    envy = Smell.at(
        FEATURE_ENVY,
        "a/A.java",
        1,
        severity="MEDIUM",
        detected_by="ORGANIC",
    )
    long_method = Smell.at(
        LONG_METHOD,
        "a/A.java",
        2,
        severity="HIGH",
        detected_by="ORGANIC",
    )
    # Mutual positives: Long Method <-> Feature Envy on the same method.
    state = state_from_smells([envy, long_method], AST_INDEX, REPO_PATH)
    graph_edges = {
        (source.id, target.id)
        for source in state
        for target in state
        if source.id != target.id
        and AST_RELATIONS.related(source.element_id, target.element_id)
        and target.type in rules[source.type].positive
    }
    assert (envy.id, long_method.id) in graph_edges
    assert (long_method.id, envy.id) in graph_edges

    order = topological_smell_order(state, rules, AST_RELATIONS)

    # Equal vertex weights inside the SCC → lex id order (long_method < envy).
    assert [smell.id for smell in order] == [long_method.id, envy.id]


def test_topo_planner_returns_first_in_order() -> None:
    lazy = Smell.at(
        LAZY_CLASS,
        "a/A.java",
        1,
        severity="MEDIUM",
        detected_by="ORGANIC",
    )
    speculative = Smell.at(
        SPECULATIVE_GENERALITY,
        "a/A.java",
        2,
        severity="MEDIUM",
        detected_by="ORGANIC",
    )

    selected = TopoPlanner(ast_index=AST_INDEX).plan_next_smell(
        [lazy, speculative],
        REPO_PATH,
    )

    assert selected == speculative
