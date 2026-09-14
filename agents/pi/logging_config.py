"""Eliot logging configuration for pi experiment runs."""

from __future__ import annotations

from pathlib import Path

from config import ROOT, configure_eliot, configure_eliot_for_path, settings


def resolve_run_log_path(run_log: str | None) -> Path | None:
    if not run_log:
        return None
    path = Path(run_log)
    if not path.is_absolute():
        path = ROOT / path
    return path


def configure_pi_logging(run_log: str | None) -> Path:
    """Send Eliot output to a per-run log file or the default project log."""
    run_log_path = resolve_run_log_path(run_log)
    if run_log_path is not None:
        return configure_eliot_for_path(run_log_path)
    return configure_eliot(settings)
