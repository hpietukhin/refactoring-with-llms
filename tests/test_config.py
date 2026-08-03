"""Tests for config.toml loading."""

from __future__ import annotations

import pytest
from eliot import log_message

from config import configure_eliot, load_settings, logging_file_path, token_budget_cutoff


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (("java", "build_tool"), "maven"),
        (("testing", "min_line_coverage"), 0.8),
        (("planning", "planner"), "no_priority"),
        (("planning", "token_budget"), 200_000),
        (("logging", "file"), "data/all.log"),
    ],
)
def test_load_settings_reads_sections_from_toml(path: tuple[str, str], expected: object) -> None:
    load_settings.cache_clear()
    cfg = load_settings()
    section, key = path
    assert cfg[section][key] == expected


def test_token_budget_cutoff_reads_planning_section() -> None:
    load_settings.cache_clear()
    cfg = load_settings()
    assert token_budget_cutoff(cfg) == 200_000


def test_configure_eliot_uses_logging_section(tmp_path) -> None:
    log_file = tmp_path / "run.log"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[logging]\nlevel = "INFO"\nfile = "{log_file}"\n',
        encoding="utf-8",
    )

    load_settings.cache_clear()
    cfg = load_settings(config_path)

    assert logging_file_path(cfg) == log_file
    assert configure_eliot(cfg) == log_file

    log_message(message_type="test:configured")
    assert log_file.exists()
    assert log_file.read_text(encoding="utf-8").strip()
