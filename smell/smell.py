"""Concrete code-smell instances with LSP locations.

A smell is a smell type plus where it appears in source. Location uses the
Language Server Protocol ``Location`` type (document URI + ``Range``), so
findings stay compatible with editor/tooling conventions (0-based lines).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlparse

from lsprotocol.types import Location, Position, Range

from planning.rules import SmellType

type Severity = Literal["LOW", "MEDIUM", "HIGH"]
type DetectorType = Literal["ORGANIC", "SONARQUBE"]


def path_to_uri(file_path: str) -> str:
    """Absolute paths become ``file:`` URIs; relative paths stay as-is."""
    if file_path.startswith("file:"):
        return file_path
    path = Path(file_path)
    if path.is_absolute():
        return path.as_uri()
    return file_path


def uri_to_path(uri: str) -> str:
    if not uri.startswith("file:"):
        return uri
    return unquote(urlparse(uri).path)


def _location_key(location: Location) -> tuple[str, int, int, int, int]:
    """Hashable projection of an LSP ``Location`` (``Location`` itself is unhashable)."""
    start = location.range.start
    end = location.range.end
    return (
        location.uri,
        start.line,
        start.character,
        end.line,
        end.character,
    )


def _identity_digest(smell: Smell) -> str:
    """Stable SHA-256 hex digest of all identity fields."""
    uri, start_line, start_char, end_line, end_char = _location_key(smell.location)
    payload = "\0".join(
        (
            smell.type,
            uri,
            str(start_line),
            str(start_char),
            str(end_line),
            str(end_char),
            smell.severity,
            smell.detected_by,
            smell.commit_hash or "",
        )
    )
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Smell:
    """A detected smell instance used for planning and comparison."""

    type: SmellType
    location: Location
    severity: Severity
    detected_by: DetectorType
    commit_hash: str | None = None
    id: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _identity_digest(self))

    @classmethod
    def at(
        cls,
        smell_type: SmellType,
        file_path: str,
        start_line: int,
        end_line: int | None = None,
        *,
        severity: Severity,
        detected_by: DetectorType,
        commit_hash: str | None = None,
    ) -> Smell:
        """Build a smell from a path and 1-based source lines."""
        start = max(start_line, 1) - 1
        end = max(end_line if end_line is not None else start_line, start_line, 1) - 1
        return cls(
            type=smell_type,
            location=Location(
                uri=path_to_uri(file_path),
                range=Range(
                    start=Position(line=start, character=0),
                    end=Position(line=end, character=0),
                ),
            ),
            severity=severity,
            detected_by=detected_by,
            commit_hash=commit_hash,
        )

    def __hash__(self) -> int:
        return int(_identity_digest(self)[:16], 16)

    @property
    def file_path(self) -> str:
        return uri_to_path(self.location.uri)

    @property
    def severity_score(self) -> int:
        if self.severity == "HIGH":
            return 3
        if self.severity == "MEDIUM":
            return 2
        return 1


__all__ = [
    "Severity",
    "DetectorType",
    "Smell",
    "path_to_uri",
    "uri_to_path",
]
