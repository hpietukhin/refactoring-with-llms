"""Composite LangGraph agent construction."""

from __future__ import annotations

from typing import cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from workflows.composite.models import CompositeWorkflowState
from workflows.composite.nodes import (
    action_route,
    action_selected,
    continue_after_replan,
    detect_smells,
    java_verification,
    planner,
    refactor_smell,
    replan_after_action,
    repo_checkout,
    step_start,
    route_after_java_tests,
    validate_refactor,
    workflow_start,
)


def build_graph() -> CompiledStateGraph:
    """Build the composite detect → plan → refactor → verify loop."""
    graph = StateGraph(CompositeWorkflowState)

    graph.add_node("workflow_start", workflow_start)
    graph.add_node("repo_checkout", repo_checkout)
    graph.add_node("detect_smells", detect_smells)
    graph.add_node("baseline_verification", java_verification)
    graph.add_node("step_start", step_start)
    graph.add_node("planner", planner)
    graph.add_node("action_selected", action_selected)
    graph.add_node("refactor_smell", refactor_smell)
    graph.add_node("validate_refactor", validate_refactor)
    graph.add_node("java_tests", java_verification)
    graph.add_node("replan_after_action", replan_after_action)

    graph.add_edge(START, "workflow_start")
    graph.add_edge("workflow_start", "repo_checkout")
    graph.add_edge("repo_checkout", "detect_smells")
    graph.add_edge("detect_smells", "baseline_verification")
    graph.add_edge("baseline_verification", "step_start")
    graph.add_edge("step_start", "planner")
    graph.add_edge("planner", "action_selected")
    graph.add_conditional_edges(
        "action_selected",
        action_route,
        {
            "refactor_smell": "refactor_smell",
            "end": END,
        },
    )
    graph.add_edge("refactor_smell", "validate_refactor")
    graph.add_edge("validate_refactor", "java_tests")
    graph.add_conditional_edges(
        "java_tests",
        route_after_java_tests,
        {
            "refactor_smell": "refactor_smell",
            "replan_after_action": "replan_after_action",
        },
    )
    graph.add_conditional_edges(
        "replan_after_action",
        continue_after_replan,
        {
            "step_start": "step_start",
            "end": END,
        },
    )

    return graph.compile()


composite_workflow = build_graph()


def main(initial_state: CompositeWorkflowState | None = None) -> CompositeWorkflowState:
    """Run the composite workflow graph."""
    result = composite_workflow.invoke(initial_state or {})
    return cast(CompositeWorkflowState, result)


if __name__ == "__main__":
    print(main())
