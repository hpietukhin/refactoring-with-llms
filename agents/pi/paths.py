"""Filesystem locations for pi deep-refactor experiments."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from config import ROOT, resolve_path, settings
from repository.repo import repo_slug
from uuid6 import uuid7

DEFAULT_WORKTREES_DIR = "experiments/pi/worktrees"
DEFAULT_OUTPUT_DIR = "data/pi"


def pi_config(cfg: Mapping[str, object] | None = None) -> dict[str, object]:
    """Return the optional ``[pi]`` section from settings."""
    section = (cfg or settings).get("pi", {})
    if not isinstance(section, dict):
        return {}
    return dict(section)


def pi_worktrees_root(cfg: Mapping[str, object] | None = None) -> Path:
    """Return the pi-only checkout root (isolated from experiments.main)."""
    section = pi_config(cfg)
    raw = str(section.get("worktrees_dir", DEFAULT_WORKTREES_DIR))
    return resolve_path(raw)


def pi_output_dir(cfg: Mapping[str, object] | None = None) -> Path:
    """Return the directory for pi jsonl / run artifacts."""
    section = pi_config(cfg)
    raw = str(section.get("output_dir", DEFAULT_OUTPUT_DIR))
    path = resolve_path(raw)
    path.mkdir(parents=True, exist_ok=True)
    return path


def pi_worktree_path(repo_url: str, cfg: Mapping[str, object] | None = None) -> Path:
    """Return a unique pi checkout path for one experiment run."""
    return pi_worktrees_root(cfg) / f"{repo_slug(repo_url)}_{uuid7()}"


def create_pi_run_paths(
    repo_url: str,
    cfg: Mapping[str, object] | None = None,
) -> tuple[Path, Path]:
    """Return ``(run_dir, run_log)`` under ``data/pi/runs/{repo}/{uuid}/``."""
    run_dir = pi_output_dir(cfg) / "runs" / repo_slug(repo_url) / str(uuid7())
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir, run_dir / "all.log"


def safe_case_id(case_id: str) -> str:
    """Filesystem-safe case id for log filenames."""
    return "".join(
        character if character.isalnum() or character in "-._" else "_"
        for character in case_id
    )


def append_pi_event(
    event_type: str,
    payload: Mapping[str, object],
    *,
    case_id: str | None = None,
    cfg: Mapping[str, object] | None = None,
) -> Path:
    """Append one JSON line under ``data/pi/`` (and optionally a per-case file)."""
    output = pi_output_dir(cfg)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event_type,
        **dict(payload),
    }
    if case_id is not None:
        record.setdefault("case_id", case_id)

    runs_path = output / "runs.jsonl"
    with runs_path.open("a", encoding="utf-8") as log_file:
        log_file.write(json.dumps(record, ensure_ascii=False) + "\n")

    if case_id is not None:
        case_path = output / f"{safe_case_id(case_id)}.jsonl"
        with case_path.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(record, ensure_ascii=False) + "\n")
        return case_path
    return runs_path


def project_relative(path: Path) -> str:
    """Return ``path`` relative to the project root when possible."""
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path.resolve())
