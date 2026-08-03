"""File tools for the Java repair agent (rooted under the checkout)."""

from __future__ import annotations

import re

from langchain_core.callbacks import CallbackManagerForToolRun
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from agents.java_test.file_management import (
    BaseFileToolMixin,
    CopyFileTool,
    DeleteFileTool,
    FileValidationError,
    FileSearchTool,
    ListDirectoryTool,
    MoveFileTool,
    ReadFileTool,
    WriteFileTool,
)
from repository.repo import Repo

# Tools that change the tree; after these the repair graph runs Maven.
MUTATING_TOOL_NAMES = frozenset(
    {
        "write_file",
        "copy_file",
        "file_delete",
        "move_file",
    }
)


class SymbolReferenceInput(BaseModel):
    """Input for SymbolReferenceTool."""

    symbol: str = Field(..., min_length=1, description="Java type or member name to find.")
    dir_path: str = Field(default=".", description="Subdirectory to search.")


class SymbolReferenceTool(BaseFileToolMixin, BaseTool):
    """Find Java source lines that reference a symbol."""

    name: str = "find_symbol_references"
    args_schema: type[BaseModel] = SymbolReferenceInput
    description: str = (
        "Find Java source references to an exact symbol and return file paths, "
        "line numbers, and matching lines. Use before multi-file refactorings."
    )

    def _run(
        self,
        symbol: str,
        dir_path: str = ".",
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        """Return matching Java lines under the selected directory."""
        del run_manager
        try:
            search_root = self.get_relative_path(dir_path)
        except FileValidationError:
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


def make_tools(repo: Repo) -> list[BaseTool]:
    """Build file tools sandboxed to ``repo.path``."""
    root = str(repo.path)
    return [
        ReadFileTool(root_dir=root),
        WriteFileTool(root_dir=root),
        CopyFileTool(root_dir=root),
        DeleteFileTool(root_dir=root),
        MoveFileTool(root_dir=root),
        FileSearchTool(root_dir=root),
        ListDirectoryTool(root_dir=root),
        SymbolReferenceTool(root_dir=root),
    ]
