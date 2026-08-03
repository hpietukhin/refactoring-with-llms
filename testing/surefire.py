"""Maven Surefire report parsing and test execution.

Public API is methods only: ``parse_surefire_reports`` and ``run_maven_tests``.
Uses ``MavenRunner`` for the Maven invocation; report parsing stays here.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from eliot import log_message

from java.runner import MavenRunner
from repository.repo import Repo

type TestStatus = Literal["PASS", "FAIL", "ERROR", "SKIPPED"]
type BuildSystem = Literal["maven"]

_SUREFIRE_REPORT_DIR = "target/surefire-reports"
_XML_TAG_TO_STATUS: dict[str, TestStatus] = {
    "failure": "FAIL",
    "error": "ERROR",
    "skipped": "SKIPPED",
}


@dataclass(slots=True)
class TestResult:
    """One Surefire testcase outcome."""

    __test__ = False

    name: str
    status: TestStatus
    duration: float = 0.0
    error_message: str | None = None
    error_type: str | None = None
    failure_trace: str | None = None


@dataclass(slots=True)
class TestCounts:
    """Aggregated counts from a test run."""

    __test__ = False

    total: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    duration: float = 0.0


@dataclass(slots=True)
class TestRunSummary:
    """Summary of one Maven test invocation with parsed Surefire results."""

    __test__ = False

    build_system: BuildSystem
    exit_code: int = 0
    tests: list[TestResult] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    counts: TestCounts = field(default_factory=TestCounts)

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and self.counts.failed == 0 and self.counts.errors == 0


def _testcase_duration(testcase: ET.Element) -> float:
    try:
        return float(testcase.get("time", "0"))
    except ValueError:
        return 0.0


def _extract_test_result(testcase: ET.Element) -> TestResult:
    name = f"{testcase.get('classname', '')}.{testcase.get('name', '')}"
    duration = _testcase_duration(testcase)
    for tag, status in _XML_TAG_TO_STATUS.items():
        element = testcase.find(tag)
        if element is not None:
            return TestResult(
                name=name,
                status=status,
                duration=duration,
                error_message=element.get("message") or None,
                error_type=element.get("type") or None,
                failure_trace=element.text,
            )
    return TestResult(name=name, status="PASS", duration=duration)


def _compute_counts(tests: list[TestResult]) -> TestCounts:
    by_status = Counter(test.status for test in tests)
    return TestCounts(
        total=len(tests),
        passed=by_status["PASS"],
        failed=by_status["FAIL"],
        errors=by_status["ERROR"],
        skipped=by_status["SKIPPED"],
        duration=sum(test.duration for test in tests),
    )


def _summary_from_process(
    project: Path,
    result: object,
    *,
    build_system: BuildSystem = "maven",
) -> TestRunSummary:
    exit_code = int(getattr(result, "returncode", -1))
    stdout = str(getattr(result, "stdout", "") or "")
    stderr = str(getattr(result, "stderr", "") or "")
    tests = parse_surefire_reports(project)
    return TestRunSummary(
        build_system=build_system,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        tests=tests,
        counts=_compute_counts(tests),
    )


def parse_surefire_reports(project_path: Path) -> list[TestResult]:
    """Parse Maven Surefire ``TEST-*.xml`` reports under the project."""
    report_dirs: list[Path] = []
    direct = project_path / _SUREFIRE_REPORT_DIR
    if direct.is_dir():
        report_dirs.append(direct)

    excluded = {".git", ".mvn", "target", "node_modules"}
    for child in sorted(project_path.iterdir()):
        if not child.is_dir() or child.name in excluded:
            continue
        module_reports = child / _SUREFIRE_REPORT_DIR
        if module_reports.is_dir():
            report_dirs.append(module_reports)

    if not report_dirs:
        report_dirs = sorted(project_path.glob(f"**/{_SUREFIRE_REPORT_DIR}"))

    results: list[TestResult] = []
    seen: set[Path] = set()
    for report_dir in report_dirs:
        resolved = report_dir.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        for xml_file in report_dir.glob("TEST-*.xml"):
            try:
                root = ET.parse(xml_file).getroot()
            except ET.ParseError as exc:
                log_message(
                    message_type="surefire:malformed",
                    path=str(xml_file),
                    exception=str(exc),
                )
                continue
            results.extend(_extract_test_result(tc) for tc in root.findall("testcase"))
    return results


def run_maven_tests(
    repo: Repo,
    *,
    clean: bool = True,
    timeout: float = 300,
    test_args: tuple[str, ...] = (),
    jacoco: bool = False,
) -> TestRunSummary:
    """Run Maven tests through ``MavenRunner`` and parse Surefire reports."""
    runner = MavenRunner(repo, timeout=timeout)
    try:
        process = runner.test(clean=clean, jacoco=jacoco, test_args=test_args)
    except (OSError, RuntimeError) as exc:
        return TestRunSummary(
            build_system="maven",
            exit_code=-1,
            stderr=str(exc),
        )
    return _summary_from_process(repo.path, process)


__all__ = [
    "TestStatus",
    "BuildSystem",
    "TestResult",
    "TestCounts",
    "TestRunSummary",
    "parse_surefire_reports",
    "run_maven_tests",
]
