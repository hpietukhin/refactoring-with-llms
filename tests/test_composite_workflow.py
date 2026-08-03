"""Tests for composite LangGraph routing and planning-backed selection."""

from __future__ import annotations

from typing import cast

import pytest
from langgraph.graph import END, START, StateGraph

from planning.ast.models import AstElement, AstIndex
from planning.planner import (
    NoPriorityPlanner,
    PriorityPlanner,
    pick_next_smell,
)
from smell.smell import Smell
from workflows.composite.graph import build_graph
from workflows.composite.models import CompositeWorkflowState
from workflows.composite.nodes.planner import action_route, planner
from workflows.composite.nodes.refactor import continue_after_replan
from workflows.composite.nodes.verify import route_after_java_tests

AST_INDEX = AstIndex(
    elements=[
        AstElement(
            id="a.A#work()",
            kind="method",
            file="a/A.java",
            startLine=1,
            endLine=20,
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


def test_pick_next_smell_uses_planning_not_severity() -> None:
    """Positive dependency effects can override detector order."""
    lazy = Smell.at(
        "Lazy Class",
        "a/A.java",
        1,
        severity="MEDIUM",
        detected_by="ORGANIC",
    )
    speculative = Smell.at(
        "Speculative Generality",
        "a/A.java",
        2,
        severity="MEDIUM",
        detected_by="ORGANIC",
    )

    assert (
        pick_next_smell(
            [lazy, speculative],
            ".",
            planner=PriorityPlanner(ast_index=AST_INDEX),
        )
        == speculative
    )


def test_pick_next_smell_stable_instance_for_type() -> None:
    a = Smell.at(
        "Lazy Class",
        "a/A.java",
        1,
        severity="MEDIUM",
        detected_by="ORGANIC",
    )
    b = Smell.at(
        "Lazy Class",
        "b/B.java",
        2,
        severity="MEDIUM",
        detected_by="ORGANIC",
    )
    chosen = pick_next_smell(
        [a, b],
        ".",
        planner=PriorityPlanner(ast_index=AST_INDEX),
    )
    assert chosen is not None
    assert chosen.type == "Lazy Class"
    assert chosen.id == a.id


def test_pick_next_smell_no_priority_ignores_dependency_rules() -> None:
    lazy = Smell.at("Lazy Class", "a/A.java", 1, severity="LOW", detected_by="ORGANIC")
    long_method = Smell.at("Long Method", "b/B.java", 2, severity="HIGH", detected_by="ORGANIC")
    chosen = pick_next_smell(
        [long_method, lazy],
        ".",
        planner=NoPriorityPlanner(),
    )
    assert chosen is not None
    assert chosen.type == "Long Method"


def test_planner_sets_stop_when_empty() -> None:
    result = planner({"smells": [], "repo_path": "."})
    assert result["selected_smell_id"] is None
    assert result["stop_reason"] == "no_smells"
    assert action_route(cast(CompositeWorkflowState, result)) == "end"


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"tests_failed": True, "retries_used": 0, "max_retries": 1}, "refactor_smell"),
        ({"tests_failed": True, "retries_used": 1, "max_retries": 1}, "replan_after_action"),
        ({"tests_failed": False}, "replan_after_action"),
    ],
)
def test_route_after_java_tests_retries_then_replans(state: dict[str, object], expected: str) -> None:
    assert route_after_java_tests(state) == expected


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"stop_reason": "smells_cleared", "smells": [], "step": 1, "max_steps": 5}, "end"),
        ({"smells": [], "step": 1, "max_steps": 5}, "end"),
        pytest.param(
            {"smells": ["lazy-smell"], "step": 1, "max_steps": 5},
            "step_start",
            id="continue",
        ),
        pytest.param(
            {"smells": ["lazy-smell"], "step": 5, "max_steps": 5},
            "end",
            id="max-steps",
        ),
    ],
)
def test_continue_after_replan(state: dict[str, object], expected: str) -> None:
    if "lazy-smell" in state.get("smells", []):
        state = {
            **state,
            "smells": [
                Smell.at("Lazy Class", "a/A.java", 1, severity="MEDIUM", detected_by="ORGANIC")
            ],
        }
    assert continue_after_replan(state) == expected


def test_build_graph_compiles() -> None:
    graph = build_graph()
    assert graph is not None


def test_routing_subgraph_with_stubs() -> None:
    """Exercise conditional edges without checkout/Organic/LLM."""

    def start(state: CompositeWorkflowState) -> CompositeWorkflowState:
        return {"step": 0, "max_steps": 2, "retries_used": 0, "max_retries": 1}

    def plan(state: CompositeWorkflowState) -> CompositeWorkflowState:
        smells = list(state.get("smells") or [])
        if not smells:
            return {"selected_smell_id": None, "stop_reason": "no_smells"}
        return {"selected_smell_id": smells[0].id}

    def refactor(state: CompositeWorkflowState) -> CompositeWorkflowState:
        return {"refactor_ok": True, "retries_used": int(state.get("retries_used") or 0) + 1}

    def verify(state: CompositeWorkflowState) -> CompositeWorkflowState:
        return {"tests_failed": False}

    def replan(state: CompositeWorkflowState) -> CompositeWorkflowState:
        return {"smells": [], "step": 1, "stop_reason": "smells_cleared"}

    graph = StateGraph(CompositeWorkflowState)
    graph.add_node("start", start)
    graph.add_node("plan", plan)
    graph.add_node("refactor", refactor)
    graph.add_node("verify", verify)
    graph.add_node("replan", replan)
    graph.add_edge(START, "start")
    graph.add_edge("start", "plan")
    graph.add_conditional_edges("plan", action_route, {"refactor_smell": "refactor", "end": END})
    graph.add_edge("refactor", "verify")
    graph.add_conditional_edges(
        "verify",
        route_after_java_tests,
        {"refactor_smell": "refactor", "replan_after_action": "replan"},
    )
    graph.add_conditional_edges("replan", continue_after_replan, {"step_start": "plan", "end": END})
    compiled = graph.compile()

    smell = Smell.at("Lazy Class", "a/A.java", 1, severity="MEDIUM", detected_by="ORGANIC")
    result = compiled.invoke({"smells": [smell]})
    assert result["stop_reason"] == "smells_cleared"

    empty = compiled.invoke({"smells": []})
    assert empty.get("stop_reason") == "no_smells"
