"""File tools for the Java repair agent (rooted under the checkout)."""

from __future__ import annotations

from pathlib import Path

from langchain_core.callbacks import CallbackManagerForToolRun
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from planning.ast.tools import find_ast_references
from agents.java_test.file_management import (
    ApplyPatchTool,
    BaseFileToolMixin,
    CopyFileTool,
    DeleteFileTool,
    FileValidationError,
    FileSearchTool,
    ListDirectoryTool,
    MoveFileTool,
    PatchSequence,
    ReadFileTool,
    CreatePatchTool,
    VerifyPatchTool,
)
from repository.repo import Repo
from agents.java_test.references import find_symbol_references

# Tools that change the tree; after these the repair graph runs Maven.
MUTATING_TOOL_NAMES = frozenset(
    {
        "apply_patch",
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
    """LangChain adapter for deterministic Java reference search."""

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
        """Call the Java reference search implementation."""
        del run_manager
        return find_symbol_references(self.root_dir or ".", symbol, dir_path)


class AstReferenceTool(BaseFileToolMixin, BaseTool):
    """LangChain adapter for Spoon AST reference search."""

    name: str = "find_ast_references"
    args_schema: type[BaseModel] = SymbolReferenceInput
    description: str = (
        "Inspect Spoon AST elements and direct call or containment relations "
        "for a Java symbol."
    )

    def _run(
        self,
        symbol: str,
        dir_path: str = ".",
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        """Call the Spoon AST reference search implementation."""
        del dir_path, run_manager
        return find_ast_references(self.root_dir or ".", symbol)


class TextSearchInput(BaseModel):
    """Input for TextSearchTool."""

    query: str = Field(..., min_length=1, description="Exact text to find.")
    path: str = Field(default=".", description="File or directory to search.")
    max_results: int = Field(default=100, ge=1, le=500)


class TextSearchTool(BaseFileToolMixin, BaseTool):
    """Search file contents and return matching lines with line numbers."""

    name: str = "search_text"
    args_schema: type[BaseModel] = TextSearchInput
    description: str = (
        "Search for exact text in a file or directory. Returns file paths, line "
        "numbers, and matching lines. Use this to find assignments and usages."
    )

    def _run(
        self,
        query: str,
        path: str = ".",
        max_results: int = 100,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        del run_manager
        try:
            search_path = self.get_relative_path(path)
        except FileValidationError:
            return f"Error: search path is outside the allowed directory: {path}"
        root = Path(self.root_dir or ".").resolve()
        candidates = [search_path] if search_path.is_file() else search_path.rglob("*")
        matches: list[str] = []
        for candidate in candidates:
            if not candidate.is_file():
                continue
            relative = candidate.relative_to(root)
            if any(part in {".git", "target", "node_modules"} for part in relative.parts):
                continue
            try:
                lines = candidate.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for line_number, line in enumerate(lines, start=1):
                if query not in line:
                    continue
                matches.append(f"{relative.as_posix()}:{line_number}: {line}")
                if len(matches) >= max_results:
                    return "\n".join(matches)
        if matches:
            return "\n".join(matches)
        return f"No text matches found for {query!r} in {path}."


def make_tools(repo: Repo) -> list[BaseTool]:
    """Build file tools sandboxed to ``repo.path``."""
    root = str(repo.path)
    sequence = PatchSequence()
    return [
        ReadFileTool(root_dir=root),
        CreatePatchTool(root_dir=root, sequence=sequence),
        ApplyPatchTool(root_dir=root, sequence=sequence),
        VerifyPatchTool(root_dir=root, sequence=sequence),
        CopyFileTool(root_dir=root),
        DeleteFileTool(root_dir=root),
        MoveFileTool(root_dir=root),
        FileSearchTool(root_dir=root),
        ListDirectoryTool(root_dir=root),
        TextSearchTool(root_dir=root),
        SymbolReferenceTool(root_dir=root),
        AstReferenceTool(root_dir=root),
    ]
