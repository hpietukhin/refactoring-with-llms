"""Tests for Spoon AST output mapping."""

from __future__ import annotations

from pathlib import Path

from planning.ast.models import AstEdge, AstElement, AstIndex
from planning.ast.relations import AstRelations


def test_element_at_returns_most_specific_enclosing_element(
    tmp_path: Path,
) -> None:
    index = AstIndex(
        elements=[
            AstElement(
                id="example.Service",
                kind="class",
                file="src/main/java/example/Service.java",
                startLine=1,
                endLine=40,
            ),
            AstElement(
                id="example.Service#run()",
                kind="method",
                file="src/main/java/example/Service.java",
                startLine=10,
                endLine=20,
            ),
        ],
        edges=[],
        unresolvedCalls=0,
    )

    element = index.element_at(
        "src/main/java/example/Service.java",
        12,
        tmp_path,
    )

    assert element is not None
    assert element.id == "example.Service#run()"


def test_relations_are_bidirectional_and_one_hop_only() -> None:
    index = AstIndex(
        elements=[
            AstElement(
                id="a",
                kind="method",
                file="A.java",
                startLine=1,
                endLine=2,
            ),
            AstElement(
                id="b",
                kind="method",
                file="B.java",
                startLine=1,
                endLine=2,
            ),
            AstElement(
                id="c",
                kind="method",
                file="C.java",
                startLine=1,
                endLine=2,
            ),
        ],
        edges=[
            AstEdge(source="a", target="b", relation="calls"),
            AstEdge(source="b", target="c", relation="contains"),
        ],
        unresolvedCalls=0,
    )

    relations = AstRelations.from_index(index)

    assert relations.related("a", "b")
    assert relations.related("b", "a")
    assert not relations.related("a", "c")
