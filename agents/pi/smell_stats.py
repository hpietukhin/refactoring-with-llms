"""Query pi smell progress from Eliot logs and ``data/pi`` event files."""

from __future__ import annotations

import argparse
import html
import io
import json
import math
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from agents.pi.paths import DEFAULT_OUTPUT_DIR, pi_output_dir, safe_case_id
from config import ROOT, resolve_path

PROGRESS_EVENTS = frozenset({"verify", "next_smell", "complete"})
SMELL_METRIC_KEYS = (
    "remaining_smells",
    "introduced_smells",
    "fixed_smells",
    "original_smells",
    "total_introduced_smells",
    "fixed_introduced_smells",
)


@dataclass(frozen=True, slots=True)
class PiEvent:
    """One line from ``data/pi/*.jsonl``."""

    source: Path
    ts: str
    event: str
    case_id: str | None
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class CaseProgress:
    """Latest smell progress for one case."""

    case_id: str
    event: str
    ts: str
    source: Path
    remaining_smells: int | None
    introduced_smells: int | None
    fixed_smells: int | None
    original_smells: int | None
    total_introduced_smells: int | None
    fixed_introduced_smells: int | None
    stop_reason: str | None
    run_log: str | None

    @property
    def short_name(self) -> str:
        project, _, suffix = self.case_id.partition(":")
        if len(suffix) > 12:
            suffix = suffix[:12]
        return f"{project}:{suffix}"


@dataclass(frozen=True, slots=True)
class EliotLogSummary:
    """Smell-related fields parsed from one ``all.log``."""

    path: Path
    case_id: str | None
    original_smells: int | None
    remaining_smells: int | None
    introduced_smells: int | None
    fixed_smells: int | None
    original_smells_total: int | None
    total_introduced_smells: int | None
    fixed_introduced_smells: int | None
    stop_reason: str | None
    last_message_type: str | None
    line_count: int


@dataclass(frozen=True, slots=True)
class SmellQueueBreakdown:
    """Queue composition from ``smells.json``."""

    path: Path
    case_id: str
    queue_size: int
    baseline_in_queue: int
    introduced_in_queue: int
    other_in_queue: int


@dataclass(frozen=True, slots=True)
class SuccessfulCaseStats:
    """Model, smell, and CK statistics for one successful batch case."""

    planner: str
    case_id: str
    stop_reason: str
    original_smells: int
    fixed_smells: int
    total_introduced_smells: int
    fixed_introduced_smells: int
    completed_smell_tasks: int
    llm_turns: int
    turns_per_smell: float
    mean_minutes_between_smells: float
    median_minutes_between_smells: float
    p95_minutes_between_smells: float
    median_turns_per_smell: float
    p95_turns_per_smell: int
    max_turns_per_smell: int
    max_attempt: int
    escalated_smell_tasks: int
    weak_model_turns: int
    strong_model_turns: int
    weak_model_tokens: int
    strong_model_tokens: int
    weak_model_cost_usd: float
    strong_model_cost_usd: float
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    total_tokens: int
    cost_usd: float
    duration_minutes: float
    verification_minutes: float
    verification_share: float
    verification_count: int
    verification_failures: int
    compilation_failures: int
    test_failures: int
    llm_error_turns: int
    peak_introduced_smells: int
    classes: int | None
    methods: int | None
    mean_cbo: float | None
    mean_lcom: float | None
    mean_wmc: float | None
    total_loc: float | None


@dataclass(frozen=True, slots=True)
class BatchOutcomeStats:
    """Final outcome counts for one completed planner batch."""

    planner: str
    profile: str
    weak_model: str
    strong_model: str
    cases: int
    successful: int
    failed: int
    skipped: int
    incomplete: int
    stop_reasons: tuple[tuple[str, int], ...]


@dataclass(slots=True)
class _CaseStatsAccumulator:
    """Mutable state while one batch activity log is parsed."""

    case_id: str
    started_at: float | None = None
    ended_at: float | None = None
    stop_reason: str = ""
    original_smells: int = 0
    fixed_smells: int = 0
    total_introduced_smells: int = 0
    fixed_introduced_smells: int = 0
    llm_turns: int = 0
    weak_model_turns: int = 0
    strong_model_turns: int = 0
    weak_model_tokens: int = 0
    strong_model_tokens: int = 0
    weak_model_cost_usd: float = 0.0
    strong_model_cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    completed_smell_keys: set[str] = field(default_factory=set)
    smell_completed_at: dict[str, float] = field(default_factory=dict)
    smell_turn_counts: dict[str, int] = field(default_factory=dict)
    smell_max_attempts: dict[str, int] = field(default_factory=dict)
    last_llm_turn_at: float | None = None
    verification_seconds: float = 0.0
    verification_count: int = 0
    verification_failures: int = 0
    compilation_failures: int = 0
    test_failures: int = 0
    llm_error_turns: int = 0
    peak_introduced_smells: int = 0
    classes: int | None = None
    methods: int | None = None
    mean_cbo: float | None = None
    mean_lcom: float | None = None
    mean_wmc: float | None = None
    total_loc: float | None = None


@dataclass(frozen=True, slots=True)
class _EvolutionTurn:
    timestamp: float
    smell_key: str


@dataclass(frozen=True, slots=True)
class _EvolutionVerify:
    timestamp: float
    accepted: bool
    total_introduced_smells: int


@dataclass(frozen=True, slots=True)
class _EvolutionCompletion:
    timestamp: float
    smell_key: str
    tokens: int


@dataclass(slots=True)
class _EvolutionCaseAccumulator:
    turns: list[_EvolutionTurn] = field(default_factory=list)
    verifications: list[_EvolutionVerify] = field(default_factory=list)
    completions: dict[str, _EvolutionCompletion] = field(default_factory=dict)
    code_metrics: dict[str, float | int | None] = field(default_factory=dict)


def _parse_ts(raw: str) -> datetime:
    normalized = raw.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _display_path(path: Path) -> str:
    absolute = path.resolve()
    try:
        return str(absolute.relative_to(ROOT))
    except ValueError:
        return str(absolute)


def pi_events_dir(data_dir: Path | None = None) -> Path:
    """Return the pi output directory."""
    if data_dir is not None:
        return data_dir
    return pi_output_dir()


def iter_pi_event_files(data_dir: Path) -> list[Path]:
    """Return jsonl files that may contain pi events."""
    files = [data_dir / "runs.jsonl"]
    files.extend(sorted(data_dir.glob("*.jsonl")))
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in files:
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        unique.append(path)
    return unique


def iter_pi_events(
    data_dir: Path | None = None,
    *,
    batch_prefix: str | None = None,
    case_id: str | None = None,
) -> Iterator[PiEvent]:
    """Yield parsed events from ``data/pi`` jsonl files."""
    root = pi_events_dir(data_dir)
    for path in iter_pi_event_files(root):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            if batch_prefix is not None and batch_prefix not in line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            event_type = record.get("event")
            if not isinstance(event_type, str):
                continue
            record_case_id = record.get("case_id")
            if case_id is not None and record_case_id != case_id:
                continue
            ts = record.get("ts")
            if not isinstance(ts, str):
                ts = ""
            yield PiEvent(
                source=path,
                ts=ts,
                event=event_type,
                case_id=record_case_id if isinstance(record_case_id, str) else None,
                payload=dict(record),
            )


def latest_progress_by_case(
    data_dir: Path | None = None,
    *,
    batch_prefix: str | None = None,
    case_ids: set[str] | None = None,
) -> dict[str, CaseProgress]:
    """Return the newest verify/next_smell/complete event per case."""
    latest: dict[str, CaseProgress] = {}
    for event in iter_pi_events(data_dir, batch_prefix=batch_prefix):
        if event.event not in PROGRESS_EVENTS:
            continue
        if event.case_id is None:
            continue
        if case_ids is not None and event.case_id not in case_ids:
            continue
        progress = CaseProgress(
            case_id=event.case_id,
            event=event.event,
            ts=event.ts,
            source=event.source,
            remaining_smells=_optional_int(event.payload.get("remaining_smells")),
            introduced_smells=_optional_int(event.payload.get("introduced_smells")),
            fixed_smells=_optional_int(event.payload.get("fixed_smells")),
            original_smells=_optional_int(event.payload.get("original_smells")),
            total_introduced_smells=_optional_int(
                event.payload.get("total_introduced_smells")
            ),
            fixed_introduced_smells=_optional_int(
                event.payload.get("fixed_introduced_smells")
            ),
            stop_reason=_optional_str(event.payload.get("stop_reason")),
            run_log=_optional_str(event.payload.get("run_log")),
        )
        previous = latest.get(event.case_id)
        if previous is None or event.ts >= previous.ts:
            latest[event.case_id] = progress
    return latest


def find_batch_run_logs(
    batch_prefix: str,
    *,
    data_dir: Path | None = None,
) -> list[Path]:
    """Return ``all.log`` paths whose run directory name contains ``batch_prefix``."""
    root = pi_events_dir(data_dir) / "runs"
    if not root.is_dir():
        return []
    return sorted(root.glob(f"*/{batch_prefix}*/all.log"))


def find_batch_smells_json(
    batch_prefix: str,
    *,
    data_dir: Path | None = None,
) -> list[Path]:
    """Return ``smells.json`` paths for one batch prefix."""
    root = pi_events_dir(data_dir) / "runs"
    if not root.is_dir():
        return []
    return sorted(root.glob(f"*/{batch_prefix}*/smells.json"))


def parse_eliot_log(path: Path) -> EliotLogSummary:
    """Parse smell metrics from one Eliot ``all.log`` file."""
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    summary = EliotLogSummary(
        path=path,
        case_id=None,
        original_smells=None,
        remaining_smells=None,
        introduced_smells=None,
        fixed_smells=None,
        original_smells_total=None,
        total_introduced_smells=None,
        fixed_introduced_smells=None,
        stop_reason=None,
        last_message_type=None,
        line_count=len(lines),
    )
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        message_type = record.get("message_type")
        if not isinstance(message_type, str):
            continue
        summary = replace(summary, last_message_type=message_type)
        case_id = record.get("case_id")
        if isinstance(case_id, str):
            summary = replace(summary, case_id=case_id)
        if message_type == "deep:case_start":
            summary = replace(
                summary,
                original_smells=_optional_int(record.get("smell_count")),
            )
        if message_type in {"pi:verify", "deep:case_summary"}:
            summary = _merge_eliot_metrics(summary, record)
        if message_type == "deep:case_summary":
            summary = replace(
                summary,
                stop_reason=_optional_str(record.get("stop_reason")),
            )
    return summary


def _merge_eliot_metrics(
    summary: EliotLogSummary,
    record: dict[str, object],
) -> EliotLogSummary:
    updates: dict[str, object] = {}
    for key in SMELL_METRIC_KEYS:
        value = _optional_int(record.get(key))
        if value is None:
            continue
        if key == "original_smells":
            updates["original_smells_total"] = value
        else:
            updates[key] = value
    if not updates:
        return summary
    return replace(summary, **updates)


def parse_smell_queue(path: Path) -> SmellQueueBreakdown:
    """Count baseline vs introduced smells still in the queue."""
    state = json.loads(path.read_text(encoding="utf-8"))
    case_id = str(state["case_id"])
    baseline = set(state["baseline_identity_keys"])
    introduced = set(state["introduced_identity_keys"])
    queue = list(state["queue"])
    baseline_in_queue = sum(1 for key in queue if key in baseline)
    introduced_in_queue = sum(1 for key in queue if key in introduced)
    other_in_queue = len(queue) - baseline_in_queue - introduced_in_queue
    return SmellQueueBreakdown(
        path=path,
        case_id=case_id,
        queue_size=len(queue),
        baseline_in_queue=baseline_in_queue,
        introduced_in_queue=introduced_in_queue,
        other_in_queue=other_in_queue,
    )


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _iter_json_records(path: Path) -> Iterator[dict[str, object]]:
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.startswith("{"):
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield dict(record)


