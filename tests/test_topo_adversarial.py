"""Adversarial tests for scored topological smell ordering."""

from __future__ import annotations

from planning.ast.relations import AstRelations
from planning.rules import (
    FEATURE_ENVY,
    LAZY_CLASS,
    LONG_METHOD,
    LONG_PARAMETER_LIST,
    MESSAGE_CHAINS,
    PlanningSmell,
    Rule,
    SPECULATIVE_GENERALITY,
    SmellType,
    State,
)
from planning.topo import (
    dependency_edge_kind,
    distance_score,
    positive_distance_edges,
    preference_graph,
    select_scored_order,
    topological_smell_order,
    vertex_score,
    vertex_weights,
)

# All smells share one element so AST locality never blocks edges.
SAME = AstRelations(neighbors={"e": frozenset()})
SPLIT = AstRelations(
    neighbors={
        "left": frozenset(),
        "right": frozenset(),
    }
)


def _smell(
    smell_id: str,
    smell_type: SmellType,
    *,
    element_id: str = "e",
    line: int = 1,
) -> PlanningSmell:
    return PlanningSmell(
        element_id=element_id,
        line=line,
        type=smell_type,
        id=smell_id,
        severity=2,
    )


def test_dependency_edge_kind_classifies_pos_neg_unspecified() -> None:
    rule = Rule(
        target=LONG_METHOD,
        positive=frozenset({FEATURE_ENVY, LONG_PARAMETER_LIST}),
        negative=frozenset({LONG_PARAMETER_LIST, MESSAGE_CHAINS}),
    )

    assert dependency_edge_kind(rule, FEATURE_ENVY) == "positive"
    assert dependency_edge_kind(rule, MESSAGE_CHAINS) == "negative"
    assert dependency_edge_kind(rule, LONG_PARAMETER_LIST) == "unspecified"
    assert dependency_edge_kind(rule, LAZY_CLASS) is None


def test_negative_only_edge_forces_predecessor_first() -> None:
    """Fixing A may cause B → prefer A before B even with no positive link."""
    rules = {
        LONG_METHOD: Rule(
            target=LONG_METHOD,
            negative=frozenset({MESSAGE_CHAINS}),
        ),
        MESSAGE_CHAINS: Rule(target=MESSAGE_CHAINS),
    }
    state: State = (
        _smell("chains", MESSAGE_CHAINS, line=2),
        _smell("long", LONG_METHOD, line=1),
    )

    order = topological_smell_order(state, rules, SAME)

    assert [smell.id for smell in order] == ["long", "chains"]


def test_unspecified_is_preference_but_not_distance_edge() -> None:
    rules = {
        LONG_METHOD: Rule(
            target=LONG_METHOD,
            positive=frozenset({LONG_PARAMETER_LIST}),
            negative=frozenset({LONG_PARAMETER_LIST}),
        ),
        LONG_PARAMETER_LIST: Rule(target=LONG_PARAMETER_LIST),
    }
    state: State = (
        _smell("long", LONG_METHOD),
        _smell("params", LONG_PARAMETER_LIST, line=2),
    )
    graph = preference_graph(state, rules, SAME)

    assert graph.has_edge("long", "params")
    assert graph.edges["long", "params"]["kind"] == "unspecified"
    assert positive_distance_edges(graph) == set()
    assert vertex_weights(graph) == {"long": 0, "params": 0}


def test_distance_score_rewards_smaller_positive_gap() -> None:
    positive = {("a", "c")}

    assert distance_score(["a", "b", "c"], positive) == 0.5
    assert distance_score(["b", "a", "c"], positive) == 1.0
    assert distance_score(["c", "a", "b"], positive) == 0.0


def test_vertex_score_rewards_high_weight_early() -> None:
    weights = {"a": 4, "b": 0}

    assert vertex_score(["a", "b"], weights) == 4.0
    assert vertex_score(["b", "a"], weights) == 2.0


def test_select_scored_order_prefers_closer_positive_when_weights_tie() -> None:
    """Distance alone must flip the winner when vertex scores are equal."""
    candidates = [["a", "b", "c"], ["b", "a", "c"]]
    positive = {("a", "c")}
    weights = {"a": 0, "b": 0, "c": 0}

    assert select_scored_order(candidates, positive, weights) == [
        "b",
        "a",
        "c",
    ]


def test_select_scored_order_prefers_high_weight_when_distance_ties() -> None:
    candidates = [["a", "b"], ["b", "a"]]
    positive: set[tuple[str, str]] = set()
    weights = {"a": 5, "b": 0}

    assert select_scored_order(candidates, positive, weights) == ["a", "b"]


