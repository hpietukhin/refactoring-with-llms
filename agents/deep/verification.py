"""Automatic validation middleware for DeepAgents source edits."""

from __future__ import annotations

import re
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeVar

from eliot import log_message
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from agents.deep.paths import virtual_path
from agents.java_test.analysis import run_java_test_analysis
from agents.java_test.diagnostics import format_verification_feedback
from detection.organic import OrganicDetector
from java.organic_feedback import OrganicFeedback
from repository.repo import Repo
from testing.surefire import TestResult, TestRunSummary, run_gradle_tests
from workflows.composite.nodes.detect import filter_smells_to_elements

_T = TypeVar("_T")

EDIT_TOOLS = frozenset({"edit_file", "write_file"})
_TIMING_APIS = ("nanotime", "currenttimemillis", "timeunit", "sleep")
_TIMING_WORDS = ("timeout", "timed out", "elapsed", "duration", "nanos", "millis", "totalt")
_SYNCHRONIZATION_APIS = (
    "synchronized",
    "countdownlatch",
    "cyclicbarrier",
    "semaphore",
    "reentrantlock",
    ".lock(",
    ".unlock(",
    ".await(",
    ".signal(",
    ".notify(",
    ".notifyall(",
    "atomic",
    "completablefuture",
    "executor",
)
_TEST_DEACTIVATION_MARKERS = (
    "@disabled",
    "@ignore",
    "@test(enabled=false",
    "assumetrue(false",
    "assumefalse(true",
    "//@test",
)


@dataclass(frozen=True, slots=True)
class VerificationFeedback:
    """Automatic verification output and its final Maven status."""

    content: str
    passed: bool
    fatal_environment_failure: bool = False
    failure_kind: Literal["jdk", "maven"] | None = None
    test_status: str = "passed"
    tests_acceptable: bool = True
    flaky_tests: tuple[str, ...] = ()
    remaining_feedback: tuple[OrganicFeedback, ...] = ()


def _drain_future(future: Future[_T]) -> None:
    """Wait for a parallel task and ignore its result or error."""
    try:
        future.result()
    except Exception:
        return


def _remaining_target_feedback(
    repo: Repo,
    elements: list[str],
) -> tuple[OrganicFeedback, ...]:
    """Run ORGANIC once and keep findings inside the case elements."""
    feedback = OrganicDetector().detect_feedback(repo.path)
    filtered_smells = set(
        filter_smells_to_elements(
            [item.smell for item in feedback],
            elements,
        )
    )
    return tuple(item for item in feedback if item.smell in filtered_smells)


def classify_environment_failure(content: str) -> Literal["jdk", "maven"] | None:
    """Classify failures that cannot be repaired with a Java source edit."""
    normalized = content.lower()
    jdk_markers = (
        "source option",
        "target option",
        "invalid source release",
        "invalid target release",
        "error: release version",
        "no compiler is provided",
        "unsupported class file major version",
        "has been compiled by a more recent version",
        "java_home environment variable is not defined correctly",
        "javac: command not found",
    )
    if any(marker in normalized for marker in jdk_markers):
        return "jdk"

    maven_markers = (
        "could not run mvn",
        "mvn: command not found",
        "no such file or directory: 'mvn'",
        "non-resolvable parent pom",
        "non-parseable pom",
        "pluginresolutionexception",
        "dependencyresolutionexception",
        "could not resolve dependencies",
        "could not transfer artifact",
        "failed to read artifact descriptor",
        "no plugin found for prefix",
        "unknown lifecycle phase",
        "there is no pom in this directory",
    )
    if any(marker in normalized for marker in maven_markers):
        return "maven"
    return None


def _maven_failure_details(
    verification: dict[str, object],
    *,
    repo_path: Path,
) -> str | None:
    """Return Maven diagnostics when the test summary is available."""
    summary = verification.get("summary")
    if not isinstance(summary, TestRunSummary):
        return None
    command = verification.get("verification_command") or verification.get("command") or "mvn test"
    return format_verification_feedback(summary, repo_path, str(command))


def _failed_tests(summary: TestRunSummary) -> list[TestResult]:
    """Return the failed and errored Surefire test cases."""
    return [test for test in summary.tests if test.status in {"FAIL", "ERROR"}]


def _split_test_name(name: str) -> tuple[str, str] | None:
    """Split a Surefire test name into its class and method."""
    test_class, separator, method = name.rpartition(".")
    if not separator or not test_class or not method:
        return None
    return test_class, method.split("[", maxsplit=1)[0]


def _is_target_test_class(test_class: str, elements: list[str]) -> bool:
    """Return whether a failed test class is one of the supplied targets."""
    normalized = test_class.replace("$", ".")
    for element in elements:
        target = element.strip().replace("$", ".")
        if "(" in target:
            target = target.rpartition(".")[0]
        if normalized == target or normalized.endswith(f".{target}"):
            return True
    return False


