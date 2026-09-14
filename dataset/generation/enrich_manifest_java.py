#!/usr/bin/env python3
"""Add baseline Java toolchain fields to an existing manifest JSONL."""

from __future__ import annotations

import argparse
from pathlib import Path

from dataset.generation.datasets_config import (
    DEFAULT_DATASETS_CONFIG_PATH,
    load_manifest_generation_config,
)
from dataset.generation.manifest_io import iter_manifest, write_manifest
from detection.java_version import detect_project_java_version
from repository.repo import Repo, repo_slug


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Enrich manifest rows with Java baseline metadata")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_DATASETS_CONFIG_PATH),
        help="Manifest generation profile TOML (uses [generation].output when set)",
    )
    parser.add_argument(
        "--input",
        default="",
        help="Input manifest JSONL (overrides config output path)",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Output manifest JSONL (default: overwrite input)",
    )
    parser.add_argument(
        "--repo-cache-root",
        default="",
        help="Git cache (overrides config repo_cache_root)",
    )
    return parser.parse_args()


def _default_command(build_system: str | None) -> str | None:
    if build_system == "gradle":
        return "gradle clean test -x signMavenJavaPublication"
    if build_system == "maven":
        return "mvn clean test"
    return None


def enrich_case(case: dict[str, object], *, cache_root: Path) -> dict[str, object]:
    repo_url = str(case["repo_url"])
    start_commit = str(case["start_commit"])
    checkout = cache_root / repo_slug(repo_url)
    repo = Repo.for_experiment(repo_url, start_commit, checkout_path=checkout)
    version = detect_project_java_version(repo.path)

    verification = dict(case.get("baseline_verification") or case.get("baseline") or {})
    if version is not None:
        verification.setdefault("build_system", version.build_system)
        verification.setdefault("java_source", version.source)
        verification.setdefault("sdkman_java", version.sdkman_java)
    verification.setdefault(
        "command",
        _default_command(str(verification.get("build_system") or "")),
    )
    case["baseline_verification"] = verification
    return case


def main() -> int:
    args = _parse_args()
    profile = load_manifest_generation_config(Path(args.config))
    input_path = Path(args.input) if args.input else profile.output
    output_path = Path(args.output) if args.output else input_path
    cache_root = (
        Path(args.repo_cache_root)
        if args.repo_cache_root
        else profile.repo_cache_root / "manifest-java"
    )
    cases = [enrich_case(dict(row), cache_root=cache_root) for row in iter_manifest(input_path)]
    write_manifest(output_path, cases)
    print(f"Wrote {len(cases)} cases to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
