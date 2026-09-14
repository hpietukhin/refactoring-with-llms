"""Low-level Gradle command runner."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from shutil import which
from typing import Sequence

from detection.java_env import java_env_for_project
from repository.repo import Repo

GRADLE_FAILED_ONLY_INIT = Path(__file__).resolve().parent / "failed_only.init.gradle"
GRADLE_FAILED_ONLY_ARGS: tuple[str, ...] = ("-I", str(GRADLE_FAILED_ONLY_INIT))
GRADLE_TOOLCHAIN_INIT = Path(__file__).resolve().parent / "toolchain.init.gradle"
GRADLE_TOOLCHAIN_ARGS: tuple[str, ...] = (
    "-I",
    str(GRADLE_TOOLCHAIN_INIT),
    "--no-configuration-cache",
)
GRADLE_INIT_ARGS: tuple[str, ...] = (
    "init",
    "--type",
    "pom",
    "--dsl",
    "kotlin",
    "--use-defaults",
    "--overwrite",
)


class GradleRunner:
    """Run Gradle in one project without invoking a shell."""

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

    def _has_gradle_build(self) -> bool:
        return any(
            (self.project / name).is_file()
            for name in (
                "settings.gradle.kts",
                "settings.gradle",
                "build.gradle.kts",
                "build.gradle",
            )
        )

    def _find_executable(self) -> str:
        wrapper = self.project / ("gradlew.bat" if os.name == "nt" else "gradlew")
        if wrapper.is_file():
            return str(wrapper)
        return which("gradle") or "gradle"

    def ensure_gradle(self) -> None:
        """Convert a Maven POM to Gradle when no Gradle build exists yet."""
        if self._has_gradle_build():
            self.executable = str(self._find_executable())
            return
        if not (self.project / "pom.xml").is_file():
            raise RuntimeError(
                f"No Gradle build or pom.xml in {self.project}"
            )
        gradle = which("gradle") or "gradle"
        try:
            with java_env_for_project(self.project) as env:
                result = subprocess.run(
                    [gradle, *GRADLE_INIT_ARGS],
                    cwd=self.project,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=self.timeout,
                    env=env,
                )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(
                f"Could not convert Maven project to Gradle: {exc}"
            ) from exc
        if result.returncode != 0:
            raise RuntimeError(
                "gradle init failed: "
                f"{result.stderr or result.stdout}"
            )
        wrapper = self.project / ("gradlew.bat" if os.name == "nt" else "gradlew")
        if wrapper.is_file():
            wrapper.chmod(wrapper.stat().st_mode | 0o111)
        self.executable = str(self._find_executable())

    def run(
        self,
        *args: str,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run Gradle with argument tokens, never through a shell."""
        self.ensure_gradle()
        command = [self.executable, *GRADLE_TOOLCHAIN_ARGS, *(str(arg) for arg in args)]
        try:
            with java_env_for_project(self.project) as env:
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
        args = ("clean", "classes") if clean else ("classes",)
        return self.run(*args)

    def test(
        self,
        *,
        clean: bool = False,
        jacoco: bool = True,
        test_args: Sequence[str] = (),
    ) -> subprocess.CompletedProcess[str]:
        """Run tests and optionally ask JaCoCo to produce its XML report."""
        args: list[str] = [*GRADLE_FAILED_ONLY_ARGS]
        if clean:
            args.append("clean")
        args.append("test")
        if jacoco:
            args.append("jacocoTestReport")
        args.extend(test_args)
        return self.run(*args)

    def jacoco_report(self) -> subprocess.CompletedProcess[str]:
        """Generate JaCoCo's XML report from the latest test results."""
        return self.run("jacocoTestReport")

    def check_project_valid(self) -> None:
        """Validate that this project can be built with Gradle."""
        if not self.project.is_dir():
            raise RuntimeError(f"Project directory does not exist: {self.project}")
        if not self._has_gradle_build() and not (self.project / "pom.xml").is_file():
            raise RuntimeError(
                f"Not a Gradle or Maven project: {self.project}"
            )
        if self.executable == "gradle" and which("gradle") is None:
            raise RuntimeError("Gradle executable 'gradle' was not found")

    def run_tests_with_report(
        self,
        *,
        clean: bool = False,
        report: str | Path = "build/reports/jacoco/test/jacocoTestReport.xml",
        test_args: Sequence[str] = (),
    ) -> tuple[subprocess.CompletedProcess[str], Path]:
        """Run tests and return both the process result and report path."""
        result = self.test(clean=clean, jacoco=True, test_args=test_args)
        report_path = Path(report)
        if not report_path.is_absolute():
            report_path = self.project / report_path
        return result, report_path
