"""Detect when a pi batch case stalls on one smell/refactoring for too long."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class StallReport:
    stalled: bool
    case_id: str | None
    smell_key: str | None
    idle_seconds: float
    last_event: str | None
    reason: str | None


def _parse_json_line(line: str) -> dict[str, object] | None:
    stripped = line.strip()
    if not stripped.startswith("{"):
        return None
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _record_timestamp(record: dict[str, object]) -> float | None:
    raw = record.get("timestamp")
    if isinstance(raw, (int, float)):
        return float(raw)
    return None


def _line_activity(record: dict[str, object], line: str) -> tuple[str | None, str | None, float | None]:
    message_type = record.get("message_type")
    if isinstance(message_type, str):
        case_id = record.get("case_id")
        smell_key = record.get("smell_key")
        return (
            str(case_id) if case_id is not None else None,
            str(smell_key) if smell_key is not None else None,
            _record_timestamp(record),
        )

    event = record.get("event")
    if event == "pi_batch:start":
        return None, None, time.time()

    if line.startswith("[done] ") or line.startswith("[fail] ") or line.startswith("[failed-done] "):
        return None, None, time.time()

    for prefix in ("[idle-timeout]", "[wall-timeout]", "[llm-retry]", "[llm-error]", "[batch-abort]"):
        if line.startswith(prefix):
            return None, None, time.time()

    if "[start]" in line:
        return None, None, time.time()

    if line.startswith("[") and "] tool " in line:
        return None, None, time.time()

    if line.startswith("[") and "] assistant:" in line:
        return None, None, time.time()

    return None, None, None


def analyze_logs(paths: list[Path], *, stall_seconds: float) -> StallReport:
    current_case_id: str | None = None
    current_smell_key: str | None = None
    last_activity_at = time.time()
    last_event: str | None = "batch_start"
    hang_reason: str | None = None

    for path in paths:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            record = _parse_json_line(line)
            if record is not None:
                message_type = record.get("message_type")
                case_id = record.get("case_id")
                smell_key = record.get("smell_key")
                timestamp = _record_timestamp(record)

                if message_type == "deep:case_start" and case_id is not None:
                    current_case_id = str(case_id)
                    current_smell_key = None
                    last_event = "case_start"
                    if timestamp is not None:
                        last_activity_at = timestamp
                    continue

                if message_type == "deep:prompt" and case_id is not None:
                    current_case_id = str(case_id)
                    smell_index = record.get("smell_index")
                    last_event = f"smell_prompt:{smell_index}"
                    if timestamp is not None:
                        last_activity_at = timestamp
                    continue

                if message_type == "pi:refactoring_complete":
                    if smell_key is not None:
                        current_smell_key = str(smell_key)
                    last_event = "refactoring_complete"
                    if timestamp is not None:
                        last_activity_at = timestamp
                    continue

                if message_type in {
                    "pi:llm_turn",
                    "pi:verify",
                    "deep:case_summary",
                    "pi:case_cost",
                    "pi:smells_introduced",
                }:
                    if case_id is not None:
                        current_case_id = str(case_id)
                    if smell_key is not None:
                        current_smell_key = str(smell_key)
                    last_event = str(message_type)
                    if timestamp is not None:
                        last_activity_at = timestamp
                    continue

                if message_type == "pi:verify" and record.get("passed") is False:
                    content = record.get("content")
                    if isinstance(content, str) and "compilation_error" in content:
                        hang_reason = "repeated verification with compile errors"
                    elif isinstance(content, str) and "tests_failed=True" in content:
                        hang_reason = "repeated verification with failing tests"

                case_from_line, smell_from_line, activity_at = _line_activity(record, line)
                if activity_at is not None:
                    if case_from_line is not None:
                        current_case_id = case_from_line
                    if smell_from_line is not None:
                        current_smell_key = smell_from_line
                    last_activity_at = activity_at
                continue

            case_from_line, smell_from_line, activity_at = _line_activity({}, line)
            if activity_at is not None:
                if case_from_line is not None:
                    current_case_id = case_from_line
                if smell_from_line is not None:
                    current_smell_key = smell_from_line
                last_activity_at = activity_at
                if line.startswith("[idle-timeout]"):
                    hang_reason = "idle timeout (no tool/LLM activity)"
                elif line.startswith("[wall-timeout]"):
                    hang_reason = "wall timeout"
                elif line.startswith("[llm-retry]") or line.startswith("[llm-error]"):
                    hang_reason = line.strip()

    idle_seconds = max(0.0, time.time() - last_activity_at)
    stalled = idle_seconds >= stall_seconds
    reason = hang_reason
    if stalled and reason is None:
        reason = (
            f"no progress for {idle_seconds / 60:.1f} minutes on "
            f"{current_case_id or 'unknown case'}"
            + (f" smell {current_smell_key[:12]}..." if current_smell_key else "")
            + f"; last event={last_event}"
        )

    return StallReport(
        stalled=stalled,
        case_id=current_case_id,
        smell_key=current_smell_key,
        idle_seconds=idle_seconds,
        last_event=last_event,
        reason=reason,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logs", nargs="+", help="Log files to inspect")
    parser.add_argument(
        "--stall-minutes",
        type=float,
        default=15.0,
        help="Report stall after this many minutes without progress",
    )
    args = parser.parse_args(argv)

    report = analyze_logs(
        [Path(path) for path in args.logs],
        stall_seconds=args.stall_minutes * 60.0,
    )
    payload = {
        "stalled": report.stalled,
        "case_id": report.case_id,
        "smell_key": report.smell_key,
        "idle_minutes": round(report.idle_seconds / 60.0, 2),
        "last_event": report.last_event,
        "reason": report.reason,
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 1 if report.stalled else 0


if __name__ == "__main__":
    raise SystemExit(main())
