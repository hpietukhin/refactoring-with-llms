"""Tests for local dataset manifest generation helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dataset.dataset import CaseRecord
from dataset.generation.datasets_config import load_manifest_generation_config
from dataset.generation.generate_manifest import (
    _connected_class_scope,
    _graph_smell_summary,
    _parse_type_quotas,
    _quota_available,
    _quotas_filled,
    _select_runtime_class_scope,
)
from dataset.generation.graph import CypherResult
from dataset.generation.manifest_io import case_to_manifest_record, make_case_id, write_manifest
from planning.ast.models import AstEdge, AstElement, AstIndex
from smell.smell import Smell


def test_make_case_id_uses_project_and_elements() -> None:
    elements = ["org.tap4j.model.TapElement", "org.tap4j.model.Plan"]
    case_id = make_case_id("Tap4j", elements)
    assert case_id.startswith("Tap4j:")
    assert len(case_id.split(":")[-1]) == 12


def test_case_to_manifest_record_includes_java_baseline_fields() -> None:
    record = case_to_manifest_record(
        {
            "case_id": "Tap4j:4413ab35b400",
            "project": "Tap4j",
            "repo_url": "https://github.com/tupilabs/tap4j.git",
            "start_commit": "fb2451c64b4d78d1db062eac6695dab2fe1af2a2",
            "elements": ["org.tap4j.model.Plan"],
            "baseline_verification": {
                "command": "mvn clean test",
                "build_system": "maven",
                "java_source": "1.6",
                "sdkman_java": "8.0.442-amzn",
                "tests_total": 10,
                "tests_failed": 0,
                "tests_errors": 0,
            },
        }
    )
    assert record["baseline"]["java_source"] == "1.6"
    assert record["baseline"]["sdkman_java"] == "8.0.442-amzn"


def test_case_to_manifest_record_uses_revamp_field_names() -> None:
    record = case_to_manifest_record(
        {
            "case_id": "Tap4j:4413ab35b400",
            "project": "Tap4j",
            "repo_url": "https://github.com/tupilabs/tap4j.git",
            "start_commit": "fb2451c64b4d78d1db062eac6695dab2fe1af2a2",
            "end_commit": "f444bc29badff0d0302872b7d6d262410426b366",
            "elements": ["org.tap4j.model.Plan"],
            "start_state": {"smells_total": 1},
            "selection": {"anchor": "org.tap4j.model.TapElement", "refactorings": 4},
        }
    )
    assert record["start_commit"] == "fb2451c64b4d78d1db062eac6695dab2fe1af2a2"
    assert record["selection"]["refactorings"] == 4
    parsed = CaseRecord.from_dict(record)
    assert parsed.start_commit == record["start_commit"]


def test_write_manifest_roundtrip(tmp_path: Path) -> None:
    cases = [
        {
            "case_id": "JUnit4:5def21bbf48a",
            "project": "JUnit4",
            "repo_url": "https://github.com/junit-team/junit4.git",
            "start_commit": "48af7e797dbefa1fc47e80394c7f74ef87c3c7ab",
            "elements": ["org.junit.runners.ParentRunner"],
            "start_state": {"smells_total": 1},
            "selection": {"anchor": "org.junit.runners.ParentRunner", "refactorings": 4},
        }
    ]
    path = tmp_path / "manifest.jsonl"
    count = write_manifest(path, cases)
    assert count == 1
    row = json.loads(path.read_text(encoding="utf-8").strip())
    assert CaseRecord.from_dict(row).case_id == cases[0]["case_id"]


def test_runtime_type_quota_helpers() -> None:
    quotas = _parse_type_quotas("2:2,3:2,4:2,5:2,6:2")
    assert quotas == {2: 2, 3: 2, 4: 2, 5: 2, 6: 2}
    assert _quota_available(quotas, {2: 1}, 2)
    assert not _quota_available(quotas, {2: 2}, 2)
    assert not _quotas_filled(quotas, {2: 2, 3: 2})
    assert _quotas_filled(quotas, {type_count: 2 for type_count in quotas})


def test_datasets_config_type_quotas_match_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NEO4J_PASSWORD", "test-password")
    config = load_manifest_generation_config()
    assert config.name == "1_10mb_verified"
    assert config.runtime_type_quotas == {2: 2, 3: 2, 4: 2, 5: 2, 6: 2}
    assert "JUnit4" in config.projects
    assert config.size_max_mib == 10.5
    assert _parse_type_quotas(config.as_namespace().runtime_type_quotas) == config.runtime_type_quotas


def test_graph_smell_summary_uses_distinct_smells_and_types() -> None:
    class RecordingGraph:
        query = ""
        params: dict[str, object] = {}

        def run(self, query: str, **params: object) -> CypherResult:
            self.query = query
            self.params = params
            return CypherResult(
                [{"smell_count": 17, "smell_types": ["LongMethod", "LazyClass"]}]
            )

    graph = RecordingGraph()
    summary = _graph_smell_summary(
        graph,  # type: ignore[arg-type]
        "JUnit4",
        ["org.junit.ParentRunner"],
        "abc",
    )
    assert "count(DISTINCT s)" in graph.query
    assert "collect(DISTINCT s.type)" in graph.query
    assert graph.params["elements"] == ["org.junit.ParentRunner"]
    assert summary == {
        "smell_count": 17,
        "smell_types": ["LazyClass", "LongMethod"],
    }


def test_connected_class_scope_projects_ast_calls_to_classes(tmp_path: Path) -> None:
    first_file = tmp_path / "src" / "First.java"
    second_file = tmp_path / "src" / "Second.java"
    index = AstIndex(
        elements=[
            AstElement(id="example.First", kind="class", file="src/First.java", startLine=1, endLine=20),
            AstElement(id="example.First.run", kind="method", file="src/First.java", startLine=5, endLine=10),
            AstElement(id="example.Second", kind="class", file="src/Second.java", startLine=1, endLine=20),
            AstElement(id="example.Second.call", kind="method", file="src/Second.java", startLine=5, endLine=10),
        ],
        edges=[
            AstEdge(source="example.First", target="example.First.run", relation="contains"),
            AstEdge(source="example.Second", target="example.Second.call", relation="contains"),
            AstEdge(source="example.First.run", target="example.Second.call", relation="calls"),
        ],
        unresolvedCalls=0,
    )
    connected, classes = _connected_class_scope(
        index,
        [(str(first_file), 6), (str(second_file), 6)],
        tmp_path,
    )
    assert connected
    assert classes == {"example.First", "example.Second"}


def test_select_runtime_class_scope_expands_from_composite_seed(
    tmp_path: Path,
) -> None:
    index = AstIndex(
        elements=[
            AstElement(id="example.First", kind="class", file="First.java", startLine=1, endLine=20),
            AstElement(id="example.Second", kind="class", file="Second.java", startLine=1, endLine=20),
        ],
        edges=[
            AstEdge(source="example.First", target="example.Second", relation="calls"),
        ],
        unresolvedCalls=0,
    )
    smells = [
        Smell.at(
            "Long Method" if index % 2 else "Feature Envy",
            str(tmp_path / ("First.java" if index < 8 else "Second.java")),
            5,
            severity="HIGH",
            detected_by="ORGANIC",
        )
        for index in range(16)
    ]
    selected = _select_runtime_class_scope(
        index,
        smells,
        ["example.First.run"],
        tmp_path,
        min_smells=15,
        max_smells=20,
        target_type_counts={2},
        max_classes=10,
    )
    assert selected is not None
    selected_smells, classes = selected
    assert len(selected_smells) == 16
    assert classes == {"example.First", "example.Second"}
