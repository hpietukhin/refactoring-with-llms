"""Tests for pi smell-run baseline tracking and work queue."""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.pi.smell_run import (
    feedback_from_record,
    feedback_to_record,
    initialize_smell_run,
    introduced_progress,
    load_smell_run,
    next_queue_smell,
    save_smell_run,
    smell_identity_key,
    smell_logical_key,
    sync_smell_run,
)
from java.organic_feedback import OrganicFeedback
from java.organic_types import OrganicScope
from planning.ast.models import AstElement, AstIndex
from planning.planner import NoPriorityPlanner, PriorityPlanner
from smell.smell import Smell


def _feedback(
    *,
    smell_type: str = "Long Method",
    file_path: str = "/tmp/repo/Main.java",
    start_line: int = 12,
    owner: str = "example.Main.run",
    commit_hash: str | None = "abc123",
    related_owner: str | None = None,
) -> OrganicFeedback:
    return OrganicFeedback(
        smell=Smell.at(
            smell_type,
            file_path,
            start_line,
            severity="HIGH",
            detected_by="ORGANIC",
            commit_hash=commit_hash,
        ),
        owner=owner,
        scope=OrganicScope.METHOD,
        reason=None,
        relevant_metrics=(),
        related_owner=related_owner,
    )


def test_smell_identity_changes_when_location_changes() -> None:
    first = _feedback(start_line=12, owner="example.Main.run")
    moved = _feedback(start_line=90, owner="example.Main.run")
    different_owner = _feedback(start_line=90, owner="example.Main.other")

    assert smell_identity_key(first, "abc123") != smell_identity_key(moved, "abc123")
    assert smell_identity_key(moved, "abc123") != smell_identity_key(
        different_owner,
        "abc123",
    )


def test_feedback_record_round_trip() -> None:
    original = _feedback(related_owner="example.Helper")
    record = feedback_to_record(original, "abc123")
    restored = feedback_from_record(record)

    assert smell_identity_key(restored, "abc123") == smell_identity_key(original, "abc123")
    assert restored.owner == original.owner
    assert restored.smell.type == original.smell.type


def test_feedback_from_record_accepts_legacy_nested_overlap_strings() -> None:
    original = OrganicFeedback(
        smell=Smell.at(
            "Long Method",
            "/tmp/repo/Main.java",
            12,
            severity="HIGH",
            detected_by="ORGANIC",
            commit_hash="abc123",
        ),
        owner="example.Outer",
        scope=OrganicScope.CLASS,
        reason=None,
        relevant_metrics=(),
        nested_overlap=True,
        nested_owners=("example.Outer.Inner",),
    )
    record = feedback_to_record(original, "abc123")
    record["nested_overlap"] = "confirmed_nested_propagation"
    restored = feedback_from_record(record)

    assert restored.nested_overlap is True
    assert restored.nested_owners == ("example.Outer.Inner",)

    record["nested_overlap"] = "none"
    cleared = feedback_from_record(record)
    assert cleared.nested_overlap is False


def test_smell_logical_key_is_stable_when_location_changes() -> None:
    first = _feedback(start_line=12, owner="example.Main.run")
    moved = _feedback(start_line=90, owner="example.Main.run")

    assert smell_logical_key(first) == smell_logical_key(moved)
    assert smell_identity_key(first, "abc123") != smell_identity_key(moved, "abc123")


def test_initialize_writes_baseline_and_queue(tmp_path: Path) -> None:
    baseline = (_feedback(owner="example.A"), _feedback(owner="example.B", start_line=20))
    state = initialize_smell_run(
        tmp_path,
        case_id="case",
        start_commit="abc123",
        feedback=baseline,
    )

    assert state.original_smells == 2
    assert state.queue == [
        smell_identity_key(baseline[0], "abc123"),
        smell_identity_key(baseline[1], "abc123"),
    ]
    assert state.introduced_identity_keys == []
    assert state.introduced_logical_keys == []
    assert state.baseline_logical_keys == [smell_logical_key(baseline[0]), smell_logical_key(baseline[1])]
    assert (tmp_path / "smells.json").is_file()
    assert load_smell_run(tmp_path).baseline_identity_keys == state.baseline_identity_keys


