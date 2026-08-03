"""Testing policies, Surefire parsing, and coverage checks."""

from testing.surefire import (
    TestCounts,
    TestResult,
    TestRunSummary,
    parse_surefire_reports,
    run_maven_tests,
)

__all__ = [
    "TestCounts",
    "TestResult",
    "TestRunSummary",
    "parse_surefire_reports",
    "run_maven_tests",
]
