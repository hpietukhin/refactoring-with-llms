"""Tests for the minimal experiment runner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dataset.dataset import CaseRecord
from agents.deep.invoke import DeepCaseResult
from experiments import runner
from openrouter_llm import is_transient_openrouter_error


def test_initial_state_for_case_uses_langgraph_config() -> None:
    case = CaseRecord(
        case_id="Tap4j:4413ab35b400",
        project="Tap4j",
        repo_url="https://github.com/tupilabs/tap4j.git",
        start_commit="fb2451c64b4d78d1db062eac6695dab2fe1af2a2",
        elements=["org.tap4j.model.TapElement"],
        raw={"verification": {"java_source": "1.6"}},
    )
    state = runner.initial_state_for_case(case)
    assert state["project"] == "Tap4j"
    assert state["current_commit"] == case.start_commit
    assert state["elements"] == case.elements
    assert "repo_path" in state
    assert "max_steps" in state
    assert "model" in state
    assert "timeout" in state
    assert state["case_id"] == case.case_id
    assert state["use_pydantic"] is False


def test_initial_states_use_distinct_experiment_checkouts() -> None:
    case = CaseRecord(
        case_id="Tap4j:4413ab35b400",
        project="Tap4j",
        repo_url="https://github.com/tupilabs/tap4j.git",
        start_commit="fb2451c64b4d78d1db062eac6695dab2fe1af2a2",
        elements=["org.tap4j.model.TapElement"],
    )

    first = runner.initial_state_for_case(case)
    second = runner.initial_state_for_case(case)

    assert first["repo_path"] != second["repo_path"]
    assert Path(str(first["repo_path"])).name.startswith("tupilabs_tap4j_")


@pytest.mark.parametrize("error", [RuntimeError("{'code': 504}"), RuntimeError("{'code': 429}")])
def test_transient_openrouter_errors_are_detected(error: RuntimeError) -> None:
    assert is_transient_openrouter_error(error)


def test_run_case_writes_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = CaseRecord(
        case_id="Tap4j:4413ab35b400",
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
    out = tmp_path / "out" / "Tap4j_4413ab35b400.json"
    assert out.is_file()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["case_id"] == case.case_id
    assert payload["stop_reason"] == "smells_cleared"
    assert payload["smell_count"] == 0


def test_run_case_executes_deep_agent_outside_composite_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = CaseRecord(
        case_id="Tap4j:4413ab35b400",
        project="Tap4j",
        repo_url="https://github.com/tupilabs/tap4j.git",
        start_commit="fb2451c64b4d78d1db062eac6695dab2fe1af2a2",
        elements=["org.tap4j.model.TapElement"],
        raw={"verification": {"java_source": "1.6"}},
    )
    calls: dict[str, object] = {}

    class FakeRepo:
        def __init__(self, path: Path) -> None:
            self.path = path

    def fake_deep(
        repo: FakeRepo,
        *,
        elements: list[str],
        model_name: str,
        timeout: int,
        case_id: str,
        profile: object,
        java_source: str | None,
    ) -> DeepCaseResult:
        calls["repo"] = repo
        calls["elements"] = elements
        calls["model_name"] = model_name
        calls["timeout"] = timeout
        calls["case_id"] = case_id
        calls["profile"] = profile
        calls["java_source"] = java_source
        return DeepCaseResult("finished", (), "smells_cleared", 1)

    monkeypatch.setattr(
        runner.Repo,
        "for_experiment",
        staticmethod(lambda repo_url, commit: FakeRepo(tmp_path / "repo")),
    )
    monkeypatch.setattr("agents.deep.invoke.invoke_deep_case_agent", fake_deep)

    summary = runner.run_case(case, use_deep=True)

    assert calls["case_id"] == case.case_id
    assert calls["elements"] == case.elements
    assert calls["java_source"] == "1.6"
    assert summary["stop_reason"] == "smells_cleared"
    assert summary["use_deep"] is True
    assert summary["use_pydantic"] is False
    assert summary["deep_profile"] == "without-planning"


def test_main_passes_pydantic_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = CaseRecord(
        case_id="Tap4j:4413ab35b400",
        project="Tap4j",
        repo_url="https://github.com/tupilabs/tap4j.git",
        start_commit="fb2451c64b4d78d1db062eac6695dab2fe1af2a2",
        elements=["org.tap4j.model.TapElement"],
    )
    seen: dict[str, object] = {}

    def fake_run_case(
        selected: CaseRecord,
        *,
        use_deep: bool = False,
        use_pydantic: bool = False,
    ) -> dict[str, object]:
        seen["case_id"] = selected.case_id
        seen["use_deep"] = use_deep
        seen["use_pydantic"] = use_pydantic
        return {
            "case_id": selected.case_id,
            "project": selected.project,
            "stop_reason": "smells_cleared",
            "step": 1,
            "smell_count": 0,
            "tests_failed": False,
            "repo_path": str(tmp_path / "repo"),
            "use_deep": use_deep,
            "use_pydantic": use_pydantic,
        }

    monkeypatch.setattr(runner, "run_case", fake_run_case)
    monkeypatch.setattr(runner, "configure_eliot", lambda cfg: tmp_path / "all.log")
    monkeypatch.setattr(runner, "experiments_output_dir", lambda: tmp_path / "out")
    monkeypatch.setattr(runner.Dataset, "load_cases", lambda self: [case])

    code = runner.main(["--case-id", case.case_id, "--pydantic"])

    assert code == 0
    assert seen["use_deep"] is False
    assert seen["use_pydantic"] is True
