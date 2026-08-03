"""LangChain file tools sandboxed to a repository root."""

from __future__ import annotations

import fnmatch
import os
import shutil
from pathlib import Path
from typing import Type

from langchain_core.callbacks import CallbackManagerForToolRun
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

INVALID_PATH_TEMPLATE = (
    "Error: Access denied to {arg_name}: {value}."
    " Permission granted exclusively to the current working directory"
)


class FileValidationError(ValueError):
    """Error for paths outside the root directory."""


class BaseFileToolMixin(BaseModel):
    """Mixin for file system tools."""

    root_dir: str | None = None
    """The final path will be chosen relative to root_dir if specified."""

    def get_relative_path(self, file_path: str) -> Path:
        """Get the relative path, returning an error if unsupported."""
        if self.root_dir is None:
            return Path(file_path)
        return get_validated_relative_path(Path(self.root_dir), file_path)


def get_validated_relative_path(root: Path, user_path: str) -> Path:
    """Resolve a relative path, raising an error if not within the root directory."""
    root = root.resolve()
    full_path = (root / user_path).resolve()
    if not full_path.is_relative_to(root):
        raise FileValidationError(
            f"Path {user_path} is outside of the allowed directory {root}"
        )
    return full_path


class ReadFileInput(BaseModel):
    """Input for ReadFileTool."""

    file_path: str = Field(..., description="name of file")


