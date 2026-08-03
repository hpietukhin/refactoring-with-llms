"""Integration test: run the full composite graph on a manifest case.

Heavy externals (git fetch, Organic, Maven, LLM) are stubbed; every LangGraph
node still executes. Case metadata comes from ``dataset/manifest.jsonl``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from git import Repo as GitRepo

from agents.java_test.models import JavaTestAnalysisResult
from dataset.dataset import CaseRecord, Dataset
from planning.ast.models import AstElement, AstIndex
from repository.repo import Repo
from smell.smell import Smell
from testing.surefire import TestCounts, TestRunSummary
from workflows.composite.graph import build_graph


EXPECTED_NODES = (
    "workflow_start",
    "repo_checkout",
    "detect_smells",
    "baseline_verification",
    "step_start",
    "planner",
    "action_selected",
    "refactor_smell",
    "validate_refactor",
    "java_tests",
    "replan_after_action",
)

JAVA_SOURCE = """\
package org.tap4j.model;

public class TapElement {
    private String value;

    public String getValue() {
        return value;
    }
}
"""


def _tap4j_case() -> CaseRecord:
    cases = Dataset().load_cases()
    for case in cases:
        if case.case_id == "range:Tap4j:ca64460a664c":
            return case
    pytest.fail("Tap4j case missing from dataset/manifest.jsonl")


def _prepare_checkout(root: Path) -> Path:
    """Build a tiny Maven git worktree shaped like the Tap4j smell target."""
    java_dir = root / "src" / "main" / "java" / "org" / "tap4j" / "model"
    java_dir.mkdir(parents=True)
    (root / "pom.xml").write_text("<project></project>\n", encoding="utf-8")
    (java_dir / "TapElement.java").write_text(JAVA_SOURCE, encoding="utf-8")
    git = GitRepo.init(root)
    git.index.add(["pom.xml", "src/main/java/org/tap4j/model/TapElement.java"])
    git.index.commit("fixture")
    return root


def test_composite_workflow_all_nodes_on_manifest_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _tap4j_case()
    checkout = _prepare_checkout(tmp_path / "worktree")
    smell_path = checkout / "src" / "main" / "java" / "org" / "tap4j" / "model" / "TapElement.java"
    initial_smell = Smell.at(
        "Lazy Class",
        str(smell_path),
        1,
        8,
        severity="MEDIUM",
        detected_by="ORGANIC",
        commit_hash=case.start_commit,
    )
    detect_calls = {"n": 0}

    def fake_for_experiment(
        repo_url: str,
        commit_hash: str,
        *,
        checkout_path: Path | None = None,
    ) -> Repo:
        del repo_url, checkout_path
        assert commit_hash == case.start_commit
        return Repo(checkout)

    def fake_detect(self: object, source_path: Path, *, commit_hash: str | None = None) -> list[Smell]:
        del self
        assert Path(source_path) == checkout
        assert commit_hash == case.start_commit
        detect_calls["n"] += 1
        if detect_calls["n"] == 1:
            return [initial_smell]
        return []

    def fake_java_test_analysis(project_path: str, **kwargs: object) -> dict[str, object]:
        del kwargs
        assert Path(project_path) == checkout
        return JavaTestAnalysisResult(
            project_path=project_path,
            build_system="maven",
            summary=TestRunSummary(
                build_system="maven",
                exit_code=0,
                counts=TestCounts(total=1, passed=1),
            ),
        ).to_dict()

    def fake_refactor_agent(
        repo: Repo,
        smell: Smell,
        *,
        elements: list[str],
        model_name: str,
        timeout: int,
    ) -> list[str]:
        assert smell.id == initial_smell.id
        assert repo.path == checkout
        assert elements == case.elements
        assert model_name
        assert timeout == 30
        smell_path.write_text(
            JAVA_SOURCE.replace(
                "private String value;",
                "private String value; // refactored",
            ),
            encoding="utf-8",
        )
        return ["src/main/java/org/tap4j/model/TapElement.java"]

    def fake_ast_inspect(self: object) -> AstIndex:
        del self
        return AstIndex(
            elements=[
                AstElement(
                    id="org.tap4j.model.TapElement",
                    kind="class",
                    file=(
                        "src/main/java/org/tap4j/model/"
                        "TapElement.java"
                    ),
                    startLine=1,
                    endLine=8,
                )
            ],
            edges=[],
            unresolvedCalls=0,
        )

    monkeypatch.setattr(
        "workflows.composite.nodes.checkout.Repo.for_experiment",
        fake_for_experiment,
    )
    monkeypatch.setattr("detection.organic.OrganicDetector.detect", fake_detect)
    monkeypatch.setattr(
        "planning.planner.MavenRunner.inspect",
        fake_ast_inspect,
    )
    def fake_ck_metrics(repo: object, **kwargs: object) -> object:
        del kwargs
        assert Path(getattr(repo, "path")) == checkout
        from java.metrics.ck_reports import MetricsSummary, MetricsTotals

        return MetricsSummary(
            exit_code=0,
            totals=MetricsTotals(
                classes=1,
                methods=1,
                mean_cbo=1.5,
                mean_lcom=0.5,
                mean_wmc=2.0,
                total_loc=12.0,
            ),
        )

    monkeypatch.setattr(
        "workflows.composite.nodes.verify.run_java_test_analysis",
        fake_java_test_analysis,
    )
    monkeypatch.setattr(
        "workflows.composite.nodes.verify.run_ck_metrics",
        fake_ck_metrics,
    )
    monkeypatch.setattr(
        "workflows.composite.nodes.refactor._run_refactor_agent",
        fake_refactor_agent,
    )

    graph = build_graph()
    initial_state = {
        "project": case.project,
        "repo_url": case.repo_url,
        "current_commit": case.start_commit,
        "repo_path": str(checkout),
        "elements": case.elements,
        "max_steps": 2,
        "max_retries": 1,
        "model": "test-model",
        "timeout": 30,
    }

    visited: list[str] = []
    final_state: dict[str, object] = {}
    for update in graph.stream(initial_state, stream_mode="updates"):
        for node_name, payload in update.items():
            visited.append(node_name)
            if isinstance(payload, dict):
                final_state.update(payload)

    for node_name in EXPECTED_NODES:
        assert node_name in visited, f"missing node {node_name!r} in {visited}"

    assert visited.count("baseline_verification") == 1
    assert visited.count("java_tests") == 1
    assert visited.count("detect_smells") == 1
    assert detect_calls["n"] == 2  # initial detect + replan re-detect
    assert final_state.get("repo_path") == str(checkout)
    assert final_state.get("stop_reason") == "smells_cleared"
    assert final_state.get("smells") == []
    assert final_state.get("tests_failed") is False
    assert final_state.get("step") == 1
    assert final_state.get("ck_classes") == 1
    assert final_state.get("ck_mean_cbo") == 1.5
    assert final_state.get("ck_total_loc") == 12.0
    rewritten = smell_path.read_text(encoding="utf-8")
    assert "// refactored" in rewritten
