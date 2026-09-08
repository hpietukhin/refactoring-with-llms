"""CK metrics report parsing and collection.

Public API is methods only: ``parse_ck_class_csv``, ``parse_ck_method_csv``,
and ``run_ck_metrics``. Uses ``GradleRunner`` for packaging/running CK; CSV
parsing and Eliot logging stay here.

Named ``ck_reports`` (not ``ck``) so it does not collide with the vendored
``java/metrics/ck`` Gradle project directory.
"""

from __future__ import annotations

import csv
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from eliot import log_message

from config import data_scratch_dir
from java.metrics.runner import GradleRunner
from repository.repo import Repo

_CLASS_CSV = "class.csv"
_METHOD_CSV = "method.csv"


@dataclass(slots=True)
class ClassMetrics:
    """One row from CK ``class.csv``."""

    file: str
    class_name: str
    type: str
    cbo: float = 0.0
    wmc: float = 0.0
    dit: float = 0.0
    noc: float = 0.0
    rfc: float = 0.0
    lcom: float = 0.0
    loc: float = 0.0
    extras: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class MethodMetrics:
    """One row from CK ``method.csv``."""

    file: str
    class_name: str
    method: str
    cbo: float = 0.0
    wmc: float = 0.0
    rfc: float = 0.0
    loc: float = 0.0
    line: int = 0
    extras: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class MetricsTotals:
    """Aggregated totals / means from a CK run."""

    classes: int = 0
    methods: int = 0
    mean_cbo: float | None = None
    mean_lcom: float | None = None
    mean_wmc: float | None = None
    total_loc: float = 0.0


@dataclass(slots=True)
class MetricsSummary:
    """Summary of one CK metrics invocation with parsed CSV results."""

    exit_code: int = 0
    classes: list[ClassMetrics] = field(default_factory=list)
    methods: list[MethodMetrics] = field(default_factory=list)
    totals: MetricsTotals = field(default_factory=MetricsTotals)
    output_dir: Path | None = None
    stdout: str = ""
    stderr: str = ""

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and self.totals.classes > 0


def _float_field(row: dict[str, str], key: str, default: float = 0.0) -> float:
    raw = row.get(key, "")
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _int_field(row: dict[str, str], key: str, default: int = 0) -> int:
    raw = row.get(key, "")
    if raw is None or raw == "":
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def _known_class_keys() -> frozenset[str]:
    return frozenset(
        {"file", "class", "type", "cbo", "wmc", "dit", "noc", "rfc", "lcom", "loc"}
    )


def _known_method_keys() -> frozenset[str]:
    return frozenset({"file", "class", "method", "cbo", "wmc", "rfc", "loc", "line"})


def parse_ck_class_csv(path: Path) -> list[ClassMetrics]:
    """Parse CK ``class.csv`` into class-level metric rows."""
    if not path.is_file():
        return []
    known = _known_class_keys()
    results: list[ClassMetrics] = []
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                results.append(
                    ClassMetrics(
                        file=row.get("file", ""),
                        class_name=row.get("class", ""),
                        type=row.get("type", ""),
                        cbo=_float_field(row, "cbo"),
                        wmc=_float_field(row, "wmc"),
                        dit=_float_field(row, "dit"),
                        noc=_float_field(row, "noc"),
                        rfc=_float_field(row, "rfc"),
                        lcom=_float_field(row, "lcom"),
                        loc=_float_field(row, "loc"),
                        extras={key: value for key, value in row.items() if key not in known},
                    )
                )
    except csv.Error as exc:
        log_message(message_type="ck:malformed", path=str(path), exception=str(exc))
    return results


def parse_ck_method_csv(path: Path) -> list[MethodMetrics]:
    """Parse CK ``method.csv`` into method-level metric rows."""
    if not path.is_file():
        return []
    known = _known_method_keys()
    results: list[MethodMetrics] = []
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                results.append(
                    MethodMetrics(
                        file=row.get("file", ""),
                        class_name=row.get("class", ""),
                        method=row.get("method", ""),
                        cbo=_float_field(row, "cbo"),
                        wmc=_float_field(row, "wmc"),
                        rfc=_float_field(row, "rfc"),
                        loc=_float_field(row, "loc"),
                        line=_int_field(row, "line"),
                        extras={key: value for key, value in row.items() if key not in known},
                    )
                )
    except csv.Error as exc:
        log_message(message_type="ck:malformed", path=str(path), exception=str(exc))
    return results


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _compute_totals(
    classes: list[ClassMetrics],
    methods: list[MethodMetrics],
) -> MetricsTotals:
    return MetricsTotals(
        classes=len(classes),
        methods=len(methods),
        mean_cbo=_mean([item.cbo for item in classes]),
        mean_lcom=_mean([item.lcom for item in classes]),
        mean_wmc=_mean([item.wmc for item in classes]),
        total_loc=sum(item.loc for item in classes),
    )


def _log_metrics(summary: MetricsSummary) -> None:
    totals = summary.totals
    log_message(
        message_type="ck:metrics",
        exit_code=summary.exit_code,
        classes=totals.classes,
        methods=totals.methods,
        mean_cbo=totals.mean_cbo,
        mean_lcom=totals.mean_lcom,
        mean_wmc=totals.mean_wmc,
        total_loc=totals.total_loc,
        output_dir=str(summary.output_dir) if summary.output_dir else None,
    )


def _summary_from_output(
    output_dir: Path,
    *,
    exit_code: int,
    stdout: str = "",
    stderr: str = "",
) -> MetricsSummary:
    classes = parse_ck_class_csv(output_dir / _CLASS_CSV)
    methods = parse_ck_method_csv(output_dir / _METHOD_CSV)
    summary = MetricsSummary(
        exit_code=exit_code,
        classes=classes,
        methods=methods,
        totals=_compute_totals(classes, methods),
        output_dir=output_dir,
        stdout=stdout,
        stderr=stderr,
    )
    _log_metrics(summary)
    return summary


def run_ck_metrics(
    repo: Repo,
    *,
    output_dir: str | Path | None = None,
    timeout: float = 300,
    use_jars: bool = False,
    max_files: int = 0,
    variables_and_fields: bool = False,
    ignored_directories: Sequence[str] = (),
    ensure_package: bool = True,
) -> MetricsSummary:
    """Run CK through ``GradleRunner``, parse CSVs, and log aggregate metrics."""
    runner = GradleRunner(repo, timeout=timeout)
    keep_output = output_dir is not None
    out = (
        Path(output_dir)
        if keep_output
        else Path(tempfile.mkdtemp(prefix="ck-metrics-", dir=data_scratch_dir()))
    )
    try:
        process = runner.collect(
            out,
            use_jars=use_jars,
            max_files=max_files,
            variables_and_fields=variables_and_fields,
            ignored_directories=ignored_directories,
            ensure_package=ensure_package,
        )
    except (OSError, RuntimeError) as exc:
        summary = MetricsSummary(exit_code=-1, output_dir=out, stderr=str(exc))
        _log_metrics(summary)
        return summary
    return _summary_from_output(
        out,
        exit_code=int(process.returncode),
        stdout=str(process.stdout or ""),
        stderr=str(process.stderr or ""),
    )


__all__ = [
    "ClassMetrics",
    "MethodMetrics",
    "MetricsTotals",
    "MetricsSummary",
    "parse_ck_class_csv",
    "parse_ck_method_csv",
    "run_ck_metrics",
]
