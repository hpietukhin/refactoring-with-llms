"""Load project settings from ``config.toml`` and configure Eliot logging."""

from __future__ import annotations

import tomllib
from functools import lru_cache, partial
from pathlib import Path
from typing import Any, BinaryIO, TextIO

from eliot import to_file

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = ROOT / "config.toml"

type Settings = dict[str, Any]
type LogFile = BinaryIO | TextIO


@lru_cache
def load_settings(path: Path | None = None) -> Settings:
    """Parse ``config.toml`` once and cache the result."""
    config_path = path or DEFAULT_CONFIG_PATH
    return tomllib.loads(config_path.read_text(encoding="utf-8"))


settings = load_settings()


def section(name: str, cfg: Settings | None = None) -> Settings:
    """Return one top-level section from ``config.toml``."""
    return (cfg or settings)[name]


dataset_config = partial(section, "dataset")
neo4j_config = partial(section, "neo4j")
experiments_config = partial(section, "experiments")
langgraph_config = partial(section, "langgraph")
openrouter_config = partial(section, "openrouter")
planning_config = partial(section, "planning")


def langgraph_model(cfg: Settings | None = None) -> str:
    """Return the OpenRouter model id from ``[langgraph].model``."""
    return str(langgraph_config(cfg)["model"])


def token_budget_cutoff(cfg: Settings | None = None) -> float:
    """A* cutoff from ``[planning].token_budget`` (required, no default)."""
    return float(planning_config(cfg)["token_budget"])


def resolve_path(raw: str, cfg: Settings | None = None) -> Path:
    """Resolve a config path relative to the project root when needed."""
    del cfg
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    return path


def experiments_output_dir(cfg: Settings | None = None) -> Path:
    """Return ``[experiments].output_dir`` as an absolute path."""
    return resolve_path(str(experiments_config(cfg)["output_dir"]), cfg)


def data_scratch_dir(cfg: Settings | None = None) -> Path:
    """Return a project-local scratch directory for transient artifacts."""
    del cfg
    path = ROOT / "data" / "scratch"
    path.mkdir(parents=True, exist_ok=True)
    return path


def logging_file_path(cfg: Settings | None = None) -> Path | None:
    """Return the configured Eliot log file path, if any."""
    logging_cfg = (cfg or settings).get("logging")
    if not logging_cfg:
        return None
    raw_path = logging_cfg.get("file")
    if not raw_path:
        return None
    path = Path(raw_path)
    if not path.is_absolute():
        path = ROOT / path
    return path


def configure_eliot(cfg: Settings) -> Path:
    """Send Eliot JSON logs to the file from ``[logging]`` in ``config.toml``."""
    path = logging_file_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    to_file(open(path, "ab"))
    return path
