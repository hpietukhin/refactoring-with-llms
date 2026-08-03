"""Tests for the minimal experiment runner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dataset.dataset import CaseRecord
from experiments import runner


def test_initial_state_for_case_uses_langgraph_config() -> None:
    case = CaseRecord(
        case_id="range:Tap4j:ca64460a664c",
        project="Tap4j",
        repo_url="https://github.com/tupilabs/tap4j.git",
        start_commit="fb2451c64b4d78d1db062eac6695dab2fe1af2a2",
        elements=["org.tap4j.model.TapElement"],
    )
    state = runner.initial_state_for_case(case)
    assert state["project"] == "Tap4j"
    assert state["current_commit"] == case.start_commit
    assert state["elements"] == case.elements
    assert "repo_path" in state
    assert "max_steps" in state
    assert "model" in state
    assert "timeout" in state


def test_run_case_writes_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = CaseRecord(
        case_id="range:Tap4j:ca64460a664c",
        project="Tap4j",
        repo_url="https://github.com/tupilabs/tap4j.git",
        start_commit="fb2451c64b4d78d1db062eac6695dab2fe1af2a2",
        elements=["org.tap4j.model.TapElement"],
    )

    class FakeGraph:
        def invoke(self, state: dict[str, object]) -> dict[str, object]:
            assert state["project"] == "Tap4j"
            return {
                "stop_reason": "smells_cleared",
                "step": 1,
                "smells": [],
                "tests_failed": False,
                "repo_path": str(tmp_path / "repo"),
            }

    monkeypatch.setattr(runner, "build_graph", lambda: FakeGraph())
    monkeypatch.setattr(runner, "configure_eliot", lambda cfg: tmp_path / "all.log")
    monkeypatch.setattr(runner, "experiments_output_dir", lambda: tmp_path / "out")
    monkeypatch.setattr(runner.Dataset, "load_cases", lambda self: [case])

    code = runner.main(["--case-id", case.case_id])
    assert code == 0
    out = tmp_path / "out" / "range_Tap4j_ca64460a664c.json"
    assert out.is_file()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["case_id"] == case.case_id
    assert payload["stop_reason"] == "smells_cleared"
    assert payload["smell_count"] == 0
