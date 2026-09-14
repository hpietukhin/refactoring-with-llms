"""Tests for the pi deep-refactor Python hooks."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agents.deep.verification import (
    VerificationFeedback,
    run_automatic_verification,
)
from agents.pi import hooks
from agents.pi.paths import append_pi_event, create_pi_run_paths, pi_worktree_path, safe_case_id
from agents.pi.smell_run import SmellRunMetrics, initialize_smell_run, smell_identity_key
from java.organic_feedback import OrganicFeedback
from java.organic_types import OrganicScope
from smell.smell import Smell


def _run_log_with_baseline(
    tmp_path: Path,
    feedback: tuple[OrganicFeedback, ...],
    *,
    start_commit: str = "abc123",
) -> str:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    run_log = run_dir / "all.log"
    run_log.write_text("", encoding="utf-8")
    initialize_smell_run(
        run_dir,
        case_id="case",
        start_commit=start_commit,
        feedback=feedback,
    )
    return str(run_log)


def test_prepare_accepts_case_id_with_spaces(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Case IDs with spaces must survive CLI argv."""

    class FakeCase:
        case_id = "PhiCode Philib:3b7222e9b466"
        project = "PhiCode Philib"
        repo_url = "https://example.com/philib.git"
        start_commit = "abc"
        elements = ("example.Main",)

    class FakeProfile:
        name = "without-planning"
        system_instructions = "Create todos."

    checkout_calls: list[dict[str, object]] = []

    def fake_for_experiment(
        url: str,
        commit: str,
        *,
        checkout_path: Path | None = None,
    ) -> SimpleNamespace:
        checkout_calls.append(
            {"url": url, "commit": commit, "checkout_path": checkout_path}
        )
        return SimpleNamespace(path=checkout_path or Path("/tmp/repo"))

    monkeypatch.setattr(hooks, "Dataset", lambda: SimpleNamespace(load_cases=lambda: [FakeCase()]))
    monkeypatch.setattr(hooks, "load_deep_agent_profile", lambda name: FakeProfile())
    monkeypatch.setattr(hooks, "langgraph_config", lambda: {"timeout_seconds": 30})
    monkeypatch.setattr(
        hooks,
        "Repo",
        SimpleNamespace(for_experiment=fake_for_experiment),
    )
    monkeypatch.setattr(hooks, "remaining_target_feedback", lambda repo, elements: ())
    monkeypatch.setattr(
        hooks,
        "build_pi_system_prompt",
        lambda instructions, *, repo_path: "prompt",
    )
    monkeypatch.setattr(hooks, "pi_worktree_path", lambda url: tmp_path / "worktrees" / "example_philib")
    monkeypatch.setattr(hooks, "pi_output_dir", lambda: tmp_path / "data" / "pi")
    monkeypatch.setattr(
        hooks,
        "append_pi_event",
        lambda *args, **kwargs: tmp_path / "data" / "pi" / "runs.jsonl",
    )
    monkeypatch.setattr(hooks, "project_relative", lambda path: str(path))
    run_dir = tmp_path / "data" / "pi" / "runs" / "example_philib" / "run-1"
    run_log = run_dir / "all.log"
    monkeypatch.setattr(
        hooks,
        "create_pi_run_paths",
        lambda repo_url: (run_dir, run_log),
    )
    monkeypatch.setattr(hooks, "configure_pi_logging", lambda run_log_arg: run_log)
    eliot_events: list[dict[str, object]] = []
    monkeypatch.setattr(hooks, "log_message", lambda **kwargs: eliot_events.append(kwargs))

    code = hooks.main(
        [
            "prepare",
            "--case-id",
            "PhiCode Philib:3b7222e9b466",
            "--profile",
            "without-planning",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert '"case_id": "PhiCode Philib:3b7222e9b466"' in out
    assert '"current_smell_key": ""' in out
    assert '"run_log":' in out
    assert str(run_log) in out
    assert checkout_calls
    assert checkout_calls[0]["checkout_path"] == tmp_path / "worktrees" / "example_philib"
    assert eliot_events[0]["message_type"] == "deep:case_start"
    assert eliot_events[0]["harness"] == "pi"


def test_build_pi_system_prompt_mentions_automatic_checks_not_builtin_tools() -> None:
    prompt = hooks.build_pi_system_prompt(
        "Create todos.",
        repo_path=Path("/tmp/repo"),
    )

    assert "Automatic verification" in prompt
    assert "ORGANIC" in prompt
    assert "advice_path" in prompt
    assert "inline refactoring advice" in prompt
    assert "Do not edit pom.xml, build.gradle, build.gradle.kts, or other build files." in prompt
    assert "If Gradle or the JDK fails" in prompt
    assert "Do not run Gradle or ORGANIC" in prompt
    assert "If a bash command times out, the case stays active" in prompt
    assert "restore the whole worktree" in prompt
    assert "implementations, subclasses, and values assigned" in prompt
    assert "remove a useful shared interface only to raise" in prompt
    assert "Treat Gradle compilation errors" in prompt
    assert "cannot find symbol" in prompt
    assert "Repository checkout: /tmp/repo" in prompt
    assert "Create todos." in prompt
    assert "read_file" not in prompt
    assert "edit_file" not in prompt
    assert "write_file" not in prompt
    assert "You must ensure that all tests pass" not in prompt



def test_parse_remaining_smells() -> None:
    assert hooks.parse_remaining_smells("tests_failed=False\nremaining_smells=3\n") == 3
    assert hooks.parse_remaining_smells("tests_failed=True\nverification_error=x") is None


def test_parse_introduced_smells() -> None:
    assert hooks.parse_introduced_smells("introduced_smells=2\n") == 2
    assert hooks.parse_introduced_smells("remaining_smells=3\n") is None


def test_build_pi_smell_tasks_are_one_per_smell() -> None:
    first = OrganicFeedback(
        smell=Smell.at(
            "Long Method",
            "/tmp/repo/src/Main.java",
            12,
            severity="HIGH",
            detected_by="ORGANIC",
        ),
        owner="example.Main.run",
        scope=OrganicScope.METHOD,
        reason="MLOC > 6.0",
        relevant_metrics=(("MethodLinesOfCode", 20.0),),
    )
    second = OrganicFeedback(
        smell=Smell.at(
            "Feature Envy",
            "/tmp/repo/src/Other.java",
            4,
            end_line=8,
            severity="MEDIUM",
            detected_by="ORGANIC",
        ),
        owner="example.Other.copy",
        scope=OrganicScope.METHOD,
        reason="CALLS_TO_example.Target > 4",
        relevant_metrics=(("CouplingIntensity", 3.0),),
        related_owner="example.Target",
        nested_overlap=True,
        nested_owners=("example.Other.Inner",),
    )
    repo = SimpleNamespace(path=Path("/tmp/repo"))
    tasks = hooks.build_pi_smell_tasks(repo, (first, second))  # type: ignore[arg-type]

    assert len(tasks) == 2
    assert "Smell 1 of 2:" in tasks[0]
    assert "Smell 2 of 2:" in tasks[1]
    assert "file_path=src/Main.java" in tasks[0]
    assert "file_path=src/Other.java" in tasks[1]
    assert "advice_path=agents/deep/smells/long-method.md" in tasks[0]
    assert "advice_path=agents/deep/smells/feature-envy.md" in tasks[1]
    assert "owner=example.Main.run" in tasks[0]
    assert "scope=method" in tasks[0]
    assert "reason=MLOC > 6.0" in tasks[0]
    assert "relevant_metrics=MethodLinesOfCode=20.0" in tasks[0]
    assert "related_owner=example.Target" in tasks[1]
    assert "nested_warning=This finding overlaps nested class finding(s)" in tasks[1]
    assert "Refactoring advice (agents/deep/smells/long-method.md):" in tasks[0]
    assert "Extract Method" in tasks[0]
    assert "Refactoring advice (agents/deep/smells/feature-envy.md):" in tasks[1]
    assert "Feature Envy" in tasks[1]
    assert "Focus only on this smell" in tasks[0]
    assert "Long Method" not in tasks[1]


def test_smell_identity_differs_when_location_changes() -> None:
    first = OrganicFeedback(
        smell=Smell.at(
            "Long Method",
            "/tmp/repo/src/Main.java",
            12,
            severity="HIGH",
            detected_by="ORGANIC",
            commit_hash="abc123",
        ),
        owner="example.Main.run",
        scope=OrganicScope.METHOD,
        reason=None,
        relevant_metrics=(),
        related_owner="example.Helper",
    )
    moved = OrganicFeedback(
        smell=Smell.at(
            "Long Method",
            "/tmp/repo/src/Main.java",
            90,
            severity="HIGH",
            detected_by="ORGANIC",
            commit_hash="abc123",
        ),
        owner="example.Main.run",
        scope=OrganicScope.METHOD,
        reason="new metrics",
        relevant_metrics=(("MethodLinesOfCode", 20.0),),
        related_owner="example.Helper",
    )
    different_owner = OrganicFeedback(
        smell=moved.smell,
        owner="example.Main.other",
        scope=OrganicScope.METHOD,
        reason=moved.reason,
        relevant_metrics=moved.relevant_metrics,
        related_owner=moved.related_owner,
    )

    assert smell_identity_key(first, "abc123") != smell_identity_key(moved, "abc123")
    assert smell_identity_key(first, "abc123") != smell_identity_key(
        different_owner,
        "abc123",
    )


def test_next_smell_exhausts_skipped_keys_without_changing_detected_count(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    feedback = OrganicFeedback(
        smell=Smell.at(
            "Long Method",
            str(tmp_path / "Main.java"),
            12,
            severity="HIGH",
            detected_by="ORGANIC",
        ),
        owner="example.Main.run",
        scope=OrganicScope.METHOD,
        reason=None,
        relevant_metrics=(),
    )
    monkeypatch.setattr(hooks, "Repo", lambda path: SimpleNamespace(path=Path(path)))
    monkeypatch.setattr(
        hooks,
        "append_pi_event",
        lambda *args, **kwargs: tmp_path / "runs.jsonl",
    )

    run_log = _run_log_with_baseline(tmp_path, (feedback,))
    payload = hooks._next_smell(
        str(tmp_path),
        ["example.Main"],
        case_id="case",
        run_log=run_log,
        skip_smell_keys={smell_identity_key(feedback, "abc123")},
    )

    assert payload["task"] == ""
    assert payload["remaining_smells"] == 1
    assert payload["introduced_smells"] == 0
    assert payload["stop_reason"] == "smells_exhausted"
    assert payload["smell_key"] == ""


def test_next_smell_clears_when_last_snapshot_is_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Empty last detect snapshot means smells_cleared; no fresh ORGANIC scan."""
    feedback = OrganicFeedback(
        smell=Smell.at(
            "Long Method",
            str(tmp_path / "Main.java"),
            12,
            severity="HIGH",
            detected_by="ORGANIC",
            commit_hash="abc123",
        ),
        owner="example.Main.run",
        scope=OrganicScope.METHOD,
        reason=None,
        relevant_metrics=(),
    )
    monkeypatch.setattr(hooks, "Repo", lambda path: SimpleNamespace(path=Path(path)))
    monkeypatch.setattr(
        hooks,
        "append_pi_event",
        lambda *args, **kwargs: tmp_path / "runs.jsonl",
    )

    run_log = _run_log_with_baseline(tmp_path, (feedback,))
    from agents.pi.smell_run import load_smell_run, run_dir_from_log, save_smell_run

    run_dir = run_dir_from_log(run_log)
    state = load_smell_run(run_dir)
    state.queue.clear()
    save_smell_run(run_dir, state)

    payload = hooks._next_smell(
        str(tmp_path),
        ["example.Main"],
        case_id="case",
        run_log=run_log,
    )

    assert payload["stop_reason"] == "smells_cleared"
    assert payload["remaining_smells"] == 0
    assert payload["task"] == ""
    assert payload["smell_key"] == ""


def test_next_smell_uses_synced_snapshot_without_redetect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    feedback = OrganicFeedback(
        smell=Smell.at(
            "Long Method",
            str(tmp_path / "Main.java"),
            12,
            severity="HIGH",
            detected_by="ORGANIC",
            commit_hash="abc123",
        ),
        owner="example.Main.run",
        scope=OrganicScope.METHOD,
        reason=None,
        relevant_metrics=(),
    )
    monkeypatch.setattr(hooks, "Repo", lambda path: SimpleNamespace(path=Path(path)))
    monkeypatch.setattr(
        hooks,
        "append_pi_event",
        lambda *args, **kwargs: tmp_path / "runs.jsonl",
    )
    monkeypatch.setattr(
        hooks,
        "build_pi_smell_task",
        lambda *args, **kwargs: "task",
    )
    monkeypatch.setattr(
        "agents.pi.smell_run.pick_next_smell",
        lambda smells, repo_path, planner=None: smells[0] if smells else None,
    )
    detect_calls = {"count": 0}

    def boom(*args: object, **kwargs: object) -> tuple[OrganicFeedback, ...]:
        detect_calls["count"] += 1
        raise AssertionError("next_smell must not re-run ORGANIC")

    monkeypatch.setattr(hooks, "remaining_target_feedback", boom)

    run_log = _run_log_with_baseline(tmp_path, (feedback,))
    payload = hooks._next_smell(
        str(tmp_path),
        ["example.Main"],
        case_id="case",
        run_log=run_log,
    )

    assert detect_calls["count"] == 0
    assert payload["stop_reason"] == "ready"
    assert payload["remaining_smells"] == 1
    assert payload["task"] == "task"
    assert payload["smell_key"] == smell_identity_key(feedback, "abc123")


def test_verify_cli_returns_feedback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from git import Repo as GitRepo

    GitRepo.init(tmp_path)
    monkeypatch.setattr(hooks, "configure_pi_logging", lambda run_log: tmp_path / "all.log")
    eliot_events: list[dict[str, object]] = []
    monkeypatch.setattr(hooks, "log_message", lambda **kwargs: eliot_events.append(kwargs))
    monkeypatch.setattr(
        hooks,
        "run_automatic_verification",
        lambda repo, *, elements, timeout: VerificationFeedback(
            content="tests_failed=False\nremaining_smells=0\nNo remaining target smells.",
            passed=True,
            remaining_feedback=(),
        ),
    )
    monkeypatch.setattr(
        hooks,
        "append_pi_event",
        lambda *args, **kwargs: tmp_path / "runs.jsonl",
    )
    monkeypatch.setattr(hooks, "project_relative", lambda path: str(path))
    baseline = tuple(
        OrganicFeedback(
            smell=Smell.at(
                "Long Method",
                str(tmp_path / f"Main{i}.java"),
                10 + i,
                severity="HIGH",
                detected_by="ORGANIC",
                commit_hash="abc123",
            ),
            owner=f"example.Main.method{i}",
            scope=OrganicScope.METHOD,
            reason=None,
            relevant_metrics=(),
        )
        for i in range(4)
    )
    run_log = _run_log_with_baseline(tmp_path, baseline)
    code = hooks.main(
        [
            "verify",
            "--repo-path",
            str(tmp_path),
            "--elements",
            "example.Main",
            "--timeout",
            "30",
            "--case-id",
            "Tap4j:4413ab35b400",
            "--run-log",
            run_log,
        ]
    )
    assert code == 0
    payload = capsys.readouterr().out
    assert '"passed": true' in payload
    assert "remaining_smells=0" in payload
    assert '"remaining_smells": 0' in payload
    assert '"fixed_smells": 4' in payload
    assert '"introduced_smells": 0' in payload
    assert '"tests_acceptable": true' in payload
    assert '"test_status": "passed"' in payload
    assert '"flaky_tests": []' in payload
    assert '"current_smell_present": null' in payload
    assert eliot_events[0]["message_type"] == "pi:verify"
    assert eliot_events[0]["case_id"] == "Tap4j:4413ab35b400"
    assert eliot_events[0]["fixed_smells"] == 4
    assert eliot_events[0]["introduced_smells"] == 0


def test_verify_reports_whether_current_smell_is_still_present(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    current = OrganicFeedback(
        smell=Smell.at(
            "Long Method",
            str(tmp_path / "Main.java"),
            12,
            severity="HIGH",
            detected_by="ORGANIC",
        ),
        owner="example.Main.run",
        scope=OrganicScope.METHOD,
        reason=None,
        relevant_metrics=(),
    )
    monkeypatch.setattr(hooks, "Repo", lambda path: SimpleNamespace(path=Path(path)))
    monkeypatch.setattr(
        hooks,
        "run_automatic_verification",
        lambda repo, *, elements, timeout: VerificationFeedback(
            content="tests_failed=False\nremaining_smells=1",
            passed=True,
            remaining_feedback=(current,),
        ),
    )
    monkeypatch.setattr(hooks, "log_message", lambda **kwargs: None)
    monkeypatch.setattr(
        hooks,
        "append_pi_event",
        lambda *args, **kwargs: tmp_path / "runs.jsonl",
    )
    monkeypatch.setattr(hooks, "project_relative", lambda path: str(path))

    run_log = _run_log_with_baseline(tmp_path, ())
    payload = hooks._verify(
        str(tmp_path),
        ["example.Main"],
        30,
        case_id="case",
        run_log=run_log,
        current_smell_key=smell_identity_key(current, "abc123"),
    )

    assert payload["current_smell_present"] is True
    assert payload["remaining_smells"] == 1
    assert payload["fixed_smells"] == 0
    assert payload["introduced_smells"] == 1


def test_verify_logs_newly_introduced_smells(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    baseline = OrganicFeedback(
        smell=Smell.at(
            "Long Method",
            str(tmp_path / "Main.java"),
            12,
            severity="HIGH",
            detected_by="ORGANIC",
            commit_hash="abc123",
        ),
        owner="example.Main.run",
        scope=OrganicScope.METHOD,
        reason=None,
        relevant_metrics=(),
    )
    introduced = OrganicFeedback(
        smell=Smell.at(
            "Feature Envy",
            str(tmp_path / "Other.java"),
            40,
            severity="MEDIUM",
            detected_by="ORGANIC",
            commit_hash="abc123",
        ),
        owner="example.Other.copy",
        scope=OrganicScope.METHOD,
        reason="CALLS_TO_example.Target > 4",
        relevant_metrics=(),
    )
    run_log = _run_log_with_baseline(tmp_path, (baseline,))
    eliot_events: list[dict[str, object]] = []
    append_events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(hooks, "Repo", lambda path: SimpleNamespace(path=Path(path)))
    monkeypatch.setattr(
        hooks,
        "run_automatic_verification",
        lambda repo, *, elements, timeout: VerificationFeedback(
            content="tests_failed=False\nremaining_smells=2",
            passed=True,
            tests_acceptable=True,
            test_status="passed",
            remaining_feedback=(baseline, introduced),
        ),
    )
    monkeypatch.setattr(hooks, "log_message", lambda **kwargs: eliot_events.append(kwargs))
    monkeypatch.setattr(
        hooks,
        "append_pi_event",
        lambda event_type, payload, **kwargs: append_events.append((event_type, dict(payload)))
        or tmp_path / "runs.jsonl",
    )
    monkeypatch.setattr(hooks, "project_relative", lambda path: str(path))

    payload = hooks._verify(
        str(tmp_path),
        ["example.Main"],
        30,
        case_id="case",
        run_log=run_log,
    )

    assert payload["introduced_smells"] == 1
    introduced_events = [
        event for event in eliot_events if event.get("message_type") == "pi:smells_introduced"
    ]
    assert len(introduced_events) == 1
    assert introduced_events[0]["newly_introduced_count"] == 1
    assert introduced_events[0]["smells"][0]["type"] == "Feature Envy"
    assert append_events[0][0] == "smells_introduced"
    assert "New ORGANIC smells (not in baseline):" in payload["content"]
    assert "Feature Envy" in payload["content"]


def test_create_pi_run_paths_uses_repo_slug_and_uuid(tmp_path: Path) -> None:
    run_dir, run_log = create_pi_run_paths(
        "https://github.com/gertvv/drugis-common.git",
        cfg={"pi": {"output_dir": str(tmp_path)}},
    )

    assert run_dir.parent.name == "gertvv_drugis-common"
    assert run_dir.name
    assert run_log == run_dir / "all.log"
    assert run_dir.is_dir()


def test_pi_worktree_paths_are_unique_under_experiments_pi() -> None:
    first = pi_worktree_path("https://github.com/PhiCode/philib.git")
    second = pi_worktree_path("https://github.com/PhiCode/philib.git")

    assert first != second
    assert first.parent.as_posix().endswith("experiments/pi/worktrees")
    assert first.name.startswith("PhiCode_philib_")


def test_append_pi_event_writes_runs_and_case_jsonl(tmp_path: Path) -> None:
    case_id = "PhiCode Philib:3b7222e9b466"
    path = append_pi_event(
        "prepare",
        {"smell_count": 2},
        case_id=case_id,
        cfg={"pi": {"output_dir": str(tmp_path)}},
    )
    assert path == tmp_path / f"{safe_case_id(case_id)}.jsonl"
    runs = (tmp_path / "runs.jsonl").read_text(encoding="utf-8")
    case_log = path.read_text(encoding="utf-8")
    assert '"event": "prepare"' in runs
    assert case_id in case_log
    assert '"smell_count": 2' in case_log


def test_run_automatic_verification_short_circuits_on_test_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import agents.deep.verification as verification
    from testing.surefire import TestCounts, TestRunSummary

    summary = TestRunSummary(
        build_system="maven",
        exit_code=1,
        counts=TestCounts(failed=0, errors=0, passed=0),
        tests=[],
        stdout="",
        stderr="[ERROR] Source option 6 is no longer supported. Use 7 or later.",
    )
    monkeypatch.setattr(
        verification,
        "run_java_test_analysis",
        lambda *args, **kwargs: {
            "summary": summary,
            "error": None,
            "verification_command": "mvn clean test",
        },
    )
    monkeypatch.setattr(
        verification,
        "_remaining_target_feedback",
        lambda repo, elements: (),
    )
    events: list[dict[str, object]] = []
    monkeypatch.setattr(verification, "log_message", lambda **kwargs: events.append(kwargs))

    feedback = run_automatic_verification(
        SimpleNamespace(
            path=tmp_path,
            filter_java_edits=lambda **kwargs: (),
        ),  # type: ignore[arg-type]
        elements=["example.Main"],
        timeout=30,
    )

    assert feedback.passed is False
    assert feedback.fatal_environment_failure is True
    assert feedback.failure_kind == "jdk"
    assert summary.success is False
    assert "tests_failed=True" in feedback.content
    assert "fatal_environment_failure=True" in feedback.content
    assert "failure_kind=jdk" in feedback.content
    assert "Source option 6 is no longer supported" in feedback.content
    assert "output_tail:" in feedback.content
    assert events[0]["message_type"] == "deep:automatic_verification"
    assert events[0]["tests_failed"] is True
    assert "Source option 6" in str(events[0]["content"])


def test_run_automatic_verification_includes_verification_error_string(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import agents.deep.verification as verification

    monkeypatch.setattr(
        verification,
        "run_java_test_analysis",
        lambda *args, **kwargs: {"summary": None, "error": "boom"},
    )
    monkeypatch.setattr(
        verification,
        "_remaining_target_feedback",
        lambda repo, elements: (),
    )
    events: list[dict[str, object]] = []
    monkeypatch.setattr(verification, "log_message", lambda **kwargs: events.append(kwargs))

    feedback = run_automatic_verification(
        SimpleNamespace(
            path=tmp_path,
            filter_java_edits=lambda **kwargs: (),
        ),  # type: ignore[arg-type]
        elements=["example.Main"],
        timeout=30,
    )

    assert feedback.passed is False
    assert feedback.fatal_environment_failure is False
    assert feedback.failure_kind is None
    assert "tests_failed=True" in feedback.content
    assert "verification_error=boom" in feedback.content
    assert events[0]["tests_failed"] is True
    assert "verification_error=boom" in str(events[0]["content"])


def test_run_automatic_verification_keeps_code_failure_recoverable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import agents.deep.verification as verification
    from testing.surefire import TestCounts, TestRunSummary

    summary = TestRunSummary(
        build_system="maven",
        exit_code=1,
        counts=TestCounts(failed=1),
        tests=[],
        stderr="[ERROR] /src/Main.java:[12,3] cannot find symbol",
    )
    monkeypatch.setattr(
        verification,
        "run_java_test_analysis",
        lambda *args, **kwargs: {
            "summary": summary,
            "error": None,
            "verification_command": "mvn clean test",
        },
    )
    monkeypatch.setattr(
        verification,
        "_remaining_target_feedback",
        lambda repo, elements: (),
    )
    monkeypatch.setattr(verification, "log_message", lambda **kwargs: None)

    feedback = run_automatic_verification(
        SimpleNamespace(
            path=tmp_path,
            filter_java_edits=lambda **kwargs: (),
        ),  # type: ignore[arg-type]
        elements=["example.Main"],
        timeout=30,
    )

    assert feedback.passed is False
    assert feedback.fatal_environment_failure is False
    assert feedback.failure_kind is None
    assert "cannot find symbol" in feedback.content


def test_run_automatic_verification_rejects_disabled_tests(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import agents.deep.verification as verification

    monkeypatch.setattr(
        verification,
        "run_java_test_analysis",
        lambda *args, **kwargs: pytest.fail("Maven must not run after test deactivation"),
    )
    monkeypatch.setattr(verification, "log_message", lambda **kwargs: None)
    repo = SimpleNamespace(
        path=tmp_path,
        filter_java_edits=lambda **kwargs: (
            Path("src/test/java/example/TargetTest.java"),
        ),
        git_repo=SimpleNamespace(
            git=SimpleNamespace(
                diff=lambda *args: "-    @Test\n+    @Disabled\n+    @Test"
            )
        ),
    )

    feedback = run_automatic_verification(
        repo,  # type: ignore[arg-type]
        elements=["example.TargetTest"],
        timeout=30,
    )

    assert feedback.passed is False
    assert feedback.tests_acceptable is False
    assert feedback.test_status == "test_deactivation_rejected"
    assert "Do not disable" in feedback.content


def test_run_automatic_verification_accepts_unrelated_timing_flake(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import agents.deep.verification as verification
    from testing.surefire import TestCounts, TestResult, TestRunSummary

    source = tmp_path / "src/test/java/other/FlakyTimingTest.java"
    source.parent.mkdir(parents=True)
    source.write_text(
        "class FlakyTimingTest { void testElapsedMillis() { "
        "long start = System.nanoTime(); /* elapsed timeout */ } }",
        encoding="utf-8",
    )
    failed_test = TestResult(
        name="other.FlakyTimingTest.testElapsedMillis",
        status="FAIL",
        error_message="elapsed millis exceeded timeout",
    )
    summary = TestRunSummary(
        build_system="maven",
        exit_code=1,
        counts=TestCounts(total=1, failed=1),
        tests=[failed_test],
        stdout="[INFO] --- maven-compiler-plugin:3.13.0:testCompile ---",
    )
    monkeypatch.setattr(
        verification,
        "run_java_test_analysis",
        lambda *args, **kwargs: {"summary": summary, "error": None},
    )
    reruns = [
        TestRunSummary(build_system="maven"),
        TestRunSummary(
            build_system="maven",
            exit_code=1,
            counts=TestCounts(total=1, failed=1),
            tests=[failed_test],
        ),
    ]
    rerun_args: list[tuple[str, ...]] = []

    def fake_run_gradle_tests(
        repo: object,
        *,
        clean: bool,
        timeout: float,
        test_args: tuple[str, ...],
        jacoco: bool,
    ) -> TestRunSummary:
        del repo, clean, timeout, jacoco
        rerun_args.append(test_args)
        return reruns.pop(0)

    monkeypatch.setattr(verification, "run_gradle_tests", fake_run_gradle_tests)
    monkeypatch.setattr(
        verification,
        "_remaining_target_feedback",
        lambda repo, elements: (),
    )
    events: list[dict[str, object]] = []
    monkeypatch.setattr(verification, "log_message", lambda **kwargs: events.append(kwargs))
    repo = SimpleNamespace(
        path=tmp_path,
        filter_java_edits=lambda **kwargs: (),
    )

    feedback = run_automatic_verification(
        repo,  # type: ignore[arg-type]
        elements=["example.Target"],
        timeout=30,
    )

    assert feedback.passed is True
    assert feedback.tests_acceptable is True
    assert feedback.test_status == "timing_flake_accepted"
    assert feedback.flaky_tests == ("other.FlakyTimingTest.testElapsedMillis",)
    assert feedback.remaining_feedback == ()
    assert rerun_args == [
        ("-Dtest=other.FlakyTimingTest#testElapsedMillis",),
        ("-Dtest=other.FlakyTimingTest#testElapsedMillis",),
    ]
    assert any(event["message_type"] == "deep:timing_flake_accepted" for event in events)
    assert "passed/failed" in feedback.content
    assert "remaining_smells=0" in feedback.content


@pytest.mark.parametrize(
    ("elements", "source_text"),
    [
        (
            ["other.FlakyTimingTest"],
            "class FlakyTimingTest { void testElapsedMillis() { "
            "long start = System.nanoTime(); /* elapsed timeout */ } }",
        ),
        (
            ["example.Target"],
            "class FlakyTimingTest { void testValue() { assert true; } }",
        ),
        (
            ["example.Target"],
            "class FlakyTimingTest { void testElapsedMillis() { "
            "CountDownLatch latch = new CountDownLatch(1); "
            "long start = System.nanoTime(); latch.await(); } }",
        ),
    ],
)
def test_run_automatic_verification_rejects_target_or_non_timing_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    elements: list[str],
    source_text: str,
) -> None:
    import agents.deep.verification as verification
    from testing.surefire import TestCounts, TestResult, TestRunSummary

    source = tmp_path / "src/test/java/other/FlakyTimingTest.java"
    source.parent.mkdir(parents=True)
    source.write_text(source_text, encoding="utf-8")
    failed_test = TestResult(
        name="other.FlakyTimingTest.testElapsedMillis",
        status="FAIL",
        error_message="elapsed millis exceeded timeout",
    )
    summary = TestRunSummary(
        build_system="maven",
        exit_code=1,
        counts=TestCounts(total=1, failed=1),
        tests=[failed_test],
    )
    monkeypatch.setattr(
        verification,
        "run_java_test_analysis",
        lambda *args, **kwargs: {"summary": summary, "error": None},
    )
    monkeypatch.setattr(
        verification,
        "_remaining_target_feedback",
        lambda repo, elements: (),
    )
    monkeypatch.setattr(
        verification,
        "run_gradle_tests",
        lambda *args, **kwargs: pytest.fail("unsafe failure must not be rerun"),
    )
    monkeypatch.setattr(verification, "log_message", lambda **kwargs: None)
    repo = SimpleNamespace(
        path=tmp_path,
        filter_java_edits=lambda **kwargs: (),
    )

    feedback = run_automatic_verification(
        repo,  # type: ignore[arg-type]
        elements=elements,
        timeout=30,
    )

    assert feedback.passed is False
    assert feedback.tests_acceptable is False
    assert feedback.test_status == "failed"
    assert feedback.flaky_tests == ()


def test_run_automatic_verification_marks_maven_failure_fatal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import agents.deep.verification as verification
    from testing.surefire import TestRunSummary

    summary = TestRunSummary(
        build_system="maven",
        exit_code=1,
        stderr="[ERROR] Non-resolvable parent POM: Could not transfer artifact",
    )
    monkeypatch.setattr(
        verification,
        "run_java_test_analysis",
        lambda *args, **kwargs: {
            "summary": summary,
            "error": None,
            "verification_command": "mvn clean test",
        },
    )
    monkeypatch.setattr(
        verification,
        "_remaining_target_feedback",
        lambda repo, elements: (),
    )
    monkeypatch.setattr(verification, "log_message", lambda **kwargs: None)

    feedback = run_automatic_verification(
        SimpleNamespace(
            path=tmp_path,
            filter_java_edits=lambda **kwargs: (),
        ),  # type: ignore[arg-type]
        elements=["example.Main"],
        timeout=30,
    )

    assert feedback.fatal_environment_failure is True
    assert feedback.failure_kind == "maven"
    assert feedback.remaining_feedback == ()


def test_run_automatic_verification_runs_clean_tests_and_returns_feedback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import agents.deep.verification as verification
    from testing.surefire import TestCounts, TestRunSummary

    summary = TestRunSummary(
        build_system="maven",
        exit_code=0,
        counts=TestCounts(passed=1),
        tests=[],
    )
    captured: dict[str, object] = {}
    remaining = (
        OrganicFeedback(
            smell=Smell.at(
                "Long Method",
                str(tmp_path / "Main.java"),
                12,
                severity="HIGH",
                detected_by="ORGANIC",
            ),
            owner="example.Main.run",
            scope=OrganicScope.METHOD,
            reason=None,
            relevant_metrics=(),
        ),
    )

    def fake_tests(*args: object, **kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"summary": summary, "error": None}

    monkeypatch.setattr(verification, "run_java_test_analysis", fake_tests)
    monkeypatch.setattr(
        verification,
        "_remaining_target_feedback",
        lambda repo, elements: remaining,
    )
    monkeypatch.setattr(verification, "log_message", lambda **kwargs: None)

    feedback = run_automatic_verification(
        SimpleNamespace(
            path=tmp_path,
            filter_java_edits=lambda **kwargs: (),
        ),  # type: ignore[arg-type]
        elements=["example.Main"],
        timeout=30,
    )

    assert captured.get("clean") is True
    assert feedback.passed is True
    assert feedback.remaining_feedback == remaining
    assert "remaining_smells=1" in feedback.content
    assert "Long Method" in feedback.content


def test_verify_skips_organic_on_verification_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    baseline = OrganicFeedback(
        smell=Smell.at(
            "Long Method",
            str(tmp_path / "Main.java"),
            12,
            severity="HIGH",
            detected_by="ORGANIC",
            commit_hash="abc123",
        ),
        owner="example.Main.run",
        scope=OrganicScope.METHOD,
        reason=None,
        relevant_metrics=(),
    )
    run_log = _run_log_with_baseline(tmp_path, (baseline,))
    detect_calls = {"count": 0}

    def boom(*args: object, **kwargs: object) -> tuple[OrganicFeedback, ...]:
        detect_calls["count"] += 1
        raise AssertionError("verify must not re-run ORGANIC after failed verification")

    monkeypatch.setattr(hooks, "Repo", lambda path: SimpleNamespace(path=Path(path)))
    monkeypatch.setattr(
        hooks,
        "run_automatic_verification",
        lambda repo, *, elements, timeout: VerificationFeedback(
            content="tests_failed=True",
            passed=False,
            tests_acceptable=False,
            test_status="failed",
        ),
    )
    monkeypatch.setattr(hooks, "remaining_target_feedback", boom)
    monkeypatch.setattr(hooks, "log_message", lambda **kwargs: None)
    monkeypatch.setattr(
        hooks,
        "append_pi_event",
        lambda *args, **kwargs: tmp_path / "runs.jsonl",
    )
    monkeypatch.setattr(hooks, "project_relative", lambda path: str(path))

    payload = hooks._verify(
        str(tmp_path),
        ["example.Main"],
        30,
        case_id="case",
        run_log=run_log,
    )

    assert detect_calls["count"] == 0
    assert payload["tests_acceptable"] is False
    assert payload["remaining_smells"] == 1
    assert payload["fixed_smells"] == 0


def test_log_case_completion_metrics_emits_summary_and_ck(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from java.metrics.ck_reports import MetricsSummary, MetricsTotals

    eliot_events: list[dict[str, object]] = []
    append_calls: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(hooks, "log_message", lambda **kwargs: eliot_events.append(kwargs))
    monkeypatch.setattr(
        hooks,
        "append_pi_event",
        lambda event_type, payload, **kwargs: append_calls.append((event_type, dict(payload)))
        or tmp_path / "runs.jsonl",
    )
    monkeypatch.setattr(hooks, "project_relative", lambda path: str(path))

    def fake_ck(repo: object, **kwargs: object) -> MetricsSummary:
        del kwargs
        assert getattr(repo, "path") == tmp_path
        return MetricsSummary(
            exit_code=0,
            totals=MetricsTotals(
                classes=3,
                methods=10,
                mean_cbo=1.5,
                mean_lcom=2.0,
                mean_wmc=4.0,
                total_loc=120.0,
            ),
        )

    monkeypatch.setattr(hooks, "run_ck_metrics", fake_ck)
    repo = SimpleNamespace(path=tmp_path)
    result = hooks.log_case_completion_metrics(
        repo,  # type: ignore[arg-type]
        case_id="Tap4j:4413ab35b400",
        timeout=30,
        metrics=SmellRunMetrics(
            remaining_smells=1,
            introduced_smells=0,
            fixed_smells=3,
            original_smells=4,
            total_introduced_smells=0,
            fixed_introduced_smells=0,
        ),
        stop_reason="smells_cleared",
        profile="without-planning",
        elements=["org.tap4j.model.BailOut"],
    )

    assert result["ck_error"] is None
    assert result["fixed_smells"] == 3
    assert result["ck"] == {
        "success": True,
        "exit_code": 0,
        "classes": 3,
        "methods": 10,
        "mean_cbo": 1.5,
        "mean_lcom": 2.0,
        "mean_wmc": 4.0,
        "total_loc": 120.0,
    }
    assert [event["message_type"] for event in eliot_events] == [
        "deep:case_summary",
        "pi:ck",
    ]
    summary = eliot_events[0]
    assert summary["case_id"] == "Tap4j:4413ab35b400"
    assert summary["harness"] == "pi"
    assert summary["fixed_smells"] == 3
    assert summary["introduced_smells"] == 0
    assert summary["remaining_smells"] == 1
    assert summary["stop_reason"] == "smells_cleared"
    ck = eliot_events[1]
    assert ck["case_id"] == "Tap4j:4413ab35b400"
    assert ck["harness"] == "pi"
    assert ck["classes"] == 3
    assert ck["mean_cbo"] == 1.5
    assert ck["total_loc"] == 120.0
    assert append_calls == [
        (
            "complete",
            {
                "repo_path": str(tmp_path),
                "stop_reason": "smells_cleared",
                "remaining_smells": 1,
                "fixed_smells": 3,
                "introduced_smells": 0,
                "original_smells": 4,
                "total_introduced_smells": 0,
                "fixed_introduced_smells": 0,
                "ck": result["ck"],
            },
        )
    ]


def test_log_case_completion_metrics_includes_usage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    eliot_events: list[dict[str, object]] = []
    monkeypatch.setattr(hooks, "log_message", lambda **kwargs: eliot_events.append(kwargs))
    monkeypatch.setattr(
        hooks,
        "append_pi_event",
        lambda *args, **kwargs: tmp_path / "runs.jsonl",
    )
    monkeypatch.setattr(
        hooks,
        "run_ck_metrics",
        lambda *args, **kwargs: SimpleNamespace(
            success=True,
            exit_code=0,
            totals=SimpleNamespace(
                classes=1,
                methods=1,
                mean_cbo=0.0,
                mean_lcom=0.0,
                mean_wmc=0.0,
                total_loc=1.0,
            ),
        ),
    )
    monkeypatch.setattr(hooks, "project_relative", lambda path: str(path))

    hooks.log_case_completion_metrics(
        SimpleNamespace(path=tmp_path),  # type: ignore[arg-type]
        case_id="Tap4j:4413ab35b400",
        timeout=30,
        metrics=SmellRunMetrics(
            remaining_smells=0,
            introduced_smells=0,
            fixed_smells=2,
            original_smells=2,
            total_introduced_smells=0,
            fixed_introduced_smells=0,
        ),
        stop_reason="smells_cleared",
        profile="without-planning",
        elements=["example.Main"],
        usage={
            "input_tokens": 1000,
            "output_tokens": 250,
            "total_tokens": 1250,
            "cost_usd": 0.42,
            "turn_count": 7,
        },
    )

    summary = eliot_events[0]
    assert summary["input_tokens"] == 1000
    assert summary["output_tokens"] == 250
    assert summary["total_tokens"] == 1250
    assert summary["cost_usd"] == 0.42
    assert summary["turn_count"] == 7


def test_log_case_completion_metrics_logs_ck_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    eliot_events: list[dict[str, object]] = []
    monkeypatch.setattr(hooks, "log_message", lambda **kwargs: eliot_events.append(kwargs))
    monkeypatch.setattr(
        hooks,
        "append_pi_event",
        lambda *args, **kwargs: tmp_path / "runs.jsonl",
    )
    monkeypatch.setattr(hooks, "project_relative", lambda path: str(path))
    monkeypatch.setattr(
        hooks,
        "run_ck_metrics",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("ck boom")),
    )

    result = hooks.log_case_completion_metrics(
        SimpleNamespace(path=tmp_path),  # type: ignore[arg-type]
        case_id="JUnit4:5def21bbf48a",
        timeout=30,
        metrics=SmellRunMetrics(
            remaining_smells=0,
            introduced_smells=0,
            fixed_smells=2,
            original_smells=2,
            total_introduced_smells=0,
            fixed_introduced_smells=0,
        ),
        stop_reason="smells_cleared",
        profile="with-planning",
        elements=["org.junit.runners.ParentRunner"],
    )

    assert result["ck"] is None
    assert result["ck_error"] == "ck boom"
    assert result["fixed_smells"] == 2
    assert result["introduced_smells"] == 0
    assert [event["message_type"] for event in eliot_events] == [
        "deep:case_summary",
        "pi:ck_failed",
    ]
    assert eliot_events[1]["exception"] == "ck boom"
    assert eliot_events[1]["case_id"] == "JUnit4:5def21bbf48a"


def test_complete_cli_redetects_and_logs_metrics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from git import Repo as GitRepo
    from java.metrics.ck_reports import MetricsSummary, MetricsTotals

    GitRepo.init(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    run_log = run_dir / "all.log"
    run_log.write_text("", encoding="utf-8")
    baseline = tuple(
        OrganicFeedback(
            smell=Smell.at(
                "Long Method",
                str(tmp_path / f"Main{i}.java"),
                10 + i,
                severity="HIGH",
                detected_by="ORGANIC",
                commit_hash="abc123",
            ),
            owner=f"example.Main.method{i}",
            scope=OrganicScope.METHOD,
            reason=None,
            relevant_metrics=(),
        )
        for i in range(5)
    )
    initialize_smell_run(
        run_dir,
        case_id="PhiCode Philib:3b7222e9b466",
        start_commit="abc123",
        feedback=baseline,
    )
    remaining = baseline[0:1]
    monkeypatch.setattr(hooks, "configure_pi_logging", lambda _: run_log)
    eliot_events: list[dict[str, object]] = []
    monkeypatch.setattr(hooks, "log_message", lambda **kwargs: eliot_events.append(kwargs))
    monkeypatch.setattr(
        hooks,
        "remaining_target_feedback",
        lambda repo, elements: tuple(remaining),
    )
    monkeypatch.setattr(
        hooks,
        "run_ck_metrics",
        lambda repo, **kwargs: MetricsSummary(
            exit_code=0,
            totals=MetricsTotals(
                classes=1,
                methods=2,
                mean_cbo=0.5,
                mean_lcom=0.0,
                mean_wmc=1.0,
                total_loc=10.0,
            ),
        ),
    )
    monkeypatch.setattr(
        hooks,
        "append_pi_event",
        lambda *args, **kwargs: tmp_path / "runs.jsonl",
    )
    monkeypatch.setattr(hooks, "project_relative", lambda path: str(path))

    code = hooks.main(
        [
            "complete",
            "--repo-path",
            str(tmp_path),
            "--elements",
            "example.Main",
            "--timeout",
            "30",
            "--case-id",
            "PhiCode Philib:3b7222e9b466",
            "--stop-reason",
            "smells_cleared",
            "--profile",
            "without-planning",
            "--run-log",
            str(run_log),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert '"remaining_smells": 1' in out
    assert '"fixed_smells": 4' in out
    assert '"introduced_smells": 0' in out
    assert '"classes": 1' in out
    assert [event["message_type"] for event in eliot_events] == [
        "deep:case_summary",
        "pi:ck",
    ]
    assert eliot_events[0]["original_smells"] == 5
    assert eliot_events[0]["remaining_smells"] == 1
    assert eliot_events[1]["classes"] == 1


def test_load_cases_uses_explicit_manifest(tmp_path: Path) -> None:
    manifest = tmp_path / "subset.jsonl"
    manifest.write_text(
        '{"case_id": "Only:one", "project": "Only", '
        '"repo_url": "https://example.com/only.git", '
        '"start_commit": "abc", "elements": ["example.Main"]}\n',
        encoding="utf-8",
    )
    cases = hooks.load_cases(str(manifest))
    assert len(cases) == 1
    assert cases[0].case_id == "Only:one"


def test_dataset_from_manifest(tmp_path: Path) -> None:
    from dataset import Dataset

    manifest = tmp_path / "custom.jsonl"
    manifest.write_text(
        '{"case_id": "Custom:1", "project": "Custom", '
        '"repo_url": "https://example.com/custom.git", '
        '"start_commit": "def", "elements": ["example.Main"]}\n',
        encoding="utf-8",
    )
    cases = Dataset.from_manifest(manifest).load_cases()
    assert [case.case_id for case in cases] == ["Custom:1"]
