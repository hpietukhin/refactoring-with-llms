"""Result types for Maven test analysis and repair."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from testing.surefire import (
    BuildSystem,
    TestCounts,
    TestResult,
    TestRunSummary,
)


@dataclass(frozen=True, slots=True)
class RepairConfig:
    """Bounded LangGraph repair settings."""

    model: str
    step_limit: int
    max_attempts: int
    timeout: int
    target_files: list[str] | None = None


@dataclass(slots=True)
class RepairResult:
    """Outcome of one repair attempt cycle."""

    attempted: bool = False
    applied: bool = False
    reverted: bool = False
    error: str | None = None
    changed_files: list[str] = field(default_factory=list)
    steps: int | None = None
    tool_calls: int = 0
    stopped_early: bool = False
    output_excerpt: str = ""
    model: str = ""
    attempt: int | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class JavaTestAnalysisResult:
    """Stable result for stages A / D / J verification."""

    project_path: str
    build_system: BuildSystem | None
    summary: TestRunSummary | None
    command: str | None = None
    command_source: str = "default"
    verification_command: str | None = None
    code_agent_repair: dict[str, object] = field(default_factory=lambda: RepairResult().to_dict())
    pre_code_agent_exit_code: int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "project_path": self.project_path,
            "build_system": self.build_system,
            "summary": self.summary,
            "command": self.command,
            "verification_command": self.verification_command,
            "command_source": self.command_source,
            "code_agent_repair": self.code_agent_repair,
            "pre_code_agent_exit_code": self.pre_code_agent_exit_code,
            "error": self.error,
        }

    @property
    def success(self) -> bool:
        return self.error is None and self.summary is not None and self.summary.success


__all__ = [
    "BuildSystem",
    "TestCounts",
    "TestResult",
    "TestRunSummary",
    "RepairConfig",
    "RepairResult",
    "JavaTestAnalysisResult",
]