def _model_matches(configured_model: str, event_model: str) -> bool:
    return configured_model == event_model or configured_model.endswith(f"/{event_model}")


def _update_usage(
    accumulator: _CaseStatsAccumulator,
    record: dict[str, object],
) -> None:
    accumulator.llm_turns = _optional_int(record.get("turn_count")) or 0
    accumulator.input_tokens = _optional_int(record.get("input_tokens")) or 0
    accumulator.output_tokens = _optional_int(record.get("output_tokens")) or 0
    accumulator.cache_read_tokens = _optional_int(record.get("cache_read_tokens")) or 0
    accumulator.cache_write_tokens = _optional_int(record.get("cache_write_tokens")) or 0
    accumulator.total_tokens = _optional_int(record.get("total_tokens")) or 0
    accumulator.cost_usd = _optional_float(record.get("cost_usd")) or 0.0


def _update_llm_turn(
    accumulator: _CaseStatsAccumulator,
    record: dict[str, object],
    *,
    weak_model: str,
    strong_model: str,
) -> None:
    accumulator.last_llm_turn_at = _optional_float(record.get("timestamp"))
    model = _optional_str(record.get("model")) or ""
    input_tokens = _optional_int(record.get("input_tokens")) or 0
    output_tokens = _optional_int(record.get("output_tokens")) or 0
    billable_tokens = input_tokens + output_tokens
    cost = _optional_float(record.get("cost_total_usd")) or 0.0
    if _model_matches(weak_model, model):
        accumulator.weak_model_turns += 1
        accumulator.weak_model_tokens += billable_tokens
        accumulator.weak_model_cost_usd += cost
    if _model_matches(strong_model, model):
        accumulator.strong_model_turns += 1
        accumulator.strong_model_tokens += billable_tokens
        accumulator.strong_model_cost_usd += cost
    if record.get("stop_reason") == "error":
        accumulator.llm_error_turns += 1

    smell_key = record.get("smell_key")
    if not isinstance(smell_key, str):
        return
    accumulator.smell_turn_counts[smell_key] = (
        accumulator.smell_turn_counts.get(smell_key, 0) + 1
    )
    attempt = _optional_int(record.get("attempt_number")) or 0
    previous_attempt = accumulator.smell_max_attempts.get(smell_key, 0)
    accumulator.smell_max_attempts[smell_key] = max(previous_attempt, attempt)


def _update_verification(
    accumulator: _CaseStatsAccumulator,
    record: dict[str, object],
) -> None:
    accumulator.verification_count += 1
    verified_at = _optional_float(record.get("timestamp"))
    if accumulator.last_llm_turn_at is not None and verified_at is not None:
        accumulator.verification_seconds += max(
            0.0,
            verified_at - accumulator.last_llm_turn_at,
        )
    accumulator.last_llm_turn_at = None
    introduced_smells = _optional_int(record.get("introduced_smells")) or 0
    accumulator.peak_introduced_smells = max(
        accumulator.peak_introduced_smells,
        introduced_smells,
    )
    if record.get("passed") is not False:
        return
    accumulator.verification_failures += 1
    content = _optional_str(record.get("content")) or ""
    if "diagnostic_type=compilation_error" in content:
        accumulator.compilation_failures += 1
    else:
        accumulator.test_failures += 1


def _update_ck(
    accumulator: _CaseStatsAccumulator,
    record: dict[str, object],
) -> None:
    accumulator.classes = _optional_int(record.get("classes"))
    accumulator.methods = _optional_int(record.get("methods"))
    accumulator.mean_cbo = _optional_float(record.get("mean_cbo"))
    accumulator.mean_lcom = _optional_float(record.get("mean_lcom"))
    accumulator.mean_wmc = _optional_float(record.get("mean_wmc"))
    accumulator.total_loc = _optional_float(record.get("total_loc"))


def _update_case_summary(
    accumulator: _CaseStatsAccumulator,
    record: dict[str, object],
) -> None:
    accumulator.ended_at = _optional_float(record.get("timestamp"))
    accumulator.stop_reason = _optional_str(record.get("stop_reason")) or ""
    accumulator.original_smells = _optional_int(record.get("original_smells")) or 0
    accumulator.fixed_smells = _optional_int(record.get("fixed_smells")) or 0
    accumulator.total_introduced_smells = (
        _optional_int(record.get("total_introduced_smells")) or 0
    )
    accumulator.fixed_introduced_smells = (
        _optional_int(record.get("fixed_introduced_smells")) or 0
    )
    accumulator.peak_introduced_smells = max(
        accumulator.peak_introduced_smells,
        _optional_int(record.get("introduced_smells")) or 0,
    )


def _update_batch_result(
    accumulator: _CaseStatsAccumulator,
    result: dict[str, object],
) -> None:
    accumulator.stop_reason = _optional_str(result.get("stopReason")) or ""
    accumulator.original_smells = _optional_int(result.get("originalSmellCount")) or 0
    accumulator.fixed_smells = _optional_int(result.get("fixedSmellCount")) or 0
    accumulator.total_introduced_smells = (
        _optional_int(result.get("totalIntroducedSmellCount")) or 0
    )
    accumulator.fixed_introduced_smells = (
        _optional_int(result.get("fixedIntroducedSmellCount")) or 0
    )


def parse_batch_outcome(path: Path) -> BatchOutcomeStats:
    """Parse final success, failure, and skip counts from one batch log."""
    planner = ""
    profile = ""
    weak_model = ""
    strong_model = ""
    started_cases = 0
    final_results: list[dict[str, object]] | None = None

    for record in _iter_json_records(path):
        event = record.get("event")
        if event == "pi_batch:start":
            planner = _optional_str(record.get("planner")) or ""
            profile = _optional_str(record.get("profile")) or ""
            weak_model = _optional_str(record.get("weakModel")) or ""
            strong_model = _optional_str(record.get("strongModel")) or ""
            started_cases = _optional_int(record.get("cases")) or 0
        elif event == "pi_batch:done":
            results = record.get("results")
            if isinstance(results, list):
                final_results = [
                    dict(result)
                    for result in results
                    if isinstance(result, dict)
                ]

    if final_results is None:
        return BatchOutcomeStats(
            planner=planner,
            profile=profile,
            weak_model=weak_model,
            strong_model=strong_model,
            cases=started_cases,
            successful=0,
            failed=0,
            skipped=0,
            incomplete=started_cases,
            stop_reasons=(("incomplete", started_cases),),
        )

    successful = sum(result.get("ok") is True for result in final_results)
    skipped = sum(
        result.get("stopReason") == "batch_aborted" for result in final_results
    )
    failed = len(final_results) - successful - skipped
    reason_counts: dict[str, int] = {}
    for result in final_results:
        reason = _optional_str(result.get("stopReason")) or "unknown"
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
    return BatchOutcomeStats(
        planner=planner,
        profile=profile,
        weak_model=weak_model,
        strong_model=strong_model,
        cases=len(final_results),
        successful=successful,
        failed=failed,
        skipped=skipped,
        incomplete=0,
        stop_reasons=tuple(sorted(reason_counts.items())),
    )


def parse_successful_batch_log(path: Path) -> list[SuccessfulCaseStats]:
    """Parse successful cases and their model/CK statistics from one batch log."""
    planner = ""
    weak_model = ""
    strong_model = ""
    accumulators: dict[str, _CaseStatsAccumulator] = {}
    successful_case_ids: list[str] | None = None

    for record in _iter_json_records(path):
        event = record.get("event")
        if event == "pi_batch:start":
            planner = _optional_str(record.get("planner")) or ""
            weak_model = _optional_str(record.get("weakModel")) or ""
            strong_model = _optional_str(record.get("strongModel")) or ""
            continue
        if event == "pi_batch:done":
            successful_case_ids = []
            results = record.get("results")
            if not isinstance(results, list):
                continue
            for raw_result in results:
                if not isinstance(raw_result, dict) or raw_result.get("ok") is not True:
                    continue
                result = dict(raw_result)
                case_id = result.get("caseId")
                if not isinstance(case_id, str):
                    continue
                successful_case_ids.append(case_id)
                if case_id not in accumulators:
                    accumulators[case_id] = _CaseStatsAccumulator(case_id=case_id)
                _update_batch_result(accumulators[case_id], result)
            continue

        case_id = record.get("case_id")
        message_type = record.get("message_type")
        if not isinstance(case_id, str) or not isinstance(message_type, str):
            continue
        if case_id not in accumulators:
            accumulators[case_id] = _CaseStatsAccumulator(case_id=case_id)
        accumulator = accumulators[case_id]

        match message_type:
            case "deep:case_start":
                accumulator.started_at = _optional_float(record.get("timestamp"))
            case "pi:llm_turn":
                _update_llm_turn(
                    accumulator,
                    record,
                    weak_model=weak_model,
                    strong_model=strong_model,
                )
            case "pi:refactoring_complete":
                smell_key = record.get("smell_key")
                if isinstance(smell_key, str):
                    accumulator.completed_smell_keys.add(smell_key)
                    completed_at = _optional_float(record.get("timestamp"))
                    if completed_at is not None and smell_key not in accumulator.smell_completed_at:
                        accumulator.smell_completed_at[smell_key] = completed_at
            case "pi:verify":
                _update_verification(accumulator, record)
            case "pi:smells_introduced":
                introduced_smells = _optional_int(record.get("introduced_smells")) or 0
                accumulator.peak_introduced_smells = max(
                    accumulator.peak_introduced_smells,
                    introduced_smells,
                )
            case "pi:case_cost":
                _update_usage(accumulator, record)
            case "deep:case_summary":
                _update_case_summary(accumulator, record)
            case "pi:ck":
                _update_ck(accumulator, record)

    if successful_case_ids is None:
        return []

    statistics: list[SuccessfulCaseStats] = []
    for case_id in successful_case_ids:
        accumulator = accumulators[case_id]
        completed_smells = len(accumulator.completed_smell_keys)
        smell_turn_counts = sorted(
            accumulator.smell_turn_counts.get(smell_key, 0)
            for smell_key in accumulator.completed_smell_keys
        )
        turns_per_smell = (
            accumulator.llm_turns / completed_smells if completed_smells else 0.0
        )
        completion_times = sorted(accumulator.smell_completed_at.values())
        completion_intervals = [
            (later - earlier) / 60
            for earlier, later in zip(
                completion_times,
                completion_times[1:],
                strict=False,
            )
        ]
        mean_minutes_between_smells = (
            sum(completion_intervals) / len(completion_intervals)
            if completion_intervals
            else 0.0
        )
        median_minutes_between_smells = (
            float(median(completion_intervals)) if completion_intervals else 0.0
        )
        sorted_intervals = sorted(completion_intervals)
        p95_minutes_between_smells = (
            sorted_intervals[math.ceil(len(sorted_intervals) * 0.95) - 1]
            if sorted_intervals
            else 0.0
        )
        median_turns_per_smell = (
            float(median(smell_turn_counts)) if smell_turn_counts else 0.0
        )
        p95_turns_per_smell = (
            smell_turn_counts[math.ceil(len(smell_turn_counts) * 0.95) - 1]
            if smell_turn_counts
            else 0
        )
        max_turns_per_smell = max(smell_turn_counts, default=0)
        completed_attempts = [
            accumulator.smell_max_attempts.get(smell_key, 0)
            for smell_key in accumulator.completed_smell_keys
        ]
        max_attempt = max(completed_attempts, default=0)
        escalated_smells = sum(attempt >= 3 for attempt in completed_attempts)
        if accumulator.started_at is not None and accumulator.ended_at is not None:
            duration_minutes = max(
                0.0,
                (accumulator.ended_at - accumulator.started_at) / 60,
            )
        else:
            duration_minutes = 0.0
        verification_minutes = accumulator.verification_seconds / 60
        verification_share = (
            verification_minutes / duration_minutes if duration_minutes else 0.0
        )
        statistics.append(
            SuccessfulCaseStats(
                planner=planner,
                case_id=case_id,
                stop_reason=accumulator.stop_reason,
                original_smells=accumulator.original_smells,
                fixed_smells=accumulator.fixed_smells,
                total_introduced_smells=accumulator.total_introduced_smells,
                fixed_introduced_smells=accumulator.fixed_introduced_smells,
                completed_smell_tasks=completed_smells,
                llm_turns=accumulator.llm_turns,
                turns_per_smell=turns_per_smell,
                mean_minutes_between_smells=mean_minutes_between_smells,
                median_minutes_between_smells=median_minutes_between_smells,
                p95_minutes_between_smells=p95_minutes_between_smells,
                median_turns_per_smell=median_turns_per_smell,
                p95_turns_per_smell=p95_turns_per_smell,
                max_turns_per_smell=max_turns_per_smell,
                max_attempt=max_attempt,
                escalated_smell_tasks=escalated_smells,
                weak_model_turns=accumulator.weak_model_turns,
                strong_model_turns=accumulator.strong_model_turns,
                weak_model_tokens=accumulator.weak_model_tokens,
                strong_model_tokens=accumulator.strong_model_tokens,
                weak_model_cost_usd=accumulator.weak_model_cost_usd,
                strong_model_cost_usd=accumulator.strong_model_cost_usd,
                input_tokens=accumulator.input_tokens,
                output_tokens=accumulator.output_tokens,
                cache_read_tokens=accumulator.cache_read_tokens,
                cache_write_tokens=accumulator.cache_write_tokens,
                total_tokens=accumulator.total_tokens,
                cost_usd=accumulator.cost_usd,
                duration_minutes=duration_minutes,
                verification_minutes=verification_minutes,
                verification_share=verification_share,
                verification_count=accumulator.verification_count,
                verification_failures=accumulator.verification_failures,
                compilation_failures=accumulator.compilation_failures,
                test_failures=accumulator.test_failures,
                llm_error_turns=accumulator.llm_error_turns,
                peak_introduced_smells=accumulator.peak_introduced_smells,
                classes=accumulator.classes,
                methods=accumulator.methods,
                mean_cbo=accumulator.mean_cbo,
                mean_lcom=accumulator.mean_lcom,
                mean_wmc=accumulator.mean_wmc,
                total_loc=accumulator.total_loc,
            )
        )
    return statistics


