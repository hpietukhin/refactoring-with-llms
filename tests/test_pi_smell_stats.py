"""Tests for Pi structured-log statistics."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.pi.smell_stats import (
    build_interactive_evolution_data,
    generate_case_metric_charts,
    generate_interactive_evolution_report,
    generate_matplotlib_evolution_report,
    generate_scientific_metric_report,
    parse_batch_outcome,
    parse_eliot_log,
    parse_successful_batch_log,
    print_eliot_logs,
)


def _write_records(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )


def test_parse_eliot_log_uses_latest_metrics_with_slots(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_path = tmp_path / "all.log"
    _write_records(
        log_path,
        [
            {
                "message_type": "deep:case_start",
                "case_id": "project:case",
                "smell_count": 5,
            },
            {
                "message_type": "pi:verify",
                "case_id": "project:case",
                "remaining_smells": 4,
                "fixed_smells": 1,
                "original_smells": 5,
            },
            {
                "message_type": "deep:case_summary",
                "case_id": "project:case",
                "remaining_smells": 0,
                "introduced_smells": 0,
                "fixed_smells": 5,
                "original_smells": 5,
                "total_introduced_smells": 2,
                "fixed_introduced_smells": 2,
                "stop_reason": "smells_cleared",
            },
        ],
    )

    summary = parse_eliot_log(log_path)

    assert summary.case_id == "project:case"
    assert summary.original_smells == 5
    assert summary.remaining_smells == 0
    assert summary.fixed_smells == 5
    assert summary.stop_reason == "smells_cleared"
    assert summary.last_message_type == "deep:case_summary"

    print_eliot_logs([log_path])
    assert "project:case" in capsys.readouterr().out


def test_parse_successful_batch_log_collects_model_and_ck_stats(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "batch.log"
    _write_records(
        log_path,
        [
            {
                "event": "pi_batch:start",
                "planner": "greedy",
                "weakModel": "openrouter/google/gemini-3.7-flash",
                "strongModel": "openrouter/z-ai/glm-5.2",
            },
            {
                "message_type": "deep:case_start",
                "case_id": "project:success",
                "timestamp": 100.0,
            },
            {
                "message_type": "pi:llm_turn",
                "case_id": "project:success",
                "model": "google/gemini-3.7-flash",
                "smell_key": "first",
                "attempt_number": 1,
                "input_tokens": 40,
                "output_tokens": 10,
                "cost_total_usd": 0.05,
                "timestamp": 105.0,
            },
            {
                "message_type": "pi:llm_turn",
                "case_id": "project:success",
                "model": "z-ai/glm-5.2",
                "smell_key": "second",
                "attempt_number": 3,
                "input_tokens": 60,
                "output_tokens": 10,
                "cost_total_usd": 0.2,
                "timestamp": 110.0,
            },
            {
                "message_type": "pi:refactoring_complete",
                "case_id": "project:success",
                "smell_key": "first",
                "timestamp": 120.0,
            },
            {
                "message_type": "pi:refactoring_complete",
                "case_id": "project:success",
                "smell_key": "second",
                "timestamp": 180.0,
            },
            {
                "message_type": "pi:case_cost",
                "case_id": "project:success",
                "turn_count": 3,
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_tokens": 50,
                "cache_write_tokens": 5,
                "total_tokens": 120,
                "cost_usd": 0.25,
            },
            {
                "message_type": "pi:verify",
                "case_id": "project:success",
                "passed": False,
                "introduced_smells": 2,
                "content": "diagnostic_type=compilation_error",
                "timestamp": 115.0,
            },
            {
                "message_type": "pi:verify",
                "case_id": "project:success",
                "passed": True,
                "introduced_smells": 0,
                "content": "tests_failed=False",
                "timestamp": 125.0,
            },
            {
                "message_type": "deep:case_summary",
                "case_id": "project:success",
                "timestamp": 220.0,
                "stop_reason": "smells_cleared",
                "fixed_smells": 2,
                "original_smells": 2,
                "total_introduced_smells": 1,
                "fixed_introduced_smells": 1,
            },
            {
                "message_type": "pi:ck",
                "case_id": "project:success",
                "classes": 10,
                "methods": 30,
                "mean_cbo": 1.5,
                "mean_lcom": 2.5,
                "mean_wmc": 3.5,
                "total_loc": 400.0,
            },
            {
                "event": "pi_batch:done",
                "results": [
                    {
                        "caseId": "project:success",
                        "ok": True,
                        "stopReason": "smells_cleared",
                        "fixedSmellCount": 2,
                        "originalSmellCount": 2,
                        "fixedIntroducedSmellCount": 1,
                        "totalIntroducedSmellCount": 1,
                    },
                    {
                        "caseId": "project:failed",
                        "ok": False,
                    },
                ],
            },
        ],
    )

    statistics = parse_successful_batch_log(log_path)
    outcome = parse_batch_outcome(log_path)

    assert len(statistics) == 1
    assert outcome.planner == "greedy"
    assert outcome.cases == 2
    assert outcome.successful == 1
    assert outcome.failed == 1
    assert outcome.skipped == 0
    result = statistics[0]
    assert result.planner == "greedy"
    assert result.case_id == "project:success"
    assert result.completed_smell_tasks == 2
    assert result.llm_turns == 3
    assert result.turns_per_smell == pytest.approx(1.5)
    assert result.mean_minutes_between_smells == pytest.approx(1.0)
    assert result.median_minutes_between_smells == pytest.approx(1.0)
    assert result.p95_minutes_between_smells == pytest.approx(1.0)
    assert result.median_turns_per_smell == pytest.approx(1.0)
    assert result.p95_turns_per_smell == 1
    assert result.max_attempt == 3
    assert result.escalated_smell_tasks == 1
    assert result.weak_model_turns == 1
    assert result.strong_model_turns == 1
    assert result.weak_model_tokens == 50
    assert result.strong_model_tokens == 70
    assert result.total_tokens == 120
    assert result.cache_read_tokens == 50
    assert result.cost_usd == pytest.approx(0.25)
    assert result.verification_count == 2
    assert result.verification_failures == 1
    assert result.compilation_failures == 1
    assert result.peak_introduced_smells == 2
    assert result.duration_minutes == pytest.approx(2.0)
    assert result.verification_minutes == pytest.approx(5 / 60)
    assert result.verification_share == pytest.approx(1 / 24)
    assert result.classes == 10
    assert result.mean_cbo == pytest.approx(1.5)

    manifest_path = tmp_path / "manifest.jsonl"
    _write_records(
        manifest_path,
        [
            {"case_id": "project:success"},
            {"case_id": "project:missing"},
        ],
    )
    output_dir = tmp_path / "charts"
    charts = generate_case_metric_charts(
        [log_path],
        manifest_path=manifest_path,
        output_dir=output_dir,
    )
    assert len(charts) == 2
    assert "Mean CBO" in charts[0].read_text(encoding="utf-8")
    assert "No successful CK metrics" in charts[1].read_text(encoding="utf-8")
    assert (output_dir / "README.md").is_file()
    html_report = (output_dir / "index.html").read_text(encoding="utf-8")
    assert html_report.count("<svg ") == 2
    assert "project:success" in html_report

    scientific_dir = tmp_path / "scientific"
    figures = generate_scientific_metric_report(
        [log_path],
        manifest_path=manifest_path,
        output_dir=scientific_dir,
    )
    assert len(figures) == 2
    assert "Mean CBO" not in figures[0].read_text(encoding="utf-8")
    scientific_html = (scientific_dir / "index.html").read_text(encoding="utf-8")
    assert "n=1" in scientific_html
    assert "× unavailable" in scientific_html

    evolution_data = build_interactive_evolution_data(
        [log_path],
        manifest_path=manifest_path,
    )
    cases = evolution_data["cases"]
    assert isinstance(cases, dict)
    success_case = cases["project:success"]
    assert isinstance(success_case, dict)
    planners = success_case["planners"]
    assert isinstance(planners, dict)
    greedy = planners["greedy"]
    assert isinstance(greedy, dict)
    trajectory = greedy["trajectory"]
    assert isinstance(trajectory, list)
    assert len(trajectory) == 2
    assert [point["cumulativeTurns"] for point in trajectory] == [2, 2]

    interactive_path = generate_interactive_evolution_report(
        [log_path],
        manifest_path=manifest_path,
        output_path=tmp_path / "interactive" / "index.html",
    )
    interactive_html = interactive_path.read_text(encoding="utf-8")
    assert "casePicker" in interactive_html
    assert "tokensPerFix" in interactive_html
    assert "best trajectory" in interactive_html

    matplotlib_path = generate_matplotlib_evolution_report(
        [log_path],
        manifest_path=manifest_path,
        output_path=tmp_path / "matplotlib" / "index.html",
    )
    matplotlib_html = matplotlib_path.read_text(encoding="utf-8")
    assert "Matplotlib planner evolution report" in matplotlib_html
    assert "plot-aggregate-cumulativeTurns" in matplotlib_html
    assert "Larger orange points" in matplotlib_html
    assert "<svg" in matplotlib_html


def test_incomplete_batch_is_reported_without_successes(tmp_path: Path) -> None:
    log_path = tmp_path / "incomplete.log"
    _write_records(
        log_path,
        [
            {
                "event": "pi_batch:start",
                "planner": "topo",
                "profile": "without-planning",
                "cases": 1,
                "weakModel": "openrouter/weak",
                "strongModel": "openrouter/strong",
            },
            {
                "message_type": "deep:case_start",
                "case_id": "project:case",
                "timestamp": 100.0,
            },
        ],
    )

    outcome = parse_batch_outcome(log_path)

    assert outcome.cases == 1
    assert outcome.incomplete == 1
    assert outcome.stop_reasons == (("incomplete", 1),)
    assert parse_successful_batch_log(log_path) == []
