"""Maven runner that packages and invokes the local CK metrics tool."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from shutil import which
from typing import Sequence

from config import ROOT, settings
from detection.java_env import java_env
from repository.repo import Repo


class MavenRunner:
    """Build CK with Maven and collect metrics for one analyzed checkout."""

    def __init__(
        self,
        project: Repo,
        *,
        ck_dir: str | Path | None = None,
        executable: str | Path | None = None,
        timeout: float = 300,
    ) -> None:
        self.project = project.path
        configured = settings.get("java", {}).get("ck_dir", "java/metrics/ck")
        self.ck_dir = Path(ck_dir or ROOT / configured).expanduser().resolve()
        self.executable = str(executable or self._find_executable())
        self.timeout = timeout

    def _find_executable(self) -> str:
        wrapper = self.ck_dir / ("mvnw.cmd" if os.name == "nt" else "mvnw")
        if wrapper.is_file():
            return str(wrapper)
        return which("mvn") or "mvn"

    def run(
        self,
        *args: str,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run Maven against the CK tool project, never through a shell."""
        command = [self.executable, *(str(arg) for arg in args)]
        try:
            with java_env((self.ck_dir / ".sdkmanrc", ROOT / ".sdkmanrc")) as env:
                return subprocess.run(
                    command,
                    cwd=self.ck_dir,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=self.timeout if timeout is None else timeout,
                    env=env,
                )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"Could not run {' '.join(command)}: {exc}") from exc

    def check_ck_valid(self) -> None:
        """Validate that the local CK Maven project is present."""
        if not self.ck_dir.is_dir():
            raise RuntimeError(f"CK directory does not exist: {self.ck_dir}")
        if not (self.ck_dir / "pom.xml").is_file():
            raise RuntimeError(f"Not a Maven CK project (pom.xml not found): {self.ck_dir}")
        if self.executable == "mvn" and which("mvn") is None:
            raise RuntimeError("Maven executable 'mvn' was not found")

    def package(self, *, skip_tests: bool = True) -> subprocess.CompletedProcess[str]:
        """Build the CK fat jar with Maven."""
        self.check_ck_valid()
        args: list[str] = ["-q", "package"]
        if skip_tests:
            args.insert(1, "-DskipTests")
        return self.run(*args)

    def fat_jar(self) -> Path:
        """Return the packaged ``jar-with-dependencies`` artifact."""
        matches = sorted(self.ck_dir.glob("target/ck-*-jar-with-dependencies.jar"))
        if not matches:
            raise RuntimeError(
                f"CK fat jar not found under {self.ck_dir / 'target'}; run package() first"
            )
        return matches[-1]

    def collect(
        self,
        output_dir: str | Path,
        *,
        use_jars: bool = False,
        max_files: int = 0,
        variables_and_fields: bool = False,
        ignored_directories: Sequence[str] = (),
        ensure_package: bool = True,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run CK against ``project`` and write CSV metrics into ``output_dir``."""
        if not self.project.is_dir():
            raise RuntimeError(f"Project directory does not exist: {self.project}")
        if ensure_package:
            try:
                self.fat_jar()
            except RuntimeError:
                packaged = self.package()
                if packaged.returncode != 0:
                    return packaged

        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        jar = self.fat_jar()
        command = [
            "java",
            "-jar",
            str(jar),
            str(self.project),
            str(use_jars).lower(),
            str(max_files),
            str(variables_and_fields).lower(),
            str(out),
            *(str(directory) for directory in ignored_directories),
        ]
        try:
            with java_env((self.ck_dir / ".sdkmanrc", ROOT / ".sdkmanrc")) as env:
                return subprocess.run(
                    command,
                    cwd=self.ck_dir,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=self.timeout if timeout is None else timeout,
                    env=env,
                )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"Could not run {' '.join(command)}: {exc}") from exc
