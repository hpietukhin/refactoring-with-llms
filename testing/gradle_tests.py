"""Gradle test execution with JUnit XML summary parsing."""

from __future__ import annotations

import os
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from shutil import which

from detection.java_env import _apply_sdkman_java
from detection.java_version import ProjectJavaVersion, detect_project_java_version
from repository.repo import Repo
from testing.surefire import TestCounts, TestRunSummary

_GRADLE_TEST_SUMMARY = re.compile(
    r"(\d+) tests completed(?:, (\d+) failed)?(?:, (\d+) skipped)?"
)


def _gradle_executable(project: Path) -> list[str]:
    wrapper_name = "gradlew.bat" if os.name == "nt" else "gradlew"
    wrapper = project / wrapper_name
    if wrapper.is_file():
        return [str(wrapper)]
    gradle = which("gradle")
    if gradle:
        return [gradle]
    raise RuntimeError(f"No Gradle wrapper or gradle executable for {project}")


def _parse_gradle_output(stdout: str, stderr: str) -> TestCounts:
    combined = f"{stdout}\n{stderr}"
    match = _GRADLE_TEST_SUMMARY.search(combined)
    if match:
        total = int(match.group(1))
        failed = int(match.group(2) or 0)
        skipped = int(match.group(3) or 0)
        passed = max(0, total - failed - skipped)
        return TestCounts(
            total=total,
            passed=passed,
            failed=failed,
            errors=0,
            skipped=skipped,
        )

    counts = TestCounts()
    results_root = Path("build/test-results/test")
    if not results_root.is_dir():
        return counts
    for xml_path in results_root.rglob("TEST-*.xml"):
        try:
            root = ET.parse(xml_path).getroot()
        except ET.ParseError:
            continue
        for testcase in root.iter("testcase"):
            counts.total += 1
            if testcase.find("failure") is not None:
                counts.failed += 1
            elif testcase.find("error") is not None:
                counts.errors += 1
            elif testcase.find("skipped") is not None:
                counts.skipped += 1
            else:
                counts.passed += 1
    return counts


def run_gradle_tests(
    repo: Repo,
    *,
    clean: bool = False,
    timeout: float = 300,
    java_version: ProjectJavaVersion | None = None,
) -> TestRunSummary:
    """Run ``gradle test`` and parse a coarse test summary."""
    project = repo.path
    if not project.is_dir():
        raise RuntimeError(f"Project directory does not exist: {project}")

    version = java_version or detect_project_java_version(project)
    env = dict(os.environ)
    if version is not None:
        env = _apply_sdkman_java(env, version.sdkman_java)

    command = [*_gradle_executable(project), "test", "-x", "signMavenJavaPublication"]
    if clean:
        command = [*_gradle_executable(project), "clean", "test", "-x", "signMavenJavaPublication"]

    process = subprocess.run(
        command,
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env=env,
    )
    counts = _parse_gradle_output(process.stdout, process.stderr)
    if counts.total == 0 and process.returncode == 0:
        counts.passed = 0
    return TestRunSummary(
        build_system="gradle",
        exit_code=int(process.returncode),
        stdout=process.stdout,
        stderr=process.stderr,
        counts=counts,
    )
