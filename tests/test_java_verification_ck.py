"""Tests for composite java_verification CK hook."""

from __future__ import annotations

from pathlib import Path

import pytest
from git import Repo as GitRepo

from agents.java_test.models import JavaTestAnalysisResult
from java.metrics.ck_reports import MetricsSummary, MetricsTotals
from repository.repo import Repo
from testing.surefire import TestCounts, TestRunSummary
from workflows.composite.nodes.verify import java_verification


def _init_repo(root: Path) -> Repo:
    (root / "pom.xml").write_text("<project></project>\n", encoding="utf-8")
    git = GitRepo.init(root)
    git.index.add(["pom.xml"])
    git.index.commit("init")
    return Repo(root, git_repo=git)


def test_java_verification_runs_ck_when_tests_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _init_repo(tmp_path)
    ck_calls = {"n": 0}

    def fake_analysis(project_path: str, **kwargs: object) -> dict[str, object]:
        del kwargs
        assert Path(project_path) == repo.path
        return JavaTestAnalysisResult(
            project_path=project_path,
            build_system="maven",
            summary=TestRunSummary(
                build_system="maven",
                exit_code=0,
                counts=TestCounts(total=1, passed=1),
            ),
        ).to_dict()

    def fake_ck(repo_arg: Repo, **kwargs: object) -> MetricsSummary:
        del kwargs
        ck_calls["n"] += 1
        assert repo_arg.path == repo.path
        return MetricsSummary(
            exit_code=0,
            totals=MetricsTotals(
                classes=2,
                mean_cbo=3.0,
                mean_lcom=1.0,
                mean_wmc=4.0,
                total_loc=40.0,
            ),
        )

    monkeypatch.setattr(
        "workflows.composite.nodes.verify.run_java_test_analysis",
        fake_analysis,
    )
    monkeypatch.setattr("workflows.composite.nodes.verify.run_ck_metrics", fake_ck)

    result = java_verification({"repo_path": str(repo.path), "timeout": 30})
    assert result["tests_failed"] is False
    assert ck_calls["n"] == 1
    assert result["ck_classes"] == 2
    assert result["ck_mean_cbo"] == 3.0
    assert result["ck_mean_lcom"] == 1.0
    assert result["ck_total_loc"] == 40.0


def test_java_verification_skips_ck_when_tests_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _init_repo(tmp_path)

    def fake_analysis(project_path: str, **kwargs: object) -> dict[str, object]:
        del kwargs
        return JavaTestAnalysisResult(
            project_path=project_path,
            build_system="maven",
            summary=TestRunSummary(
                build_system="maven",
                exit_code=1,
                counts=TestCounts(total=1, failed=1),
            ),
        ).to_dict()

    def boom(*args: object, **kwargs: object) -> MetricsSummary:
        del args, kwargs
        raise AssertionError("CK must not run when tests fail")

    monkeypatch.setattr(
        "workflows.composite.nodes.verify.run_java_test_analysis",
        fake_analysis,
    )
    monkeypatch.setattr("workflows.composite.nodes.verify.run_ck_metrics", boom)

    result = java_verification({"repo_path": str(repo.path), "timeout": 30})
    assert result["tests_failed"] is True
    assert "ck_classes" not in result
