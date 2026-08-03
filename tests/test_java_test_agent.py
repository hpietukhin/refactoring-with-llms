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
from agents.java_test.repair import _last_tool_names
from agents.java_test.tools import MUTATING_TOOL_NAMES, make_tools
from langchain_core.messages import AIMessage
from repository.repo import Repo
from testing.surefire import parse_surefire_reports, run_maven_tests


def _init_maven_project(root: Path) -> Repo:
    (root / "pom.xml").write_text("<project></project>\n", encoding="utf-8")
    git = GitRepo.init(root)
    git.index.add(["pom.xml"])
    git.index.commit("init")
    return Repo(root, git_repo=git)


@pytest.mark.parametrize(
    ("has_pom", "expected"),
    [(True, "maven"), (False, None)],
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


def test_run_maven_tests_uses_runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
        return subprocess.CompletedProcess(["mvn", "test"], 0, stdout="ok", stderr="")

    monkeypatch.setattr("java.runner.MavenRunner.test", fake_test)
    summary = run_maven_tests(repo, clean=True, jacoco=False)
    assert summary.success
    assert summary.counts.passed == 1


def test_run_java_test_analysis_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _init_maven_project(tmp_path)

    def fake_run_maven_tests(*args: object, **kwargs: object):
        del args, kwargs
        from testing.surefire import TestCounts, TestRunSummary

        return TestRunSummary(
            build_system="maven",
            exit_code=0,
            counts=TestCounts(total=1, passed=1),
        )

    monkeypatch.setattr("agents.java_test.analysis.run_maven_tests", fake_run_maven_tests)
    monkeypatch.setattr("agents.java_test.analysis._open_repo", lambda path: repo)
    result = run_java_test_analysis(str(tmp_path))
    assert result["build_system"] == "maven"
    assert result["summary"].success  # type: ignore[union-attr]


def test_community_file_tools_sandbox(tmp_path: Path) -> None:
    repo = _init_maven_project(tmp_path)
    src = tmp_path / "src" / "Main.java"
    src.parent.mkdir(parents=True)
    src.write_text("package a;\nclass Main {}\n", encoding="utf-8")
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


@pytest.mark.parametrize(
    ("tool_name", "expects_mutating"),
    [("write_file", True), ("read_file", False)],
)
def test_mutating_tools_trigger_test_route(tool_name: str, expects_mutating: bool) -> None:
    message = AIMessage(
        content="",
        tool_calls=[{"name": tool_name, "args": {"file_path": "a.java"}, "id": "1"}],
    )
    assert bool(_last_tool_names([message]) & MUTATING_TOOL_NAMES) is expects_mutating
