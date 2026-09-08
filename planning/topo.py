"""Topological smell ordering with scored preference sequences.

Builds a refactoring preference graph from AST-local positive, negative, and
unspecified (positive ∩ negative) rule edges. Cycles become SCCs via NetworkX
condensation. Candidate linear orders are topological sorts of the condensed
DAG; each order is ranked by the Orlova/Polasek distance and vertex scores.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import networkx as nx

from planning.ast.models import AstIndex
from planning.ast.relations import AstRelations
from planning.ast.runner import GradleRunner
from planning.organic_rules import organic_rules
from planning.rules import PlanningSmell, Rule, SmellType, State
from planning.state import state_from_smells
from repository.repo import Repo
from smell.smell import Smell

type EdgeKind = Literal["positive", "negative", "unspecified"]

# Cap enumeration; n! grows fast on sparse preference graphs.
_MAX_TOPO_ORDERS = 4096


def dependency_edge_kind(
    rule: Rule,
    target_type: SmellType,
) -> EdgeKind | None:
    """Classify how fixing the rule target affects ``target_type``."""
    in_positive = target_type in rule.positive
    in_negative = target_type in rule.negative
    if in_positive and in_negative:
        return "unspecified"
    if in_positive:
        return "positive"
    if in_negative:
        return "negative"
    return None


def preference_graph(
    state: State,
    rule_map: dict[SmellType, Rule],
    relations: AstRelations,
) -> nx.DiGraph:
    """Build preference graph with ``kind`` on each edge.

    Preference edge ``A -> B`` means fix ``A`` before ``B``. Positive, negative,
    and unspecified dependencies all contribute that direction (paper rules).
    """
    by_id = {smell.id: smell for smell in state}
    graph = nx.DiGraph()
    graph.add_nodes_from(by_id)

    for source in state:
        rule = rule_map.get(source.type)
        if rule is None:
            continue
        for target in state:
            if source.id == target.id:
                continue
            if not relations.related(source.element_id, target.element_id):
                continue
            kind = dependency_edge_kind(rule, target.type)
            if kind is None:
                continue
            graph.add_edge(source.id, target.id, kind=kind)

    return graph


def vertex_weights(graph: nx.DiGraph) -> dict[str, int]:
    """W[v] = positive out-degree − negative out-degree.

    Unspecified edges count in both degrees (net zero for that edge).
    """
    weights = {node_id: 0 for node_id in graph.nodes}
    for source, _target, data in graph.edges(data=True):
        kind = data["kind"]
        if kind in {"positive", "unspecified"}:
            weights[source] += 1
        if kind in {"negative", "unspecified"}:
            weights[source] -= 1
    return weights


def positive_distance_edges(graph: nx.DiGraph) -> set[tuple[str, str]]:
    """Pure positive (solve) edges used by the distance score."""
    return {
        (source, target)
        for source, target, data in graph.edges(data=True)
        if data["kind"] == "positive"
    }


def distance_score(
    order: Sequence[str],
    positive_edges: set[tuple[str, str]],
) -> float:
    """Reward positive influence pairs that stay close (1 / gap).

    Positions are 1-based. Edges that violate order (gap ≤ 0) contribute 0 —
    that can happen inside an SCC after condensation.
    """
    position = {node_id: index + 1 for index, node_id in enumerate(order)}
    score = 0.0
    for source, target in positive_edges:
        if source not in position or target not in position:
            continue
        gap = position[target] - position[source]
        if gap > 0:
            score += 1.0 / gap
    return score


def vertex_score(
    order: Sequence[str],
    weights: dict[str, int],
) -> float:
    """Reward high-W smells appearing earlier (W[v] / position[v])."""
    score = 0.0
    for index, node_id in enumerate(order):
        score += weights[node_id] / (index + 1)
    return score


def _normalize(values: Sequence[float]) -> list[float]:
    if not values:
        return []
    lo = min(values)
    hi = max(values)
    if hi == lo:
        return [0.0] * len(values)
    return [(value - lo) / (hi - lo) for value in values]


def _scc_member_order(
    members: Sequence[str],
    weights: dict[str, int],
) -> list[str]:
    return sorted(members, key=lambda node_id: (-weights[node_id], node_id))


def _expand_condensation_order(
    condensation: nx.DiGraph,
    scc_order: Sequence[object],
    weights: dict[str, int],
) -> list[str]:
    expanded: list[str] = []
    for scc_id in scc_order:
        members = condensation.nodes[scc_id]["members"]
        expanded.extend(_scc_member_order(members, weights))
    return expanded


def _candidate_orders(
    graph: nx.DiGraph,
    weights: dict[str, int],
) -> list[list[str]]:
    """Enumerate expanded topological orders of the SCC condensation DAG."""
    if graph.number_of_nodes() == 0:
        return [[]]

    condensation = nx.condensation(graph)
    for scc_id in condensation.nodes:
        members = condensation.nodes[scc_id]["members"]
        condensation.nodes[scc_id]["members"] = _scc_member_order(
            members,
            weights,
        )

    orders: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()

    def add_order(node_ids: list[str]) -> None:
        key = tuple(node_ids)
        if key in seen:
            return
        seen.add(key)
        orders.append(node_ids)

    # Always keep the deterministic lex baseline.
    baseline_scc = list(
        nx.lexicographical_topological_sort(
            condensation,
            key=lambda scc_id: min(condensation.nodes[scc_id]["members"]),
        )
    )
    add_order(_expand_condensation_order(condensation, baseline_scc, weights))

    for scc_order in nx.all_topological_sorts(condensation):
        add_order(_expand_condensation_order(condensation, scc_order, weights))
        if len(orders) >= _MAX_TOPO_ORDERS:
            break
    return orders


def select_scored_order(
    candidates: Sequence[Sequence[str]],
    positive_edges: set[tuple[str, str]],
    weights: dict[str, int],
) -> list[str]:
    """Pick the order with max normalized distance + vertex score.

    Ties break on lexicographic id sequence.
    """
    if not candidates:
        return []
    distance_scores = [
        distance_score(order, positive_edges) for order in candidates
    ]
    vertex_scores = [vertex_score(order, weights) for order in candidates]
    distance_norm = _normalize(distance_scores)
    vertex_norm = _normalize(vertex_scores)
    best_index = min(
        range(len(candidates)),
        key=lambda index: (
            -(distance_norm[index] + vertex_norm[index]),
            tuple(candidates[index]),
        ),
    )
    return list(candidates[best_index])


def topological_smell_order(
    state: State,
    rule_map: dict[SmellType, Rule],
    relations: AstRelations,
) -> list[PlanningSmell]:
    """Order smells by scored topological sorts of the preference graph."""
    if not state:
        return []

    by_id = {smell.id: smell for smell in state}
    graph = preference_graph(state, rule_map, relations)
    weights = vertex_weights(graph)
    candidates = _candidate_orders(graph, weights)
    best = select_scored_order(
        candidates,
        positive_distance_edges(graph),
        weights,
    )
    return [by_id[node_id] for node_id in best]


@dataclass(frozen=True, slots=True)
class TopoPlanner:
    """Return the first smell of the scored topological preference order."""

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
        order = topological_smell_order(
            state_from_smells(smells, index, repo_path),
            active_rules,
            AstRelations.from_index(index),
        )
        if not order:
            return None
        selected_id = order[0].id
        for smell in smells:
            if smell.id == selected_id:
                return smell
        raise RuntimeError(
            f"Planned smell is absent from detector state: {selected_id}"
        )
