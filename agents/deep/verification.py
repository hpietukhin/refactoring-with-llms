"""Automatic validation middleware for DeepAgents source edits."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from eliot import log_message
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from agents.java_test.analysis import run_java_test_analysis
from agents.deep.paths import virtual_path
from detection.organic import OrganicDetector
from repository.repo import Repo
from workflows.composite.nodes.detect import filter_smells_to_elements

EDIT_TOOLS = frozenset({"edit_file", "write_file"})


@dataclass(frozen=True, slots=True)
class VerificationFeedback:
    """Automatic verification output and its final Maven status."""

    content: str
    passed: bool


class VerificationMiddleware(AgentMiddleware):
    """Append Java verification feedback after each successful source edit."""

    def __init__(self, repo: Repo, *, elements: list[str], timeout: int) -> None:
        super().__init__()
        self._repo = repo
        self._elements = elements
        self._timeout = timeout
        self._last_verification_passed = False

    @property
    def last_verification_passed(self) -> bool:
        """Return whether the final source edit passed automatic verification."""
        return self._last_verification_passed

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        """Run validation after DeepAgents modifies a file."""
        result = handler(request)
        if (
            request.tool_call["name"] not in EDIT_TOOLS
            or not isinstance(result, ToolMessage)
            or result.status == "error"
        ):
            return result

        feedback = self._verification_feedback()
        self._last_verification_passed = feedback.passed
        return result.model_copy(
            update={"content": f"{result.content}\n\nAutomatic verification:\n{feedback.content}"}
        )

    def _verification_feedback(self) -> VerificationFeedback:
        verification = run_java_test_analysis(
            str(self._repo.path),
            clean=True,
            timeout=self._timeout,
            code_agent_max_attempts=0,
        )
        summary = verification.get("summary")
        tests_failed = verification.get("error") is not None or not bool(
            getattr(summary, "success", False)
        )
        lines = [f"tests_failed={tests_failed}"]
        if error := verification.get("error"):
            lines.append(f"verification_error={error}")
        if tests_failed:
            log_message(message_type="deep:automatic_verification", tests_failed=True)
            return VerificationFeedback(content="\n".join(lines), passed=False)

        smells = OrganicDetector().detect(self._repo.path)
        remaining = filter_smells_to_elements(smells, self._elements)
        lines.append(f"remaining_smells={len(remaining)}")
        if remaining:
            lines.extend(
                f"{smell.type}: {virtual_path(self._repo.path, smell.file_path)}:"
                f"{smell.location.range.start.line + 1}"
                for smell in remaining
            )
        else:
            lines.append("No remaining target smells.")

        log_message(
            message_type="deep:automatic_verification",
            tests_failed=False,
            remaining_smells=len(remaining),
        )
        return VerificationFeedback(content="\n".join(lines), passed=True)
