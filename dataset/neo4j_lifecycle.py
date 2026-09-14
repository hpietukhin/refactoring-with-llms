"""Start/stop the local Neo4j dataset runtime around graph queries."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from config import ROOT, neo4j_config
from dataset.dataset import Dataset


def _script_path(key: str) -> Path:
    configured = Path(neo4j_config()[key])
    if not configured.is_absolute():
        configured = ROOT / configured
    return configured


def _run_script(script: Path) -> None:
    if not script.is_file():
        raise FileNotFoundError(f"Neo4j script not found: {script}")
    if not script.stat().st_mode & 0o111:
        raise PermissionError(f"Neo4j script is not executable: {script}")
    result = subprocess.run(
        [str(script)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"{script.name} failed ({result.returncode}): {detail}")


def check_prerequisites() -> None:
    """Fail fast when start/stop scripts are missing."""
    start = _script_path("start_script")
    stop = _script_path("stop_script")
    missing = [str(path) for path in (start, stop) if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            "Neo4j lifecycle scripts missing: " + ", ".join(missing)
        )


def ensure_running(dataset: Dataset | None = None) -> Dataset:
    """Start Neo4j if needed and wait until Cypher HTTP is reachable."""
    check_prerequisites()
    ds = dataset or Dataset()
    if ds.is_available():
        return ds

    _run_script(_script_path("start_script"))
    deadline = time.monotonic() + float(neo4j_config()["startup_timeout_seconds"])
    while time.monotonic() < deadline:
        if ds.is_available():
            return ds
        time.sleep(1)

    raise TimeoutError(
        f"Neo4j did not become available at {ds.uri} "
        f"within {neo4j_config()['startup_timeout_seconds']}s"
    )


def tear_down(dataset: Dataset | None = None) -> None:
    """Stop the Neo4j runtime and wait until HTTP stops answering."""
    check_prerequisites()
    _run_script(_script_path("stop_script"))
    ds = dataset or Dataset()
    deadline = time.monotonic() + float(neo4j_config()["startup_timeout_seconds"])
    while time.monotonic() < deadline:
        if not ds.is_available():
            return
        time.sleep(0.5)
    raise TimeoutError(f"Neo4j is still reachable at {ds.uri} after stop")


@contextmanager
def neo4j_session(dataset: Dataset | None = None) -> Iterator[Dataset]:
    """Bring Neo4j up for the block, then always tear it down."""
    ds = ensure_running(dataset)
    try:
        yield ds
    finally:
        tear_down(ds)
