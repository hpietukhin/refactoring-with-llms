"""Concrete code-smell instances with LSP locations.

A smell is a smell type plus where it appears in source. Location uses the
Language Server Protocol ``Location`` type (document URI + ``Range``), so
findings stay compatible with editor/tooling conventions (0-based lines).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal
from urllib.parse import unquote, urlparse

from lsprotocol.types import Location, Position, Range

from planning.rules import SmellType

type Severity = Literal["LOW", "MEDIUM", "HIGH"]
type DetectorType = Literal["ORGANIC", "SONARQUBE"]

ADVICE_DIR: Final = Path("agents/deep/smells")
SMELL_ADVICE_PATHS: Final[dict[SmellType, Path]] = {
    "Long Method": ADVICE_DIR / "long-method.md",
    "Large Class": ADVICE_DIR / "large-class.md",
    "Long Parameter List": ADVICE_DIR / "long-parameter-list.md",
    "Duplicated Code": ADVICE_DIR / "duplicated-code.md",
    "Divergent Change": ADVICE_DIR / "divergent-change.md",
    "Shotgun Surgery": ADVICE_DIR / "shotgun-surgery.md",
    "Feature Envy": ADVICE_DIR / "feature-envy.md",
    "Data Clumps": ADVICE_DIR / "data-clump.md",
    "Primitive Obsession": ADVICE_DIR / "primitive-obsession.md",
    "Switch Statement": ADVICE_DIR / "switch-statement.md",
    "Parallel Inheritance Hierarchies": ADVICE_DIR
    / "parallel-inheritance-hierarchies.md",
    "Lazy Class": ADVICE_DIR / "lazy-element.md",
    "Speculative Generality": ADVICE_DIR / "speculative-generality.md",
    "Temporary Field": ADVICE_DIR / "temporary-field.md",
    "Message Chains": ADVICE_DIR / "message-chain.md",
    "Middle Man": ADVICE_DIR / "middle-man.md",
    "Inappropriate Intimacy": ADVICE_DIR / "inappropriate-intimacy.md",
    "Alternative Classes with Different Interfaces": ADVICE_DIR
    / "alternative-classes-with-different-interfaces.md",
    "Incomplete Library Class": ADVICE_DIR / "incomplete-library-class.md",
    "Data Class": ADVICE_DIR / "data-class.md",
    "Refused Bequest": ADVICE_DIR / "refused-bequest.md",
    "Comments": ADVICE_DIR / "comments.md",
    "Complex Method": ADVICE_DIR / "complex-method.md",
    "Conditional Complexity": ADVICE_DIR / "conditional-complexity.md",
    "God Class": ADVICE_DIR / "god-class.md",
    "Bad Class Content": ADVICE_DIR / "bad-class-content.md",
    "Bad Inheritance": ADVICE_DIR / "bad-inheritance.md",
    "Needless Part": ADVICE_DIR / "needless-part.md",
    "Duplicated Conditions": ADVICE_DIR / "duplicated-conditions.md",
    "Print Statements": ADVICE_DIR / "print-statements.md",
    "Complex Class": ADVICE_DIR / "complex-class.md",
    "Spaghetti Code": ADVICE_DIR / "spaghetti-code.md",
    "Class Data Should Be Private": ADVICE_DIR / "class-data-should-be-private.md",
    "Brain Method": ADVICE_DIR / "brain-method.md",
    "Brain Class": ADVICE_DIR / "brain-class.md",
    "Intensive Coupling": ADVICE_DIR / "intensive-coupling.md",
    "Dispersed Coupling": ADVICE_DIR / "dispersed-coupling.md",
}


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
    advice: Path = field(init=False)
    id: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "advice", SMELL_ADVICE_PATHS[self.type])
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

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable representation of this smell."""
        source_range = self.location.range
        return {
            "type": self.type,
            "location": {
                "uri": self.location.uri,
                "range": {
                    "start": {
                        "line": source_range.start.line,
                        "character": source_range.start.character,
                    },
                    "end": {
                        "line": source_range.end.line,
                        "character": source_range.end.character,
                    },
                },
            },
            "severity": self.severity,
            "detected_by": self.detected_by,
            "commit_hash": self.commit_hash,
            "advice": str(self.advice),
            "id": self.id,
        }

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
    "ADVICE_DIR",
    "SMELL_ADVICE_PATHS",
    "Smell",
    "path_to_uri",
    "uri_to_path",
]