def build_interactive_evolution_data(
    paths: list[Path],
    *,
    manifest_path: Path,
) -> dict[str, object]:
    """Build validated per-fix trajectories for successful planner-case runs."""
    case_order = load_manifest_case_ids(manifest_path)
    planner_order = ("none", "greedy", "topo", "bfs")
    cases: dict[str, dict[str, object]] = {
        case_id: {"planners": {}} for case_id in case_order
    }

    for path in paths:
        planner = ""
        accumulators: dict[str, _EvolutionCaseAccumulator] = {}
        successful_case_ids: set[str] = set()
        for record in _iter_json_records(path):
            event = record.get("event")
            if event == "pi_batch:start":
                planner = _optional_str(record.get("planner")) or ""
                continue
            if event == "pi_batch:done":
                results = record.get("results")
                if isinstance(results, list):
                    successful_case_ids = {
                        str(result["caseId"])
                        for result in results
                        if isinstance(result, dict)
                        and result.get("ok") is True
                        and isinstance(result.get("caseId"), str)
                    }
                continue

            case_id = record.get("case_id")
            message_type = record.get("message_type")
            if not isinstance(case_id, str) or not isinstance(message_type, str):
                continue
            accumulator = accumulators.setdefault(
                case_id,
                _EvolutionCaseAccumulator(),
            )
            timestamp = _optional_float(record.get("timestamp"))
            match message_type:
                case "pi:llm_turn":
                    smell_key = record.get("smell_key")
                    if timestamp is not None and isinstance(smell_key, str):
                        accumulator.turns.append(
                            _EvolutionTurn(timestamp=timestamp, smell_key=smell_key)
                        )
                case "pi:verify":
                    if timestamp is None:
                        continue
                    accepted = (
                        record.get("tests_acceptable") is True
                        and record.get("current_smell_present") is False
                    )
                    accumulator.verifications.append(
                        _EvolutionVerify(
                            timestamp=timestamp,
                            accepted=accepted,
                            total_introduced_smells=(
                                _optional_int(record.get("total_introduced_smells"))
                                or 0
                            ),
                        )
                    )
                case "pi:refactoring_complete":
                    smell_key = record.get("smell_key")
                    if timestamp is None or not isinstance(smell_key, str):
                        continue
                    accumulator.completions.setdefault(
                        smell_key,
                        _EvolutionCompletion(
                            timestamp=timestamp,
                            smell_key=smell_key,
                            tokens=(
                                (_optional_int(record.get("input_tokens")) or 0)
                                + (_optional_int(record.get("output_tokens")) or 0)
                            ),
                        ),
                    )
                case "pi:ck":
                    accumulator.code_metrics = {
                        "meanCbo": _optional_float(record.get("mean_cbo")),
                        "meanLcom": _optional_float(record.get("mean_lcom")),
                        "meanWmc": _optional_float(record.get("mean_wmc")),
                        "classes": _optional_int(record.get("classes")),
                        "methods": _optional_int(record.get("methods")),
                        "loc": _optional_float(record.get("total_loc")),
                    }

        if planner not in planner_order:
            continue
        for case_id in successful_case_ids:
            accumulator = accumulators.get(case_id)
            if accumulator is None or case_id not in cases:
                continue
            turns = sorted(accumulator.turns, key=lambda item: item.timestamp)
            verifications = sorted(
                accumulator.verifications,
                key=lambda item: item.timestamp,
            )
            completions = sorted(
                accumulator.completions.values(),
                key=lambda item: item.timestamp,
            )
            trajectory: list[dict[str, int | float | str]] = []
            previous_introduced = 0
            for fix_index, completion in enumerate(completions, start=1):
                turns_so_far = sum(
                    turn.timestamp <= completion.timestamp for turn in turns
                )
                verifies_so_far = [
                    verification
                    for verification in verifications
                    if verification.timestamp <= completion.timestamp
                ]
                successful_edits = sum(
                    verification.accepted for verification in verifies_so_far
                )
                success_rate = (
                    successful_edits / len(verifies_so_far) * 100
                    if verifies_so_far
                    else 0.0
                )
                cumulative_introduced = (
                    verifies_so_far[-1].total_introduced_smells
                    if verifies_so_far
                    else previous_introduced
                )
                introduced_this_fix = max(
                    0,
                    cumulative_introduced - previous_introduced,
                )
                previous_introduced = cumulative_introduced
                trajectory.append(
                    {
                        "fix": fix_index,
                        "smellKey": completion.smell_key,
                        "cumulativeTurns": turns_so_far,
                        "editSuccessRate": success_rate,
                        "introducedPerFix": introduced_this_fix,
                        "cumulativeIntroduced": cumulative_introduced,
                        "tokensPerFix": completion.tokens,
                    }
                )

            cumulative_turns = [
                int(point["cumulativeTurns"]) for point in trajectory
            ]
            cumulative_introduced_values = [
                int(point["cumulativeIntroduced"]) for point in trajectory
            ]
            success_rates = [
                float(point["editSuccessRate"]) for point in trajectory
            ]
            if cumulative_turns != sorted(cumulative_turns):
                raise ValueError(f"Turns are not monotonic for {planner}/{case_id}")
            if cumulative_introduced_values != sorted(cumulative_introduced_values):
                raise ValueError(
                    f"Introduced-smell total is not monotonic for {planner}/{case_id}"
                )
            if any(rate < 0 or rate > 100 for rate in success_rates):
                raise ValueError(f"Invalid success rate for {planner}/{case_id}")
            if any(int(point["tokensPerFix"]) < 0 for point in trajectory):
                raise ValueError(f"Negative token count for {planner}/{case_id}")

            planner_rows = cases[case_id]["planners"]
            if not isinstance(planner_rows, dict):
                raise TypeError("planner rows must be a dictionary")
            planner_rows[planner] = {
                "trajectory": trajectory,
                "code": accumulator.code_metrics,
                "source": _display_path(path),
            }

    return {
        "caseOrder": case_order,
        "plannerOrder": list(planner_order),
        "cases": cases,
    }


def load_manifest_case_ids(manifest_path: Path) -> list[str]:
    """Read case ids from a manifest jsonl file."""
    case_ids: list[str] = []
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        case_id = record.get("case_id")
        if isinstance(case_id, str):
            case_ids.append(case_id)
    return case_ids


def case_ids_from_prepare_events(
    data_dir: Path | None = None,
    *,
    batch_prefix: str | None = None,
) -> dict[str, dict[str, object]]:
    """Return the latest prepare event per case (optionally filtered by batch)."""
    prepared: dict[str, dict[str, object]] = {}
    for event in iter_pi_events(data_dir, batch_prefix=batch_prefix):
        if event.event != "prepare" or event.case_id is None:
            continue
        previous = prepared.get(event.case_id)
        if previous is None or event.ts >= str(previous.get("ts", "")):
            prepared[event.case_id] = event.payload
    return prepared


def format_progress_line(progress: CaseProgress) -> str:
    """Format one case progress row."""
    original = progress.fixed_smells
    original_total = progress.original_smells
    intro_fixed = progress.fixed_introduced_smells
    intro_total = progress.total_introduced_smells
    original_text = (
        f"{original}/{original_total}"
        if original is not None and original_total is not None
        else "?"
    )
    if intro_total is not None and intro_fixed is not None:
        intro_text = f"{intro_fixed}/{intro_total}"
    elif progress.introduced_smells is not None:
        intro_text = str(progress.introduced_smells)
    else:
        intro_text = "?"
    remaining = progress.remaining_smells
    remaining_text = str(remaining) if remaining is not None else "?"
    stop = progress.stop_reason or progress.event
    age = ""
    if progress.ts:
        seconds = (datetime.now(timezone.utc) - _parse_ts(progress.ts)).total_seconds()
        age = f", last {seconds / 60:.1f}m ago"
    return (
        f"{progress.short_name}: rem={remaining_text} "
        f"orig={original_text} intro={intro_text} "
        f"stop={stop}{age}"
    )


def print_batch_report(
    batch_prefix: str,
    *,
    data_dir: Path | None = None,
    manifest_path: Path | None = None,
) -> None:
    """Print a batch status report like the ad-hoc monitoring scripts."""
    prepared = case_ids_from_prepare_events(data_dir, batch_prefix=batch_prefix)
    progress = latest_progress_by_case(
        data_dir,
        batch_prefix=batch_prefix,
        case_ids=set(prepared) if prepared else None,
    )

    print(f"Batch prefix: {batch_prefix}")
    print(f"Prepared cases: {len(prepared)}")
    print(f"Cases with progress events: {len(progress)}")

    run_logs = find_batch_run_logs(batch_prefix, data_dir=data_dir)
    print(f"Run logs: {len(run_logs)}")
    for path in run_logs:
        print(f"  {_display_path(path)}")

    if manifest_path is not None and manifest_path.is_file():
        manifest_cases = load_manifest_case_ids(manifest_path)
        started = set(prepared) | set(progress)
        pending = [case_id for case_id in manifest_cases if case_id not in started]
        print(f"Manifest: {len(manifest_cases)} cases")
        print(f"Pending: {len(pending)}")
        for case_id in pending:
            print(f"  {case_id}")

    print()
    print("=== latest progress ===")
    total_remaining = 0
    total_introduced = 0
    total_original_fixed = 0
    total_original = 0
    for case_id in sorted(progress, key=lambda value: value.lower()):
        item = progress[case_id]
        print(format_progress_line(item))
        if item.remaining_smells is not None:
            total_remaining += item.remaining_smells
        if item.introduced_smells is not None:
            total_introduced += item.introduced_smells
        if item.fixed_smells is not None:
            total_original_fixed += item.fixed_smells
        if item.original_smells is not None:
            total_original += item.original_smells

    print()
    print(
        f"Totals: remaining={total_remaining} introduced_outstanding={total_introduced} "
        f"original_fixed={total_original_fixed}/{total_original}"
    )

    smells_files = find_batch_smells_json(batch_prefix, data_dir=data_dir)
    if smells_files:
        print()
        print("=== smell queues ===")
        for path in smells_files:
            breakdown = parse_smell_queue(path)
            print(
                f"{breakdown.case_id}: queue={breakdown.queue_size} "
                f"({breakdown.baseline_in_queue} baseline, "
                f"{breakdown.introduced_in_queue} introduced, "
                f"{breakdown.other_in_queue} other)"
            )