class ReadFileTool(BaseFileToolMixin, BaseTool):
    """Tool that reads a file."""

    name: str = "read_file"
    args_schema: Type[BaseModel] = ReadFileInput
    description: str = "Read file from disk"

    def _run(
        self,
        file_path: str,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        del run_manager
        try:
            read_path = self.get_relative_path(file_path)
        except FileValidationError:
            return INVALID_PATH_TEMPLATE.format(arg_name="file_path", value=file_path)
        if not read_path.exists():
            return f"Error: no such file or directory: {file_path}"
        try:
            return read_path.read_text(encoding="utf-8")
        except OSError as exc:
            return f"Error: {exc}"


class WriteFileInput(BaseModel):
    """Input for WriteFileTool."""

    file_path: str = Field(..., description="name of file")
    text: str = Field(..., description="text to write to file")
    append: bool = Field(
        default=False, description="Whether to append to an existing file."
    )


class WriteFileTool(BaseFileToolMixin, BaseTool):
    """Tool that writes a file to disk."""

    name: str = "write_file"
    args_schema: Type[BaseModel] = WriteFileInput
    description: str = "Write file to disk"

    def _run(
        self,
        file_path: str,
        text: str,
        append: bool = False,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        del run_manager
        try:
            write_path = self.get_relative_path(file_path)
        except FileValidationError:
            return INVALID_PATH_TEMPLATE.format(arg_name="file_path", value=file_path)
        try:
            write_path.parent.mkdir(exist_ok=True, parents=True)
            mode = "a" if append else "w"
            with write_path.open(mode, encoding="utf-8") as handle:
                handle.write(text)
            return f"File written successfully to {file_path}."
        except OSError as exc:
            return f"Error: {exc}"


class FileCopyInput(BaseModel):
    """Input for CopyFileTool."""

    source_path: str = Field(..., description="Path of the file to copy")
    destination_path: str = Field(..., description="Path to save the copied file")


class CopyFileTool(BaseFileToolMixin, BaseTool):
    """Tool that copies a file."""

    name: str = "copy_file"
    args_schema: Type[BaseModel] = FileCopyInput
    description: str = "Create a copy of a file in a specified location"

    def _run(
        self,
        source_path: str,
        destination_path: str,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        del run_manager
        try:
            source_path_ = self.get_relative_path(source_path)
        except FileValidationError:
            return INVALID_PATH_TEMPLATE.format(
                arg_name="source_path", value=source_path
            )
        try:
            destination_path_ = self.get_relative_path(destination_path)
        except FileValidationError:
            return INVALID_PATH_TEMPLATE.format(
                arg_name="destination_path", value=destination_path
            )
        try:
            shutil.copy2(source_path_, destination_path_, follow_symlinks=False)
            return f"File copied successfully from {source_path} to {destination_path}."
        except OSError as exc:
            return f"Error: {exc}"


class FileDeleteInput(BaseModel):
    """Input for DeleteFileTool."""

    file_path: str = Field(..., description="Path of the file to delete")


class DeleteFileTool(BaseFileToolMixin, BaseTool):
    """Tool that deletes a file."""

    name: str = "file_delete"
    args_schema: Type[BaseModel] = FileDeleteInput
    description: str = "Delete a file"

    def _run(
        self,
        file_path: str,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        del run_manager
        try:
            file_path_ = self.get_relative_path(file_path)
        except FileValidationError:
            return INVALID_PATH_TEMPLATE.format(arg_name="file_path", value=file_path)
        if not file_path_.exists():
            return f"Error: no such file or directory: {file_path}"
        try:
            os.remove(file_path_)
            return f"File deleted successfully: {file_path}."
        except OSError as exc:
            return f"Error: {exc}"


class FileMoveInput(BaseModel):
    """Input for MoveFileTool."""

    source_path: str = Field(..., description="Path of the file to move")
    destination_path: str = Field(..., description="New path for the moved file")


class MoveFileTool(BaseFileToolMixin, BaseTool):
    """Tool that moves a file."""

    name: str = "move_file"
    args_schema: Type[BaseModel] = FileMoveInput
    description: str = "Move or rename a file from one location to another"

    def _run(
        self,
        source_path: str,
        destination_path: str,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        del run_manager
        try:
            source_path_ = self.get_relative_path(source_path)
        except FileValidationError:
            return INVALID_PATH_TEMPLATE.format(
                arg_name="source_path", value=source_path
            )
        try:
            destination_path_ = self.get_relative_path(destination_path)
        except FileValidationError:
            return INVALID_PATH_TEMPLATE.format(
                arg_name="destination_path", value=destination_path
            )
        if not source_path_.exists():
            return f"Error: no such file or directory {source_path}"
        try:
            shutil.move(str(source_path_), destination_path_)
            return f"File moved successfully from {source_path} to {destination_path}."
        except OSError as exc:
            return f"Error: {exc}"


class DirectoryListingInput(BaseModel):
    """Input for ListDirectoryTool."""

    dir_path: str = Field(default=".", description="Subdirectory to list.")


class ListDirectoryTool(BaseFileToolMixin, BaseTool):
    """Tool that lists files and directories in a specified folder."""

    name: str = "list_directory"
    args_schema: Type[BaseModel] = DirectoryListingInput
    description: str = "List files and directories in a specified folder"

    def _run(
        self,
        dir_path: str = ".",
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        del run_manager
        try:
            dir_path_ = self.get_relative_path(dir_path)
        except FileValidationError:
            return INVALID_PATH_TEMPLATE.format(arg_name="dir_path", value=dir_path)
        try:
            entries = os.listdir(dir_path_)
            if entries:
                return "\n".join(entries)
            return f"No files found in directory {dir_path}"
        except OSError as exc:
            return f"Error: {exc}"


class FileSearchInput(BaseModel):
    """Input for FileSearchTool."""

    dir_path: str = Field(
        default=".",
        description="Subdirectory to search in.",
    )
    pattern: str = Field(
        ...,
        description="Unix shell regex, where * matches everything.",
    )


class FileSearchTool(BaseFileToolMixin, BaseTool):
    """Tool that searches for files in a subdirectory that match a regex pattern."""

    name: str = "file_search"
    args_schema: Type[BaseModel] = FileSearchInput
    description: str = (
        "Recursively search for files in a subdirectory that match the regex pattern"
    )

    def _run(
        self,
        pattern: str,
        dir_path: str = ".",
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        del run_manager
        try:
            dir_path_ = self.get_relative_path(dir_path)
        except FileValidationError:
            return INVALID_PATH_TEMPLATE.format(arg_name="dir_path", value=dir_path)
        matches: list[str] = []
        try:
            for root, _, filenames in os.walk(dir_path_):
                for filename in fnmatch.filter(filenames, pattern):
                    absolute_path = os.path.join(root, filename)
                    relative_path = os.path.relpath(absolute_path, dir_path_)
                    matches.append(relative_path)
            if matches:
                return "\n".join(matches)
            return f"No files found for pattern {pattern} in directory {dir_path}"
        except OSError as exc:
            return f"Error: {exc}"
