"""Maven project operations used by the repository workflow.

This module deliberately keeps Maven-specific concerns in one place.  A
``Repo`` object can compose :class:`MavenRunner` and provide the project path
and changed files; the runner itself only deals with subprocesses and reports.
"""

from __future__ import annotations

import shlex
from typing import Sequence

from repository.repo import Repo
from testing.testing import run_jacoco, run_tests_with_report
from .runner import MavenRunner


def check_project_valid(project: Repo) -> MavenRunner:
    """Create and validate a Maven runner for ``project``."""
    runner = MavenRunner(project)
    runner.check_project_valid()
    return runner


def run_build(
    project: Repo,
    *,
    clean: bool = False,
) :
    """Convenience wrapper for compiling a Maven project."""
    runner = check_project_valid(project)
    return runner.build(clean=clean)


def run_tests(
    project: Repo,
    *,
    clean: bool = False,
    test_args: Sequence[str] = (),
):
    """Compatibility wrapper delegated to the testing module."""
    from testing.testing import run_tests as execute_tests

    return execute_tests(project, clean=clean, test_args=tuple(test_args))


def maven_args(command: str) -> list[str]:
    """Parse a human-written Maven command without enabling shell syntax."""
    tokens = shlex.split(command)
    if tokens and tokens[0] in {"mvn", "./mvnw", "mvnw"}:
        tokens.pop(0)
    return tokens
