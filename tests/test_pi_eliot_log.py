"""Tests for pi Eliot usage logging."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from eliot import add_destinations, remove_destination

from agents.pi import eliot_log


@pytest.fixture
def eliot_messages() -> Iterator[list[dict[str, object]]]:
    messages: list[dict[str, object]] = []
    destination = messages.append
    add_destinations(destination)
    yield messages
    remove_destination(destination)


def test_log_pi_eliot_writes_structured_message(
    eliot_messages: list[dict[str, object]],
) -> None:
    eliot_log.log_pi_eliot(
        {
            "message_type": "pi:llm_turn",
            "case_id": "Tap4j:4413ab35b400",
            "input_tokens": 120,
            "output_tokens": 40,
            "cost_total_usd": 0.00012,
        }
    )

    logged = eliot_messages[-1]
    assert logged["message_type"] == "pi:llm_turn"
    assert logged["harness"] == "pi"
    assert logged["case_id"] == "Tap4j:4413ab35b400"
    assert logged["input_tokens"] == 120
    assert logged["cost_total_usd"] == 0.00012


def test_log_pi_eliot_writes_manifest_resume(
    eliot_messages: list[dict[str, object]],
) -> None:
    eliot_log.log_pi_eliot(
        {
            "message_type": "pi_batch:manifest_resume",
            "trigger": "batch_end",
            "manifest": "dataset/manifest_bfs_remaining.jsonl",
            "completed_count": 2,
            "pending_count": 1,
            "successful_count": 1,
            "failed_count": 1,
            "skipped_count": 0,
            "cases": [
                {
                    "index": 1,
                    "case_id": "Drugis Common:bcbdecd601d0",
                    "status": "successful",
                    "stop_reason": "smells_cleared",
                }
            ],
        }
    )

    logged = eliot_messages[-1]
    assert logged["message_type"] == "pi_batch:manifest_resume"
    assert logged["trigger"] == "batch_end"
    assert logged["completed_count"] == 2


def test_eliot_log_cli_accepts_payload(
    monkeypatch: pytest.MonkeyPatch,
    eliot_messages: list[dict[str, object]],
) -> None:
    monkeypatch.setattr(eliot_log, "configure_pi_logging", lambda run_log: None)
    code = eliot_log.main(
        [
            "--payload",
            '{"message_type":"pi:case_cost","case_id":"x","cost_usd":1.25,"turn_count":3}',
            "--run-log",
            "data/pi/runs/example/run/all.log",
        ]
    )

    assert code == 0
    logged = eliot_messages[-1]
    assert logged["message_type"] == "pi:case_cost"
    assert logged["cost_usd"] == 1.25
    assert logged["turn_count"] == 3
