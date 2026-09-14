"""AST queries used by agent-facing tools."""

from __future__ import annotations

from pathlib import Path

from planning.ast.runner import GradleRunner
from repository.repo import Repo


def find_ast_references(repo_path: str | Path, symbol: str) -> str:
    """Return AST elements and direct relations that contain ``symbol``."""
    index = GradleRunner(Repo(repo_path)).inspect()
    elements = [element for element in index.elements if symbol in element.id]
    element_ids = {element.id for element in elements}
    edges = [
        edge
        for edge in index.edges
        if edge.source in element_ids or edge.target in element_ids
    ]
    if not elements:
        return f"No AST references found for symbol {symbol!r}"
    lines = [f"{element.kind}: {element.id} ({element.file})" for element in elements]
    lines.extend(
        f"{edge.relation}: {edge.source} -> {edge.target}"
        for edge in edges
    )
    return "\n".join(lines)
