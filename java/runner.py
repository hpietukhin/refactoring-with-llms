"""Low-level Maven command runner."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from shutil import which
from typing import Sequence

from detection.java_env import java_env_for_maven
from repository.repo import Repo


class MavenRunner:
    """Run Maven in one project without invoking a shell."""

    def __init__(
        self,
        project: Repo,
        *,
        executable: str | Path | None = None,
        timeout: float = 300,
    ) -> None:
        self.project = project.path
        self.executable = str(executable or self._find_executable())
        self.timeout = timeout

    def _find_executable(self) -> str:
        wrapper = self.project / ("mvnw.cmd" if os.name == "nt" else "mvnw")
        if wrapper.is_file():
            return str(wrapper)
        return which("mvn") or "mvn"

    def run(
        self,
        *args: str,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run Maven with argument tokens, never through a shell."""
        command = [self.executable, *(str(arg) for arg in args)]
        try:
            with java_env_for_maven(self.project) as env:
                return subprocess.run(
                    command,
                    cwd=self.project,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=self.timeout if timeout is None else timeout,
                    env=env,
                )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"Could not run {' '.join(command)}: {exc}") from exc

    def build(self, *, clean: bool = False) -> subprocess.CompletedProcess[str]:
        """Compile production code without running tests."""
        args = ("clean", "compile") if clean else ("compile",)
        return self.run(*args)

    def test(
        self,
        *,
        clean: bool = False,
        jacoco: bool = True,
        test_args: Sequence[str] = (),
    ) -> subprocess.CompletedProcess[str]:
        """Run tests and optionally ask JaCoCo to produce its XML report."""
        args: list[str] = []
        if clean:
            args.append("clean")
        args.append("test")
        if jacoco:
            args.append("jacoco:report")
        args.extend(test_args)
        return self.run(*args)

    def jacoco_report(self) -> subprocess.CompletedProcess[str]:
        """Generate JaCoCo's XML report from the latest test results."""
        return self.run("jacoco:report")

    def check_project_valid(self) -> None:
        """Validate that this is a Maven project before running commands."""
        if not self.project.is_dir():
            raise RuntimeError(f"Project directory does not exist: {self.project}")
        if not (self.project / "pom.xml").is_file():
            raise RuntimeError(f"Not a Maven project (pom.xml not found): {self.project}")
        if self.executable == "mvn" and which("mvn") is None:
            raise MavenError("Maven executable 'mvn' was not found")

    def run_tests_with_report(
        self,
        *,
        clean: bool = False,
        report: str | Path = "target/site/jacoco/jacoco.xml",
        test_args: Sequence[str] = (),
    ) -> tuple[subprocess.CompletedProcess[str], Path]:
        """Run tests and return both the process result and report path."""
        result = self.test(clean=clean, jacoco=True, test_args=test_args)
        report_path = Path(report)
        if not report_path.is_absolute():
            report_path = self.project / report_path
        return result, report_path