def _test_source(repo_path: Path, test_class: str) -> Path | None:
    """Find the Java source file for a Surefire test class."""
    outer_class = test_class.split("$", maxsplit=1)[0]
    relative = Path(*outer_class.split(".")).with_suffix(".java")
    matches = [
        path
        for path in repo_path.glob(f"**/{relative.as_posix()}")
        if "target" not in path.parts and path.is_file()
    ]
    test_matches = [path for path in matches if "test" in path.parts]
    candidates = test_matches or matches
    return sorted(candidates)[0] if candidates else None


def _is_changed_source(repo: Repo, source: Path) -> bool:
    """Return whether ``source`` is among the current Java edits."""
    changed = {
        path if path.is_absolute() else repo.path / path
        for path in repo.filter_java_edits(include_untracked=True)
    }
    return source.resolve() in {path.resolve() for path in changed}


def _test_deactivation_edits(repo: Repo) -> tuple[str, ...]:
    """Return test files whose diff disables or removes test annotations."""
    test_files = [
        path
        for path in repo.filter_java_edits(include_untracked=True)
        if {"test", "tests"} & {part.lower() for part in path.parts}
    ]
    violations: list[str] = []
    for path in test_files:
        diff = repo.git_repo.git.diff("HEAD", "--", path.as_posix())
        added = [
            line[1:].strip().lower()
            for line in diff.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        ]
        removed = [
            line[1:].strip().lower()
            for line in diff.splitlines()
            if line.startswith("-") and not line.startswith("---")
        ]
        added_test_annotations = sum(line.startswith("@test") for line in added)
        removed_test_annotations = sum(line.startswith("@test") for line in removed)
        if (
            any(
                marker in line.replace(" ", "")
                for line in added
                for marker in _TEST_DEACTIVATION_MARKERS
            )
            or removed_test_annotations > added_test_annotations
        ):
            violations.append(path.as_posix())
    return tuple(violations)


def _method_source(source: Path, method: str) -> str:
    """Return the source block for one Java method, if it can be located."""
    text = source.read_text(encoding="utf-8", errors="replace")
    match = re.search(rf"\b{re.escape(method)}\s*\([^)]*\)\s*(?:throws\s+[^{{]+)?\{{", text)
    if match is None:
        return ""
    opening_brace = text.find("{", match.start())
    depth = 0
    for index in range(opening_brace, len(text)):
        match text[index]:
            case "{":
                depth += 1
            case "}":
                depth -= 1
                if depth == 0:
                    return text[match.start() : index + 1]
            case _:
                pass
    return ""


def _is_timing_failure(test: TestResult, source: Path, method: str) -> bool:
    """Recognize a timing-bound test that does not exercise synchronization."""
    diagnostics = "\n".join(
        part
        for part in (test.name, test.error_message, test.error_type, test.failure_trace)
        if part
    ).lower()
    method_text = _method_source(source, method).lower()
    return (
        bool(method_text)
        and any(marker in method_text for marker in _TIMING_APIS)
        and any(marker in diagnostics for marker in _TIMING_WORDS)
        and not any(marker in method_text for marker in _SYNCHRONIZATION_APIS)
    )


def _timing_flake_candidates(
    repo: Repo,
    summary: TestRunSummary,
    elements: list[str],
) -> list[tuple[TestResult, str, str]] | None:
    """Validate that every Maven failure is an unchanged, unrelated timing test."""
    build_output = f"{summary.stdout}\n{summary.stderr}".lower()
    if "compilation error" in build_output:
        return None
    failed = _failed_tests(summary)
    if not failed or len(failed) != summary.counts.failed + summary.counts.errors:
        return None

    candidates: list[tuple[TestResult, str, str]] = []
    for test in failed:
        identity = _split_test_name(test.name)
        if identity is None:
            return None
        test_class, method = identity
        source = _test_source(repo.path, test_class)
        if (
            _is_target_test_class(test_class, elements)
            or source is None
            or _is_changed_source(repo, source)
            or not _is_timing_failure(test, source, method)
        ):
            return None
        candidates.append((test, test_class, method))
    return candidates


def _rerun_timing_tests(
    repo: Repo,
    candidates: list[tuple[TestResult, str, str]],
    *,
    timeout: int,
) -> tuple[list[str], list[str]]:
    """Run each timing-sensitive test twice and record both outcomes."""
    names: list[str] = []
    statuses: list[str] = []
    for test, test_class, method in candidates:
        names.append(test.name)
        outcomes: list[str] = []
        test_arg = (f"-Dtest={test_class}#{method}",)
        for _ in range(2):
            rerun = run_gradle_tests(
                repo,
                clean=False,
                timeout=float(timeout),
                test_args=test_arg,
                jacoco=False,
            )
            outcomes.append("passed" if rerun.success else "failed")
        statuses.append(f"{test.name}:{'/'.join(outcomes)}")
    return names, statuses