def print_eliot_logs(paths: list[Path]) -> None:
    """Print summaries for one or more Eliot log files."""
    for path in paths:
        summary = parse_eliot_log(path)
        print(f"{_display_path(path)} ({summary.line_count} lines)")
        if summary.case_id:
            print(f"  case_id: {summary.case_id}")
        if summary.original_smells is not None:
            print(f"  start smells: {summary.original_smells}")
        if summary.fixed_smells is not None and summary.original_smells_total is not None:
            print(
                f"  original: {summary.fixed_smells}/{summary.original_smells_total}"
            )
        if (
            summary.fixed_introduced_smells is not None
            and summary.total_introduced_smells is not None
        ):
            print(
                "  introduced: "
                f"{summary.fixed_introduced_smells}/{summary.total_introduced_smells}"
            )
        if summary.remaining_smells is not None:
            print(f"  remaining: {summary.remaining_smells}")
        if summary.introduced_smells is not None:
            print(f"  introduced outstanding: {summary.introduced_smells}")
        if summary.stop_reason:
            print(f"  stop_reason: {summary.stop_reason}")
        if summary.last_message_type:
            print(f"  last message: {summary.last_message_type}")
        print()


def _markdown_text(value: str) -> str:
    return value.replace("|", "\\|")


def _optional_metric(value: int | float | None, digits: int = 2) -> str:
    if value is None:
        return "-"
    if isinstance(value, int):
        return str(value)
    return f"{value:.{digits}f}"


def print_successful_case_stats(paths: list[Path]) -> None:
    """Print model, smell, and CK tables for successful batch cases."""
    outcomes = [parse_batch_outcome(path) for path in paths]
    statistics = [
        item
        for path in paths
        for item in parse_successful_batch_log(path)
    ]

    print("## Batch outcomes")
    print()
    print(
        "| Planner | Profile | Cases | Successful | Failed | Skipped | Incomplete | "
        "Success rate | Weak model | Strong model | Stop reasons |"
    )
    print("|---|---|---:|---:|---:|---:|---:|---:|---|---|---|")
    for outcome in outcomes:
        success_rate = (
            outcome.successful / outcome.cases if outcome.cases else 0.0
        )
        reasons = ", ".join(
            f"{reason}={count}" for reason, count in outcome.stop_reasons
        )
        print(
            f"| {outcome.planner} | {outcome.profile} | {outcome.cases} | "
            f"{outcome.successful} | {outcome.failed} | {outcome.skipped} | "
            f"{outcome.incomplete} | "
            f"{success_rate:.1%} | {outcome.weak_model} | {outcome.strong_model} | "
            f"{reasons} |"
        )

    print()
    print("## Planner totals")
    print()
    print(
        "| Planner | Cases | Original fixed | Introduced | Introduced fixed | "
        "Total fixed | Tasks | Turns | Turns/task | Escalated | Verifications | "
        "Failed | LLM errors | Tokens | Cache read | Cost USD | Minutes |"
    )
    print(
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
        "---:|---:|---:|---:|"
    )
    planners = sorted({item.planner for item in statistics})
    for planner in planners:
        rows = [item for item in statistics if item.planner == planner]
        original_fixed = sum(item.fixed_smells for item in rows)
        original_total = sum(item.original_smells for item in rows)
        introduced_total = sum(item.total_introduced_smells for item in rows)
        introduced_fixed = sum(item.fixed_introduced_smells for item in rows)
        smell_tasks = sum(item.completed_smell_tasks for item in rows)
        turns = sum(item.llm_turns for item in rows)
        turns_per_smell = turns / smell_tasks if smell_tasks else 0.0
        print(
            f"| {planner} | {len(rows)} | {original_fixed}/{original_total} | "
            f"{introduced_total} | {introduced_fixed}/{introduced_total} | "
            f"{original_fixed + introduced_fixed} | {smell_tasks} | {turns} | "
            f"{turns_per_smell:.2f} | "
            f"{sum(item.escalated_smell_tasks for item in rows)} | "
            f"{sum(item.verification_count for item in rows)} | "
            f"{sum(item.verification_failures for item in rows)} | "
            f"{sum(item.llm_error_turns for item in rows)} | "
            f"{sum(item.total_tokens for item in rows)} | "
            f"{sum(item.cache_read_tokens for item in rows)} | "
            f"{sum(item.cost_usd for item in rows):.4f} | "
            f"{sum(item.duration_minutes for item in rows):.1f} |"
        )

    print()
    print("## Timing decomposition")
    print()
    print(
        "| Planner | Mean min between fixes | Verification min | Verification share | "
        "Other min |"
    )
    print("|---|---:|---:|---:|---:|")
    for planner in planners:
        rows = [item for item in statistics if item.planner == planner]
        interval_count = sum(max(item.completed_smell_tasks - 1, 0) for item in rows)
        interval_minutes = sum(
            item.mean_minutes_between_smells * max(item.completed_smell_tasks - 1, 0)
            for item in rows
        )
        mean_interval = interval_minutes / interval_count if interval_count else 0.0
        duration = sum(item.duration_minutes for item in rows)
        verification = sum(item.verification_minutes for item in rows)
        verification_share = verification / duration if duration else 0.0
        print(
            f"| {planner} | {mean_interval:.2f} | {verification:.1f} | "
            f"{verification_share:.1%} | {max(0.0, duration - verification):.1f} |"
        )

    print()
    print("### Case timing")
    print()
    print(
        "| Planner | Case | Mean min/fix | Median | P95 | Verification min | "
        "Verification share |"
    )
    print("|---|---|---:|---:|---:|---:|---:|")
    for item in statistics:
        print(
            f"| {item.planner} | {_markdown_text(item.case_id)} | "
            f"{item.mean_minutes_between_smells:.2f} | "
            f"{item.median_minutes_between_smells:.2f} | "
            f"{item.p95_minutes_between_smells:.2f} | "
            f"{item.verification_minutes:.1f} | {item.verification_share:.1%} |"
        )

    print()
    print("## Successful cases")
    print()
    print(
        "| Planner | Case | Original | Introduced | Peak intro | Tasks | Turns | "
        "Mean | Median | P95 | Max | Max attempt | Escalated | Cost USD | Minutes |"
    )
    print(
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
    )
    for item in statistics:
        case_id = _markdown_text(item.case_id)
        print(
            f"| {item.planner} | {case_id} | "
            f"{item.fixed_smells}/{item.original_smells} | "
            f"{item.fixed_introduced_smells}/{item.total_introduced_smells} | "
            f"{item.peak_introduced_smells} | "
            f"{item.completed_smell_tasks} | {item.llm_turns} | "
            f"{item.turns_per_smell:.2f} | {item.median_turns_per_smell:.1f} | "
            f"{item.p95_turns_per_smell} | {item.max_turns_per_smell} | "
            f"{item.max_attempt} | {item.escalated_smell_tasks} | "
            f"{item.cost_usd:.4f} | "
            f"{item.duration_minutes:.1f} |"
        )

    print()
    print("## Model and verification details")
    print()
    print(
        "| Planner | Case | Weak turns | Weak tokens | Weak cost | Strong turns | "
        "Strong tokens | Strong cost | Cache read | Cache write | Verifications | "
        "Failed | Compile | Test | LLM errors |"
    )
    print(
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
    )
    for item in statistics:
        case_id = _markdown_text(item.case_id)
        print(
            f"| {item.planner} | {case_id} | {item.weak_model_turns} | "
            f"{item.weak_model_tokens} | {item.weak_model_cost_usd:.4f} | "
            f"{item.strong_model_turns} | {item.strong_model_tokens} | "
            f"{item.strong_model_cost_usd:.4f} | {item.cache_read_tokens} | "
            f"{item.cache_write_tokens} | {item.verification_count} | "
            f"{item.verification_failures} | {item.compilation_failures} | "
            f"{item.test_failures} | {item.llm_error_turns} |"
        )

    print()
    print("## CK metrics")
    print()
    print("| Planner | Case | Classes | Methods | Mean CBO | Mean LCOM | Mean WMC | LOC |")
    print("|---|---|---:|---:|---:|---:|---:|---:|")
    for item in statistics:
        case_id = _markdown_text(item.case_id)
        print(
            f"| {item.planner} | {case_id} | "
            f"{_optional_metric(item.classes)} | {_optional_metric(item.methods)} | "
            f"{_optional_metric(item.mean_cbo)} | {_optional_metric(item.mean_lcom)} | "
            f"{_optional_metric(item.mean_wmc)} | {_optional_metric(item.total_loc, 0)} |"
        )


def _chart_values(
    rows: dict[str, SuccessfulCaseStats],
    planners: tuple[str, ...],
    getter: Callable[[SuccessfulCaseStats], int | float | None],
) -> list[float | None]:
    values: list[float | None] = []
    for planner in planners:
        row = rows.get(planner)
        value = getter(row) if row is not None else None
        values.append(float(value) if value is not None else None)
    return values


def _append_svg_panel(
    svg: list[str],
    *,
    title: str,
    top: float,
    height: float,
    planners: tuple[str, ...],
    series: list[tuple[str, str, list[float | None]]],
) -> None:
    left = 90.0
    right = 950.0
    bottom = top + height
    values = [
        value
        for _, _, points in series
        for value in points
        if value is not None
    ]
    maximum = max(values, default=1.0)
    if maximum <= 0:
        maximum = 1.0
    x_step = (right - left) / max(len(planners) - 1, 1)
    x_positions = [left + index * x_step for index in range(len(planners))]

    svg.append(
        f'<text x="{left:.0f}" y="{top - 18:.0f}" font-size="18" '
        f'font-weight="bold">{html.escape(title)}</text>'
    )
    for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
        y = bottom - fraction * height
        svg.append(
            f'<line x1="{left:.0f}" y1="{y:.1f}" x2="{right:.0f}" y2="{y:.1f}" '
            'stroke="#d1d5db" stroke-width="1"/>'
        )
        svg.append(
            f'<text x="{left - 10:.0f}" y="{y + 4:.1f}" text-anchor="end" '
            f'font-size="11" fill="#4b5563">{maximum * fraction:.2f}</text>'
        )
    svg.append(
        f'<line x1="{left:.0f}" y1="{top:.0f}" x2="{left:.0f}" y2="{bottom:.0f}" '
        'stroke="#374151" stroke-width="1.5"/>'
    )
    svg.append(
        f'<line x1="{left:.0f}" y1="{bottom:.0f}" x2="{right:.0f}" y2="{bottom:.0f}" '
        'stroke="#374151" stroke-width="1.5"/>'
    )

    for index, planner in enumerate(planners):
        svg.append(
            f'<text x="{x_positions[index]:.1f}" y="{bottom + 22:.1f}" '
            f'text-anchor="middle" font-size="12">{html.escape(planner)}</text>'
        )

    legend_x = left
    for name, color, points in series:
        coordinates = [
            (
                x_positions[index],
                bottom - value / maximum * height,
                value,
            )
            for index, value in enumerate(points)
            if value is not None
        ]
        if len(coordinates) >= 2:
            polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y, _ in coordinates)
            svg.append(
                f'<polyline points="{polyline}" fill="none" stroke="{color}" '
                'stroke-width="2.5"/>'
            )
        for x, y, value in coordinates:
            svg.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="{color}"/>')
            svg.append(
                f'<text x="{x:.1f}" y="{y - 9:.1f}" text-anchor="middle" '
                f'font-size="11" fill="{color}">{value:.2f}</text>'
            )
        svg.append(
            f'<line x1="{legend_x:.0f}" y1="{top - 42:.0f}" '
            f'x2="{legend_x + 24:.0f}" y2="{top - 42:.0f}" '
            f'stroke="{color}" stroke-width="3"/>'
        )
        svg.append(
            f'<text x="{legend_x + 30:.0f}" y="{top - 37:.0f}" '
            f'font-size="12">{html.escape(name)}</text>'
        )
        legend_x += 150


