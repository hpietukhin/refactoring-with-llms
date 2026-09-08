"""Testing policies and report parsing."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from config import settings
from repository.repo import Repo


@dataclass(frozen=True, slots=True)
class CoverageResult:
    """Coverage outcome for changed production files."""

    checked: int
    failed: tuple[str, ...]
    missing: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failed and not self.missing


def run_tests(
    project: Repo,
    *,
    clean: bool = False,
    test_args: tuple[str, ...] = (),
):
    """Run Gradle tests for a repository object or checkout path."""
    from java.gradle import GradleRunner

    return GradleRunner(project).test(clean=clean, jacoco=False, test_args=test_args)


def run_jacoco(project: Repo):
    """Generate JaCoCo's report from the latest test results."""
    from java.gradle import GradleRunner

    return GradleRunner(project).jacoco_report()


def run_tests_with_report(
    project: Repo,
    *,
    clean: bool = False,
    report: str | Path = "build/reports/jacoco/test/jacocoTestReport.xml",
    test_args: tuple[str, ...] = (),
):
    """Run Gradle tests and return the command result and report path."""
    from java.gradle import GradleRunner

    return GradleRunner(project).run_tests_with_report(
        clean=clean,
        report=report,
        test_args=test_args,
    )


def _report_counter(root: ET.Element, file_path: str | Path) -> tuple[int, int] | None:
    """Find a JaCoCo LINE counter by Java source path."""
    parts = Path(file_path).parts
    try:
        source_index = parts.index("src") + 2
        if parts[source_index - 1] != "main" or parts[source_index] != "java":
            return None
        relative = Path(*parts[source_index + 1 :])
    except (ValueError, IndexError):
        return None

    package = relative.parent.as_posix()
    source_name = relative.name
    for package_node in root.findall(".//package"):
        if package_node.get("name") != package:
            continue
        for source in package_node.findall("sourcefile"):
            if source.get("name") == source_name:
                counter = source.find("./counter[@type='LINE']")
                if counter is not None:
                    return int(counter.get("missed", "0")), int(counter.get("covered", "0"))
    return None


def check_coverage(
    changed_files: Iterable[str | Path],
    report: str | Path = "target/site/jacoco/jacoco.xml",
    *,
    project: Repo,
    minimum: float | None = None,
) -> CoverageResult:
    """Check JaCoCo line coverage for changed production Java files."""
    if minimum is None:
        minimum = float(settings["testing"]["min_line_coverage"])

    report_path = Path(report)
    if not report_path.is_absolute():
        report_path = project.path / report_path
    try:
        root = ET.parse(report_path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise RuntimeError(f"Could not read JaCoCo report {report_path}: {exc}") from exc

    failed: list[str] = []
    missing: list[str] = []
    checked = 0
    for raw_path in changed_files:
        path = Path(raw_path)
        if not {"src", "main", "java"}.issubset(path.parts):
            continue
        checked += 1
        counter = _report_counter(root, path)
        display = path.as_posix()
        if counter is None:
            missing.append(display)
            continue
        missed, covered = counter
        total = missed + covered
        coverage = covered / total if total else 0.0
        if coverage < minimum:
            failed.append(display)

    return CoverageResult(checked, tuple(failed), tuple(missing))