def test_sync_replaces_detect_snapshot(tmp_path: Path) -> None:
    baseline = (_feedback(owner="example.A"),)
    state = initialize_smell_run(
        tmp_path,
        case_id="case",
        start_commit="abc123",
        feedback=baseline,
    )
    state.queue.clear()

    sync = sync_smell_run(state, baseline)

    key = smell_identity_key(baseline[0], "abc123")
    assert state.queue == [key]
    assert sync.metrics.remaining_smells == 1
    assert sync.metrics.fixed_smells == 0
    assert sync.newly_introduced == ()


def test_next_queue_smell_uses_detect_order_and_keeps_introduced(
    tmp_path: Path,
) -> None:
    first = _feedback(owner="example.A", start_line=10)
    second = _feedback(owner="example.B", start_line=20)
    state = initialize_smell_run(
        tmp_path,
        case_id="case",
        start_commit="abc123",
        feedback=(first, second),
    )
    introduced = _feedback(owner="example.C", start_line=30)
    current = (second, introduced)
    next_feedback, sync = next_queue_smell(
        state,
        current,
        repo_path=tmp_path,
        skip_identity_keys=set(),
        planner=NoPriorityPlanner(),
    )

    assert next_feedback is not None
    assert next_feedback.owner == "example.B"
    assert sync.metrics.introduced_smells == 1
    assert state.queue == [
        smell_identity_key(second, "abc123"),
        smell_identity_key(introduced, "abc123"),
    ]


def test_next_queue_smell_reuses_synced_snapshot_without_feedback(
    tmp_path: Path,
) -> None:
    only = _feedback(owner="example.A")
    state = initialize_smell_run(
        tmp_path,
        case_id="case",
        start_commit="abc123",
        feedback=(only,),
    )
    next_feedback, sync = next_queue_smell(
        state,
        repo_path=tmp_path,
        skip_identity_keys=set(),
        planner=NoPriorityPlanner(),
    )

    assert next_feedback is not None
    assert next_feedback.owner == "example.A"
    assert sync.metrics.remaining_smells == 1
    assert sync.newly_introduced == ()


def test_next_queue_smell_skips_exhausted_keys(tmp_path: Path) -> None:
    only = _feedback(owner="example.A")
    state = initialize_smell_run(
        tmp_path,
        case_id="case",
        start_commit="abc123",
        feedback=(only,),
    )
    key = smell_identity_key(only, "abc123")
    next_feedback, sync = next_queue_smell(
        state,
        (only,),
        repo_path=tmp_path,
        skip_identity_keys={key},
        planner=NoPriorityPlanner(),
    )

    assert next_feedback is None
    assert sync.metrics.remaining_smells == 1
    assert sync.metrics.introduced_smells == 0


def test_next_queue_smell_bfs_uses_dependency_order(tmp_path: Path) -> None:
    lazy = _feedback(
        smell_type="Lazy Class",
        file_path="a/A.java",
        start_line=1,
        owner="a.A",
        commit_hash="abc123",
    )
    speculative = _feedback(
        smell_type="Speculative Generality",
        file_path="a/A.java",
        start_line=2,
        owner="a.A",
        commit_hash="abc123",
    )
    state = initialize_smell_run(
        tmp_path,
        case_id="case",
        start_commit="abc123",
        feedback=(lazy, speculative),
    )
    index = AstIndex(
        elements=[
            AstElement(
                id="a.A",
                kind="class",
                file="a/A.java",
                startLine=1,
                endLine=100,
            ),
        ],
        edges=[],
        unresolvedCalls=0,
    )

    next_feedback, _ = next_queue_smell(
        state,
        (lazy, speculative),
        repo_path=tmp_path,
        skip_identity_keys=set(),
        planner=PriorityPlanner(ast_index=index),
    )

    assert next_feedback is not None
    assert next_feedback.smell.type == "Speculative Generality"