def generate_case_metric_charts(
    paths: list[Path],
    *,
    manifest_path: Path,
    output_dir: Path,
) -> list[Path]:
    """Generate one SVG planner-comparison chart per manifest case."""
    statistics = [
        item
        for path in paths
        for item in parse_successful_batch_log(path)
    ]
    planners = ("none", "greedy", "topo", "bfs")
    by_case: dict[str, dict[str, SuccessfulCaseStats]] = {}
    for item in statistics:
        by_case.setdefault(item.case_id, {})[item.planner] = item

    output_dir.mkdir(parents=True, exist_ok=True)
    generated: list[Path] = []
    index_lines = ["# Case metric charts", ""]
    html_sections = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>Case metric charts</title>",
        "<style>",
        "body{font-family:system-ui,sans-serif;max-width:1200px;margin:0 auto;padding:24px;"
        "background:#f3f4f6;color:#111827}",
        "section{background:white;margin:24px 0;padding:20px;border-radius:10px;"
        "box-shadow:0 1px 4px #0002}",
        "svg{display:block;width:100%;height:auto}",
        "h1,h2{margin-top:0}",
        "</style>",
        "</head>",
        "<body>",
        "<h1>Case metric charts</h1>",
    ]
    for case_id in load_manifest_case_ids(manifest_path):
        rows = by_case.get(case_id, {})
        svg = [
            '<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="820" '
            'viewBox="0 0 1000 820">',
            '<rect width="1000" height="820" fill="white"/>',
            f'<text x="500" y="34" text-anchor="middle" font-size="22" '
            f'font-weight="bold">{html.escape(case_id)}</text>',
        ]
        if rows:
            _append_svg_panel(
                svg,
                title="Design metrics",
                top=120,
                height=150,
                planners=planners,
                series=[
                    ("Mean CBO", "#2563eb", _chart_values(rows, planners, lambda row: row.mean_cbo)),
                    ("Mean LCOM", "#dc2626", _chart_values(rows, planners, lambda row: row.mean_lcom)),
                    ("Mean WMC", "#059669", _chart_values(rows, planners, lambda row: row.mean_wmc)),
                ],
            )
            _append_svg_panel(
                svg,
                title="Code entities",
                top=380,
                height=150,
                planners=planners,
                series=[
                    ("Classes", "#7c3aed", _chart_values(rows, planners, lambda row: row.classes)),
                    ("Methods", "#ea580c", _chart_values(rows, planners, lambda row: row.methods)),
                ],
            )
            _append_svg_panel(
                svg,
                title="Lines of code",
                top=640,
                height=100,
                planners=planners,
                series=[
                    ("LOC", "#0891b2", _chart_values(rows, planners, lambda row: row.total_loc)),
                ],
            )
        else:
            svg.append(
                '<text x="500" y="410" text-anchor="middle" font-size="20" '
                'fill="#6b7280">No successful CK metrics for this case</text>'
            )
        svg.append("</svg>")
        chart_name = safe_case_id(case_id)
        svg_text = "\n".join(svg)
        chart_path = output_dir / f"{chart_name}.svg"
        chart_path.write_text(svg_text + "\n", encoding="utf-8")
        generated.append(chart_path)
        index_lines.extend(
            [
                f"## {case_id}",
                "",
                f"![{case_id}]({chart_path.name})",
                "",
            ]
        )
        html_sections.extend(
            [
                f'<section id="{html.escape(chart_name)}">',
                f"<h2>{html.escape(case_id)}</h2>",
                svg_text,
                "</section>",
            ]
        )
    (output_dir / "README.md").write_text(
        "\n".join(index_lines),
        encoding="utf-8",
    )
    html_sections.extend(["</body>", "</html>"])
    (output_dir / "index.html").write_text(
        "\n".join(html_sections) + "\n",
        encoding="utf-8",
    )
    return generated


def _svg_marker(x: float, y: float, shape: str, color: str) -> str:
    if shape == "circle":
        return f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="{color}"/>'
    if shape == "square":
        return (
            f'<rect x="{x - 5:.1f}" y="{y - 5:.1f}" width="10" height="10" '
            f'fill="{color}"/>'
        )
    if shape == "triangle":
        points = f"{x:.1f},{y - 6:.1f} {x - 6:.1f},{y + 5:.1f} {x + 6:.1f},{y + 5:.1f}"
        return f'<polygon points="{points}" fill="{color}"/>'
    points = f"{x:.1f},{y - 6:.1f} {x - 6:.1f},{y:.1f} {x:.1f},{y + 6:.1f} {x + 6:.1f},{y:.1f}"
    return f'<polygon points="{points}" fill="{color}"/>'


def _append_grouped_dot_panel(
    svg: list[str],
    *,
    panel_label: str,
    title: str,
    top: float,
    height: float,
    case_ids: list[str],
    rows: dict[str, dict[str, SuccessfulCaseStats]],
    planners: tuple[str, ...],
    styles: dict[str, tuple[str, str]],
    getter: Callable[[SuccessfulCaseStats], int | float | None],
) -> None:
    left = 90.0
    right = 1450.0
    bottom = top + height
    all_values = [
        float(value)
        for case_id in case_ids
        for planner in planners
        if (row := rows.get(case_id, {}).get(planner)) is not None
        if (value := getter(row)) is not None
    ]
    maximum = max(all_values, default=1.0) * 1.08
    if maximum <= 0:
        maximum = 1.0
    x_step = (right - left) / max(len(case_ids) - 1, 1)
    planner_offsets = (-18.0, -6.0, 6.0, 18.0)

    svg.extend(
        [
            f'<text x="30" y="{top - 18:.0f}" font-size="20" '
            f'font-weight="bold">{html.escape(panel_label)}</text>',
            f'<text x="{left:.0f}" y="{top - 18:.0f}" font-size="18" '
            f'font-weight="bold">{html.escape(title)}</text>',
        ]
    )
    for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
        y = bottom - fraction * height
        svg.append(
            f'<line x1="{left:.0f}" y1="{y:.1f}" x2="{right:.0f}" y2="{y:.1f}" '
            'stroke="#d1d5db" stroke-width="1"/>'
        )
        svg.append(
            f'<text x="{left - 10:.0f}" y="{y + 4:.1f}" text-anchor="end" '
            f'font-size="12" fill="#374151">{maximum * fraction:.2f}</text>'
        )
    svg.append(
        f'<line x1="{left:.0f}" y1="{top:.0f}" x2="{left:.0f}" y2="{bottom:.0f}" '
        'stroke="#111827" stroke-width="1.5"/>'
    )
    svg.append(
        f'<line x1="{left:.0f}" y1="{bottom:.0f}" x2="{right:.0f}" y2="{bottom:.0f}" '
        'stroke="#111827" stroke-width="1.5"/>'
    )

    for case_index, case_id in enumerate(case_ids):
        center_x = left + case_index * x_step
        svg.append(
            f'<text x="{center_x:.1f}" y="{bottom + 24:.1f}" text-anchor="middle" '
            f'font-size="12">C{case_index + 1}</text>'
        )
        for planner_index, planner in enumerate(planners):
            x = center_x + planner_offsets[planner_index]
            row = rows.get(case_id, {}).get(planner)
            value = getter(row) if row is not None else None
            if value is None:
                svg.append(
                    f'<text x="{x:.1f}" y="{bottom - 5:.1f}" text-anchor="middle" '
                    'font-size="13" fill="#9ca3af">×</text>'
                )
                continue
            y = bottom - float(value) / maximum * height
            color, shape = styles[planner]
            svg.append(_svg_marker(x, y, shape, color))


def generate_scientific_metric_report(
    paths: list[Path],
    *,
    manifest_path: Path,
    output_dir: Path,
) -> list[Path]:
    """Generate publication-style figures and a self-contained HTML report."""
    statistics = [
        item
        for path in paths
        for item in parse_successful_batch_log(path)
    ]
    planners = ("none", "greedy", "topo", "bfs")
    styles = {
        "none": ("#0072B2", "circle"),
        "greedy": ("#E69F00", "square"),
        "topo": ("#009E73", "triangle"),
        "bfs": ("#CC79A7", "diamond"),
    }
    case_ids = load_manifest_case_ids(manifest_path)
    by_case: dict[str, dict[str, SuccessfulCaseStats]] = {}
    for item in statistics:
        by_case.setdefault(item.case_id, {})[item.planner] = item

    output_dir.mkdir(parents=True, exist_ok=True)
    figure_specs: list[
        tuple[
            str,
            str,
            list[
                tuple[
                    str,
                    str,
                    Callable[[SuccessfulCaseStats], int | float | None],
                ]
            ],
        ]
    ] = [
        (
            "figure1_design_metrics.svg",
            "Final design metrics by case and planner",
            [
                ("a", "Mean coupling between objects (CBO)", lambda row: row.mean_cbo),
                ("b", "Mean lack of cohesion (LCOM)", lambda row: row.mean_lcom),
                ("c", "Mean weighted methods per class (WMC)", lambda row: row.mean_wmc),
            ],
        ),
        (
            "figure2_size_metrics.svg",
            "Final code-size metrics by case and planner",
            [
                ("a", "Classes", lambda row: row.classes),
                ("b", "Methods", lambda row: row.methods),
                ("c", "Lines of code (LOC)", lambda row: row.total_loc),
            ],
        ),
    ]

    figures: list[Path] = []
    embedded_figures: list[tuple[str, str]] = []
    for filename, figure_title, panels in figure_specs:
        svg = [
            '<svg xmlns="http://www.w3.org/2000/svg" width="1500" height="1120" '
            'viewBox="0 0 1500 1120">',
            '<rect width="1500" height="1120" fill="white"/>',
            f'<text x="750" y="38" text-anchor="middle" font-size="24" '
            f'font-weight="bold">{html.escape(figure_title)}</text>',
        ]
        legend_x = 360.0
        for planner in planners:
            color, shape = styles[planner]
            svg.append(_svg_marker(legend_x, 72, shape, color))
            svg.append(
                f'<text x="{legend_x + 12:.0f}" y="77" font-size="14">'
                f"{html.escape(planner)}</text>"
            )
            legend_x += 190
        svg.append(
            '<text x="1260" y="77" font-size="13" fill="#6b7280">'
            "× unavailable</text>"
        )
        for panel_index, (label, title, getter) in enumerate(panels):
            _append_grouped_dot_panel(
                svg,
                panel_label=label,
                title=title,
                top=145 + panel_index * 325,
                height=235,
                case_ids=case_ids,
                rows=by_case,
                planners=planners,
                styles=styles,
                getter=getter,
            )
        svg.append("</svg>")
        svg_text = "\n".join(svg)
        figure_path = output_dir / filename
        figure_path.write_text(svg_text + "\n", encoding="utf-8")
        figures.append(figure_path)
        embedded_figures.append((figure_title, svg_text))

    table_rows = []
    for case_index, case_id in enumerate(case_ids, start=1):
        for planner in planners:
            row = by_case.get(case_id, {}).get(planner)
            table_rows.append(
                "<tr>"
                f"<td>C{case_index}</td><td>{html.escape(case_id)}</td>"
                f"<td>{planner}</td>"
                f"<td>{_optional_metric(row.mean_cbo) if row else 'N/A'}</td>"
                f"<td>{_optional_metric(row.mean_lcom) if row else 'N/A'}</td>"
                f"<td>{_optional_metric(row.mean_wmc) if row else 'N/A'}</td>"
                f"<td>{_optional_metric(row.classes) if row else 'N/A'}</td>"
                f"<td>{_optional_metric(row.methods) if row else 'N/A'}</td>"
                f"<td>{_optional_metric(row.total_loc, 0) if row else 'N/A'}</td>"
                "</tr>"
            )
    html_report = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        "<title>Scientific metric figures</title>",
        "<style>",
        "body{font-family:Arial,Helvetica,sans-serif;max-width:1500px;margin:auto;"
        "padding:24px;color:#111827;background:#f3f4f6}",
        "figure,section{background:white;padding:20px;margin:24px 0;border:1px solid #d1d5db}",
        "svg{width:100%;height:auto}figcaption{font-size:14px;line-height:1.45}",
        "table{border-collapse:collapse;width:100%;font-size:13px}",
        "th,td{border:1px solid #d1d5db;padding:6px;text-align:right}",
        "th:nth-child(-n+3),td:nth-child(-n+3){text-align:left}",
        "</style></head><body>",
        "<h1>Planner comparison: final CK metrics</h1>",
        "<p><strong>Exploratory results.</strong> Each point represents one successful "
        "planner-case run (n=1); therefore no uncertainty interval is shown. "
        "A grey × marks missing or unsuccessful runs.</p>",
    ]
    for index, (title, svg_text) in enumerate(embedded_figures, start=1):
        html_report.extend(
            [
                "<figure>",
                svg_text,
                "<figcaption>",
                f"<strong>Figure {index}.</strong> {html.escape(title)}. "
                "Cases C1–C10 follow manifest order. Planner identity is encoded "
                "by both color and point shape. Axes start at zero and share one "
                "scale within each metric panel.",
                "</figcaption></figure>",
            ]
        )
    html_report.extend(
        [
            "<section><h2>Exact values</h2>",
            "<table><thead><tr><th>Case</th><th>Case ID</th><th>Planner</th>"
            "<th>CBO</th><th>LCOM</th><th>WMC</th><th>Classes</th>"
            "<th>Methods</th><th>LOC</th></tr></thead><tbody>",
            *table_rows,
            "</tbody></table></section>",
            "</body></html>",
        ]
    )
    (output_dir / "index.html").write_text(
        "\n".join(html_report) + "\n",
        encoding="utf-8",
    )
    return figures


