"""Direct AST relationships used by dependency rules."""

from __future__ import annotations

from dataclasses import dataclass

from planning.ast.models import AstIndex


@dataclass(frozen=True, slots=True)
class AstRelations:
    """Undirected direct caller, callee, and containment relationships."""

    neighbors: dict[str, frozenset[str]]

    @classmethod
    def from_index(cls, index: AstIndex) -> AstRelations:
        """Build bidirectional one-hop relationships from a Spoon index."""
        mutable: dict[str, set[str]] = {
            element.id: set() for element in index.elements
        }
        for edge in index.edges:
            mutable.setdefault(edge.source, set()).add(edge.target)
            mutable.setdefault(edge.target, set()).add(edge.source)
        return cls(
            neighbors={
                element_id: frozenset(related)
                for element_id, related in mutable.items()
            }
        )

    def related(self, first: str, second: str) -> bool:
        """Return whether two elements are equal or directly connected."""
        return first == second or second in self.neighbors.get(
            first,
            frozenset(),
        )
