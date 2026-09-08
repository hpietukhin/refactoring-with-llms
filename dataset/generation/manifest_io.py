"""Read and write experiment manifest JSONL."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from dataset.dataset import CaseRecord

REJECTION_TAXONOMY = frozenset(
    {
        "project_excluded",
        "out_of_bounds",
        "unsupported_commit_build_system_window",
        "checkout_fail",
        "build_fail",
        "test_fail",
        "toolchain_fail",
        "neutral_trajectory",
        "baseline_verified",
    }
)


def make_case_id(project: str, elements: list[str]) -> str:
    """Stable case id: ``{project}:{12-char-hash}``."""
    key = "|".join(sorted(elements))
    digest = hashlib.sha1(f"{project}|{key}".encode("utf-8")).hexdigest()[:12]
    return f"{project}:{digest}"


def normalize_selection(raw: dict[str, Any]) -> dict[str, Any]:
    """Map legacy ``range_metadata`` keys to the simplified ``selection`` shape."""
    if not raw:
        return {}
    selection: dict[str, Any] = {
        "anchor": raw.get("anchor") or raw.get("anchor_element", ""),
        "developer": raw.get("developer", ""),
        "refactorings": raw.get("refactorings", raw.get("ref_count", 0)),
        "ref_types": list(raw.get("ref_types") or []),
    }
    if raw.get("ref_ids"):
        selection["ref_ids"] = list(raw["ref_ids"])
    smells_after = raw.get("smells_after")
    if smells_after is not None:
        selection["smells_after"] = smells_after
    smell_delta = raw.get("smell_delta", raw.get("smell_delta_abs"))
    if smell_delta is not None:
        selection["smell_delta"] = smell_delta
    return selection


def case_to_manifest_record(case: dict[str, Any]) -> dict[str, Any]:
    start_commit = str(case.get("start_commit") or case["start_commit_hash"])
    end_commit = case.get("end_commit") or case.get("end_commit_hash")
    selection = dict(case.get("selection") or normalize_selection(case.get("range_metadata") or {}))
    record = CaseRecord(
        case_id=str(case["case_id"]),
        project=str(case["project"]),
        repo_url=str(case["repo_url"]),
        start_commit=start_commit,
        elements=list(case.get("elements") or []),
        start_commit_order=case.get("start_commit_order"),
        end_commit=str(end_commit) if end_commit else None,
        end_commit_order=case.get("end_commit_order"),
        start_state=dict(case.get("start_state") or {}),
        selection=selection,
    )
    payload = record.to_dict()
    commit_window = case.get("commit_build_system_window")
    if commit_window:
        payload["commit_build_system_window"] = dict(commit_window)
    verification = dict(case.get("baseline_verification") or case.get("baseline") or {})
    baseline_fields = {
        key: verification[key]
        for key in (
            "command",
            "tests_total",
            "tests_failed",
            "tests_errors",
            "build_system",
            "java_source",
            "sdkman_java",
        )
        if verification.get(key) is not None
    }
    if baseline_fields:
        payload["baseline"] = baseline_fields
    return payload


def write_manifest(path: Path, cases: list[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for case in cases:
            line = case_to_manifest_record(case)
            handle.write(json.dumps(line, ensure_ascii=False))
            handle.write("\n")
            count += 1
    return count


def iter_manifest(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at {path}:{line_no}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"JSONL line must be an object at {path}:{line_no}")
            yield row


def load_manifest_cases(path: Path) -> list[dict[str, Any]]:
    return list(iter_manifest(path))