def generate_interactive_evolution_report(
    paths: list[Path],
    *,
    manifest_path: Path,
    output_path: Path,
) -> Path:
    """Generate one self-contained interactive evolution report."""
    data = build_interactive_evolution_data(paths, manifest_path=manifest_path)
    embedded_data = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    template = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Interactive planner evolution report</title>
<style>
:root{--bg:#f5f7fa;--card:#fff;--text:#17212b;--muted:#64748b;--grid:#dce2e8}
*{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--text);
font-family:Arial,Helvetica,sans-serif} main{max-width:1400px;margin:auto;padding:28px}
.controls,.card{background:var(--card);border:1px solid #d9e0e7;border-radius:14px;
box-shadow:0 2px 10px #0f172a12}.controls{display:flex;gap:20px;padding:18px;
align-items:end;flex-wrap:wrap}.control{display:grid;gap:6px;min-width:260px}
label{font-size:13px;font-weight:700}select{font:inherit;padding:9px 12px;border:1px solid
#aeb8c4;border-radius:7px;background:white}.card{margin-top:20px;padding:20px}
#chart{width:100%;height:auto;display:block}.legend{display:flex;gap:24px;flex-wrap:wrap;
margin:8px 0 0}.legend-item{display:flex;align-items:center;gap:7px;font-size:13px}
.swatch{width:13px;height:13px}.note{font-size:13px;color:var(--muted);line-height:1.45}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:8px;
border-bottom:1px solid #e5e7eb;text-align:right}th:first-child,td:first-child{text-align:left}
.best{font-weight:700;color:#b91c1c}h1,h2{margin-top:0}
</style>
</head>
<body><main>
<h1>Interactive planner evolution report</h1>
<p class="note">Successful planner-case runs only. Missing planner markers are intentionally
absent. Larger markers indicate the best value at that step. The best complete trajectory is
drawn thicker. “Best” means lower for turns, introduced smells, tokens, and CK metrics; higher
for edit success rate. For classes, methods, and LOC, the highlighted value is the minimum and
is not necessarily preferable. CK metrics are final-only because intermediate CK measurements
were not logged.</p>
<div class="controls">
  <div class="control"><label for="casePicker">Case</label><select id="casePicker"></select></div>
  <div class="control"><label for="metricPicker">Metric</label><select id="metricPicker"></select></div>
</div>
<section class="card">
  <h2 id="chartTitle"></h2>
  <svg id="chart" viewBox="0 0 1200 650" role="img"></svg>
  <div id="legend" class="legend"></div>
</section>
<section class="card"><h2>Planner summary</h2><div id="summary"></div></section>
</main>
<script>
const DATA=__DATA__;
const COLORS={none:"#0072B2",greedy:"#E69F00",topo:"#009E73",bfs:"#CC79A7"};
const SHAPES={none:"circle",greedy:"square",topo:"triangle",bfs:"diamond"};
const METRICS={
 cumulativeTurns:{label:"Cumulative LLM turns",kind:"evolution",direction:"min",unit:"turns"},
 editSuccessRate:{label:"Cumulative successful-edit rate",kind:"evolution",direction:"max",unit:"%"},
 introducedPerFix:{label:"New introduced smells per fix",kind:"evolution",direction:"min",unit:"smells"},
 cumulativeIntroduced:{label:"Cumulative introduced smells",kind:"evolution",direction:"min",unit:"smells"},
 tokensPerFix:{label:"Tokens spent per smell fix",kind:"evolution",direction:"min",unit:"tokens"},
 meanCbo:{label:"Final mean CBO",kind:"code",direction:"min",unit:"CBO"},
 meanLcom:{label:"Final mean LCOM",kind:"code",direction:"min",unit:"LCOM"},
 meanWmc:{label:"Final mean WMC",kind:"code",direction:"min",unit:"WMC"},
 classes:{label:"Final class count",kind:"code",direction:"min",unit:"classes"},
 methods:{label:"Final method count",kind:"code",direction:"min",unit:"methods"},
 loc:{label:"Final lines of code",kind:"code",direction:"min",unit:"LOC"}
};
const planners=DATA.plannerOrder;
const casePicker=document.getElementById("casePicker");
const metricPicker=document.getElementById("metricPicker");
casePicker.innerHTML='<option value="aggregate">Aggregate: all successful cases</option>'+
 DATA.caseOrder.map((id,i)=>`<option value="${i}">C${i+1}: ${id}</option>`).join("");
metricPicker.innerHTML=Object.entries(METRICS).map(([key,m])=>
 `<option value="${key}">${m.label}</option>`).join("");

function caseRows(caseId){return DATA.cases[caseId]?.planners||{}}
function mean(values){return values.length?values.reduce((a,b)=>a+b,0)/values.length:null}
function aggregateEvolution(planner,metric){
 const trajectories=DATA.caseOrder.map(id=>caseRows(id)[planner]?.trajectory||[])
   .filter(t=>t.length);
 return Array.from({length:11},(_,i)=>{
   const progress=i*10;
   const values=trajectories.map(t=>t[Math.round((t.length-1)*progress/100)]?.[metric])
     .filter(v=>Number.isFinite(v));
   return {x:progress,y:mean(values),n:values.length,label:`${progress}%`};
 }).filter(p=>p.y!==null);
}
function getSeries(selection,metric){
 const meta=METRICS[metric];
 if(meta.kind==="evolution"){
   if(selection==="aggregate") return planners.map(planner=>({
     planner,points:aggregateEvolution(planner,metric)
   }));
   const caseId=DATA.caseOrder[Number(selection)], rows=caseRows(caseId);
   return planners.map(planner=>({planner,points:(rows[planner]?.trajectory||[]).map(p=>({
     x:p.fix,y:p[metric],n:1,label:`fix ${p.fix}`,smellKey:p.smellKey
   }))}));
 }
 if(selection==="aggregate"){
   return planners.map(planner=>({planner,points:DATA.caseOrder.map((id,i)=>{
     const value=caseRows(id)[planner]?.code?.[metric];
     return Number.isFinite(value)?{x:i+1,y:value,n:1,label:`C${i+1}`} : null;
   }).filter(Boolean)}));
 }
 const caseId=DATA.caseOrder[Number(selection)],rows=caseRows(caseId);
 return planners.map((planner,i)=>{
   const value=rows[planner]?.code?.[metric];
   return {planner,points:Number.isFinite(value)?[{x:i+1,y:value,n:1,label:planner}]:[]};
 });
}
function score(series,meta){
 if(!series.points.length)return null;
 if(meta.kind==="evolution"&&["introducedPerFix","tokensPerFix"].includes(metricPicker.value))
   return mean(series.points.map(p=>p.y));
 if(meta.kind==="code"&&casePicker.value==="aggregate")
   return mean(series.points.map(p=>p.y));
 return series.points[series.points.length-1].y;
}
function bestValue(values,direction){
 if(!values.length)return null; return direction==="max"?Math.max(...values):Math.min(...values)
}
function marker(x,y,r,shape,color,best,title){
 const stroke=best?"#d62728":"white",sw=best?3:1.5;
 let body="";
 if(shape==="circle")body=`<circle cx="${x}" cy="${y}" r="${r}"/>`;
 if(shape==="square")body=`<rect x="${x-r}" y="${y-r}" width="${2*r}" height="${2*r}"/>`;
 if(shape==="triangle")body=`<polygon points="${x},${y-r} ${x-r},${y+r} ${x+r},${y+r}"/>`;
 if(shape==="diamond")body=`<polygon points="${x},${y-r} ${x-r},${y} ${x},${y+r} ${x+r},${y}"/>`;
 return `<g fill="${color}" stroke="${stroke}" stroke-width="${sw}">${body}<title>${title}</title></g>`;
}
function render(){
 const selection=casePicker.value,metric=metricPicker.value,meta=METRICS[metric];
 const series=getSeries(selection,metric),all=series.flatMap(s=>s.points);
 const svg=document.getElementById("chart");
 const title=selection==="aggregate"?"Aggregate: normalized case progress":
   DATA.caseOrder[Number(selection)];
 document.getElementById("chartTitle").textContent=`${meta.label} — ${title}`;
 if(!all.length){svg.innerHTML='<text x="600" y="320" text-anchor="middle" font-size="22" fill="#64748b">No successful data</text>';return}
 const W=1200,H=650,L=95,R=1140,T=55,B=565;
 let minY=Math.min(...all.map(p=>p.y)),maxY=Math.max(...all.map(p=>p.y));
 let spread=maxY-minY,pad=Math.max(spread*0.08,Math.abs(maxY||1)*0.03,meta.unit==="%"?1:0.1);
 minY-=pad;maxY+=pad;if(meta.unit==="%"){minY=Math.max(0,minY);maxY=Math.min(100,maxY)}
 if(minY===maxY){minY-=1;maxY+=1}
 let minX=Math.min(...all.map(p=>p.x)),maxX=Math.max(...all.map(p=>p.x));
 if(minX===maxX){minX-=1;maxX+=1}
 const sx=x=>L+(x-minX)/(maxX-minX)*(R-L),sy=y=>B-(y-minY)/(maxY-minY)*(B-T);
 let out='<rect width="1200" height="650" fill="white"/>';
 for(let i=0;i<=5;i++){const y=T+(B-T)*i/5,v=maxY-(maxY-minY)*i/5;
   out+=`<line x1="${L}" y1="${y}" x2="${R}" y2="${y}" stroke="#dce2e8"/>`;
   out+=`<text x="${L-12}" y="${y+4}" text-anchor="end" font-size="12" fill="#475569">${v.toFixed(2)}</text>`}
 out+=`<line x1="${L}" y1="${T}" x2="${L}" y2="${B}" stroke="#111827" stroke-width="1.5"/>`;
 out+=`<line x1="${L}" y1="${B}" x2="${R}" y2="${B}" stroke="#111827" stroke-width="1.5"/>`;
 const xValues=[...new Set(all.map(p=>p.x))].sort((a,b)=>a-b);
 const maxLabels=12,step=Math.max(1,Math.ceil(xValues.length/maxLabels));
 xValues.filter((_,i)=>i%step===0||i===xValues.length-1).forEach(x=>{
   const label=selection==="aggregate"&&meta.kind==="evolution"?`${x}%`:
     meta.kind==="code"&&selection==="aggregate"?`C${x}`:
     meta.kind==="code"?planners[x-1]:`${x}`;
   out+=`<text x="${sx(x)}" y="${B+24}" text-anchor="middle" font-size="12">${label}</text>`});
 const scores=series.map(s=>({planner:s.planner,value:score(s,meta)})).filter(s=>s.value!==null);
 const bestScore=bestValue(scores.map(s=>s.value),meta.direction);
 const bestPlanner=scores.find(s=>s.value===bestScore)?.planner;
 const byX={};all.forEach(p=>(byX[p.x]??=[]).push(p.y));
 series.forEach(s=>{
   const isBest=s.planner===bestPlanner,pts=s.points;
   if(pts.length>1){const d=pts.map((p,i)=>`${i?"L":"M"}${sx(p.x)},${sy(p.y)}`).join(" ");
     out+=`<path d="${d}" fill="none" stroke="${COLORS[s.planner]}" stroke-width="${isBest?5:2.4}" opacity="${isBest?1:.72}"/>`}
   pts.forEach(p=>{const best= p.y===bestValue(byX[p.x],meta.direction),r=best?9:(isBest?7:5);
     out+=marker(sx(p.x),sy(p.y),r,SHAPES[s.planner],COLORS[s.planner],best,
       `${s.planner}; ${p.label}; ${meta.label}: ${p.y.toFixed(3)}${p.n?`; n=${p.n}`:""}`)})
 });
 out+=`<text x="${(L+R)/2}" y="625" text-anchor="middle" font-size="14">${selection==="aggregate"&&meta.kind==="evolution"?"Normalized fix progress":meta.kind==="evolution"?"Fix index":selection==="aggregate"?"Benchmark case":"Planner"}</text>`;
 out+=`<text x="22" y="${(T+B)/2}" transform="rotate(-90 22 ${(T+B)/2})" text-anchor="middle" font-size="14">${meta.unit}</text>`;
 svg.innerHTML=out;
 document.getElementById("legend").innerHTML=planners.map(p=>
   `<div class="legend-item"><svg viewBox="0 0 18 18" width="18" height="18">${marker(9,9,5,SHAPES[p],COLORS[p],false,p)}</svg>${p}${p===bestPlanner?' <span class="best">(best trajectory)</span>':''}</div>`).join("");
 document.getElementById("summary").innerHTML='<table><thead><tr><th>Planner</th><th>Points</th><th>Successful cases</th><th>Trajectory score</th></tr></thead><tbody>'+
   series.map(s=>`<tr><td>${s.planner}</td><td>${s.points.length}</td><td>${selection==="aggregate"?Math.max(0,...s.points.map(p=>p.n||0)):(s.points.length?1:0)}</td><td class="${s.planner===bestPlanner?'best':''}">${score(s,meta)?.toFixed(3)??"N/A"}</td></tr>`).join("")+'</tbody></table>';
}
casePicker.addEventListener("change",render);metricPicker.addEventListener("change",render);render();
</script></body></html>"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        template.replace("__DATA__", embedded_data),
        encoding="utf-8",
    )
    return output_path


