"""Typed output of the Spoon AST indexer."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

type AstElementKind = Literal[
    "class",
    "interface",
    "enum",
    "type",
    "method",
    "constructor",
]
type AstRelation = Literal["calls", "contains"]


class AstElement(BaseModel):
    """One source element found by Spoon."""

    id: str
    kind: AstElementKind
    file: str
    start_line: int = Field(alias="startLine")
    end_line: int = Field(alias="endLine")


class AstEdge(BaseModel):
    """A direct call or containment relation."""

    source: str
    target: str
    relation: AstRelation


class AstIndex(BaseModel):
    """Elements and direct relations for one repository state."""

    elements: list[AstElement]
    edges: list[AstEdge]
    unresolved_calls: int = Field(alias="unresolvedCalls")

    def element_at(
        self,
        file_path: str,
        line: int,
        repo_path: Path,
    ) -> AstElement | None:
        """Return the most specific element containing a source location."""
        normalized = Path(file_path).as_posix()
        repository = repo_path.expanduser().resolve()
        candidate = Path(file_path).expanduser()
        if candidate.is_absolute():
            try:
                normalized = candidate.resolve().relative_to(repository).as_posix()
            except ValueError:
                normalized = candidate.as_posix()

        files = {
            element.file
            for element in self.elements
            if normalized == element.file
            or normalized.endswith("/" + element.file)
            or element.file.endswith("/" + normalized)
        }
        if len(files) != 1:
            return None
        matched_file = next(iter(files))
        containing = [
            element
            for element in self.elements
            if element.file == matched_file
            and element.start_line <= line <= element.end_line
        ]
        if not containing:
            return None
        return min(
            containing,
            key=lambda element: (
                element.end_line - element.start_line,
                element.kind not in {"method", "constructor"},
                element.id,
            ),
        )
