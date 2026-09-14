"""Deterministic Gradle verification with bounded LangGraph repair."""

from __future__ import annotations

from pathlib import Path

from agents.java_test.models import (
    BuildSystem,
    JavaTestAnalysisResult,
    RepairConfig,
    RepairResult,
)
from config import settings
from detection.java_version import detect_project_java_version
from openrouter_llm import configured_model_name
from repository.repo import Repo
from testing.surefire import gradle_test_command, run_gradle_tests


def detect_build_system(project_path: str | Path) -> BuildSystem | None:
    """Return ``gradle`` when a POM or Gradle build file exists."""
    path = Path(project_path)
    if (
        (path / "pom.xml").is_file()
        or (path / "build.gradle").is_file()
        or (path / "build.gradle.kts").is_file()
    ):
        return "gradle"
    return None


def _open_repo(project_path: str | Path) -> Repo:
    path = Path(project_path).expanduser().resolve()
    return Repo(path)


def _default_repair_model() -> str:
    return configured_model_name()


def _repair_config(
    *,
    code_agent_model: str | None,
    code_agent_step_limit: int | None,
    code_agent_max_attempts: int | None,
    code_agent_timeout: int | None,
    timeout: int,
    target_files: list[str] | None,
) -> RepairConfig:
    langgraph = settings["langgraph"]
    return RepairConfig(
        model=code_agent_model or _default_repair_model(),
        step_limit=code_agent_step_limit
        if code_agent_step_limit is not None
        else int(langgraph.get("repair_step_limit", 4)),
        max_attempts=code_agent_max_attempts
        if code_agent_max_attempts is not None
        else int(langgraph.get("repair_max_attempts", 3)),
        timeout=timeout if code_agent_timeout is None else code_agent_timeout,
        target_files=target_files,
    )


def run_java_test_analysis(
    project_path: str,
    *,
    clean: bool = True,
    timeout: int = 300,
    code_agent_model: str | None = None,
    llm_repair_model: str | None = None,
    code_agent_step_limit: int | None = None,
    code_agent_max_attempts: int | None = None,
    code_agent_timeout: int | None = None,
    target_files: list[str] | None = None,
) -> dict[str, object]:
    """Convert Maven projects if needed, run Gradle tests, and repair on failure."""
    if code_agent_model is None:
        code_agent_model = llm_repair_model

    build_system = detect_build_system(project_path)
    if build_system is None:
        return JavaTestAnalysisResult(
            project_path=project_path,
            build_system=None,
            summary=None,
            error=f"No Java build system detected in {project_path}",
        ).to_dict()

    repo = _open_repo(project_path)
    detect_project_java_version(repo.path)
    test_args: tuple[str, ...] = ()
    command = gradle_test_command(clean=clean)
    command_source = "default"
    if target_files:
        command_source = "default"

    summary = run_gradle_tests(
        repo,
        clean=clean,
        timeout=float(timeout),
        test_args=test_args,
        jacoco=False,
    )

    repair_cfg = _repair_config(
        code_agent_model=code_agent_model,
        code_agent_step_limit=code_agent_step_limit,
        code_agent_max_attempts=code_agent_max_attempts,
        code_agent_timeout=code_agent_timeout,
        timeout=timeout,
        target_files=target_files,
    )

    code_agent_result = RepairResult().to_dict()
    pre_code_agent_exit_code: int | None = None
    if not summary.success and repair_cfg.max_attempts > 0:
        from agents.java_test.repair import repair_checkout

        pre_code_agent_exit_code = summary.exit_code
        code_agent_result = repair_checkout(
            repo,
            summary,
            verification_command=command,
            model_name=repair_cfg.model,
            step_limit=repair_cfg.step_limit,
            timeout=repair_cfg.timeout,
            max_attempts=repair_cfg.max_attempts,
        )
        if code_agent_result.get("applied"):
            summary = run_gradle_tests(
                repo,
                clean=clean,
                timeout=float(timeout),
                test_args=test_args,
                jacoco=False,
            )

    return JavaTestAnalysisResult(
        project_path=project_path,
        build_system=build_system,
        summary=summary,
        command=command,
        verification_command=command,
        command_source=command_source,
        code_agent_repair=code_agent_result,
        pre_code_agent_exit_code=pre_code_agent_exit_code,
    ).to_dict()
