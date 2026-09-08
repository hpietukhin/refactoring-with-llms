"""LangChain file tools sandboxed to a repository root."""

from __future__ import annotations

import difflib
import fnmatch
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Type, assert_never

from langchain_core.callbacks import CallbackManagerForToolRun
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field

INVALID_PATH_TEMPLATE = (
    "Error: Access denied to {arg_name}: {value}."
    " Permission granted exclusively to the current working directory"
)
GIT_COMMAND_TIMEOUT_SECONDS = 10


class FileValidationError(ValueError):
    """Error for paths outside the root directory."""


class PatchStage(StrEnum):
    """Stages in the lifecycle of one patch attempt."""

    CREATED = "created"
    APPLIED = "applied"
    VERIFIED = "verified"


@dataclass(frozen=True)
class PatchAttempt:
    """A patch and its current lifecycle stage."""

    patch_path: Path
    source_path: Path
    stage: PatchStage


class BaseFileToolMixin(BaseModel):
    """Mixin for file system tools."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    root_dir: str | None = None
    """The final path will be chosen relative to root_dir if specified."""

    def get_relative_path(self, file_path: str) -> Path:
        """Get the relative path, returning an error if unsupported."""
        if self.root_dir is None:
            return Path(file_path)
        return get_validated_relative_path(Path(self.root_dir), file_path)


@dataclass
class PatchSequence:
    """Track successful patch stages for one repair-agent run."""

    attempts: list[PatchAttempt] = field(default_factory=list)

    def pending_patch(self) -> Path | None:
        """Return the newest created patch that has not been applied."""
        for attempt in reversed(self.attempts):
            if attempt.stage is PatchStage.CREATED:
                return attempt.patch_path
        return None

    def latest_patch(self) -> Path | None:
        """Return the newest successfully created patch."""
        return self.attempts[-1].patch_path if self.attempts else None

    def record_created(self, patch_path: Path, source_path: Path) -> None:
        """Record a patch after it passes Git validation."""
        self.attempts.append(
            PatchAttempt(patch_path, source_path, PatchStage.CREATED)
        )

    def mark_applied(self, patch_path: Path) -> PatchAttempt | None:
        """Advance a created patch to the applied stage."""
        return self._transition(patch_path, PatchStage.APPLIED)

    def mark_verified(self, patch_path: Path) -> PatchAttempt | None:
        """Advance an applied patch to the verified stage."""
        return self._transition(patch_path, PatchStage.VERIFIED)

    def _transition(
        self,
        patch_path: Path,
        target: PatchStage,
    ) -> PatchAttempt | None:
        """Advance one patch by exactly one valid lifecycle transition."""
        for index in range(len(self.attempts) - 1, -1, -1):
            attempt = self.attempts[index]
            if attempt.patch_path != patch_path:
                continue
            match attempt.stage, target:
                case PatchStage.CREATED, PatchStage.APPLIED:
                    next_stage = PatchStage.APPLIED
                case PatchStage.APPLIED, PatchStage.VERIFIED:
                    next_stage = PatchStage.VERIFIED
                case PatchStage.CREATED | PatchStage.APPLIED, _:
                    return None
                case PatchStage.VERIFIED, _:
                    return None
                case stage, _:
                    assert_never(stage)
            transitioned = PatchAttempt(
                attempt.patch_path,
                attempt.source_path,
                next_stage,
            )
            self.attempts[index] = transitioned
            return transitioned
        return None

    def source_for(self, patch_path: Path) -> Path | None:
        """Return the source file associated with a known patch."""
        for attempt in reversed(self.attempts):
            if attempt.patch_path == patch_path:
                return attempt.source_path
        return None

    def is_applied(self, patch_path: Path) -> bool:
        """Return whether a patch has reached the applied stage."""
        for attempt in reversed(self.attempts):
            if attempt.patch_path != patch_path:
                continue
            match attempt.stage:
                case PatchStage.CREATED:
                    return False
                case PatchStage.APPLIED | PatchStage.VERIFIED:
                    return True
                case stage:
                    assert_never(stage)
        return False

    def is_verified(self, patch_path: Path) -> bool:
        """Return whether a patch has reached the verified stage."""
        for attempt in reversed(self.attempts):
            if attempt.patch_path != patch_path:
                continue
            match attempt.stage:
                case PatchStage.VERIFIED:
                    return True
                case PatchStage.CREATED | PatchStage.APPLIED:
                    return False
                case stage:
                    assert_never(stage)
        return False


def get_validated_relative_path(root: Path, user_path: str) -> Path:
    """Resolve a relative path, raising an error if not within the root directory."""
    root = root.resolve()
    path = Path(user_path)
    full_path = (path if path.is_absolute() else root / path).resolve()
    if not full_path.is_relative_to(root):
        raise FileValidationError(
            f"Path {user_path} is outside of the allowed directory {root}"
        )
    return full_path


class ReadFileInput(BaseModel):
    """Input for ReadFileTool."""

    file_path: str = Field(..., description="name of file")
    start_line: int = Field(
        default=1,
        ge=1,
        description="First 1-based line to return.",
    )
    end_line: int | None = Field(
        default=None,
        ge=1,
        description="Last 1-based line to return, inclusive.",
    )


class ReadFileTool(BaseFileToolMixin, BaseTool):
    """Tool that reads a file."""

    name: str = "read_file"
    args_schema: Type[BaseModel] = ReadFileInput
    description: str = "Read file from disk"

    def _run(
        self,
        file_path: str,
        start_line: int = 1,
        end_line: int | None = None,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        del run_manager
        if end_line is not None and end_line < start_line:
            return "Error: end_line must be greater than or equal to start_line."
        try:
            read_path = self.get_relative_path(file_path)
        except FileValidationError:
            return INVALID_PATH_TEMPLATE.format(arg_name="file_path", value=file_path)
        if not read_path.exists():
            return f"Error: no such file or directory: {file_path}"
        try:
            content = read_path.read_text(encoding="utf-8")
            lines = content.splitlines()
            selected = lines[start_line - 1 : end_line]
            return "\n".join(
                f"{line_number} | {line}"
                for line_number, line in enumerate(selected, start=start_line)
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return f"Error: {exc}"


class CreatePatchInput(BaseModel):
    """Input for CreatePatchTool."""

    file_path: str = Field(..., description="Path of the file to patch")
    old_text: str = Field(..., description="Exact text to replace")
    new_text: str = Field(..., description="Replacement text")


class CreatePatchTool(BaseFileToolMixin, BaseTool):
    """Create and validate a unified diff without changing source code."""

    name: str = "create_patch"
    args_schema: Type[BaseModel] = CreatePatchInput
    description: str = (
        "Create and validate a unified diff as <file>.patch. This does not "
        "change the source file; call apply_patch next."
    )
    sequence: PatchSequence

    def _run(
        self,
        file_path: str,
        old_text: str,
        new_text: str,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        del run_manager
        try:
            source_path = self.get_relative_path(file_path)
        except FileValidationError:
            return INVALID_PATH_TEMPLATE.format(arg_name="file_path", value=file_path)
        if not source_path.is_file():
            return f"Error: no such file or directory: {file_path}"
        if not old_text:
            return "Error: old_text cannot be empty."
        if old_text == new_text:
            return "Error: old_text and new_text are identical."
        try:
            original = source_path.read_text(encoding="utf-8")
            occurrences = original.count(old_text)
            if occurrences != 1:
                return f"Error: expected one old_text match, found {occurrences}."
            updated = original.replace(old_text, new_text, 1)
            relative_source = source_path.relative_to(
                Path(self.root_dir or ".").resolve()
            ).as_posix()
            patch = "".join(
                difflib.unified_diff(
                    original.splitlines(keepends=True),
                    updated.splitlines(keepends=True),
                    fromfile=f"a/{relative_source}",
                    tofile=f"b/{relative_source}",
                )
            )
            if not patch:
                return "Error: difflib produced an empty patch."
            patch_path = source_path.with_name(f"{source_path.name}.patch")
            patch_path.write_text(patch, encoding="utf-8")
            root = Path(self.root_dir or ".").resolve()
            relative_patch = patch_path.relative_to(root)
            check = subprocess.run(
                ["git", "apply", "--check", "--", str(relative_patch)],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
                timeout=GIT_COMMAND_TIMEOUT_SECONDS,
            )
            if check.returncode != 0:
                patch_path.unlink()
                return f"Error: patch validation failed: {check.stderr.strip()}"
            self.sequence.record_created(patch_path, source_path)
            return f"Created and validated patch {patch_path.name} for {file_path}."
        except (OSError, subprocess.SubprocessError) as exc:
            return f"Error: {exc}"


class ApplyPatchInput(BaseModel):
    """Input for ApplyPatchTool."""

    patch_path: str = Field(..., description="Path of a successfully created .patch file")


class ApplyPatchTool(BaseFileToolMixin, BaseTool):
    """Apply a patch after successful creation."""

    name: str = "apply_patch"
    args_schema: Type[BaseModel] = ApplyPatchInput
    description: str = "Apply a previously created .patch file after a dry-run check."
    sequence: PatchSequence

    def _run(
        self,
        patch_path: str,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        del run_manager
        try:
            resolved_patch = self.get_relative_path(patch_path)
        except FileValidationError:
            return INVALID_PATH_TEMPLATE.format(arg_name="patch_path", value=patch_path)
        if resolved_patch.suffix != ".patch":
            return "Error: patch_path must have a .patch extension."
        source_path = self.sequence.source_for(resolved_patch)
        if source_path is None:
            return "Error: create_patch must succeed before apply_patch."
        root = Path(self.root_dir or ".").resolve()
        relative_patch = resolved_patch.relative_to(root)
        try:
            check = subprocess.run(
                ["git", "apply", "--check", "--", str(relative_patch)],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
                timeout=GIT_COMMAND_TIMEOUT_SECONDS,
            )
            if check.returncode != 0:
                return f"Error: patch validation failed: {check.stderr.strip()}"
            applied = subprocess.run(
                ["git", "apply", "--", str(relative_patch)],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
                timeout=GIT_COMMAND_TIMEOUT_SECONDS,
            )
            if applied.returncode != 0:
                return f"Error: patch application failed: {applied.stderr.strip()}"
            self.sequence.mark_applied(resolved_patch)
            return f"Applied patch {patch_path}."
        except (OSError, subprocess.SubprocessError) as exc:
            return f"Error: {exc}"


class VerifyPatchInput(BaseModel):
    """Input for VerifyPatchTool."""

    patch_path: str = Field(..., description="Path of an applied .patch file")


class VerifyPatchTool(BaseFileToolMixin, BaseTool):
    """Verify the source diff produced by an applied patch."""

    name: str = "verify_patch"
    args_schema: Type[BaseModel] = VerifyPatchInput
    description: str = "Verify the applied patch with git diff and git diff --check."
    sequence: PatchSequence

    def _run(
        self,
        patch_path: str,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        del run_manager
        try:
            resolved_patch = self.get_relative_path(patch_path)
        except FileValidationError:
            return INVALID_PATH_TEMPLATE.format(arg_name="patch_path", value=patch_path)
        source_path = self.sequence.source_for(resolved_patch)
        if source_path is None:
            return "Error: create_patch must succeed before verify_patch."
        if not self.sequence.is_applied(resolved_patch):
            return "Error: apply_patch must succeed before verify_patch."
        root = Path(self.root_dir or ".").resolve()
        relative_source = source_path.relative_to(root)
        try:
            check = subprocess.run(
                ["git", "diff", "--check", "--", str(relative_source)],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
                timeout=GIT_COMMAND_TIMEOUT_SECONDS,
            )
            if check.returncode != 0:
                return f"Error: git diff check failed: {check.stderr.strip()}"
            diff = subprocess.run(
                ["git", "diff", "--", str(relative_source)],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
                timeout=GIT_COMMAND_TIMEOUT_SECONDS,
            )
            if diff.returncode != 0:
                return f"Error: could not read git diff: {diff.stderr.strip()}"
            if not diff.stdout.strip():
                return "Error: applied patch produced no source diff."
            self.sequence.mark_verified(resolved_patch)
            return f"Verified patch {patch_path}.\n{diff.stdout}"
        except (OSError, subprocess.SubprocessError) as exc:
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
