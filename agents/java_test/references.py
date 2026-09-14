"""Deterministic Java source reference search."""

from __future__ import annotations

import re
from pathlib import Path


def find_symbol_references(
    root_dir: str | Path,
    symbol: str,
    dir_path: str = ".",
) -> str:
    """Return Java source lines that reference an exact symbol."""
    root = Path(root_dir).resolve()
    search_root = (root / dir_path).resolve()
    if not search_root.is_relative_to(root):
        return f"Error: Access denied to dir_path: {dir_path}."

    pattern = re.compile(rf"\b{re.escape(symbol)}\b")
    matches: list[str] = []
    try:
        for source_path in sorted(search_root.rglob("*.java")):
            if "target" in source_path.parts:
                continue
            for line_number, line in enumerate(
                source_path.read_text(encoding="utf-8").splitlines(),
                start=1,
            ):
                if pattern.search(line):
                    relative = source_path.relative_to(search_root)
                    matches.append(f"{relative}:{line_number}: {line.strip()}")
                    if len(matches) == 200:
                        return "\n".join(matches) + "\nResults truncated at 200 matches."
        if matches:
            return "\n".join(matches)
        return f"No Java references found for symbol {symbol!r} in {dir_path}"
    except OSError as exc:
        return f"Error: {exc}"
