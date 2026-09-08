#!/usr/bin/env python3
"""Clone Neo4j dataset projects and report checkout sizes."""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from git import Repo as GitRepo

from config import ROOT
from dataset.generation.graph import Neo4jGraph
from repository.repo import repo_slug


@dataclass(frozen=True, slots=True)
class ProjectSizeRow:
    project: str
    repo_url: str
    clone_path: str
    size_kib: int
    size_mib: float
    clone_status: str
    clone_details: str


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clone Neo4j projects and sort them by checkout size",
    )
    parser.add_argument(
        "--projects-root",
        default=str(ROOT / "dataset/projects"),
        help="Directory where project repositories are cloned",
    )
    parser.add_argument(
        "--output-csv",
        default=str(ROOT / "dataset/helper/project_sizes.csv"),
        help="CSV with all projects sorted by size",
    )
    parser.add_argument(
        "--output-json",
        default=str(ROOT / "dataset/helper/project_sizes.json"),
        help="JSON report with projects sorted by size",
    )
    parser.add_argument(
        "--skip-clone",
        action="store_true",
        help="Skip cloning; only measure existing checkouts",
    )
    return parser.parse_args()


def _directory_size_kib(path: Path) -> int:
    result = subprocess.run(
        ["du", "-sk", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"du failed for {path}: {result.stderr.strip()}")
    return int(result.stdout.split()[0])


def _clone_project(repo_url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if (destination / ".git").exists():
        return
    GitRepo.clone_from(repo_url, destination, depth=1)


def _load_neo4j_projects(graph: Neo4jGraph) -> list[dict[str, str | None]]:
    return graph.run(
        """
        MATCH (p:Project)
        RETURN p.name AS project, p.url AS repo_url
        ORDER BY project
        """
    ).data()


def _measure_project(
    project: str,
    repo_url: str | None,
    projects_root: Path,
    *,
    skip_clone: bool,
) -> ProjectSizeRow:
    if not repo_url:
        return ProjectSizeRow(
            project=project,
            repo_url="",
            clone_path="",
            size_kib=0,
            size_mib=0.0,
            clone_status="skip_no_url",
            clone_details="Neo4j project has no repository URL",
        )

    clone_path = projects_root / repo_slug(repo_url)
    if not skip_clone:
        try:
            _clone_project(repo_url, clone_path)
        except Exception as exc:
            return ProjectSizeRow(
                project=project,
                repo_url=repo_url,
                clone_path=str(clone_path),
                size_kib=0,
                size_mib=0.0,
                clone_status="clone_fail",
                clone_details=str(exc),
            )

    if not clone_path.is_dir():
        return ProjectSizeRow(
            project=project,
            repo_url=repo_url,
            clone_path=str(clone_path),
            size_kib=0,
            size_mib=0.0,
            clone_status="clone_missing",
            clone_details=f"checkout not found at {clone_path}",
        )

    size_kib = _directory_size_kib(clone_path)
    return ProjectSizeRow(
        project=project,
        repo_url=repo_url,
        clone_path=str(clone_path),
        size_kib=size_kib,
        size_mib=round(size_kib / 1024, 1),
        clone_status="cloned",
        clone_details="checkout present",
    )


def _write_csv(path: Path, rows: list[ProjectSizeRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(ProjectSizeRow.__dataclass_fields__)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def main() -> None:
    args = _parse_args()
    projects_root = Path(args.projects_root).expanduser().resolve()
    graph = Neo4jGraph()
    project_rows = _load_neo4j_projects(graph)

    measured: list[ProjectSizeRow] = []
    for index, row in enumerate(project_rows, start=1):
        project = str(row["project"])
        repo_url = row.get("repo_url")
        print(f"[{index}/{len(project_rows)}] {project}", flush=True)
        measured.append(
            _measure_project(
                project,
                None if repo_url is None else str(repo_url),
                projects_root,
                skip_clone=args.skip_clone,
            )
        )

    by_size = sorted(measured, key=lambda row: row.size_kib)

    output_csv = Path(args.output_csv)
    _write_csv(output_csv, by_size)

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "projects_root": str(projects_root),
        "summary": {
            "total": len(measured),
            "cloned": sum(1 for row in measured if row.clone_status == "cloned"),
            "failed_or_missing": sum(
                1 for row in measured if row.clone_status != "cloned"
            ),
        },
        "by_size": [asdict(row) for row in by_size],
    }
    output_json = Path(args.output_json)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print()
    print(f"Wrote {output_csv}")
    print(f"Wrote {output_json}")
    print()
    print("Projects sorted by size (smallest first):")
    for row in by_size:
        if row.clone_status != "cloned":
            print(f"  {'—':>7}       {row.project}  [{row.clone_status}]")
            continue
        print(f"  {row.size_mib:>7.1f} MiB  {row.project}  ({row.repo_url})")


if __name__ == "__main__":
    main()