def test_sync_removes_fixed_smells_from_queue(tmp_path: Path) -> None:
    first = _feedback(owner="example.A", start_line=10)
    second = _feedback(owner="example.B", start_line=20)
    state = initialize_smell_run(
        tmp_path,
        case_id="case",
        start_commit="abc123",
        feedback=(first, second),
    )
    sync = sync_smell_run(state, (second,))

    assert sync.metrics.fixed_smells == 1
    assert sync.metrics.remaining_smells == 1
    assert state.queue == [smell_identity_key(second, "abc123")]
    save_smell_run(tmp_path, state)
    assert load_smell_run(tmp_path).queue == state.queue


def test_sync_location_shift_does_not_bump_introduced_total(tmp_path: Path) -> None:
    baseline = (_feedback(owner="example.A"),)
    state = initialize_smell_run(
        tmp_path,
        case_id="case",
        start_commit="abc123",
        feedback=baseline,
    )
    introduced = _feedback(owner="example.B", start_line=40)
    sync1 = sync_smell_run(state, (baseline[0], introduced))

    assert sync1.metrics.total_introduced_smells == 1
    assert sync1.metrics.introduced_smells == 1
    assert sync1.metrics.fixed_introduced_smells == 0
    assert len(sync1.newly_introduced) == 1

    moved = _feedback(owner="example.B", start_line=90)
    sync2 = sync_smell_run(state, (baseline[0], moved))

    assert sync2.metrics.total_introduced_smells == 1
    assert sync2.metrics.introduced_smells == 1
    assert sync2.metrics.fixed_introduced_smells == 0
    assert sync2.newly_introduced == ()
    assert len(state.introduced_identity_keys) == 2


def test_sync_tracks_introduced_fix_progress(tmp_path: Path) -> None:
    baseline = (_feedback(owner="example.A"),)
    state = initialize_smell_run(
        tmp_path,
        case_id="case",
        start_commit="abc123",
        feedback=baseline,
    )
    introduced = _feedback(owner="example.B", start_line=40)
    sync1 = sync_smell_run(state, (baseline[0], introduced))

    assert sync1.metrics.total_introduced_smells == 1
    assert sync1.metrics.fixed_introduced_smells == 0
    assert sync1.metrics.introduced_smells == 1

    sync2 = sync_smell_run(state, (baseline[0],))
    assert sync2.metrics.total_introduced_smells == 1
    assert sync2.metrics.fixed_introduced_smells == 1
    assert sync2.metrics.introduced_smells == 0

    another = _feedback(owner="example.C", start_line=50)
    sync3 = sync_smell_run(state, (baseline[0], another))
    assert sync3.metrics.total_introduced_smells == 2
    assert sync3.metrics.fixed_introduced_smells == 1
    assert sync3.metrics.introduced_smells == 1


def test_introduced_progress_invariant_holds(tmp_path: Path) -> None:
    baseline = (_feedback(owner="example.A"),)
    state = initialize_smell_run(
        tmp_path,
        case_id="case",
        start_commit="abc123",
        feedback=baseline,
    )
    introduced_b = _feedback(owner="example.B", start_line=40)
    introduced_c = _feedback(owner="example.C", start_line=50)
    current: tuple[OrganicFeedback, ...] = (baseline[0], introduced_b)
    for offset in range(3):
        sync = sync_smell_run(state, current)
        metrics = sync.metrics
        assert (
            metrics.fixed_introduced_smells + metrics.introduced_smells
            == metrics.total_introduced_smells
        )
        current = (
            baseline[0],
            _feedback(owner="example.B", start_line=40 + offset * 10),
            introduced_c,
        )
    final_sync = sync_smell_run(state, current)
    metrics = final_sync.metrics
    assert metrics.total_introduced_smells == 2
    assert metrics.introduced_smells == 2
    assert metrics.fixed_introduced_smells == 0
    assert (
        metrics.fixed_introduced_smells + metrics.introduced_smells
        == metrics.total_introduced_smells
    )


def test_introduced_progress_without_sync_mutation(tmp_path: Path) -> None:
    baseline = (_feedback(owner="example.A"),)
    state = initialize_smell_run(
        tmp_path,
        case_id="case",
        start_commit="abc123",
        feedback=baseline,
    )
    introduced = _feedback(owner="example.B", start_line=40)
    sync_smell_run(state, (baseline[0], introduced))
    fixed, total, remaining = introduced_progress(state, (baseline[0],))
    assert fixed == 1
    assert total == 1
    assert remaining == 0
