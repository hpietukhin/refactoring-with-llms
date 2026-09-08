"""Gradle runner that packages and invokes the local CK metrics tool."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from shutil import which
from typing import Sequence

from config import ROOT, settings
from detection.java_env import java_env
from repository.repo import Repo

_SHADOW_PLUGIN = 'id("com.gradleup.shadow") version "8.3.8"'
_GRADLE_INIT_ARGS = (
    "init",
    "--type",
    "pom",
    "--dsl",
    "kotlin",
    "--use-defaults",
    "--overwrite",
)


class GradleRunner:
    """Build CK with Gradle and collect metrics for one analyzed checkout."""

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

    def _has_gradle_build(self) -> bool:
        return any(
            (self.ck_dir / name).is_file()
            for name in (
                "settings.gradle.kts",
                "settings.gradle",
                "build.gradle.kts",
                "build.gradle",
            )
        )

    def _find_executable(self) -> str:
        wrapper = self.ck_dir / ("gradlew.bat" if os.name == "nt" else "gradlew")
        if wrapper.is_file():
            return str(wrapper)
        return which("gradle") or "gradle"

    def _ensure_gradle(self) -> None:
        if self._has_gradle_build():
            self.executable = str(self._find_executable())
            return
        if not (self.ck_dir / "pom.xml").is_file():
            raise RuntimeError(f"Not a Gradle or Maven CK project: {self.ck_dir}")
        gradle = which("gradle") or "gradle"
        with java_env((self.ck_dir / ".sdkmanrc", ROOT / ".sdkmanrc")) as env:
            result = subprocess.run(
                [gradle, *_GRADLE_INIT_ARGS],
                cwd=self.ck_dir,
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout,
                env=env,
            )
        if result.returncode != 0:
            raise RuntimeError(
                "gradle init failed for CK: "
                f"{result.stderr or result.stdout}"
            )
        wrapper = self.ck_dir / ("gradlew.bat" if os.name == "nt" else "gradlew")
        if wrapper.is_file():
            wrapper.chmod(wrapper.stat().st_mode | 0o111)
        self.executable = str(self._find_executable())
        self._ensure_shadow_plugin()

    def _ensure_shadow_plugin(self) -> None:
        build_file = self.ck_dir / "build.gradle.kts"
        if not build_file.is_file():
            return
        text = build_file.read_text(encoding="utf-8")
        if "com.gradleup.shadow" not in text:
            text = text.replace("plugins {", "plugins {\n    " + _SHADOW_PLUGIN, 1)
        if "shadowJar" not in text:
            text += """

tasks.shadowJar {
    archiveClassifier.set("all")
    mergeServiceFiles()
    manifest {
        attributes["Main-Class"] = "com.github.mauricioaniche.ck.Runner"
    }
}
"""
        if "testLogging" not in text:
            text += """

tasks.withType<Test>().configureEach {
    testLogging {
        events("failed")
        exceptionFormat = org.gradle.api.tasks.testing.logging.TestExceptionFormat.FULL
        showStandardStreams = false
    }
}
"""
        build_file.write_text(text, encoding="utf-8")

    def run(
        self,
        *args: str,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run Gradle against the CK tool project, never through a shell."""
        self._ensure_gradle()
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
        """Validate that the local CK project is present."""
        if not self.ck_dir.is_dir():
            raise RuntimeError(f"CK directory does not exist: {self.ck_dir}")
        if not self._has_gradle_build() and not (self.ck_dir / "pom.xml").is_file():
            raise RuntimeError(f"Not a Gradle or Maven CK project: {self.ck_dir}")
        if self.executable == "gradle" and which("gradle") is None:
            raise RuntimeError("Gradle executable 'gradle' was not found")

    def package(self, *, skip_tests: bool = True) -> subprocess.CompletedProcess[str]:
        """Build the CK fat jar with Gradle."""
        self.check_ck_valid()
        args: list[str] = ["shadowJar", "-q"]
        if skip_tests:
            args.extend(["-x", "test"])
        return self.run(*args)

    def fat_jar(self) -> Path:
        """Return the packaged fat jar artifact."""
        matches = sorted(self.ck_dir.glob("build/libs/*-all.jar"))
        if not matches:
            matches = sorted(self.ck_dir.glob("build/libs/*.jar"))
            matches = [path for path in matches if "sources" not in path.name and "javadoc" not in path.name]
        if not matches:
            raise RuntimeError(
                f"CK fat jar not found under {self.ck_dir / 'build/libs'}; run package() first"
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
