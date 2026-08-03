"""Tests for LSP-backed Smell instances."""

from __future__ import annotations

from lsprotocol.types import Location, Position, Range

from planning.rules import LONG_METHOD
from smell import Smell, path_to_uri


def _location(
    file_path: str,
    start_line: int,
    end_line: int | None = None,
) -> Location:
    end = end_line if end_line is not None else start_line
    return Location(
        uri=path_to_uri(file_path),
        range=Range(
            start=Position(line=start_line - 1, character=0),
            end=Position(line=end - 1, character=0),
        ),
    )


def test_constructor_builds_hashlib_id() -> None:
    smell = Smell(
        type=LONG_METHOD,
        location=_location("/tmp/Foo.java", 42, end_line=50),
        severity="HIGH",
        detected_by="ORGANIC",
        commit_hash="abc123",
    )

    assert len(smell.id) == 64
    assert smell.type == LONG_METHOD
    assert smell.commit_hash == "abc123"
    assert smell.file_path == "/tmp/Foo.java"
    assert smell.location.range.start.line == 41  # LSP is 0-indexed
    assert smell.severity_score == 3


def test_same_fields_share_stable_id_and_dedupe_in_frozenset() -> None:
    a = Smell(
        type=LONG_METHOD,
        location=_location("src/App.java", 10),
        detected_by="ORGANIC",
        severity="MEDIUM",
        commit_hash="c1",
    )
    b = Smell(
        type=LONG_METHOD,
        location=_location("src/App.java", 10),
        detected_by="ORGANIC",
        severity="MEDIUM",
        commit_hash="c1",
    )
    c = Smell(
        type=LONG_METHOD,
        location=_location("src/App.java", 10),
        detected_by="ORGANIC",
        severity="MEDIUM",
        commit_hash="c2",
    )

    assert a.id == b.id
    assert hash(a) == hash(b)
    assert a.id != c.id
    assert frozenset({a, b, c}) == frozenset({a, c})