def generate_matplotlib_evolution_report(
    paths: list[Path],
    *,
    manifest_path: Path,
    output_path: Path,
) -> Path:
    """Render all interactive picker states with Matplotlib into one HTML file."""
    import matplotlib.pyplot as plt

    data = build_interactive_evolution_data(paths, manifest_path=manifest_path)
    case_order = data["caseOrder"]
    planner_order = data["plannerOrder"]
    cases = data["cases"]
    if not isinstance(case_order, list) or not all(
        isinstance(case_id, str) for case_id in case_order
    ):
        raise TypeError("caseOrder must contain strings")
    if not isinstance(planner_order, list) or not all(
        isinstance(planner, str) for planner in planner_order
    ):
        raise TypeError("plannerOrder must contain strings")
    if not isinstance(cases, dict):
        raise TypeError("cases must be a dictionary")

    colors = {
        "none": "#0072B2",
        "greedy": "#E69F00",
        "topo": "#009E73",
        "bfs": "#CC79A7",
    }
    markers = {"none": "o", "greedy": "s", "topo": "^", "bfs": "D"}
    metrics = {
        "cumulativeTurns": (
            "Cumulative LLM turns",
            "Turns",
            "evolution",
            "min",
        ),
        "editSuccessRate": (
            "Cumulative successful-edit rate",
            "Success rate (%)",
            "evolution",
            "max",
        ),
        "introducedPerFix": (
            "New introduced smells per fix",
            "Introduced smells",
            "evolution",
            "min",
        ),
        "cumulativeIntroduced": (
            "Cumulative introduced smells",
            "Introduced smells",
            "evolution",
            "min",
        ),
        "tokensPerFix": (
            "Tokens spent per smell fix",
            "Input + output tokens",
            "evolution",
            "min",
        ),
        "meanCbo": ("Final mean CBO", "CBO", "code", "min"),
        "meanLcom": ("Final mean LCOM", "LCOM", "code", "min"),
        "meanWmc": ("Final mean WMC", "WMC", "code", "min"),
        "classes": ("Final class count", "Classes", "code", "min"),
        "methods": ("Final method count", "Methods", "code", "min"),
        "loc": ("Final lines of code", "LOC", "code", "min"),
    }

    def planner_record(case_id: str, planner: str) -> dict[str, object] | None:
        case = cases.get(case_id)
        if not isinstance(case, dict):
            return None
        planner_rows = case.get("planners")
        if not isinstance(planner_rows, dict):
            return None
        record = planner_rows.get(planner)
        return record if isinstance(record, dict) else None

    def evolution_points(
        selection: str,
        planner: str,
        metric: str,
    ) -> tuple[list[float], list[float], int]:
        if selection != "aggregate":
            record = planner_record(case_order[int(selection)], planner)
            trajectory = record.get("trajectory") if record is not None else None
            if not isinstance(trajectory, list):
                return [], [], 0
            points = [
                point
                for point in trajectory
                if isinstance(point, dict)
                and isinstance(point.get("fix"), int | float)
                and isinstance(point.get(metric), int | float)
            ]
            return (
                [float(point["fix"]) for point in points],
                [float(point[metric]) for point in points],
                1 if points else 0,
            )

        trajectories: list[list[dict[str, object]]] = []
        for case_id in case_order:
            record = planner_record(case_id, planner)
            trajectory = record.get("trajectory") if record is not None else None
            if isinstance(trajectory, list) and trajectory:
                valid = [point for point in trajectory if isinstance(point, dict)]
                if valid:
                    trajectories.append(valid)
        if not trajectories:
            return [], [], 0
        x_values = [float(progress) for progress in range(0, 101, 10)]
        y_values: list[float] = []
        for progress in range(0, 101, 10):
            samples: list[float] = []
            for trajectory in trajectories:
                index = round((len(trajectory) - 1) * progress / 100)
                value = trajectory[index].get(metric)
                if isinstance(value, int | float):
                    samples.append(float(value))
            y_values.append(sum(samples) / len(samples))
        return x_values, y_values, len(trajectories)

    def code_points(
        selection: str,
        planner: str,
        metric: str,
    ) -> tuple[list[float], list[float], int]:
        if selection != "aggregate":
            record = planner_record(case_order[int(selection)], planner)
            code = record.get("code") if record is not None else None
            value = code.get(metric) if isinstance(code, dict) else None
            planner_index = planner_order.index(planner) + 1
            if isinstance(value, int | float):
                return [float(planner_index)], [float(value)], 1
            return [], [], 0

        x_values: list[float] = []
        y_values: list[float] = []
        for case_index, case_id in enumerate(case_order, start=1):
            record = planner_record(case_id, planner)
            code = record.get("code") if record is not None else None
            value = code.get(metric) if isinstance(code, dict) else None
            if isinstance(value, int | float):
                x_values.append(float(case_index))
                y_values.append(float(value))
        return x_values, y_values, len(y_values)

    def trajectory_score(
        y_values: list[float],
        *,
        metric: str,
        kind: str,
    ) -> float | None:
        if not y_values:
            return None
        if metric in {"introducedPerFix", "tokensPerFix"}:
            return sum(y_values) / len(y_values)
        if kind == "code":
            return sum(y_values) / len(y_values)
        return y_values[-1]

    def render_plot(selection: str, metric: str) -> tuple[str, list[dict[str, object]]]:
        title, y_label, kind, direction = metrics[metric]
        series: list[dict[str, object]] = []
        for planner in planner_order:
            if kind == "evolution":
                x_values, y_values, sample_count = evolution_points(
                    selection,
                    planner,
                    metric,
                )
            else:
                x_values, y_values, sample_count = code_points(
                    selection,
                    planner,
                    metric,
                )
            series.append(
                {
                    "planner": planner,
                    "x": x_values,
                    "y": y_values,
                    "n": sample_count,
                    "score": trajectory_score(
                        y_values,
                        metric=metric,
                        kind=kind,
                    ),
                }
            )

        scores = [
            float(item["score"])
            for item in series
            if isinstance(item["score"], int | float)
        ]
        best_score = (
            (max(scores) if direction == "max" else min(scores))
            if scores
            else None
        )
        best_planner = next(
            (
                str(item["planner"])
                for item in series
                if item["score"] == best_score
            ),
            "",
        )
        all_y = [
            value
            for item in series
            for value in item["y"]
            if isinstance(value, float)
        ]
        all_x = [
            value
            for item in series
            for value in item["x"]
            if isinstance(value, float)
        ]

        with plt.rc_context(
            {
                "font.family": "DejaVu Sans",
                "font.size": 11,
                "axes.facecolor": "#f7f7f6",
                "figure.facecolor": "#f7f7f6",
                "svg.fonttype": "none",
            }
        ):
            figure, axis = plt.subplots(figsize=(14, 8), dpi=120)
            if not all_y:
                axis.text(
                    0.5,
                    0.5,
                    "No successful data for this case and metric",
                    ha="center",
                    va="center",
                    transform=axis.transAxes,
                    fontsize=16,
                    color="#68727a",
                )
                axis.set_axis_off()
            else:
                minimum = min(all_y)
                maximum = max(all_y)
                spread = maximum - minimum
                padding = max(
                    spread * 0.08,
                    abs(maximum if maximum else 1.0) * 0.03,
                    1.0 if metric == "editSuccessRate" else 0.1,
                )
                y_min = minimum - padding
                y_max = maximum + padding
                if metric == "editSuccessRate":
                    y_min = max(0.0, y_min)
                    y_max = min(100.0, y_max)
                if y_min == y_max:
                    y_min -= 1
                    y_max += 1
                axis.set_ylim(y_min, y_max)

                point_groups: dict[float, list[float]] = {}
                for item in series:
                    for x_value, y_value in zip(
                        item["x"],
                        item["y"],
                        strict=True,
                    ):
                        point_groups.setdefault(float(x_value), []).append(
                            float(y_value)
                        )
                for item in series:
                    planner = str(item["planner"])
                    x_values = [float(value) for value in item["x"]]
                    y_values = [float(value) for value in item["y"]]
                    if not x_values:
                        continue
                    is_best_trajectory = planner == best_planner
                    axis.plot(
                        x_values,
                        y_values,
                        color=colors[planner],
                        marker=markers[planner],
                        markersize=6,
                        linewidth=3.8 if is_best_trajectory else 2.0,
                        alpha=1.0 if is_best_trajectory else 0.68,
                        zorder=4 if is_best_trajectory else 2,
                    )
                    if is_best_trajectory and len(x_values) >= 2:
                        axis.plot(
                            x_values,
                            y_values,
                            color="#f4512a",
                            linewidth=2.5,
                            linestyle=(0, (2, 3)),
                            zorder=5,
                        )
                        axis.fill_between(
                            x_values,
                            y_min,
                            y_values,
                            color="#f15a3a",
                            alpha=0.08,
                            zorder=1,
                        )
                    best_x: list[float] = []
                    best_y: list[float] = []
                    for x_value, y_value in zip(
                        x_values,
                        y_values,
                        strict=True,
                    ):
                        candidates = point_groups[x_value]
                        best_value = (
                            max(candidates)
                            if direction == "max"
                            else min(candidates)
                        )
                        if math.isclose(y_value, best_value):
                            best_x.append(x_value)
                            best_y.append(y_value)
                    axis.scatter(
                        best_x,
                        best_y,
                        s=150,
                        color="#f4512a",
                        edgecolor="white",
                        linewidth=2,
                        zorder=7,
                    )
                    axis.annotate(
                        f"{planner} (n={item['n']})",
                        (x_values[-1], y_values[-1]),
                        xytext=(10, 0),
                        textcoords="offset points",
                        va="center",
                        fontsize=10,
                        color=colors[planner],
                        bbox={
                            "boxstyle": "round,pad=0.35",
                            "facecolor": "#fafafa",
                            "edgecolor": "none",
                            "alpha": 0.92,
                        },
                        zorder=8,
                    )

                if all_x:
                    x_min = min(all_x)
                    x_max = max(all_x)
                    x_spread = x_max - x_min
                    x_padding = max(x_spread * 0.04, 0.4)
                    axis.set_xlim(x_min - x_padding, x_max + x_padding)
                if kind == "evolution":
                    axis.set_xlabel(
                        "Normalized fix progress (%)"
                        if selection == "aggregate"
                        else "Fix index"
                    )
                elif selection == "aggregate":
                    axis.set_xlabel("Benchmark case")
                    axis.set_xticks(range(1, len(case_order) + 1))
                    axis.set_xticklabels(
                        [f"C{index}" for index in range(1, len(case_order) + 1)]
                    )
                else:
                    axis.set_xlabel("Planner")
                    axis.set_xticks(range(1, len(planner_order) + 1))
                    axis.set_xticklabels(planner_order)
                axis.set_ylabel(y_label)
                axis.grid(
                    True,
                    which="major",
                    color="#d9dddf",
                    linewidth=0.8,
                    alpha=0.8,
                )
                for spine in axis.spines.values():
                    spine.set_visible(False)
                axis.tick_params(
                    axis="both",
                    length=0,
                    colors="#70787e",
                )

            selection_title = (
                "Aggregate across successful cases"
                if selection == "aggregate"
                else f"C{int(selection) + 1}: {case_order[int(selection)]}"
            )
            axis.set_title(f"{title}\n{selection_title}", fontsize=15, pad=18)
            figure.tight_layout()
            buffer = io.StringIO()
            figure.savefig(
                buffer,
                format="svg",
                bbox_inches="tight",
                facecolor=figure.get_facecolor(),
            )
            plt.close(figure)
        svg_text = buffer.getvalue()
        svg_text = svg_text[svg_text.index("<svg") :]
        return svg_text, series

    selections = ["aggregate", *[str(index) for index in range(len(case_order))]]
    plot_templates: list[str] = []
    metadata: dict[str, dict[str, list[dict[str, object]]]] = {}
    for selection in selections:
        metadata[selection] = {}
        for metric in metrics:
            svg_text, series = render_plot(selection, metric)
            plot_templates.append(
                f'<template id="plot-{selection}-{metric}">{svg_text}</template>'
            )
            metadata[selection][metric] = series

    embedded_metadata = json.dumps(metadata, ensure_ascii=False).replace(
        "<",
        "\\u003c",
    )
    case_options = [
        '<option value="aggregate">Aggregate: all successful cases</option>',
        *[
            f'<option value="{index}">C{index + 1}: {html.escape(case_id)}</option>'
            for index, case_id in enumerate(case_order)
        ],
    ]
    metric_options = [
        f'<option value="{key}">{html.escape(spec[0])}</option>'
        for key, spec in metrics.items()
    ]
    report = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        "<title>Matplotlib planner evolution report</title>",
        "<style>",
        "body{font-family:Arial,Helvetica,sans-serif;max-width:1500px;margin:auto;"
        "padding:24px;background:#f3f4f6;color:#17212b}",
        ".controls,.card{background:white;border:1px solid #d9e0e7;border-radius:12px;"
        "padding:18px;margin:18px 0}.controls{display:flex;gap:18px;flex-wrap:wrap}",
        "label{font-weight:700;font-size:13px;display:grid;gap:6px}",
        "select{min-width:310px;padding:8px;font:inherit}",
        "#plot svg{display:block;width:100%;height:auto}",
        "table{border-collapse:collapse;width:100%;font-size:13px}",
        "th,td{border-bottom:1px solid #e5e7eb;padding:7px;text-align:right}",
        "th:first-child,td:first-child{text-align:left}.best{font-weight:700;color:#b91c1c}",
        ".note{color:#64748b;line-height:1.45;font-size:13px}",
        "</style></head><body>",
        "<h1>Matplotlib planner evolution report</h1>",
        '<p class="note">Each chart is rendered by Matplotlib. Larger orange points '
        "mark the best value at each X position; the best trajectory is thicker, "
        "overlaid with an orange dashed line, and lightly shaded. Y limits start "
        "at the observed minimum and end at the observed maximum with padding. "
        "Only successful planner-case runs are included. Intermediate CK values "
        "were not logged, so CK charts use final values only.</p>",
        '<div class="controls">',
        '<label>Case<select id="casePicker">',
        *case_options,
        "</select></label>",
        '<label>Metric<select id="metricPicker">',
        *metric_options,
        "</select></label></div>",
        '<section class="card"><div id="plot"></div></section>',
        '<section class="card"><h2>Selected plot data</h2><div id="summary"></div></section>',
        *plot_templates,
        "<script>",
        f"const META={embedded_metadata};",
        "const casePicker=document.getElementById('casePicker');",
        "const metricPicker=document.getElementById('metricPicker');",
        "function render(){",
        " const selection=casePicker.value,metric=metricPicker.value;",
        " const template=document.getElementById(`plot-${selection}-${metric}`);",
        " document.getElementById('plot').replaceChildren(template.content.cloneNode(true));",
        " const rows=META[selection][metric];",
        " const scores=rows.filter(r=>r.score!==null).map(r=>r.score);",
        " const higher=metric==='editSuccessRate';",
        " const best=scores.length?(higher?Math.max(...scores):Math.min(...scores)):null;",
        " document.getElementById('summary').innerHTML='<table><thead><tr><th>Planner</th>"
        "<th>Points</th><th>Successful cases</th><th>Trajectory score</th></tr></thead><tbody>'+",
        " rows.map(r=>`<tr><td>${r.planner}</td><td>${r.x.length}</td><td>${r.n}</td>"
        "<td class=\"${r.score===best?'best':''}\">${r.score===null?'N/A':r.score.toFixed(3)}"
        "</td></tr>`).join('')+'</tbody></table>';",
        "}",
        "casePicker.addEventListener('change',render);",
        "metricPicker.addEventListener('change',render);render();",
        "</script></body></html>",
    ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(report) + "\n", encoding="utf-8")
    return output_path


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help=f"Pi output directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    batch_parser = subparsers.add_parser("batch", help="Batch status report")
    batch_parser.add_argument("batch_prefix", help="Run directory uuid prefix")
    batch_parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Manifest jsonl used to list pending cases",
    )

    cases_parser = subparsers.add_parser("cases", help="Latest progress per case")
    cases_parser.add_argument(
        "--batch-prefix",
        default=None,
        help="Only events whose json line contains this prefix",
    )
    cases_parser.add_argument(
        "--case-id",
        action="append",
        default=[],
        help="Restrict to one or more case ids",
    )

    elog_parser = subparsers.add_parser("elog", help="Parse Eliot all.log files")
    elog_parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="One or more all.log paths",
    )
    elog_parser.add_argument(
        "--batch-prefix",
        default=None,
        help="Expand to all logs under data/pi/runs matching this prefix",
    )

    successful_parser = subparsers.add_parser(
        "successful",
        help="Report successful cases from completed pi_batch activity logs",
    )
    successful_parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="Completed pi_batch log files",
    )

    charts_parser = subparsers.add_parser(
        "charts",
        help="Generate per-case SVG charts from successful batch logs",
    )
    charts_parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="Completed pi_batch log files",
    )
    charts_parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="Manifest that defines case order",
    )
    charts_parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory for SVG files and the Markdown index",
    )

    scientific_parser = subparsers.add_parser(
        "scientific-charts",
        help="Generate publication-style SVG figures and one HTML report",
    )
    scientific_parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="Completed pi_batch log files",
    )
    scientific_parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="Manifest that defines case order",
    )
    scientific_parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory for SVG figures and index.html",
    )

    evolution_parser = subparsers.add_parser(
        "interactive-evolution",
        help="Generate a self-contained interactive Matplotlib evolution report",
    )
    evolution_parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="Completed pi_batch log files",
    )
    evolution_parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="Manifest that defines case order",
    )
    evolution_parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Output HTML file",
    )

    queue_parser = subparsers.add_parser("queue", help="Inspect smells.json queues")
    queue_parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="smells.json paths (optional if --batch-prefix is set)",
    )
    queue_parser.add_argument(
        "--batch-prefix",
        default=None,
        help="Find smells.json under data/pi/runs for this batch prefix",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)
    data_dir = resolve_path(str(args.data_dir)) if args.data_dir else None

    match args.command:
        case "batch":
            manifest = args.manifest
            if manifest is not None:
                manifest = resolve_path(str(manifest))
            print_batch_report(args.batch_prefix, data_dir=data_dir, manifest_path=manifest)
            return 0
        case "cases":
            case_ids = set(args.case_id) if args.case_id else None
            progress = latest_progress_by_case(
                data_dir,
                batch_prefix=args.batch_prefix,
                case_ids=case_ids,
            )
            for case_id in sorted(progress, key=lambda value: value.lower()):
                print(format_progress_line(progress[case_id]))
            return 0
        case "elog":
            paths = list(args.paths)
            if args.batch_prefix:
                paths.extend(find_batch_run_logs(args.batch_prefix, data_dir=data_dir))
            if not paths:
                print("No log paths given.", file=sys.stderr)
                return 1
            print_eliot_logs(paths)
            return 0
        case "successful":
            print_successful_case_stats(list(args.paths))
            return 0
        case "charts":
            generated = generate_case_metric_charts(
                list(args.paths),
                manifest_path=args.manifest,
                output_dir=args.output_dir,
            )
            print(f"Generated {len(generated)} SVG charts in {_display_path(args.output_dir)}")
            return 0
        case "scientific-charts":
            generated = generate_scientific_metric_report(
                list(args.paths),
                manifest_path=args.manifest,
                output_dir=args.output_dir,
            )
            print(
                f"Generated {len(generated)} scientific SVG figures and one HTML report "
                f"in {_display_path(args.output_dir)}"
            )
            return 0
        case "interactive-evolution":
            output = generate_matplotlib_evolution_report(
                list(args.paths),
                manifest_path=args.manifest,
                output_path=args.output,
            )
            print(f"Generated Matplotlib evolution report at {_display_path(output)}")
            return 0
        case "queue":
            paths = list(args.paths)
            if args.batch_prefix:
                paths.extend(find_batch_smells_json(args.batch_prefix, data_dir=data_dir))
            if not paths:
                print("No smells.json paths given.", file=sys.stderr)
                return 1
            for path in paths:
                breakdown = parse_smell_queue(path)
                print(
                    f"{breakdown.case_id}: queue={breakdown.queue_size} "
                    f"({breakdown.baseline_in_queue} baseline, "
                    f"{breakdown.introduced_in_queue} introduced, "
                    f"{breakdown.other_in_queue} other) "
                    f"[{_display_path(path)}]"
                )
            return 0
        case _:
            parser.error(f"unknown command: {args.command}")
            return 2


if __name__ == "__main__":
    raise SystemExit(main())
