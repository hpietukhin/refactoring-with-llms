"""Path conversion for DeepAgents virtual filesystem tools."""

from __future__ import annotations

from pathlib import Path


def virtual_path(repo_path: Path, file_path: str) -> str:
    """Convert a repository file path into a DeepAgents virtual path."""
    path = Path(file_path)
    relative = path.relative_to(repo_path) if path.is_absolute() else path
    return f"/{relative.as_posix().lstrip('/')}"
