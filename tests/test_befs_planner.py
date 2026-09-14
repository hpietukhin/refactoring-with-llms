"""Tests for instance-level dependency-aware planning."""

from __future__ import annotations

from pathlib import Path

from planning.ast.models import AstEdge, AstElement, AstIndex
from planning.ast.relations import AstRelations
from planning.graph import transitions
from planning.planner import (
    NoPriorityPlanner,
    PriorityPlanner,
    best_first_smell_plan,
    planner_from_config,
    smell_burden,
    state_from_smells,
)
from planning.rules import (
    FEATURE_ENVY,
    LAZY_CLASS,
    LONG_METHOD,
    LONG_PARAMETER_LIST,
    MESSAGE_CHAINS,
    SPECULATIVE_GENERALITY,
    rules,
)
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


def test_rule_effects_ignore_unrelated_ast_elements() -> None:
    long_method = Smell.at(
        LONG_METHOD,
        "a/A.java",
        1,
        severity="HIGH",
        detected_by="ORGANIC",
    )
    local_feature_envy = Smell.at(
        FEATURE_ENVY,
        "a/A.java",
        2,
        severity="MEDIUM",
        detected_by="ORGANIC",
    )
    remote_feature_envy = Smell.at(
        FEATURE_ENVY,
        "b/B.java",
        2,
        severity="MEDIUM",
        detected_by="ORGANIC",
    )
    state = state_from_smells(
        [long_method, local_feature_envy, remote_feature_envy],
        AST_INDEX,
        REPO_PATH,
    )
    action = next(smell for smell in state if smell.id == long_method.id)

    next_state = rules[LONG_METHOD].apply(
        state,
        action,
        AST_RELATIONS,
    )

    assert local_feature_envy.id not in {smell.id for smell in next_state}
    assert remote_feature_envy.id in {smell.id for smell in next_state}
    assert {
        smell.type
        for smell in next_state
        if smell.element_id == "a.A#work()"
    } == {LONG_PARAMETER_LIST, MESSAGE_CHAINS}


def test_best_first_selects_action_with_positive_side_effect() -> None:
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
    state = state_from_smells(
        [lazy, speculative],
        AST_INDEX,
        REPO_PATH,
    )

    path = best_first_smell_plan(
        state,
        rules,
        AST_RELATIONS,
        max_expansions=100,
    )

    assert [action.id for action in path] == [speculative.id]
    assert smell_burden(state) == 4


def test_priority_planner_returns_concrete_selected_instance() -> None:
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

    selected = PriorityPlanner(ast_index=AST_INDEX).plan_next_smell(
        [lazy, speculative],
        REPO_PATH,
    )

    assert selected == speculative


def test_no_priority_planner_preserves_detector_order() -> None:
    lazy = Smell.at(
        LAZY_CLASS,
        "a/A.java",
        1,
        severity="MEDIUM",
        detected_by="ORGANIC",
    )
    long_method = Smell.at(
        LONG_METHOD,
        "b/B.java",
        2,
        severity="HIGH",
        detected_by="ORGANIC",
    )

    assert (
        NoPriorityPlanner().plan_next_smell(
            [long_method, lazy],
            REPO_PATH,
        )
        == long_method
    )


def test_planner_from_config_defaults_to_greedy() -> None:
    from config import load_settings
    from planning.greedy import GreedyPlanner

    load_settings.cache_clear()
    planner = planner_from_config()
    assert isinstance(planner, GreedyPlanner)


def test_planner_from_config_topo_env(monkeypatch) -> None:
    from planning.topo import TopoPlanner

    monkeypatch.setenv("PLANNING_PLANNER", "topo")
    planner = planner_from_config()
    assert isinstance(planner, TopoPlanner)


def test_transitions_keep_distinct_actions_with_same_successor() -> None:
    first = Smell.at(
        LAZY_CLASS,
        "a/A.java",
        1,
        severity="MEDIUM",
        detected_by="ORGANIC",
    )
    second = Smell.at(
        LAZY_CLASS,
        "a/A.java",
        2,
        severity="MEDIUM",
        detected_by="ORGANIC",
    )
    state = state_from_smells(
        [first, second],
        AST_INDEX,
        REPO_PATH,
    )

    available = transitions(state, rules, AST_RELATIONS)

    assert len(available) == 2
    assert {transition.action.id for transition in available} == {
        first.id,
        second.id,
    }


def test_rule_effects_include_direct_callee() -> None:
    long_method = Smell.at(
        LONG_METHOD,
        "a/A.java",
        1,
        severity="HIGH",
        detected_by="ORGANIC",
    )
    feature_envy = Smell.at(
        FEATURE_ENVY,
        "b/B.java",
        2,
        severity="MEDIUM",
        detected_by="ORGANIC",
    )
    index = AST_INDEX.model_copy(
        update={
            "edges": [
                AstEdge(
                    source="a.A#work()",
                    target="b.B#work()",
                    relation="calls",
                )
            ]
        }
    )
    state = state_from_smells(
        [long_method, feature_envy],
        index,
        REPO_PATH,
    )
    action = next(smell for smell in state if smell.id == long_method.id)

    next_state = rules[LONG_METHOD].apply(
        state,
        action,
        AstRelations.from_index(index),
    )

    assert feature_envy.id not in {smell.id for smell in next_state}
