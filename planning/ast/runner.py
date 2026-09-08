"""Build and run the Spoon AST indexer."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from shutil import which

from eliot import log_message

from config import ROOT
from detection.java_env import java_env
from planning.ast.models import AstIndex
from repository.repo import Repo


class GradleRunner:
    """Package the Spoon tool and index one Java checkout."""

    def __init__(
        self,
        project: Repo,
        *,
        executable: str | Path | None = None,
        timeout: float = 300,
    ) -> None:
        self.project = project.path
        self.tool_dir = Path(__file__).resolve().parent
        self.executable = str(executable or self._find_executable())
        self.timeout = timeout

    def _find_executable(self) -> str:
        wrapper = self.tool_dir / ("gradlew.bat" if os.name == "nt" else "gradlew")
        if wrapper.is_file():
            return str(wrapper)
        return which("gradle") or "gradle"

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run Gradle against the Spoon tool project."""
        command = [self.executable, *(str(arg) for arg in args)]
        try:
            with java_env(
                (self.tool_dir / ".sdkmanrc", ROOT / ".sdkmanrc")
            ) as env:
                return subprocess.run(
                    command,
                    cwd=self.tool_dir,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=self.timeout,
                    env=env,
                )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(
                f"Could not run {' '.join(command)}: {exc}"
            ) from exc

    def package(self) -> subprocess.CompletedProcess[str]:
        """Build the Spoon fat JAR with Gradle."""
        if not (self.tool_dir / "build.gradle.kts").is_file():
            raise RuntimeError(
                f"Spoon Gradle project is missing: {self.tool_dir / 'build.gradle.kts'}"
            )
        return self.run("shadowJar", "-q")

    def fat_jar(self) -> Path:
        """Return the packaged Spoon indexer JAR."""
        return self.tool_dir / "build" / "libs" / "spoon-ast-indexer.jar"

    def inspect(self) -> AstIndex:
        """Build an AST index for the current project state."""
        jar = self.fat_jar()
        if not jar.is_file():
            packaged = self.package()
            if packaged.returncode != 0:
                raise RuntimeError(
                    "Could not package Spoon AST indexer: "
                    f"{packaged.stderr or packaged.stdout}"
                )
        command = ["java", "-jar", str(jar), str(self.project)]
        try:
            with java_env(
                (self.tool_dir / ".sdkmanrc", ROOT / ".sdkmanrc")
            ) as env:
                process = subprocess.run(
                    command,
                    cwd=self.tool_dir,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=self.timeout,
                    env=env,
                )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(
                f"Could not run Spoon AST indexer: {exc}"
            ) from exc
        if process.returncode != 0:
            raise RuntimeError(
                "Spoon AST indexer failed: "
                f"{process.stderr or process.stdout}"
            )
        lines = [line for line in process.stdout.splitlines() if line.strip()]
        if not lines:
            raise RuntimeError("Spoon AST indexer returned no JSON")
        index = AstIndex.model_validate_json(lines[-1])
        log_message(
            message_type="planner:ast_indexed",
            elements=len(index.elements),
            edges=len(index.edges),
            unresolved_calls=index.unresolved_calls,
        )
        return index
