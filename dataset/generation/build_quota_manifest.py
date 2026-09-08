#!/usr/bin/env python3
"""Build a quota-filled manifest by searching projects from datasets.config.toml."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from dataset.generation.commit_build_system_filter import classify_commit_window_build_system
from dataset.generation.datasets_config import (
    DEFAULT_DATASETS_CONFIG_PATH,
    load_manifest_generation_config,
)
from dataset.generation.enrich_manifest_java import enrich_case
from dataset.generation.generate_manifest import (
    _commit_window_supported,
    _evaluate_runtime_case,
    _select_draft_cases,
    make_case_id,
)
from dataset.generation.graph import Neo4jGraph
from dataset.generation.manifest_io import write_manifest


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build quota-filled experiment manifest")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_DATASETS_CONFIG_PATH),
        help="Manifest generation profile TOML",
    )
    return parser.parse_args()


def _load_repo_urls(csv_path: Path) -> dict[str, str]:
    with csv_path.open(encoding="utf-8") as handle:
        return {row["project"]: row["repo_url"] for row in csv.DictReader(handle)}


def _build_case_from_verification(
    *,
    draft: dict[str, object],
    project: str,
    repo_url: str,
    commit_build_window: object,
    verification: dict[str, object],
) -> dict[str, object]:
    runtime = dict(verification["runtime"])
    runtime_type_count = int(runtime["smell_type_count"])
    return {
        **draft,
        "elements": runtime["elements"],
        "case_id": make_case_id(project, list(runtime["elements"])),
        "start_state": {
            "smells_total": runtime["smells_total"],
            "smell_types": runtime["smell_types"],
            "smell_type_count": runtime_type_count,
            "class_count": runtime["class_count"],
            "classes": runtime["classes"],
            "classes_connected": runtime["classes_connected"],
        },
        "repo_url": repo_url,
        "commit_build_system_window": commit_build_window.to_dict(),  # type: ignore[union-attr]
        "baseline_verification": verification.get("baseline", verification),
    }


def _evaluate_planned_case(
    *,
    graph: Neo4jGraph,
    project: str,
    draft_case_id: str,
    type_count: int,
    repo_urls: dict[str, str],
    args: argparse.Namespace,
) -> dict[str, object] | None:
    repo_url = repo_urls.get(project)
    if not repo_url:
        return None
    for draft in _select_draft_cases(graph, project, args):
        if str(draft["case_id"]) != draft_case_id:
            continue
        commit_build_window = classify_commit_window_build_system(
            graph,
            project,
            repo_url,
            draft["start_commit_order"],
            draft["end_commit_order"],
            cache_root=args.repo_cache_root,
        )
        if not _commit_window_supported(
            commit_build_window,
            repo_url=repo_url,
            start_commit=str(draft["start_commit"]),
            cache_root=args.repo_cache_root,
        ):
            return None
        verification = _evaluate_runtime_case(draft, repo_url, args, {type_count})
        if verification.get("status") != "passed":
            return None
        runtime = dict(verification["runtime"])
        if int(runtime["smell_type_count"]) != type_count:
            return None
        return _build_case_from_verification(
            draft=draft,
            project=project,
            repo_url=repo_url,
            commit_build_window=commit_build_window,
            verification=verification,
        )
    return None


def build_quota_manifest(config_path: Path) -> tuple[list[dict[str, object]], dict[int, int]]:
    config = load_manifest_generation_config(config_path)
    args = config.as_namespace()
    graph = Neo4jGraph(args.uri, auth=(args.user, args.password))
    repo_urls = _load_repo_urls(config.ready_repos_csv)

    quotas = dict(config.runtime_type_quotas)
    remaining = dict(quotas)
    cases: list[dict[str, object]] = []
    used_case_ids: set[str] = set()

    while remaining:
        progress = False
        for type_count in sorted(remaining):
            if remaining[type_count] <= 0:
                continue
            for project in config.projects:
                if remaining[type_count] <= 0:
                    break
                repo_url = repo_urls.get(project)
                if not repo_url:
                    continue
                for draft in _select_draft_cases(graph, project, args):
                    if remaining[type_count] <= 0:
                        break
                    commit_build_window = classify_commit_window_build_system(
                        graph,
                        project,
                        repo_url,
                        draft["start_commit_order"],
                        draft["end_commit_order"],
                        cache_root=args.repo_cache_root,
                    )
                    if not _commit_window_supported(
                        commit_build_window,
                        repo_url=repo_url,
                        start_commit=str(draft["start_commit"]),
                        cache_root=args.repo_cache_root,
                    ):
                        continue
                    verification = _evaluate_runtime_case(
                        draft,
                        repo_url,
                        args,
                        {type_count},
                    )
                    if verification.get("status") != "passed":
                        continue
                    runtime = dict(verification["runtime"])
                    runtime_type_count = int(runtime["smell_type_count"])
                    if runtime_type_count != type_count:
                        continue
                    case = _build_case_from_verification(
                        draft=draft,
                        project=project,
                        repo_url=repo_url,
                        commit_build_window=commit_build_window,
                        verification=verification,
                    )
                    case_id = str(case["case_id"])
                    if case_id in used_case_ids:
                        continue
                    remaining[type_count] -= 1
                    progress = True
                    used_case_ids.add(case_id)
                    cases.append(case)
                    break
        if not progress:
            break

    if config.enrich_java_after_generation:
        java_cache = config.repo_cache_root / "manifest-java"
        cases = [enrich_case(dict(case), cache_root=java_cache) for case in cases]

    return cases, remaining


def main() -> int:
    args = _parse_args()
    config_path = Path(args.config)
    config = load_manifest_generation_config(config_path)
    cases, remaining = build_quota_manifest(config_path)

    report_path = config.output.with_suffix(".quota.report.json")
    report = {
        "config": config.name,
        "quotas": config.runtime_type_quotas,
        "remaining": remaining,
        "accepted": len(cases),
        "cases": [
            {
                "case_id": case["case_id"],
                "project": case["project"],
                "type_count": case["start_state"]["smell_type_count"],
            }
            for case in cases
        ],
    }
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    write_manifest(config.output, cases)

    print(f"accepted {len(cases)} cases")
    print(f"remaining quotas: {remaining}")
    print(f"manifest: {config.output}")
    print(f"report: {report_path}")

    if remaining and config.fail_if_quotas_unfilled:
        raise RuntimeError(f"Could not fill runtime smell type quotas: {remaining}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
