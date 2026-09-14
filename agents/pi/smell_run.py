"""Persist baseline smells, introduced smells, and last ORGANIC snapshot for pi runs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from lsprotocol.types import Location, Position, Range

from java.organic_feedback import OrganicFeedback
from java.organic_types import OrganicScope
from planning.planner import PlannerProtocol, pick_next_smell
from smell.smell import Smell


@dataclass(frozen=True, slots=True)
class SmellRunMetrics:
    """Snapshot smell counts after one ORGANIC detection."""

    remaining_smells: int
    introduced_smells: int
    fixed_smells: int
    original_smells: int
    total_introduced_smells: int
    fixed_introduced_smells: int


@dataclass(frozen=True, slots=True)
class SmellRunSync:
    """Sync result plus smells first seen this pass."""

    metrics: SmellRunMetrics
    newly_introduced: tuple[OrganicFeedback, ...]


@dataclass(slots=True)
class SmellRunState:
    """Baseline smells, introduced registry, and last ORGANIC snapshot for one pi run.

    ``queue`` holds identity keys from the latest successful detect only. It is
    replaced on each sync, not grown as a long-lived FIFO across edits.
    """

    case_id: str
    start_commit: str
    baseline_identity_keys: list[str]
    baseline_logical_keys: list[str]
    queue: list[str]
    introduced_identity_keys: list[str]
    introduced_logical_keys: list[str]
    entries: dict[str, dict[str, object]]

    @property
    def original_smells(self) -> int:
        return len(self.baseline_identity_keys)


def _append_unique(keys: list[str], key: str) -> None:
    if key not in keys:
        keys.append(key)


def _logical_keys_from_identity_keys(
    identity_keys: list[str],
    entries: dict[str, dict[str, object]],
) -> list[str]:
    logical_keys: list[str] = []
    for identity_key in identity_keys:
        record = entries.get(identity_key)
        if record is None:
            continue
        _append_unique(logical_keys, smell_logical_key(feedback_from_record(record)))
    return logical_keys


def smells_json_path(run_dir: Path) -> Path:
    """Return the smell-run state file under one pi run directory."""
    return run_dir / "smells.json"


def run_dir_from_log(run_log: str | None) -> Path:
    """Resolve the pi run directory from ``all.log`` path."""
    if not run_log:
        raise RuntimeError("run_log is required for smell-run tracking")
    return Path(run_log).expanduser().resolve().parent


def smell_identity_key(feedback: OrganicFeedback, start_commit: str) -> str:
    """Hash identity from location, start commit, smell type, and owner FQN."""
    smell = feedback.smell
    source_range = smell.location.range
    payload = json.dumps(
        {
            "type": smell.type,
            "owner": feedback.owner,
            "commit_hash": smell.commit_hash or start_commit,
            "location": {
                "uri": smell.location.uri,
                "start_line": source_range.start.line,
                "start_character": source_range.start.character,
                "end_line": source_range.end.line,
                "end_character": source_range.end.character,
            },
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def smell_logical_key(feedback: OrganicFeedback) -> str:
    """Stable smell identity for progress metrics (ignores line shifts)."""
    payload = json.dumps(
        {
            "type": feedback.smell.type,
            "owner": feedback.owner,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _scope_from_value(raw: str) -> OrganicScope:
    match raw:
        case "class":
            return OrganicScope.CLASS
        case "method":
            return OrganicScope.METHOD
        case "constructor":
            return OrganicScope.CONSTRUCTOR
        case _:
            raise ValueError(f"Unknown Organic scope: {raw!r}")


def _nested_overlap_from_value(raw: object) -> bool:
    """Accept bool records and older string labels from smell-run files."""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw not in ("", "none")
    return False


def smell_from_dict(data: Mapping[str, object]) -> Smell:
    """Rebuild a ``Smell`` from ``Smell.to_dict()`` output."""
    location_raw = data["location"]
    if not isinstance(location_raw, dict):
        raise ValueError("Smell location must be an object")
    range_raw = location_raw.get("range")
    if not isinstance(range_raw, dict):
        raise ValueError("Smell range must be an object")
    start_raw = range_raw.get("start")
    end_raw = range_raw.get("end")
    if not isinstance(start_raw, dict) or not isinstance(end_raw, dict):
        raise ValueError("Smell range endpoints must be objects")
    commit_hash = data.get("commit_hash")
    return Smell(
        type=str(data["type"]),
        location=Location(
            uri=str(location_raw["uri"]),
            range=Range(
                start=Position(
                    line=int(start_raw["line"]),
                    character=int(start_raw["character"]),
                ),
                end=Position(
                    line=int(end_raw["line"]),
                    character=int(end_raw["character"]),
                ),
            ),
        ),
        severity=str(data["severity"]),  # type: ignore[arg-type]
        detected_by=str(data["detected_by"]),  # type: ignore[arg-type]
        commit_hash=str(commit_hash) if commit_hash is not None else None,
    )


def feedback_to_record(
    feedback: OrganicFeedback,
    start_commit: str,
) -> dict[str, object]:
    """Serialize one ORGANIC finding for ``smells.json``."""
    return {
        "identity_key": smell_identity_key(feedback, start_commit),
        "owner": feedback.owner,
        "scope": feedback.scope.value,
        "reason": feedback.reason,
        "relevant_metrics": [
            [name, value] for name, value in feedback.relevant_metrics
        ],
        "related_owner": feedback.related_owner,
        "nested_overlap": feedback.nested_overlap,
        "nested_owners": list(feedback.nested_owners),
        "smell": feedback.smell.to_dict(),
    }


def feedback_from_record(record: Mapping[str, object]) -> OrganicFeedback:
    """Rebuild ``OrganicFeedback`` from a stored record."""
    smell_raw = record.get("smell")
    if not isinstance(smell_raw, dict):
        raise ValueError("Smell record is missing smell object")
    metrics_raw = record.get("relevant_metrics")
    metrics: tuple[tuple[str, float | None], ...] = ()
    if isinstance(metrics_raw, list):
        parsed: list[tuple[str, float | None]] = []
        for item in metrics_raw:
            if (
                isinstance(item, list)
                and len(item) == 2
                and isinstance(item[0], str)
                and (item[1] is None or isinstance(item[1], (int, float)))
            ):
                parsed.append((item[0], float(item[1]) if item[1] is not None else None))
        metrics = tuple(parsed)
    nested_owners_raw = record.get("nested_owners")
    nested_owners = (
        tuple(str(item) for item in nested_owners_raw)
        if isinstance(nested_owners_raw, list)
        else ()
    )
    related_owner = record.get("related_owner")
    reason = record.get("reason")
    return OrganicFeedback(
        smell=smell_from_dict(smell_raw),
        owner=str(record["owner"]),
        scope=_scope_from_value(str(record["scope"])),
        reason=str(reason) if isinstance(reason, str) else None,
        relevant_metrics=metrics,
        related_owner=str(related_owner) if isinstance(related_owner, str) else None,
        nested_overlap=_nested_overlap_from_value(record.get("nested_overlap", False)),
        nested_owners=nested_owners,
    )


def initialize_smell_run(
    run_dir: Path,
    *,
    case_id: str,
    start_commit: str,
    feedback: tuple[OrganicFeedback, ...],
) -> SmellRunState:
    """Write the baseline smell list and initial detect snapshot."""
    run_dir.mkdir(parents=True, exist_ok=True)
    baseline_identity_keys: list[str] = []
    baseline_logical_keys: list[str] = []
    entries: dict[str, dict[str, object]] = {}
    for item in feedback:
        key = smell_identity_key(item, start_commit)
        baseline_identity_keys.append(key)
        _append_unique(baseline_logical_keys, smell_logical_key(item))
        entries[key] = feedback_to_record(item, start_commit)
    state = SmellRunState(
        case_id=case_id,
        start_commit=start_commit,
        baseline_identity_keys=baseline_identity_keys,
        baseline_logical_keys=baseline_logical_keys,
        queue=list(baseline_identity_keys),
        introduced_identity_keys=[],
        introduced_logical_keys=[],
        entries=entries,
    )
    save_smell_run(run_dir, state)
    return state


def load_smell_run(run_dir: Path) -> SmellRunState:
    """Load smell-run state from ``run_dir/smells.json``."""
    path = smells_json_path(run_dir)
    if not path.is_file():
        raise RuntimeError(f"Smell run state is missing: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError(f"Smell run state root must be an object: {path}")
    entries_raw = raw.get("entries")
    if not isinstance(entries_raw, dict):
        raise RuntimeError(f"Smell run entries must be an object: {path}")
    entries: dict[str, dict[str, object]] = {}
    for key, value in entries_raw.items():
        if isinstance(key, str) and isinstance(value, dict):
            entries[key] = value
    baseline_identity_keys = [
        str(item) for item in raw.get("baseline_identity_keys", [])
    ]
    introduced_identity_keys = [
        str(item) for item in raw.get("introduced_identity_keys", [])
    ]
    baseline_logical_keys = [
        str(item) for item in raw.get("baseline_logical_keys", [])
    ]
    introduced_logical_keys = [
        str(item) for item in raw.get("introduced_logical_keys", [])
    ]
    if not baseline_logical_keys:
        baseline_logical_keys = _logical_keys_from_identity_keys(
            baseline_identity_keys,
            entries,
        )
    if not introduced_logical_keys:
        introduced_logical_keys = _logical_keys_from_identity_keys(
            introduced_identity_keys,
            entries,
        )
    return SmellRunState(
        case_id=str(raw["case_id"]),
        start_commit=str(raw["start_commit"]),
        baseline_identity_keys=baseline_identity_keys,
        baseline_logical_keys=baseline_logical_keys,
        queue=[str(item) for item in raw.get("queue", [])],
        introduced_identity_keys=introduced_identity_keys,
        introduced_logical_keys=introduced_logical_keys,
        entries=entries,
    )


def save_smell_run(run_dir: Path, state: SmellRunState) -> Path:
    """Persist smell-run state."""
    path = smells_json_path(run_dir)
    payload = {
        "case_id": state.case_id,
        "start_commit": state.start_commit,
        "baseline_identity_keys": state.baseline_identity_keys,
        "baseline_logical_keys": state.baseline_logical_keys,
        "queue": state.queue,
        "introduced_identity_keys": state.introduced_identity_keys,
        "introduced_logical_keys": state.introduced_logical_keys,
        "entries": state.entries,
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _current_introduced_logical_keys(
    state: SmellRunState,
    current_feedback: tuple[OrganicFeedback, ...],
) -> set[str]:
    baseline_logical = set(state.baseline_logical_keys)
    return {
        smell_logical_key(item)
        for item in current_feedback
        if smell_logical_key(item) not in baseline_logical
    }


def introduced_progress(
    state: SmellRunState,
    current_feedback: tuple[OrganicFeedback, ...],
) -> tuple[int, int, int]:
    """Return ``(fixed_introduced, total_introduced, introduced_remaining)``."""
    current_introduced_logical = _current_introduced_logical_keys(
        state,
        current_feedback,
    )
    introduced_registry = set(state.introduced_logical_keys)
    total_introduced = len(introduced_registry)
    fixed_introduced = len(introduced_registry - current_introduced_logical)
    introduced_remaining = len(current_introduced_logical)
    return fixed_introduced, total_introduced, introduced_remaining


def sync_smell_run(
    state: SmellRunState,
    current_feedback: tuple[OrganicFeedback, ...],
) -> SmellRunSync:
    """Replace the detect snapshot and update the introduced registry."""
    start_commit = state.start_commit
    baseline_set = set(state.baseline_identity_keys)
    baseline_logical = set(state.baseline_logical_keys)
    current_by_key = {
        smell_identity_key(item, start_commit): item for item in current_feedback
    }
    current_keys = set(current_by_key)
    newly_introduced: list[OrganicFeedback] = []
    snapshot_keys: list[str] = []

    for key, item in current_by_key.items():
        snapshot_keys.append(key)
        state.entries[key] = feedback_to_record(item, start_commit)
        logical_key = smell_logical_key(item)
        if key not in baseline_set:
            if key not in state.introduced_identity_keys:
                state.introduced_identity_keys.append(key)
            if (
                logical_key not in baseline_logical
                and logical_key not in state.introduced_logical_keys
            ):
                state.introduced_logical_keys.append(logical_key)
                newly_introduced.append(item)

    state.queue = snapshot_keys

    fixed_smells = len(baseline_set - current_keys)
    fixed_introduced_smells, total_introduced_smells, introduced_smells = (
        introduced_progress(state, current_feedback)
    )
    return SmellRunSync(
        metrics=SmellRunMetrics(
            remaining_smells=len(current_feedback),
            introduced_smells=introduced_smells,
            fixed_smells=fixed_smells,
            original_smells=state.original_smells,
            total_introduced_smells=total_introduced_smells,
            fixed_introduced_smells=fixed_introduced_smells,
        ),
        newly_introduced=tuple(newly_introduced),
    )


def metrics_from_state(
    state: SmellRunState,
    current_feedback: tuple[OrganicFeedback, ...],
) -> SmellRunMetrics:
    """Compute metrics without mutating queue or introduced registry."""
    start_commit = state.start_commit
    baseline_set = set(state.baseline_identity_keys)
    current_keys = {
        smell_identity_key(item, start_commit) for item in current_feedback
    }
    fixed_introduced_smells, total_introduced_smells, introduced_smells = (
        introduced_progress(state, current_feedback)
    )
    return SmellRunMetrics(
        remaining_smells=len(current_feedback),
        introduced_smells=introduced_smells,
        fixed_smells=len(baseline_set - current_keys),
        original_smells=state.original_smells,
        total_introduced_smells=total_introduced_smells,
        fixed_introduced_smells=fixed_introduced_smells,
    )


def queued_feedback(state: SmellRunState) -> tuple[OrganicFeedback, ...]:
    """Return smells from the last successful ORGANIC snapshot."""
    return tuple(
        feedback_from_record(state.entries[key])
        for key in state.queue
        if key in state.entries
    )


def metrics_from_queued_snapshot(state: SmellRunState) -> SmellRunMetrics:
    """Compute metrics from the last synced detect snapshot."""
    return metrics_from_state(state, queued_feedback(state))


def next_queue_smell(
    state: SmellRunState,
    current_feedback: tuple[OrganicFeedback, ...] | None = None,
    *,
    repo_path: str | Path,
    skip_identity_keys: set[str],
    planner: PlannerProtocol | None = None,
) -> tuple[OrganicFeedback | None, SmellRunSync]:
    """Pick the next smell from a detect snapshot.

    When ``current_feedback`` is set, sync that snapshot first. When omitted,
    reuse ``state.queue`` from the last successful verify/prepare detect.
    """
    if current_feedback is None:
        feedback = queued_feedback(state)
        sync = SmellRunSync(
            metrics=metrics_from_state(state, feedback),
            newly_introduced=(),
        )
    else:
        sync = sync_smell_run(state, current_feedback)
        feedback = current_feedback
    current_by_key = {
        smell_identity_key(item, state.start_commit): item for item in feedback
    }
    candidates = [
        current_by_key[key]
        for key in state.queue
        if key not in skip_identity_keys and key in current_by_key
    ]
    if not candidates:
        return None, sync
    selected = pick_next_smell(
        [item.smell for item in candidates],
        repo_path,
        planner=planner,
    )
    if selected is None:
        return None, sync
    for item in candidates:
        if item.smell.id == selected.id:
            return item, sync
    raise RuntimeError(
        f"Planned smell is absent from queue candidates: {selected.id}"
    )


def append_metrics_to_content(
    content: str,
    metrics: SmellRunMetrics,
) -> str:
    """Ensure verification content always reports remaining and introduced counts."""
    replacements = {
        "remaining_smells": str(metrics.remaining_smells),
        "introduced_smells": str(metrics.introduced_smells),
        "fixed_smells": str(metrics.fixed_smells),
        "original_smells": str(metrics.original_smells),
        "total_introduced_smells": str(metrics.total_introduced_smells),
        "fixed_introduced_smells": str(metrics.fixed_introduced_smells),
    }
    lines: list[str] = []
    seen: set[str] = set()
    for line in content.splitlines():
        prefix = line.split("=", maxsplit=1)[0] if "=" in line else ""
        if prefix in replacements:
            lines.append(f"{prefix}={replacements[prefix]}")
            seen.add(prefix)
            continue
        lines.append(line)
    for key, value in replacements.items():
        if key not in seen:
            lines.append(f"{key}={value}")
    return "\n".join(lines)
