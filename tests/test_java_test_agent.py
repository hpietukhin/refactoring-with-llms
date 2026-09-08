"""Tests for Maven test analysis and file tools."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from git import Repo as GitRepo

from agents.java_test.analysis import (
    detect_build_system,
    run_java_test_analysis,
)
from agents.java_test.diagnostics import (
    format_verification_feedback,
    parse_maven_diagnostics,
)
from agents.java_test.file_management import (
    CreatePatchTool,
    PatchSequence,
    PatchStage,
)
from agents.java_test.repair import _last_tool_names
from agents.java_test.tool_logging import ToolCallRepeatGuard
from agents.java_test.tools import MUTATING_TOOL_NAMES, make_tools
from langchain_core.messages import AIMessage
from repository.repo import Repo
from java.runner import (
    GRADLE_FAILED_ONLY_ARGS,
    GRADLE_TOOLCHAIN_ARGS,
    GradleRunner,
)
from testing.surefire import (
    TestCounts,
    TestResult,
    TestRunSummary,
    gradle_test_command,
    parse_surefire_reports,
    run_gradle_tests,
)


def _init_maven_project(root: Path) -> Repo:
    (root / "pom.xml").write_text("<project></project>\n", encoding="utf-8")
    git = GitRepo.init(root)
    git.index.add(["pom.xml"])
    git.index.commit("init")
    return Repo(root, git_repo=git)


@pytest.mark.parametrize(
    ("has_pom", "expected"),
    [(True, "gradle"), (False, None)],
)
def test_detect_build_system(tmp_path: Path, has_pom: bool, expected: str | None) -> None:
    if has_pom:
        (tmp_path / "pom.xml").write_text("<project></project>\n", encoding="utf-8")
        assert detect_build_system(tmp_path) == expected
    else:
        assert detect_build_system(tmp_path / "missing") is None


def test_parse_surefire_reports(tmp_path: Path) -> None:
    report_dir = tmp_path / "target" / "surefire-reports"
    report_dir.mkdir(parents=True)
    (report_dir / "TEST-Example.xml").write_text(
        """
        <testsuite>
          <testcase classname="com.Example" name="ok" time="0.1"/>
          <testcase classname="com.Example" name="bad" time="0.2">
            <failure message="boom" type="AssertionError">trace</failure>
          </testcase>
          <testcase classname="com.Example" name="err" time="0.0">
            <error message="crash" type="RuntimeException"/>
          </testcase>
        </testsuite>
        """,
        encoding="utf-8",
    )
    results = parse_surefire_reports(tmp_path)
    assert len(results) == 3
    by_name = {item.name: item for item in results}
    assert by_name["com.Example.ok"].status == "PASS"
    assert by_name["com.Example.bad"].status == "FAIL"
    assert by_name["com.Example.err"].status == "ERROR"


def test_run_java_test_analysis_no_build_system(tmp_path: Path) -> None:
    result = run_java_test_analysis(str(tmp_path))
    assert result["build_system"] is None
    assert result["error"]


def test_run_gradle_tests_uses_runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _init_maven_project(tmp_path)
    report_dir = tmp_path / "target" / "surefire-reports"
    report_dir.mkdir(parents=True)
    (report_dir / "TEST-Ok.xml").write_text(
        '<testsuite><testcase classname="A" name="t" time="0.01"/></testsuite>',
        encoding="utf-8",
    )

    def fake_test(
        self: object,
        *,
        clean: bool = False,
        jacoco: bool = True,
        test_args: object = (),
    ) -> subprocess.CompletedProcess[str]:
        del self, clean, jacoco, test_args
        return subprocess.CompletedProcess(["gradle", "test"], 0, stdout="ok", stderr="")

    monkeypatch.setattr("java.runner.GradleRunner.test", fake_test)
    summary = run_gradle_tests(repo, clean=True, jacoco=False)
    assert summary.success
    assert summary.counts.passed == 1


def test_gradle_test_command_shows_failures_only() -> None:
    command = gradle_test_command(clean=True)
    assert command.startswith("./gradlew")
    assert "-I" in command
    assert "failed_only.init.gradle" in command
    assert "clean" in command
    assert "test" in command
    assert "signMavenJavaPublication" not in command


def test_maven_runner_test_passes_failures_only_report_args(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _init_maven_project(tmp_path)
    captured: list[tuple[str, ...]] = []

    def fake_run(
        self: GradleRunner,
        *args: str,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del self, timeout
        captured.append(args)
        return subprocess.CompletedProcess(["gradle", *args], 0, stdout="", stderr="")

    monkeypatch.setattr(GradleRunner, "run", fake_run)
    GradleRunner(repo).test(clean=True, jacoco=False, test_args=("--tests", "FooTest"))
    assert captured == [
        (*GRADLE_FAILED_ONLY_ARGS, "clean", "test", "--tests", "FooTest"),
    ]


def test_gradle_run_loads_toolchain_init(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "build.gradle.kts").write_text("plugins { java }\n", encoding="utf-8")
    (tmp_path / "settings.gradle.kts").write_text(
        'rootProject.name = "demo"\n',
        encoding="utf-8",
    )
    wrapper = tmp_path / "gradlew"
    wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
    git = GitRepo.init(tmp_path)
    git.index.add(["build.gradle.kts", "settings.gradle.kts", "gradlew"])
    git.index.commit("init")
    captured: list[list[str]] = []

    def fake_run(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        del kwargs
        captured.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    GradleRunner(Repo(tmp_path, git_repo=git), executable=str(wrapper)).run("classes")
    assert captured
    command = captured[0]
    assert command[0] == str(wrapper)
    assert "-I" in command
    assert any("toolchain.init.gradle" in part for part in command)
    assert "--no-configuration-cache" in command
    assert command[-1] == "classes"


def test_format_verification_prefers_failures_only_html_report(tmp_path: Path) -> None:
    html_dir = tmp_path / "target" / "site"
    html_dir.mkdir(parents=True)
    (html_dir / "surefire-report.html").write_text(
        "<html><body><h2>Failures</h2><p>com.Example.bad AssertionError</p></body></html>",
        encoding="utf-8",
    )
    summary = TestRunSummary(
        build_system="gradle",
        exit_code=1,
        stdout="[INFO] Tests run: 1, Failures: 0, Errors: 0 - in com.Example.ok\n",
        counts=TestCounts(passed=1, failed=1),
        tests=[
            TestResult(name="com.Example.ok", status="PASS"),
            TestResult(name="com.Example.bad", status="FAIL", error_message="AssertionError"),
        ],
    )
    feedback = format_verification_feedback(summary, tmp_path, gradle_test_command())
    assert "failed_test_details:" in feedback
    assert "com.Example.bad" in feedback
    assert "com.Example.ok" not in feedback
    assert "output_tail:" not in feedback


def test_changed_files_uses_bounded_git_status(tmp_path: Path) -> None:
    repo = _init_maven_project(tmp_path)
    source = tmp_path / "Main.java"
    source.write_text("class Main {}\n", encoding="utf-8")
    repo.git_repo.index.add(["Main.java"])
    repo.git_repo.index.commit("add source")

    source.write_text("class Main { int value; }\n", encoding="utf-8")
    untracked = tmp_path / "Extra.java"
    untracked.write_text("class Extra {}\n", encoding="utf-8")

    assert repo.changed_files() == (Path("Main.java"),)
    assert set(repo.changed_files(include_untracked=True)) == {
        Path("Main.java"),
        Path("Extra.java"),
    }


def test_run_java_test_analysis_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _init_maven_project(tmp_path)

    def fake_run_gradle_tests(*args: object, **kwargs: object):
        del args, kwargs
        from testing.surefire import TestCounts, TestRunSummary

        return TestRunSummary(
            build_system="gradle",
            exit_code=0,
            counts=TestCounts(total=1, passed=1),
        )

    monkeypatch.setattr("agents.java_test.analysis.run_gradle_tests", fake_run_gradle_tests)
    monkeypatch.setattr("agents.java_test.analysis._open_repo", lambda path: repo)
    result = run_java_test_analysis(str(tmp_path))
    assert result["build_system"] == "gradle"
    assert result["summary"].success  # type: ignore[union-attr]


def test_community_file_tools_sandbox(tmp_path: Path) -> None:
    repo = _init_maven_project(tmp_path)
    src = tmp_path / "src" / "Main.java"
    src.parent.mkdir(parents=True)
    src.write_text("package a;\nclass Main {}\n", encoding="utf-8")
    repo.git_repo.index.add(["src/Main.java"])
    repo.git_repo.index.commit("add source fixture")
    tools = {tool.name: tool for tool in make_tools(repo)}
    listed = tools["list_directory"].invoke({"dir_path": "src"})
    assert "Main.java" in listed
    content = tools["read_file"].invoke({"file_path": "src/Main.java"})
    assert "class Main" in content
    references = tools["find_symbol_references"].invoke({"symbol": "Main"})
    assert "src/Main.java:2: class Main {}" in references
    no_partial_references = tools["find_symbol_references"].invoke({"symbol": "Mai"})
    assert "No Java references found" in no_partial_references
    denied = tools["read_file"].invoke({"file_path": "/etc/passwd"})
    assert "Error" in denied or "error" in denied.lower() or "not" in denied.lower()
    assert "create_patch must succeed" in tools["apply_patch"].invoke(
        {"patch_path": "src/Main.java.patch"}
    )
    assert "create_patch must succeed" in tools["verify_patch"].invoke(
        {"patch_path": "src/Main.java.patch"}
    )
    create_result = tools["create_patch"].invoke(
        {
            "file_path": "src/Main.java",
            "old_text": "class Main {}",
            "new_text": "class Main { int value; }",
        }
    )
    assert "Created and validated patch" in create_result
    assert (src.with_name("Main.java.patch")).is_file()
    apply_result = tools["apply_patch"].invoke({"patch_path": "src/Main.java.patch"})
    assert "Applied patch" in apply_result
    verify_result = tools["verify_patch"].invoke({"patch_path": "src/Main.java.patch"})
    assert "Verified patch" in verify_result
    assert "class Main { int value; }" in src.read_text(encoding="utf-8")


def test_read_file_can_return_a_line_range(tmp_path: Path) -> None:
    repo = _init_maven_project(tmp_path)
    src = tmp_path / "src" / "Main.java"
    src.parent.mkdir(parents=True)
    src.write_text("line 1\nline 2\nline 3\nline 4\n", encoding="utf-8")
    repo.git_repo.index.add(["src/Main.java"])
    repo.git_repo.index.commit("add source fixture")
    tools = {tool.name: tool for tool in make_tools(repo)}

    result = tools["read_file"].invoke(
        {"file_path": "src/Main.java", "start_line": 2, "end_line": 3}
    )

    assert result == "2 | line 2\n3 | line 3"


def test_search_text_returns_numbered_matching_lines(tmp_path: Path) -> None:
    repo = _init_maven_project(tmp_path)
    src = tmp_path / "src" / "Main.java"
    src.parent.mkdir(parents=True)
    src.write_text(
        "class Main {\n"
        "    private Executor executor;\n"
        "    void reset() {\n"
        "        executor = createExecutor();\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    tools = {tool.name: tool for tool in make_tools(repo)}

    result = tools["search_text"].invoke(
        {"query": "executor", "path": "src/Main.java"}
    )

    assert result == (
        "src/Main.java:2:     private Executor executor;\n"
        "src/Main.java:4:         executor = createExecutor();"
    )


def test_maven_diagnostics_include_source_context(tmp_path: Path) -> None:
    source = tmp_path / "src" / "Main.java"
    source.parent.mkdir(parents=True)
    source.write_text(
        "".join(f"line {line}\n" for line in range(1, 61)),
        encoding="utf-8",
    )
    output = (
        f"[ERROR] {source}:[55,17] cannot assign a value to final variable executor\n"
    )
    diagnostics = parse_maven_diagnostics(output, tmp_path)

    assert len(diagnostics) == 1
    assert diagnostics[0].file_path == "src/Main.java"
    assert diagnostics[0].line == 55
    assert diagnostics[0].column == 17
    assert diagnostics[0].message == "cannot assign a value to final variable executor"

    summary = TestRunSummary(
        build_system="gradle",
        exit_code=1,
        stdout=output,
        counts=TestCounts(),
    )
    feedback = format_verification_feedback(summary, tmp_path, "./gradlew test")

    assert "diagnostic_type=compilation_error" in feedback
    assert "file=src/Main.java line=55 column=17" in feedback
    assert "50 | line 50" in feedback
    assert "55 | line 55" in feedback
    assert "60 | line 60" in feedback


def test_tool_call_repeat_guard_blocks_consecutive_duplicate_calls() -> None:
    guard = ToolCallRepeatGuard(max_consecutive=1)

    assert not guard.is_blocked("read_file", {"file_path": "Main.java", "start_line": 55})
    assert guard.is_blocked("read_file", {"start_line": 55, "file_path": "Main.java"})
    assert not guard.is_blocked("search_text", {"query": "executor", "path": "Main.java"})
    assert not guard.is_blocked("read_file", {"file_path": "Main.java", "start_line": 55})


def test_community_file_tools_accept_absolute_paths_inside_repo(tmp_path: Path) -> None:
    repo = _init_maven_project(tmp_path)
    src = tmp_path / "src" / "Main.java"
    src.parent.mkdir(parents=True)
    src.write_text("package a;\nclass Main {}\n", encoding="utf-8")
    repo.git_repo.index.add(["src/Main.java"])
    repo.git_repo.index.commit("add source fixture")
    tools = {tool.name: tool for tool in make_tools(repo)}

    create_result = tools["create_patch"].invoke(
        {
            "file_path": str(src),
            "old_text": "class Main {}",
            "new_text": "class Main { int value; }",
        }
    )
    assert "Created and validated patch" in create_result

    patch = src.with_name("Main.java.patch")
    apply_result = tools["apply_patch"].invoke({"patch_path": str(patch)})
    assert "Applied patch" in apply_result
    verify_result = tools["verify_patch"].invoke({"patch_path": str(patch)})
    assert "Verified patch" in verify_result
    assert "class Main { int value; }" in src.read_text(encoding="utf-8")


def test_create_patch_rejects_unapplicable_tap_element_diff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _init_maven_project(tmp_path)
    source = tmp_path / "TapElement.java"
    source.write_text(
        "".join(f"line {number}\n" for number in range(1, 40)),
        encoding="utf-8",
    )
    repo.git_repo.index.add(["TapElement.java"])
    repo.git_repo.index.commit("add TapElement source")
    tool = CreatePatchTool(
        root_dir=str(tmp_path),
        sequence=PatchSequence(),
    )

    def invalid_diff(
        original: list[str],
        updated: list[str],
        *,
        fromfile: str,
        tofile: str,
    ) -> list[str]:
        del original, updated
        return [
            f"--- {fromfile}\n",
            f"+++ {tofile}\n",
            "@@ -33,1 +33,1 @@\n",
            "-line that is not in TapElement.java\n",
            "+replacement\n",
        ]

    monkeypatch.setattr("agents.java_test.file_management.difflib.unified_diff", invalid_diff)

    result = tool.invoke(
        {
            "file_path": "TapElement.java",
            "old_text": "line 33",
            "new_text": "replacement",
        }
    )

    assert result.startswith("Error: patch validation failed:")
    assert tool.sequence.attempts == []


def test_patch_sequence_allows_only_ordered_stage_transitions(tmp_path: Path) -> None:
    sequence = PatchSequence()
    source = tmp_path / "TapElement.java"
    patch = tmp_path / "TapElement.java.patch"
    sequence.record_created(patch, source)

    assert sequence.attempts[0].stage is PatchStage.CREATED
    assert sequence.mark_verified(patch) is None
    assert sequence.attempts[0].stage is PatchStage.CREATED

    applied = sequence.mark_applied(patch)
    assert applied is not None
    assert applied.stage is PatchStage.APPLIED
    assert sequence.is_applied(patch)
    assert not sequence.is_verified(patch)

    verified = sequence.mark_verified(patch)
    assert verified is not None
    assert verified.stage is PatchStage.VERIFIED
    assert sequence.is_verified(patch)
    assert sequence.mark_applied(patch) is None


@pytest.mark.parametrize(
    ("tool_name", "expects_mutating"),
    [("apply_patch", True), ("read_file", False)],
)
def test_mutating_tools_trigger_test_route(tool_name: str, expects_mutating: bool) -> None:
    message = AIMessage(
        content="",
        tool_calls=[{"name": tool_name, "args": {"file_path": "a.java"}, "id": "1"}],
    )
    assert bool(_last_tool_names([message]) & MUTATING_TOOL_NAMES) is expects_mutating
