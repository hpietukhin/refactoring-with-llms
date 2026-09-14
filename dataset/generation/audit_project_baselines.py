#!/usr/bin/env python3
"""Clone Neo4j dataset projects and audit Maven baseline verification."""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from git import Repo as GitRepo

from agents.java_test.analysis import run_java_test_analysis
from config import ROOT
from dataset.generation.graph import Neo4jGraph
from repository.repo import repo_slug
from testing.surefire import TestRunSummary


@dataclass(frozen=True, slots=True)
class ProjectAuditRow:
    project: str
    repo_url: str
    clone_path: str
    size_kib: int
    size_mib: float
    baseline_status: str
    baseline_details: str
    build_system: str | None
    command: str | None
    exit_code: int | None
    tests_total: int | None
    tests_failed: int | None
    tests_errors: int | None


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clone Neo4j projects and run Maven baseline verification",
    )
    parser.add_argument(
        "--projects-root",
        default=str(ROOT / "dataset/projects"),
        help="Directory where project repositories are cloned",
    )
    parser.add_argument(
        "--output-csv",
        default=str(ROOT / "dataset/helper/project_baseline_audit.csv"),
        help="CSV with all audited projects",
    )
    parser.add_argument(
        "--output-json",
        default=str(ROOT / "dataset/helper/project_baseline_audit.json"),
        help="JSON report with passing projects sorted by size",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Timeout in seconds for each baseline test run",
    )
    parser.add_argument(
        "--skip-clone",
        action="store_true",
        help="Skip cloning; reuse existing checkouts under projects-root",
    )
    return parser.parse_args()


def _classify_baseline_failure(result: dict[str, object]) -> tuple[str, str]:
    summary = result.get("summary")
    if not isinstance(summary, TestRunSummary):
        return "toolchain_fail", str(result.get("error") or "no test summary returned")
    if summary.exit_code == 0:
        return "passed", "baseline build/test passed"
    if summary.counts.failed or summary.counts.errors:
        return (
            "test_fail",
            f"tests failed: failed={summary.counts.failed} errors={summary.counts.errors}",
        )
    return "build_fail", f"command exited with code {summary.exit_code}"


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


def _verify_baseline(project_path: Path, timeout: int) -> dict[str, object]:
    result = run_java_test_analysis(
        str(project_path),
        clean=True,
        timeout=timeout,
        code_agent_max_attempts=0,
    )
    status, details = _classify_baseline_failure(result)
    if status != "passed":
        result = run_java_test_analysis(
            str(project_path),
            clean=False,
            timeout=timeout,
            code_agent_max_attempts=0,
        )
        status, details = _classify_baseline_failure(result)
    summary = result.get("summary")
    return {
        "status": status,
        "details": details,
        "build_system": result.get("build_system"),
        "command": result.get("command"),
        "exit_code": (
            int(summary.exit_code) if isinstance(summary, TestRunSummary) else None
        ),
        "tests_total": (
            int(summary.counts.total) if isinstance(summary, TestRunSummary) else None
        ),
        "tests_failed": (
            int(summary.counts.failed) if isinstance(summary, TestRunSummary) else None
        ),
        "tests_errors": (
            int(summary.counts.errors) if isinstance(summary, TestRunSummary) else None
        ),
    }


def _load_neo4j_projects(graph: Neo4jGraph) -> list[dict[str, str | None]]:
    return graph.run(
        """
        MATCH (p:Project)
        RETURN p.name AS project, p.url AS repo_url
        ORDER BY project
        """
    ).data()


def _audit_project(
    project: str,
    repo_url: str | None,
    projects_root: Path,
    *,
    skip_clone: bool,
    timeout: int,
) -> ProjectAuditRow:
    if not repo_url:
        return ProjectAuditRow(
            project=project,
            repo_url="",
            clone_path="",
            size_kib=0,
            size_mib=0.0,
            baseline_status="clone_skip_no_url",
            baseline_details="Neo4j project has no repository URL",
            build_system=None,
            command=None,
            exit_code=None,
            tests_total=None,
            tests_failed=None,
            tests_errors=None,
        )

    clone_path = projects_root / repo_slug(repo_url)
    if not skip_clone:
        try:
            _clone_project(repo_url, clone_path)
        except Exception as exc:
            return ProjectAuditRow(
                project=project,
                repo_url=repo_url,
                clone_path=str(clone_path),
                size_kib=0,
                size_mib=0.0,
                baseline_status="clone_fail",
                baseline_details=str(exc),
                build_system=None,
                command=None,
                exit_code=None,
                tests_total=None,
                tests_failed=None,
                tests_errors=None,
            )

    if not clone_path.is_dir():
        return ProjectAuditRow(
            project=project,
            repo_url=repo_url,
            clone_path=str(clone_path),
            size_kib=0,
            size_mib=0.0,
            baseline_status="clone_missing",
            baseline_details=f"checkout not found at {clone_path}",
            build_system=None,
            command=None,
            exit_code=None,
            tests_total=None,
            tests_failed=None,
            tests_errors=None,
        )

    size_kib = _directory_size_kib(clone_path)
    verification = _verify_baseline(clone_path, timeout)
    return ProjectAuditRow(
        project=project,
        repo_url=repo_url,
        clone_path=str(clone_path),
        size_kib=size_kib,
        size_mib=round(size_kib / 1024, 1),
        baseline_status=str(verification["status"]),
        baseline_details=str(verification["details"]),
        build_system=(
            str(verification["build_system"])
            if verification["build_system"] is not None
            else None
        ),
        command=(
            str(verification["command"]) if verification["command"] is not None else None
        ),
        exit_code=verification["exit_code"],
        tests_total=verification["tests_total"],
        tests_failed=verification["tests_failed"],
        tests_errors=verification["tests_errors"],
    )


def _write_csv(path: Path, rows: list[ProjectAuditRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(asdict(rows[0]).keys()) if rows else list(ProjectAuditRow.__dataclass_fields__)
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

    audited: list[ProjectAuditRow] = []
    for index, row in enumerate(project_rows, start=1):
        project = str(row["project"])
        repo_url = row.get("repo_url")
        print(f"[{index}/{len(project_rows)}] {project}", flush=True)
        audited.append(
            _audit_project(
                project,
                None if repo_url is None else str(repo_url),
                projects_root,
                skip_clone=args.skip_clone,
                timeout=args.timeout,
            )
        )

    passing = sorted(
        (row for row in audited if row.baseline_status == "passed"),
        key=lambda row: row.size_kib,
    )
    failing = [row for row in audited if row.baseline_status != "passed"]

    output_csv = Path(args.output_csv)
    _write_csv(output_csv, audited)

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "projects_root": str(projects_root),
        "summary": {
            "total": len(audited),
            "passed": len(passing),
            "failed": len(failing),
        },
        "passing_by_size": [asdict(row) for row in passing],
        "failed": [asdict(row) for row in failing],
    }
    output_json = Path(args.output_json)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print()
    print(f"Wrote {output_csv}")
    print(f"Wrote {output_json}")
    print(f"Passed baseline: {len(passing)}/{len(audited)}")
    print()
    print("Passing projects sorted by size:")
    for row in passing:
        print(
            f"  {row.size_mib:>7.1f} MiB  {row.project}  ({row.repo_url})"
        )


if __name__ == "__main__":
    main()