def run_automatic_verification(
    repo: Repo,
    *,
    elements: list[str],
    timeout: int,
) -> VerificationFeedback:
    """Run Maven verification and ORGANIC smell detection for ``elements``.

    Maven tests and ORGANIC run in parallel. ORGANIC feedback is returned only
    when tests are acceptable; test failures skip sync of a new detect snapshot.
    """
    if deactivated_tests := _test_deactivation_edits(repo):
        content = "\n".join(
            (
                "tests_failed=True",
                "tests_acceptable=False",
                "test_status=test_deactivation_rejected",
                f"deactivated_test_files={','.join(deactivated_tests)}",
                "Do not disable, ignore, remove, or comment out tests.",
            )
        )
        log_message(
            message_type="deep:test_deactivation_rejected",
            files=list(deactivated_tests),
            content=content,
        )
        return VerificationFeedback(
            content=content,
            passed=False,
            test_status="test_deactivation_rejected",
            tests_acceptable=False,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        test_future = executor.submit(
            run_java_test_analysis,
            str(repo.path),
            clean=True,
            timeout=timeout,
            code_agent_max_attempts=0,
        )
        organic_future = executor.submit(_remaining_target_feedback, repo, elements)
        verification = test_future.result()
        summary = verification.get("summary")
        tests_failed = verification.get("error") is not None or not bool(
            getattr(summary, "success", False)
        )
        lines = [f"tests_failed={tests_failed}"]
        if error := verification.get("error"):
            lines.append(f"verification_error={error}")
        flaky_tests: list[str] = []
        if tests_failed:
            if details := _maven_failure_details(verification, repo_path=repo.path):
                lines.append(details)
            failure_kind = classify_environment_failure("\n".join(lines))
            if failure_kind is not None:
                lines[1:1] = [
                    "fatal_environment_failure=True",
                    f"failure_kind={failure_kind}",
                ]
            timing_candidates = (
                _timing_flake_candidates(repo, summary, elements)
                if (
                    failure_kind is None
                    and verification.get("error") is None
                    and isinstance(summary, TestRunSummary)
                )
                else None
            )
            if timing_candidates is not None:
                flaky_tests, rerun_statuses = _rerun_timing_tests(
                    repo,
                    timing_candidates,
                    timeout=timeout,
                )
                lines.extend(
                    (
                        "tests_acceptable=True",
                        "test_status=timing_flake_accepted",
                        f"flaky_tests={','.join(flaky_tests)}",
                        f"flaky_test_reruns={','.join(rerun_statuses)}",
                    )
                )
                log_message(
                    message_type="deep:timing_flake_accepted",
                    flaky_tests=flaky_tests,
                    rerun_statuses=rerun_statuses,
                )
            else:
                _drain_future(organic_future)
                content = "\n".join(lines)
                log_message(
                    message_type="deep:automatic_verification",
                    tests_failed=True,
                    tests_acceptable=False,
                    test_status="failed",
                    flaky_tests=[],
                    fatal_environment_failure=failure_kind is not None,
                    failure_kind=failure_kind,
                    content=content,
                )
                return VerificationFeedback(
                    content=content,
                    passed=False,
                    fatal_environment_failure=failure_kind is not None,
                    failure_kind=failure_kind,
                    test_status="failed",
                    tests_acceptable=False,
                )

        remaining = organic_future.result()
        lines.append(f"remaining_smells={len(remaining)}")
        if remaining:
            lines.extend(
                f"{item.smell.type}: {virtual_path(repo.path, item.smell.file_path)}:"
                f"{item.smell.location.range.start.line + 1}"
                for item in remaining
            )
        else:
            lines.append("No remaining target smells.")

        content = "\n".join(lines)
        test_status = "timing_flake_accepted" if tests_failed else "passed"
        log_message(
            message_type="deep:automatic_verification",
            tests_failed=tests_failed,
            tests_acceptable=True,
            test_status=test_status,
            flaky_tests=flaky_tests,
            remaining_smells=len(remaining),
            content=content,
        )
        return VerificationFeedback(
            content=content,
            passed=True,
            test_status=test_status,
            tests_acceptable=True,
            flaky_tests=tuple(flaky_tests),
            remaining_feedback=remaining,
        )


class VerificationMiddleware(AgentMiddleware):
    """Append Java verification feedback after each successful source edit."""

    def __init__(
        self,
        repo: Repo,
        *,
        elements: list[str],
        timeout: int,
    ) -> None:
        super().__init__()
        self._repo = repo
        self._elements = elements
        self._timeout = timeout
        self._last_verification_passed = False

    @property
    def last_verification_passed(self) -> bool:
        """Return whether the final source edit passed automatic verification."""
        return self._last_verification_passed

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        """Run validation after DeepAgents modifies a file."""
        result = handler(request)
        if (
            request.tool_call["name"] not in EDIT_TOOLS
            or not isinstance(result, ToolMessage)
            or result.status == "error"
        ):
            return result

        feedback = run_automatic_verification(
            self._repo,
            elements=self._elements,
            timeout=self._timeout,
        )
        self._last_verification_passed = feedback.passed
        return result.model_copy(
            update={"content": f"{result.content}\n\nAutomatic verification:\n{feedback.content}"}
        )
