"""Tests for config.toml loading."""

from __future__ import annotations

import pytest
from eliot import log_message

from config import configure_eliot, configure_eliot_for_path, load_settings, logging_file_path, token_budget_cutoff


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (("java", "build_tool"), "maven"),
        (("testing", "min_line_coverage"), 0.8),
        (("planning", "planner"), "greedy"),
        (("planning", "token_budget"), 200_000),
        (("langgraph", "reasoning_effort"), "high"),
        (("logging", "file"), "data/all.log"),
        (("pi", "worktrees_dir"), "experiments/pi/worktrees"),
        (("pi", "output_dir"), "data/pi"),
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


def test_configure_eliot_also_writes_stderr(
    tmp_path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_file = tmp_path / "run.log"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[logging]\nlevel = "INFO"\nfile = "{log_file}"\n',
        encoding="utf-8",
    )
    load_settings.cache_clear()
    configure_eliot(load_settings(config_path))
    log_message(message_type="test:stderr_mirror", case_id="demo")
    err = capsys.readouterr().err
    assert "test:stderr_mirror" in err
    assert "demo" in err


def test_configure_eliot_for_path_writes_to_custom_file(tmp_path) -> None:
    log_file = tmp_path / "pi" / "runs" / "repo" / "uuid" / "all.log"
    configure_eliot_for_path(log_file)
    log_message(message_type="test:pi_run")
    assert log_file.exists()
    assert "test:pi_run" in log_file.read_text(encoding="utf-8")
