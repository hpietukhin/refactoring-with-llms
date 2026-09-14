"""Load manifest generation settings from ``dataset/datasets.config.toml``."""

from __future__ import annotations

import argparse
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import ROOT, neo4j_password, resolve_path

DEFAULT_DATASETS_CONFIG_PATH = ROOT / "dataset" / "datasets.config.toml"


@dataclass(frozen=True, slots=True)
class ManifestGenerationConfig:
    name: str
    output: Path
    report: Path | None
    ready_repos_csv: Path
    repo_cache_root: Path
    timeout_seconds: int
    skip_baseline_verification: bool
    fail_if_quotas_unfilled: bool
    enrich_java_after_generation: bool
    projects: tuple[str, ...]
    blocked_projects: dict[str, str]
    limit_per_project: int
    min_ref_count: int
    max_ref_count: int
    min_elements: int
    max_elements: int
    elements_count: int | None
    outlier_percentile: float
    min_elements_with_smell: int
    max_elements_per_case: int | None
    max_smells_per_case: int | None
    runtime_min_smells: int | None
    runtime_max_smells: int | None
    runtime_type_quotas: dict[int, int]
    max_runtime_classes: int
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str
    size_min_mib: float | None
    size_max_mib: float | None
    graph_fallback_projects: tuple[str, ...]

    def as_namespace(self) -> argparse.Namespace:
        """Expose legacy ``args`` field names used by ``generate_manifest``."""
        return argparse.Namespace(
            projects=",".join(self.projects),
            uri=self.neo4j_uri,
            user=self.neo4j_user,
            password=self.neo4j_password,
            limit_per_project=self.limit_per_project,
            min_ref_count=self.min_ref_count,
            max_ref_count=self.max_ref_count,
            min_elements=self.min_elements,
            max_elements=self.max_elements,
            output=str(self.output),
            report=str(self.report) if self.report is not None else "",
            ready_repos_csv=str(self.ready_repos_csv),
            repo_cache_root=str(self.repo_cache_root),
            timeout=self.timeout_seconds,
            elements_count=self.elements_count,
            outlier_percentile=self.outlier_percentile,
            min_elements_with_smell=self.min_elements_with_smell,
            max_elements_per_case=self.max_elements_per_case,
            max_smells_per_case=self.max_smells_per_case,
            skip_baseline_verification=self.skip_baseline_verification,
            runtime_min_smells=self.runtime_min_smells,
            runtime_max_smells=self.runtime_max_smells,
            runtime_type_quotas=self._runtime_type_quotas_string(),
            max_runtime_classes=self.max_runtime_classes,
            fail_if_quotas_unfilled=self.fail_if_quotas_unfilled,
            enrich_java_after_generation=self.enrich_java_after_generation,
            graph_fallback_projects=list(self.graph_fallback_projects),
            blocked_projects=dict(self.blocked_projects),
            config_name=self.name,
            size_min_mib=self.size_min_mib,
            size_max_mib=self.size_max_mib,
        )

    def _runtime_type_quotas_string(self) -> str:
        if not self.runtime_type_quotas:
            return ""
        return ",".join(
            f"{type_count}:{case_count}"
            for type_count, case_count in sorted(self.runtime_type_quotas.items())
        )


def _optional_int(raw: object) -> int | None:
    if raw is None:
        return None
    return int(raw)


def _optional_float(raw: object) -> float | None:
    if raw is None:
        return None
    return float(raw)


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    section = raw.get(name)
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ValueError(f"[{name}] must be a table")
    return section


def _parse_type_quotas(raw: dict[str, Any]) -> dict[int, int]:
    quotas: dict[int, int] = {}
    for type_count_raw, case_count_raw in raw.items():
        type_count = int(type_count_raw)
        case_count = int(case_count_raw)
        if type_count < 1 or case_count < 1:
            raise ValueError("runtime type quota values must be positive")
        if type_count in quotas:
            raise ValueError(f"duplicate runtime type quota: {type_count}")
        quotas[type_count] = case_count
    return quotas


def load_manifest_generation_config(
    path: Path | None = None,
) -> ManifestGenerationConfig:
    """Parse ``datasets.config.toml`` into a typed generation profile."""
    config_path = path or DEFAULT_DATASETS_CONFIG_PATH
    raw = tomllib.loads(config_path.read_text(encoding="utf-8"))

    generation = _section(raw, "generation")
    projects = _section(raw, "projects")
    blocked = _section(projects, "blocked")
    selection = _section(raw, "selection")
    runtime = _section(raw, "runtime")
    type_quotas_raw = _section(runtime, "type_quotas")
    neo4j = _section(raw, "neo4j")
    size_filter = _section(raw, "size_filter")
    extras = _section(raw, "extras")

    output = resolve_path(str(generation.get("output", "dataset/manifest.jsonl")))
    report_raw = generation.get("report")
    report = resolve_path(str(report_raw)) if report_raw else None

    project_names = projects.get("names")
    if not isinstance(project_names, list) or not project_names:
        raise ValueError("[projects].names must be a non-empty array")

    blocked_projects = {
        str(project): str(reason)
        for project, reason in blocked.items()
    }

    graph_fallback = extras.get("graph_fallback_projects", [])
    if graph_fallback is None:
        graph_fallback = []
    if not isinstance(graph_fallback, list):
        raise ValueError("[extras].graph_fallback_projects must be an array")

    return ManifestGenerationConfig(
        name=str(generation.get("name", config_path.stem)),
        output=output,
        report=report,
        ready_repos_csv=resolve_path(
            str(generation.get("ready_repos_csv", "dataset/helper/projects_1_5mb.csv"))
        ),
        repo_cache_root=resolve_path(
            str(generation.get("repo_cache_root", "temp/eval_repos"))
        ),
        timeout_seconds=int(generation.get("timeout_seconds", 300)),
        skip_baseline_verification=bool(
            generation.get("skip_baseline_verification", False)
        ),
        fail_if_quotas_unfilled=bool(generation.get("fail_if_quotas_unfilled", True)),
        enrich_java_after_generation=bool(
            generation.get("enrich_java_after_generation", True)
        ),
        projects=tuple(str(project) for project in project_names),
        blocked_projects=blocked_projects,
        limit_per_project=int(selection.get("limit_per_project", 5)),
        min_ref_count=int(selection.get("min_ref_count", 3)),
        max_ref_count=int(selection.get("max_ref_count", 20)),
        min_elements=int(selection.get("min_elements", 2)),
        max_elements=int(selection.get("max_elements", 10)),
        elements_count=_optional_int(selection.get("elements_count")),
        outlier_percentile=float(selection.get("outlier_percentile", 0.95)),
        min_elements_with_smell=int(selection.get("min_elements_with_smell", 1)),
        max_elements_per_case=_optional_int(selection.get("max_elements_per_case")),
        max_smells_per_case=_optional_int(selection.get("max_smells_per_case")),
        runtime_min_smells=_optional_int(runtime.get("min_smells")),
        runtime_max_smells=_optional_int(runtime.get("max_smells")),
        runtime_type_quotas=_parse_type_quotas(type_quotas_raw),
        max_runtime_classes=int(runtime.get("max_classes", 10)),
        neo4j_uri=str(neo4j.get("uri", "http://127.0.0.1:7474")),
        neo4j_user=str(neo4j.get("user", "neo4j")),
        neo4j_password=neo4j_password(),
        size_min_mib=_optional_float(size_filter.get("min_mib")),
        size_max_mib=_optional_float(size_filter.get("max_mib")),
        graph_fallback_projects=tuple(str(project) for project in graph_fallback),
    )