def test_select_scored_order_lex_breaks_equal_scores() -> None:
    candidates = [["b", "a"], ["a", "b"]]
    positive: set[tuple[str, str]] = set()
    weights = {"a": 0, "b": 0}

    assert select_scored_order(candidates, positive, weights) == ["a", "b"]


def test_scoring_puts_positive_pair_adjacent_over_lex_interleaving() -> None:
    """spec→lazy positive; envy free. Closer pair beats envy between them."""
    rules = {
        SPECULATIVE_GENERALITY: Rule(
            target=SPECULATIVE_GENERALITY,
            positive=frozenset({LAZY_CLASS}),
        ),
        LAZY_CLASS: Rule(target=LAZY_CLASS),
        FEATURE_ENVY: Rule(target=FEATURE_ENVY),
    }
    state: State = (
        _smell("envy", FEATURE_ENVY, line=3),
        _smell("lazy", LAZY_CLASS, line=2),
        _smell("spec", SPECULATIVE_GENERALITY, line=1),
    )

    order = topological_smell_order(state, rules, SAME)

    assert [smell.id for smell in order] == ["spec", "lazy", "envy"]


def test_mixed_pos_neg_constraints_are_all_respected() -> None:
    rules = {
        LONG_METHOD: Rule(
            target=LONG_METHOD,
            positive=frozenset({FEATURE_ENVY}),
        ),
        MESSAGE_CHAINS: Rule(
            target=MESSAGE_CHAINS,
            positive=frozenset({LAZY_CLASS}),
            negative=frozenset({FEATURE_ENVY}),
        ),
        FEATURE_ENVY: Rule(target=FEATURE_ENVY),
        LAZY_CLASS: Rule(target=LAZY_CLASS),
    }
    state: State = (
        _smell("a_long", LONG_METHOD, line=1),
        _smell("b_chains", MESSAGE_CHAINS, line=2),
        _smell("c_envy", FEATURE_ENVY, line=3),
        _smell("d_lazy", LAZY_CLASS, line=4),
    )
    order_ids = [smell.id for smell in topological_smell_order(state, rules, SAME)]

    assert order_ids.index("a_long") < order_ids.index("c_envy")
    assert order_ids.index("b_chains") < order_ids.index("c_envy")
    assert order_ids.index("b_chains") < order_ids.index("d_lazy")


def test_scc_orders_members_by_weight_then_id() -> None:
    rules = {
        LONG_METHOD: Rule(
            target=LONG_METHOD,
            positive=frozenset({FEATURE_ENVY, MESSAGE_CHAINS}),
        ),
        FEATURE_ENVY: Rule(
            target=FEATURE_ENVY,
            positive=frozenset({LONG_METHOD}),
        ),
        MESSAGE_CHAINS: Rule(target=MESSAGE_CHAINS),
    }
    # Mutual LONG ↔ ENVY → one SCC. LONG has extra positive to chains → higher W.
    state: State = (
        _smell("envy", FEATURE_ENVY, line=2),
        _smell("long", LONG_METHOD, line=1),
        _smell("chains", MESSAGE_CHAINS, line=3),
    )
    graph = preference_graph(state, rules, SAME)
    weights = vertex_weights(graph)

    assert weights["long"] > weights["envy"]
    order = topological_smell_order(state, rules, SAME)
    order_ids = [smell.id for smell in order]

    assert order_ids.index("long") < order_ids.index("envy")
    assert order_ids.index("long") < order_ids.index("chains")


def test_unrelated_elements_get_no_preference_edge() -> None:
    rules = {
        SPECULATIVE_GENERALITY: Rule(
            target=SPECULATIVE_GENERALITY,
            positive=frozenset({LAZY_CLASS}),
        ),
        LAZY_CLASS: Rule(target=LAZY_CLASS),
    }
    state: State = (
        _smell("spec", SPECULATIVE_GENERALITY, element_id="left"),
        _smell("lazy", LAZY_CLASS, element_id="right", line=2),
    )
    graph = preference_graph(state, rules, SPLIT)

    assert graph.number_of_edges() == 0
    # Equal scores → lex by id: lazy < spec
    order = topological_smell_order(state, rules, SPLIT)
    assert [smell.id for smell in order] == ["lazy", "spec"]


def test_empty_and_singleton_state() -> None:
    assert topological_smell_order((), {}, SAME) == []

    rules = {LAZY_CLASS: Rule(target=LAZY_CLASS)}
    alone = (_smell("only", LAZY_CLASS),)
    assert [smell.id for smell in topological_smell_order(alone, rules, SAME)] == [
        "only"
    ]


def test_violated_positive_inside_scc_does_not_crash_distance() -> None:
    """Inside an SCC, member order may place a positive successor first."""
    assert distance_score(["b", "a"], {("a", "b")}) == 0.0
