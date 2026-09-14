"""Structured Maven diagnostics and source context for repair agents."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from testing.surefire import TestRunSummary, surefire_failure_report_text

ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
MAVEN_PREFIX = re.compile(r"^\[(?:ERROR|WARNING|INFO)\]\s*")
BRACKETED_DIAGNOSTIC = re.compile(
    r"(?P<path>.+?\.java):\[(?P<line>\d+),(?P<column>\d+)\]\s*(?P<message>.*)"
)
COLON_DIAGNOSTIC = re.compile(
    r"(?P<path>.+?\.java):(?P<line>\d+):(?:(?P<column>\d+):)?\s*(?P<message>.*)"
)
SOURCE_CONTEXT_LINES = 5
OUTPUT_TAIL_CHARS = 4000


@dataclass(frozen=True, slots=True)
class MavenDiagnostic:
    """One compiler diagnostic parsed from Maven output."""

    file_path: str
    line: int
    column: int | None
    message: str


def _relative_diagnostic_path(raw_path: str, project_root: Path) -> str:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        return path.as_posix()
    try:
        return path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def parse_maven_diagnostics(output: str, project_root: Path) -> list[MavenDiagnostic]:
    """Parse Java file, line, column, and message from Maven output."""
    diagnostics: list[MavenDiagnostic] = []
    seen: set[tuple[str, int, int | None, str]] = set()
    clean_output = ANSI_ESCAPE.sub("", output)
    for raw_line in clean_output.splitlines():
        line = MAVEN_PREFIX.sub("", raw_line.strip())
        match = BRACKETED_DIAGNOSTIC.search(line) or COLON_DIAGNOSTIC.search(line)
        if match is None:
            continue
        column_text = match.groupdict().get("column")
        diagnostic = MavenDiagnostic(
            file_path=_relative_diagnostic_path(match.group("path"), project_root),
            line=int(match.group("line")),
            column=int(column_text) if column_text else None,
            message=match.group("message").strip(),
        )
        identity = (
            diagnostic.file_path,
            diagnostic.line,
            diagnostic.column,
            diagnostic.message,
        )
        if identity not in seen:
            diagnostics.append(diagnostic)
            seen.add(identity)
    return diagnostics


def _source_context(diagnostic: MavenDiagnostic, project_root: Path) -> str:
    source = Path(diagnostic.file_path)
    if not source.is_absolute():
        source = project_root / source
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except OSError:
        return "source_context=unavailable"
    start = max(1, diagnostic.line - SOURCE_CONTEXT_LINES)
    end = min(len(lines), diagnostic.line + SOURCE_CONTEXT_LINES)
    return "\n".join(
        f"{line_number} | {lines[line_number - 1]}"
        for line_number in range(start, end + 1)
    )


def format_verification_feedback(
    summary: TestRunSummary,
    project_root: Path,
    verification_command: str,
) -> str:
    """Format test results with structured compiler diagnostics and source lines."""
    failed_tests = [
        test.name for test in summary.tests if test.status in {"FAIL", "ERROR"}
    ]
    output = (summary.stdout or "") + "\n" + (summary.stderr or "")
    diagnostics = parse_maven_diagnostics(output, project_root)
    sections = [
        "Gradle verification result.",
        f"command={verification_command}",
        f"success={summary.success} exit_code={summary.exit_code} "
        f"passed={summary.counts.passed} failed={summary.counts.failed} "
        f"errors={summary.counts.errors}",
        f"failed_tests={failed_tests[:20]}",
    ]
    for diagnostic in diagnostics:
        column = diagnostic.column if diagnostic.column is not None else "unknown"
        sections.extend(
            [
                "diagnostic_type=compilation_error",
                f"file={diagnostic.file_path} line={diagnostic.line} column={column}",
                f"message={diagnostic.message}",
                "source_context:",
                _source_context(diagnostic, project_root),
            ]
        )
    failed_details = [
        test
        for test in summary.tests
        if test.status in {"FAIL", "ERROR"}
    ]
    if failed_details:
        sections.append("failed_test_details:")
        for test in failed_details[:20]:
            sections.append(f"{test.name}: {test.error_message or test.status}")
            if test.failure_trace:
                sections.append(test.failure_trace[-OUTPUT_TAIL_CHARS:])
    report_text = surefire_failure_report_text(project_root)
    if report_text and not failed_details:
        sections.extend(["surefire_report:", report_text[-OUTPUT_TAIL_CHARS:]])
    if diagnostics or (not failed_details and not report_text):
        sections.extend(["output_tail:", output[-OUTPUT_TAIL_CHARS:]])
    return "\n".join(sections)
