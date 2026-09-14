#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
import tempfile
from bisect import bisect_left
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
import time

import json

from agents.java_test.analysis import run_java_test_analysis
from config import ROOT
from detection.java_version import detect_project_java_version
from dataset.generation.datasets_config import (
    DEFAULT_DATASETS_CONFIG_PATH,
    load_manifest_generation_config,
)
from dataset.generation.manifest_io import make_case_id, write_manifest
from dataset.generation.commit_build_system_filter import (
    classify_commit_build_system,
    classify_commit_window_build_system,
)
from dataset.generation.graph import Neo4jGraph
from detection.organic import OrganicDetector
from planning.ast.models import AstElement, AstIndex
from planning.ast.runner import GradleRunner
from repository.repo import Repo, repo_slug
from testing.surefire import TestRunSummary
from workflows.composite.nodes.detect import filter_smells_to_elements
from smell.smell import Smell


# Start-condition defaults (strict, paper-aligned practical filter)
DEFAULT_MIN_REFS = 3
DEFAULT_MAX_REFS = 20
DEFAULT_MIN_ELEMENTS = 2
DEFAULT_MAX_ELEMENTS = 10
ALLOWED_PROJECTS = {
    "Achilles",
    "Alfred MPI",
    "Bublag Confetti",
    "Dagger",
    "Drugis Common",
    "Facebook SDK for Android",
    "GitHub Pull Request Builder",
    "IRC Bot (c2nes/ircbot)",
    "Ikasan",
    "JBoss Ballroom",
    "JUnit4",
    "Lyra",
    "Netflix Hystrix",
    "Netflix SimianArmy",
    "OkHttp",
    "Passsafe",
    "PhiCode Philib",
    "Presto DB",
    "Pusher Java Client",
    "Retrofit",
    "Spring Boot",
    "TUBAME Migration Tool",
    "Tap4j",
    "Whydah - UserAdminService",
}

BLOCKED_PROJECTS = {
    "OkHttp": "excluded: historical compatibility/patch target",
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate experiment manifest from Neo4j composite refactorings",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_DATASETS_CONFIG_PATH),
        help="Manifest generation profile TOML (default: dataset/datasets.config.toml)",
    )
    cli = parser.parse_args()
    return load_manifest_generation_config(Path(cli.config)).as_namespace()


def _parse_type_quotas(raw: str) -> dict[int, int]:
    quotas: dict[int, int] = {}
    for item in raw.split(","):
        stripped = item.strip()
        if not stripped:
            continue
        type_count_raw, separator, case_count_raw = stripped.partition(":")
        if not separator:
            raise ValueError(
                "--runtime-type-quotas entries must use <types>:<cases>"
            )
        type_count = int(type_count_raw)
        case_count = int(case_count_raw)
        if type_count < 1 or case_count < 1:
            raise ValueError("runtime type quota values must be positive")
        if type_count in quotas:
            raise ValueError(f"duplicate runtime type quota: {type_count}")
        quotas[type_count] = case_count
    return quotas


def _owning_class(index: AstIndex, element: AstElement) -> AstElement | None:
    candidates = [
        candidate
        for candidate in index.elements
        if candidate.kind in {"class", "interface", "enum", "type"}
        and candidate.file == element.file
        and candidate.start_line <= element.start_line
        and candidate.end_line >= element.end_line
    ]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda candidate: (
            candidate.end_line - candidate.start_line,
            candidate.id,
        ),
    )


def _element_class_map(index: AstIndex) -> dict[str, str]:
    owners: dict[str, str] = {}
    for element in index.elements:
        owner = _owning_class(index, element)
        if owner is not None:
            owners[element.id] = owner.id
    return owners


def _connected_class_scope(
    index: AstIndex,
    smell_locations: list[tuple[str, int]],
    repo_path: Path,
) -> tuple[bool, set[str]]:
    element_owners = _element_class_map(index)
    smell_classes: set[str] = set()
    for file_path, line in smell_locations:
        element = index.element_at(file_path, line, repo_path)
        if element is None:
            continue
        if owner := element_owners.get(element.id):
            smell_classes.add(owner)
    if not smell_classes:
        return False, set()

    class_neighbors: dict[str, set[str]] = {}
    for edge in index.edges:
        source = element_owners.get(edge.source)
        target = element_owners.get(edge.target)
        if source is None or target is None or source == target:
            continue
        class_neighbors.setdefault(source, set()).add(target)
        class_neighbors.setdefault(target, set()).add(source)

    first = next(iter(smell_classes))
    visited = {first}
    queue = deque([first])
    while queue:
        current = queue.popleft()
        for neighbor in class_neighbors.get(current, set()):
            if neighbor in smell_classes and neighbor not in visited:
                visited.add(neighbor)
                queue.append(neighbor)
    return visited == smell_classes, smell_classes


def _class_graph(index: AstIndex) -> tuple[dict[str, str], dict[str, set[str]]]:
    owners = _element_class_map(index)
    neighbors: dict[str, set[str]] = {
        element.id: set()
        for element in index.elements
        if element.kind in {"class", "interface", "enum", "type"}
    }
    for edge in index.edges:
        source = owners.get(edge.source)
        target = owners.get(edge.target)
        if source is None or target is None or source == target:
            continue
        neighbors.setdefault(source, set()).add(target)
        neighbors.setdefault(target, set()).add(source)
    return owners, neighbors


def _smells_by_class(
    index: AstIndex,
    smells: list[Smell],
    repo_path: Path,
) -> dict[str, list[Smell]]:
    owners = _element_class_map(index)
    grouped: dict[str, list[Smell]] = {}
    for smell in smells:
        element = index.element_at(
            smell.file_path,
            smell.location.range.start.line + 1,
            repo_path,
        )
        if element is None:
            continue
        if owner := owners.get(element.id):
            grouped.setdefault(owner, []).append(smell)
    return grouped


def _composite_seed_classes(index: AstIndex, elements: list[str]) -> set[str]:
    class_ids = {
        element.id
        for element in index.elements
        if element.kind in {"class", "interface", "enum", "type"}
    }
    seeds: set[str] = set()
    for raw in elements:
        normalized = raw.removesuffix("()")
        matches = [
            class_id
            for class_id in class_ids
            if normalized == class_id
            or normalized.startswith(f"{class_id}.")
        ]
        if matches:
            seeds.add(max(matches, key=len))
    return seeds


def _select_runtime_class_scope(
    index: AstIndex,
    smells: list[Smell],
    composite_elements: list[str],
    repo_path: Path,
    *,
    min_smells: int,
    max_smells: int,
    target_type_counts: set[int],
    max_classes: int,
) -> tuple[list[Smell], set[str]] | None:
    """Find a connected class region around a composite with desired smells."""
    smells_by_class = _smells_by_class(index, smells, repo_path)
    _, neighbors = _class_graph(index)
    seeds = _composite_seed_classes(index, composite_elements)
    if not seeds:
        return None

    queue = deque(frozenset({seed}) for seed in sorted(seeds))
    seen = set(queue)
    while queue:
        classes = queue.popleft()
        selected_smells = [
            smell
            for class_id in classes
            for smell in smells_by_class.get(class_id, [])
        ]
        smell_count = len(selected_smells)
        type_count = len({smell.type for smell in selected_smells})
        if (
            min_smells <= smell_count <= max_smells
            and (not target_type_counts or type_count in target_type_counts)
        ):
            return selected_smells, set(classes)
        if len(classes) >= max_classes or smell_count > max_smells:
            continue
        candidates = {
            neighbor
            for class_id in classes
            for neighbor in neighbors.get(class_id, set())
            if neighbor not in classes
        }
        ordered = sorted(
            candidates,
            key=lambda class_id: (
                -len(smells_by_class.get(class_id, [])),
                class_id,
            ),
        )
        for candidate in ordered:
            expanded = classes | {candidate}
            if expanded not in seen:
                seen.add(expanded)
                queue.append(expanded)
        if len(seen) > 20_000:
            break
    return None


def _quota_available(
    quotas: dict[int, int],
    accepted_by_type_count: dict[int, int],
    type_count: int,
) -> bool:
    return type_count in quotas and accepted_by_type_count.get(type_count, 0) < quotas[type_count]


def _quotas_filled(
    quotas: dict[int, int],
    accepted_by_type_count: dict[int, int],
) -> bool:
    return all(
        accepted_by_type_count.get(type_count, 0) >= required
        for type_count, required in quotas.items()
    )


def _commits_by_developer(g: Neo4jGraph, project: str) -> dict[str, list[int]]:
    rows = g.run(
        """
        MATCH (c:Commit)-[:BELONGS_TO]->(:Project {name: $project})
        RETURN c.author_email AS developer, c.order AS commit_order
        ORDER BY developer, commit_order
        """,
        project=project,
    ).data()
    commits: dict[str, list[int]] = {}
    for row in rows:
        commits.setdefault(str(row["developer"] or ""), []).append(int(row["commit_order"]))
    return commits


def _worked_between(commits_by_developer: dict[str, list[int]], developer: str, start_order: int, end_order: int) -> bool:
    """Original range/scope script's temporal guard.

    `detect_scope_based.py` keeps refactorings in one batch only when the same
    developer has not worked in another commit between the current batch and
    the candidate refactoring.  The script implements this as developer-commit
    index distance > 1.
    """
    commits = commits_by_developer.get(developer or "", [])
    start_index = bisect_left(commits, int(start_order))
    end_index = bisect_left(commits, int(end_order))
    if start_index == len(commits) or commits[start_index] != int(start_order):
        return True
    if end_index == len(commits) or commits[end_index] != int(end_order):
        return True
    return abs(end_index - start_index) > 1


def _load_project_refactorings(g: Neo4jGraph, project: str) -> list[dict]:
    """Load refactorings in the same shape used by the range/scope script."""
    rows = g.run(
        """
        MATCH (r:Refactoring)-[:STARTED_AT]->(cs:Commit)-[:BELONGS_TO]->(p:Project {name: $project})
        MATCH (r)-[:ENDED_AT]->(ce:Commit)-[:BELONGS_TO]->(p)
        OPTIONAL MATCH (r)-[:CHANGED]->(ec:Element)
        WITH r, cs, ce, collect(DISTINCT ec.name) AS changed_names
        OPTIONAL MATCH (r)-[:PRODUCED]->(ep:Element)
        WITH r, cs, ce, changed_names, collect(DISTINCT ep.name) AS produced_names
        RETURN r.hash_id AS ref_id,
               r.type AS ref_type,
               r.classification AS classification,
               r.degradation_level AS degradation_level,
               cs.hash AS start_commit_hash,
               cs.hash_id AS start_dataset_commit_id,
               cs.order AS start_commit_order,
               ce.author_email AS developer,
               ce.hash AS end_commit_hash,
               ce.hash_id AS end_dataset_commit_id,
               ce.order AS end_commit_order,
               changed_names,
               produced_names
        ORDER BY end_commit_order, ref_id
        """,
        project=project,
    ).data()

    refs: list[dict] = []
    for row in rows:
        changed = [x for x in (row.get("changed_names") or []) if x]
        produced = [x for x in (row.get("produced_names") or []) if x]
        elements = sorted(set(changed + produced))
        if not elements:
            continue
        refs.append({
            "ref_id": str(row["ref_id"] or ""),
            "ref_type": str(row["ref_type"] or ""),
            "classification": str(row["classification"] or ""),
            "degradation_level": str(row["degradation_level"] or ""),
            "developer": str(row["developer"] or ""),
            "start_commit_hash": str(row["start_commit_hash"]),
            "start_dataset_commit_id": str(row["start_dataset_commit_id"]),
            "start_commit_order": int(row["start_commit_order"]),
            "end_commit_hash": str(row["end_commit_hash"]),
            "end_dataset_commit_id": str(row["end_dataset_commit_id"]),
            "end_commit_order": int(row["end_commit_order"]),
            # Original detect_scope_based.py batches by row["commit"], i.e. the
            # commit where RefactoringMiner reports the refactoring.  In this
            # graph that is ENDED_AT (the child/refactoring commit), while
            # STARTED_AT is its parent/before state.
            "composite_commit_order": int(row["end_commit_order"]),
            "changed_elements": changed,
            "produced_elements": produced,
            "elements": elements,
        })
    return refs


def _build_composites(refactorings: list[dict], commits_by_developer: dict[str, list[int]]) -> list[dict]:
    """Group refactorings into composite episodes (port of detect_scope_based.py)."""
    composites: list[dict] = []

    def add_refactoring(composite: dict, refactoring: dict) -> None:
        if any(r["ref_id"] == refactoring["ref_id"] for r in composite["refactorings"]):
            return
        composite["refactorings"].append(refactoring)
        composite["elements"].update(refactoring["elements"])
        composite["last_commit_order"] = max(
            composite["last_commit_order"], refactoring["composite_commit_order"]
        )

    def merge_into(target: dict, source: dict) -> None:
        target["last_commit_order"] = max(target["last_commit_order"], source["last_commit_order"])
        for refactoring in source["refactorings"]:
            add_refactoring(target, refactoring)

    for refactoring in refactorings:
        ref_elements = set(refactoring["elements"])
        matches = [
            composite
            for composite in composites
            if composite["developer"] == refactoring["developer"]
            and not _worked_between(
                commits_by_developer,
                composite["developer"],
                composite["last_commit_order"],
                refactoring["composite_commit_order"],
            )
            and bool(composite["elements"] & ref_elements)
        ]

        if len(matches) > 1:
            retained = matches[0]
            for composite in matches[1:]:
                merge_into(retained, composite)
                composites.remove(composite)
            add_refactoring(retained, refactoring)
        elif len(matches) == 1:
            add_refactoring(matches[0], refactoring)
        else:
            composites.append(
                {
                    "developer": refactoring["developer"],
                    "refactorings": [refactoring],
                    "elements": set(refactoring["elements"]),
                    "last_commit_order": refactoring["composite_commit_order"],
                }
            )

    return [composite for composite in composites if len(composite["refactorings"]) > 1]


def _composite_to_draft(composite: dict) -> dict:
    refs = composite["refactorings"]
    start_ref = min(refs, key=lambda r: (r["start_commit_order"], r["ref_id"]))
    end_ref = max(refs, key=lambda r: (r["end_commit_order"], r["ref_id"]))
    element_ref_counts = {element: 0 for element in composite["elements"]}
    for ref in refs:
        for element in ref["elements"]:
            element_ref_counts[element] = element_ref_counts.get(element, 0) + 1
    anchor = sorted(element_ref_counts, key=lambda e: (-element_ref_counts[e], e))[0]
    return {
        "anchor": anchor,
        "developer": composite["developer"],
        "refactorings": len(refs),
        "element_count": len(composite["elements"]),
        "all_elements": sorted(composite["elements"]),
        "ref_ids": [r["ref_id"] for r in refs],
        "ref_types": [r["ref_type"] for r in refs],
        "element_ref_counts": element_ref_counts,
        "start_commit": start_ref["start_commit_hash"],
        "start_commit_order": start_ref["start_commit_order"],
        "start_dataset_commit_id": start_ref["start_dataset_commit_id"],
        "end_commit": end_ref["end_commit_hash"],
        "end_commit_order": end_ref["end_commit_order"],
        "end_dataset_commit_id": end_ref["end_dataset_commit_id"],
    }


def _extreme_commit_snapshot(g: Neo4jGraph, project: str, element: str, latest: bool) -> dict:
    order_clause = "DESC" if latest else "ASC"
    row = g.run(
        f"""
        MATCH (p:Project {{name: $project}})
        MATCH (e:Element {{name: $element}})-[:COMMITTED_IN]->(c:Commit)-[:BELONGS_TO]->(p)
        OPTIONAL MATCH (e)-[:AFFECTED_BY]->(s:Smell)
        WITH c.hash AS commit_hash, c.hash_id AS dataset_commit_id, c.order AS commit_order, count(s) AS smell_count
        ORDER BY commit_order {order_clause}
        LIMIT 1
        RETURN commit_hash, dataset_commit_id, commit_order, smell_count
        """,
        project=project,
        element=element,
    ).data()
    if not row:
        raise ValueError(f"No commit snapshots found for element={element!r} in project={project!r}")
    return {
        "commit_hash": str(row[0]["commit_hash"]),
        "commit_order": int(row[0]["commit_order"]),
        "dataset_commit_id": str(row[0]["dataset_commit_id"]),
        "smell_count": int(row[0]["smell_count"]),
    }


def _refactoring_window_snapshot(g: Neo4jGraph, project: str, element: str, *, end: bool) -> dict:
    """Endpoint of the refactoring window for an anchor element.

    Sousa et al. classify a composite by comparing smells before the first
    refactoring and after the last refactoring in the composite scope.  For this
    selector's anchor-derived candidate pool, approximate that window as:

    - start: earliest STARTED_AT commit among refactorings touching the anchor;
    - end: latest ENDED_AT commit among refactorings touching the anchor.
    """
    rel = "ENDED_AT" if end else "STARTED_AT"
    order_clause = "DESC" if end else "ASC"
    row = g.run(
        f"""
        MATCH (p:Project {{name: $project}})
        MATCH (e:Element {{name: $element}})
        MATCH (r:Refactoring)-[:CHANGED|PRODUCED]->(e)
        MATCH (r)-[:{rel}]->(c:Commit)-[:BELONGS_TO]->(p)
        RETURN c.hash AS commit_hash,
               c.hash_id AS dataset_commit_id,
               c.order AS commit_order
        ORDER BY commit_order {order_clause}
        LIMIT 1
        """,
        project=project,
        element=element,
    ).data()
    if not row:
        raise ValueError(f"No refactoring window found for element={element!r} in project={project!r}")
    return {
        "commit_hash": str(row[0]["commit_hash"]),
        "commit_order": int(row[0]["commit_order"]),
        "dataset_commit_id": str(row[0]["dataset_commit_id"]),
    }


def _pick_start_elements(
    g: Neo4jGraph, project: str, anchor_element: str, scope_elements: list[str], k: int
) -> list[str]:
    assert scope_elements, "scope_elements must be non-empty"
    assert 1 <= k <= len(scope_elements)

    rows = g.run(
        """
        MATCH (p:Project {name: $project})
        UNWIND $scope_elements AS scope_name
        MATCH (e:Element {name: scope_name})
        OPTIONAL MATCH (ea:Element {name: $anchor_element})<-[:CHANGED|PRODUCED]-(r:Refactoring)-[:STARTED_AT]->(:Commit)-[:BELONGS_TO]->(p)
        WHERE (r)-[:CHANGED|PRODUCED]->(e)
        WITH e.name AS element,
             count(DISTINCT r) AS ref_coverage,
             CASE WHEN e.name = $anchor_element THEN 1 ELSE 0 END AS is_anchor
        ORDER BY is_anchor DESC, ref_coverage DESC, element ASC
        LIMIT $k
        RETURN element
        """,
        project=project,
        anchor_element=anchor_element,
        scope_elements=scope_elements,
        k=k,
    ).data()

    return [str(r["element"]) for r in rows]


def _smells_per_element(
    g: Neo4jGraph, project: str, elements: list[str], commit_hash: str
) -> dict[str, int]:
    if not elements:
        return {}
    rows = g.run(
        """
        MATCH (e:Element)-[:COMMITTED_IN]->(c:Commit {hash: $commit_hash})-[:BELONGS_TO]->(:Project {name: $project})
        WHERE e.name IN $elements
        OPTIONAL MATCH (e)-[:AFFECTED_BY]->(s:Smell)
        RETURN e.name AS element, count(s) AS smell_count
        """,
        project=project,
        commit_hash=commit_hash,
        elements=elements,
    ).data()
    out = {e: 0 for e in elements}
    for r in rows:
        out[str(r["element"])] = int(r["smell_count"])
    return out


def _smell_ids_for_elements(
    g: Neo4jGraph, project: str, elements: list[str], commit_hash: str
) -> set[str]:
    """Stable smell identity set for a scope at one commit.

    The dataset paper classifies composite effects by smell incidence before
    vs. after the composite scope.  Candidate selection therefore compares the
    smell *set* over the whole scope, not only the smell count of the anchor
    element.  `hash_id` is normally present; the fallback keeps tests/odd rows
    deterministic if a smell node lacks it.
    """
    if not elements:
        return set()

    rows = g.run(
        """
        MATCH (e:Element)-[:COMMITTED_IN]->(c:Commit {hash: $commit_hash})-[:BELONGS_TO]->(:Project {name: $project})
        WHERE e.name IN $elements
        OPTIONAL MATCH (e)-[:AFFECTED_BY]->(s:Smell)
        RETURN e.name AS element,
               e.path AS element_path,
               s.hash_id AS smell_hash,
               s.type AS smell_type,
               s.starting_line AS starting_line
        """,
        project=project,
        commit_hash=commit_hash,
        elements=elements,
    ).data()

    smell_ids: set[str] = set()
    for row in rows:
        smell_hash = row.get("smell_hash")
        smell_type = row.get("smell_type")
        if smell_hash:
            smell_ids.add(str(smell_hash))
        elif smell_type:
            smell_ids.add(
                f"{smell_type}:{row.get('element') or ''}:{row.get('element_path') or ''}:{row.get('starting_line') or 0}"
            )
    return smell_ids


def _smell_types_for_elements(
    g: Neo4jGraph, project: str, elements: list[str], commit_hash: str
) -> list[str]:
    if not elements:
        return []
    rows = g.run(
        """
        MATCH (e:Element)-[:COMMITTED_IN]->(c:Commit {hash: $commit_hash})-[:BELONGS_TO]->(:Project {name: $project})
        WHERE e.name IN $elements
        MATCH (e)-[:AFFECTED_BY]->(s:Smell)
        RETURN DISTINCT s.type AS smell_type
        ORDER BY smell_type
        """,
        project=project,
        commit_hash=commit_hash,
        elements=elements,
    ).data()
    return [str(r["smell_type"]) for r in rows if r.get("smell_type")]


def _graph_smell_summary(
    g: Neo4jGraph,
    project: str,
    elements: list[str],
    commit_hash: str,
) -> dict[str, object]:
    """Return distinct graph smells for candidate ranking only."""
    rows = g.run(
        """
        MATCH (e:Element)-[:COMMITTED_IN]->(c:Commit {hash: $commit_hash})
              -[:BELONGS_TO]->(:Project {name: $project})
        WHERE e.name IN $elements
        OPTIONAL MATCH (e)-[:AFFECTED_BY]->(s:Smell)
        RETURN count(DISTINCT s) AS smell_count,
               collect(DISTINCT s.type) AS smell_types
        """,
        project=project,
        commit_hash=commit_hash,
        elements=elements,
    ).data()
    if not rows:
        return {"smell_count": 0, "smell_types": []}
    return {
        "smell_count": int(rows[0].get("smell_count") or 0),
        "smell_types": sorted(
            str(item) for item in (rows[0].get("smell_types") or []) if item
        ),
    }


def _percentile(values: list[int], q: float) -> int:
    assert values, "values must be non-empty"
    assert 0.0 < q <= 1.0
    s = sorted(values)
    idx = max(0, min(len(s) - 1, math.ceil(q * len(s)) - 1))
    return int(s[idx])


def _load_repo_urls(path: str) -> dict[str, str]:
    csv_path = Path(path)
    rows = csv.DictReader(csv_path.open())
    mapping = {str(r["project"]): str(r["repo_url"]) for r in rows if r.get("project") and r.get("repo_url")}
    assert mapping, f"No repo URLs found in {csv_path}"
    return mapping


def _select_draft_cases(g: Neo4jGraph, project: str, args: argparse.Namespace) -> list[dict]:
    refactorings = _load_project_refactorings(g, project)
    composites = _build_composites(refactorings, _commits_by_developer(g, project))
    rows = [
        _composite_to_draft(composite)
        for composite in composites
        if args.min_ref_count <= len(composite["refactorings"]) <= args.max_ref_count
        and args.min_elements <= len(composite["elements"]) <= args.max_elements
    ]
    rows.sort(
        key=lambda r: (-r["refactorings"], -r["element_count"], r["anchor"], r["start_commit_order"])
    )

    drafts: list[dict] = []
    for row in rows:
        all_elements = sorted(row["all_elements"])
        smells_before = _smell_ids_for_elements(g, project, all_elements, row["start_commit"])
        smells_after = _smell_ids_for_elements(g, project, all_elements, row["end_commit"])
        if smells_before == smells_after:
            continue
        drafts.append(
            {
                **row,
                "smells_before": len(smells_before),
                "smells_after": len(smells_after),
                "smell_delta": abs(len(smells_after) - len(smells_before)),
            }
        )

    selected: list[dict] = []
    if not drafts:
        return selected

    ref_cutoff = _percentile([x["refactorings"] for x in drafts], args.outlier_percentile)
    element_cutoff = _percentile([x["element_count"] for x in drafts], args.outlier_percentile)
    delta_cutoff = _percentile([x["smell_delta"] for x in drafts], args.outlier_percentile)

    for draft in drafts:
        if (
            draft["refactorings"] > ref_cutoff
            or draft["element_count"] > element_cutoff
            or draft["smell_delta"] > delta_cutoff
        ):
            continue

        anchor = draft["anchor"]
        all_elements = sorted(draft["all_elements"])
        selected_elements = all_elements
        if args.elements_count is not None:
            if len(all_elements) < args.elements_count:
                continue
            if len(all_elements) > args.elements_count:
                element_ref_counts = draft.get("element_ref_counts") or {}
                selected_elements = sorted(
                    all_elements,
                    key=lambda e: (e != anchor, -int(element_ref_counts.get(e, 0)), e),
                )[: args.elements_count]

        if args.max_elements_per_case is not None:
            if len(selected_elements) > args.max_elements_per_case:
                continue

        selected_smells_before = _smell_ids_for_elements(
            g, project, selected_elements, draft["start_commit"]
        )
        selected_smells_after = _smell_ids_for_elements(
            g, project, selected_elements, draft["end_commit"]
        )
        if selected_smells_before == selected_smells_after:
            continue

        per_element_smells = _smells_per_element(g, project, selected_elements, draft["start_commit"])
        start_smell_count = sum(per_element_smells.values())
        if args.max_smells_per_case is not None and start_smell_count > args.max_smells_per_case:
            continue

        elements_with_smell = sum(1 for count in per_element_smells.values() if count > 0)
        if elements_with_smell < args.min_elements_with_smell:
            continue
        graph_summary = _graph_smell_summary(
            g,
            project,
            selected_elements,
            draft["start_commit"],
        )

        selected.append(
            {
                "case_id": make_case_id(project, selected_elements),
                "project": project,
                "elements": selected_elements,
                "start_commit": draft["start_commit"],
                "start_commit_order": draft["start_commit_order"],
                "end_commit": draft["end_commit"],
                "end_commit_order": draft["end_commit_order"],
                "start_state": {
                    "smells_total": start_smell_count,
                    "elements_with_smell": elements_with_smell,
                    "smell_types": _smell_types_for_elements(
                        g, project, selected_elements, draft["start_commit"]
                    ),
                    "graph_distinct_smells": graph_summary["smell_count"],
                    "graph_distinct_smell_types": graph_summary["smell_types"],
                },
                "selection": {
                    "anchor": anchor,
                    "developer": draft.get("developer", ""),
                    "refactorings": draft["refactorings"],
                    "ref_ids": draft.get("ref_ids", []),
                    "ref_types": sorted(set(draft.get("ref_types", []))),
                    "smells_after": len(selected_smells_after),
                    "smell_delta": abs(len(selected_smells_after) - len(selected_smells_before)),
                },
            }
        )
        if not args.runtime_type_quotas and len(selected) >= args.limit_per_project:
            break

    return selected


def _prepare_repo_checkout(
    *,
    repo_url: str,
    repos_root: Path,
    commit_hash: str,
    worktree_suffix: str = "",
) -> Path:
    leaf = commit_hash[:12]
    if worktree_suffix:
        safe_suffix = "".join(
            character if character.isalnum() or character in "-._" else "_"
            for character in worktree_suffix
        )
        leaf = f"{leaf}-{safe_suffix[:80]}"
    checkout = repos_root / repo_slug(repo_url) / leaf
    repo = Repo.for_experiment(repo_url, commit_hash, checkout_path=checkout)
    return repo.path


def _baseline_java_fields(repo_path: Path) -> dict[str, str]:
    version = detect_project_java_version(repo_path)
    if version is None:
        return {}
    return {
        "build_system": version.build_system,
        "java_source": version.source,
        "sdkman_java": version.sdkman_java,
    }


def _commit_window_supported(
    window: object,
    *,
    repo_url: str,
    start_commit: str,
    cache_root: str | Path,
) -> bool:
    start_build = classify_commit_build_system(
        repo_url,
        start_commit,
        cache_root=cache_root,
    )
    if start_build.primary == "maven":
        return bool(getattr(window, "all_maven", False))
    if start_build.primary == "gradle":
        return bool(getattr(window, "all_gradle", False))
    return False


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


def _verify_case(draft: dict[str, object], repo_url: str, args: argparse.Namespace) -> dict[str, object]:
    assert draft["project"], "draft.project must be set"
    assert draft["start_commit"], "draft.start_commit must be set"
    assert draft["case_id"], "draft.case_id must be set"
    with tempfile.TemporaryDirectory(prefix="manifest-verify-") as td:
        try:
            repo_path = _prepare_repo_checkout(
                repo_url=repo_url,
                repos_root=Path(td),
                commit_hash=str(draft["start_commit"]),
                worktree_suffix=str(draft["case_id"]),
            )
        except Exception as exc:
            return {"status": "checkout_fail", "details": str(exc)}

        result = run_java_test_analysis(
            str(repo_path),
            clean=True,
            timeout=args.timeout,
            code_agent_max_attempts=0,
        )
        status, details = _classify_baseline_failure(result)
        summary = result.get("summary")
        repair = dict(result.get("code_agent_repair") or {})
        return {
            "status": status,
            "details": details,
            "build_system": result.get("build_system"),
            "command": result.get("command"),
            "command_source": result.get("command_source"),
            **_baseline_java_fields(repo_path),
            "llm_repair": repair.get("applied"),
            "pre_repair_exit_code": result.get("pre_code_agent_exit_code"),
            "exit_code": None if not isinstance(summary, TestRunSummary) else int(summary.exit_code),
            "tests_total": None if not isinstance(summary, TestRunSummary) else int(summary.counts.total),
            "tests_failed": None if not isinstance(summary, TestRunSummary) else int(summary.counts.failed),
            "tests_errors": None if not isinstance(summary, TestRunSummary) else int(summary.counts.errors),
            "tests_skipped": None if not isinstance(summary, TestRunSummary) else int(summary.counts.skipped),
            "patches_applied": False,
        }


def _evaluate_runtime_case(
    draft: dict[str, object],
    repo_url: str,
    args: argparse.Namespace,
    available_type_counts: set[int],
) -> dict[str, object]:
    """Checkout once and validate baseline, ORGANIC findings, and class scope."""
    try:
        cached_repo = Repo.for_experiment(
            repo_url,
            str(draft["start_commit"]),
            checkout_path=(
                Path(args.repo_cache_root)
                / "runtime-organic"
                / repo_slug(repo_url)
            ),
        )
        cached_repo.git_repo.git.clean("-fdx")
        repo_path = cached_repo.path
    except Exception as exc:
        return {"status": "checkout_fail", "details": str(exc)}

    try:
        detected = OrganicDetector(timeout=float(args.timeout)).detect(repo_path)
    except (OSError, RuntimeError, ValueError) as exc:
        return {"status": "organic_fail", "details": str(exc)}

    try:
        ast_index = GradleRunner(
            Repo(repo_path),
            timeout=float(args.timeout),
        ).inspect()
    except (OSError, RuntimeError, ValueError) as exc:
        return {"status": "ast_fail", "details": str(exc)}

    selected = _select_runtime_class_scope(
        ast_index,
        detected,
        list(draft["elements"]),
        repo_path,
        min_smells=int(args.runtime_min_smells),
        max_smells=int(args.runtime_max_smells),
        target_type_counts=available_type_counts,
        max_classes=args.max_runtime_classes,
    )
    if selected is None:
        return {
            "status": "runtime_scope_not_found",
            "details": "no connected class scope satisfies runtime quotas",
        }
    smells, classes = selected
    smell_types = sorted({smell.type for smell in smells})

    if not args.skip_baseline_verification:
        result = run_java_test_analysis(
            str(repo_path),
            clean=True,
            timeout=args.timeout,
            code_agent_max_attempts=0,
        )
        status, details = _classify_baseline_failure(result)
        if status != "passed":
            result = run_java_test_analysis(
                str(repo_path),
                clean=False,
                timeout=args.timeout,
                code_agent_max_attempts=0,
            )
            status, details = _classify_baseline_failure(result)
        if status != "passed":
            return {"status": status, "details": details}
        summary = result.get("summary")
        baseline = {
            "status": status,
            "details": details,
            "build_system": result.get("build_system"),
            "command": result.get("command"),
            "command_source": result.get("command_source"),
            **_baseline_java_fields(repo_path),
            "exit_code": (
                int(summary.exit_code)
                if isinstance(summary, TestRunSummary)
                else None
            ),
            "tests_total": (
                int(summary.counts.total)
                if isinstance(summary, TestRunSummary)
                else None
            ),
            "tests_failed": (
                int(summary.counts.failed)
                if isinstance(summary, TestRunSummary)
                else None
            ),
            "tests_errors": (
                int(summary.counts.errors)
                if isinstance(summary, TestRunSummary)
                else None
            ),
            "patches_applied": False,
        }
    else:
        baseline = {
            "status": "passed",
            "details": "baseline verification skipped",
        }

    return {
        "status": "passed",
        "details": "baseline and runtime selection passed",
        "baseline": baseline,
        "runtime": {
            "smells_total": len(smells),
            "smell_types": smell_types,
            "smell_type_count": len(smell_types),
            "class_count": len(classes),
            "classes": sorted(classes),
            "classes_connected": True,
            "elements": sorted(classes),
        },
    }


def _append_graph_fallback_cases(
    cases: list[dict],
    accepted_report: list[dict],
    rejected: list[dict],
    *,
    graph: Neo4jGraph,
    repo_urls: dict[str, str],
    args: argparse.Namespace,
) -> None:
    """Append Neo4j graph drafts that did not satisfy runtime smell quotas."""
    fallback_projects = list(getattr(args, "graph_fallback_projects", []) or [])
    if not fallback_projects:
        return

    accepted_case_ids = {str(case["case_id"]) for case in cases}
    accepted_projects = {str(case["project"]) for case in cases}

    for project in fallback_projects:
        if project in accepted_projects:
            continue
        repo_url = repo_urls.get(project)
        if not repo_url:
            rejected.append(
                {
                    "case_id": None,
                    "project": project,
                    "start_commit": None,
                    "reason": "missing_repo_url",
                    "details": f"No repo_url for graph fallback project {project!r}",
                }
            )
            continue

        draft_cases = _select_draft_cases(graph, project, args)
        if not draft_cases:
            rejected.append(
                {
                    "case_id": None,
                    "project": project,
                    "start_commit": None,
                    "reason": "graph_fallback_not_found",
                    "details": "no Neo4j composite draft matched selection filters",
                }
            )
            continue

        for draft in draft_cases:
            if draft["case_id"] in accepted_case_ids:
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
                rejected.append(
                    {
                        "case_id": draft["case_id"],
                        "project": project,
                        "start_commit": draft["start_commit"],
                        "reason": "unsupported_commit_build_system_window",
                        "details": "graph fallback commit window unsupported",
                        "commit_build_system_window": commit_build_window.to_dict(),
                    }
                )
                continue

            if args.skip_baseline_verification:
                verification = {
                    "status": "passed",
                    "details": "baseline verification skipped",
                }
            else:
                verification = _verify_case(draft, repo_url, args)

            case = {
                **draft,
                "repo_url": repo_url,
                "commit_build_system_window": commit_build_window.to_dict(),
                "baseline_verification": verification,
            }
            cases.append(case)
            accepted_case_ids.add(str(draft["case_id"]))
            accepted_projects.add(project)
            accepted_report.append(
                {
                    "case_id": draft["case_id"],
                    "project": project,
                    "start_commit": draft["start_commit"],
                    "reason": (
                        "graph_fallback"
                        if verification["status"] == "passed"
                        else f"graph_fallback_{verification['status']}"
                    ),
                    "runtime": None,
                }
            )
            break


def _partition_requested_projects(
    projects: list[str],
    blocked_projects: dict[str, str],
) -> tuple[list[str], dict[str, str]]:
    active = [project for project in projects if project not in blocked_projects]
    blocked = {project: blocked_projects[project] for project in projects if project in blocked_projects}
    return active, blocked


def main() -> int:
    args = _parse_args()
    t_run_start = time.perf_counter()
    type_quotas = _parse_type_quotas(args.runtime_type_quotas)
    accepted_by_type_count: dict[int, int] = {}

    assert args.limit_per_project >= 1
    assert 1 <= args.min_ref_count <= args.max_ref_count
    assert 1 <= args.min_elements <= args.max_elements
    assert 0.0 < args.outlier_percentile <= 1.0
    assert args.timeout > 0, "--timeout must be positive"
    if args.elements_count is not None:
        assert args.elements_count >= 1
    if args.max_elements_per_case is not None:
        assert args.max_elements_per_case >= args.min_elements
    if args.max_smells_per_case is not None:
        assert args.max_smells_per_case >= 0
    if args.runtime_min_smells is not None:
        assert args.runtime_min_smells >= 0
    if args.runtime_max_smells is not None:
        assert args.runtime_max_smells >= 0
    if args.runtime_min_smells is not None and args.runtime_max_smells is not None:
        assert args.runtime_min_smells <= args.runtime_max_smells
    assert args.max_runtime_classes >= 1
    assert args.min_elements_with_smell >= 0

    requested_projects = [project.strip() for project in args.projects.split(",") if project.strip()]
    assert requested_projects, "--projects must contain at least one project"
    invalid = [project for project in requested_projects if project not in ALLOWED_PROJECTS]
    assert not invalid, (
        f"Projects not in allowed Maven set: {invalid}. Allowed: {sorted(ALLOWED_PROJECTS)}"
    )
    projects, blocked_projects = _partition_requested_projects(
        requested_projects,
        dict(getattr(args, "blocked_projects", BLOCKED_PROJECTS)),
    )

    print(f"[DBG] selection start: projects={projects}, blocked={list(blocked_projects)}")
    print(
        "[DBG] filters="
        f"min_ref={args.min_ref_count}, max_ref={args.max_ref_count}, "
        f"elements=[{args.min_elements},{args.max_elements}], "
        f"max_elements_per_case={args.max_elements_per_case}, "
        f"max_smells_per_case={args.max_smells_per_case}, "
        f"elements_count={args.elements_count}"
    )

    graph = Neo4jGraph(args.uri, auth=(args.user, args.password))
    repo_urls = _load_repo_urls(args.ready_repos_csv)
    assert repo_urls, "Repo URL map from csv must be non-empty"

    cases: list[dict] = []
    accepted_report: list[dict] = []
    rejected: list[dict] = []
    draft_count = 0

    for project, reason in blocked_projects.items():
        rejected.append(
            {
                "case_id": None,
                "project": project,
                "start_commit": None,
                "reason": "project_excluded",
                "details": reason,
            }
        )

    for project in projects:
        project_start = time.perf_counter()
        draft_cases = _select_draft_cases(graph, project, args)
        draft_count += len(draft_cases)
        print(f"[DBG] project={project}: {len(draft_cases)} draft cases after selection")

        repo_url = repo_urls.get(project)
        assert repo_url, f"Missing repo_url for project={project!r} in {args.ready_repos_csv}"

        for draft in draft_cases:
            if type_quotas and _quotas_filled(type_quotas, accepted_by_type_count):
                break
            print(
                f"[DBG] verifying case={draft['case_id']} "
                f"elems={len(draft['elements'])} smells={draft['start_state']['smells_total']}"
            )
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
                start_build = classify_commit_build_system(
                    repo_url,
                    str(draft["start_commit"]),
                    cache_root=args.repo_cache_root,
                )
                rejected.append(
                    {
                        "case_id": draft["case_id"],
                        "project": project,
                        "start_commit": draft["start_commit"],
                        "reason": "unsupported_commit_build_system_window",
                        "details": (
                            "commit window is not fully "
                            f"{start_build.primary}; "
                            f"all_maven={commit_build_window.all_maven} "
                            f"all_gradle={commit_build_window.all_gradle}"
                        ),
                        "commit_build_system_window": commit_build_window.to_dict(),
                    }
                )
                continue

            if type_quotas:
                available_type_counts = {
                    type_count
                    for type_count, required in type_quotas.items()
                    if accepted_by_type_count.get(type_count, 0) < required
                }
                verification = _evaluate_runtime_case(
                    draft,
                    repo_url,
                    args,
                    available_type_counts,
                )
            elif args.skip_baseline_verification:
                verification = {
                    "status": "passed",
                    "details": "baseline verification skipped",
                    "build_system": "maven",
                    "command": None,
                    "command_source": None,
                    "llm_repair": False,
                    "pre_repair_exit_code": None,
                    "exit_code": None,
                    "tests_total": None,
                    "tests_failed": None,
                    "tests_errors": None,
                    "tests_skipped": None,
                    "patches_applied": False,
                }
            else:
                verification = _verify_case(draft, repo_url, args)

            runtime = dict(verification.get("runtime") or {})
            runtime_type_count = int(runtime.get("smell_type_count") or 0)
            runtime_classes = int(runtime.get("class_count") or 0)
            runtime_connected = bool(runtime.get("classes_connected"))
            runtime_eligible = (
                not type_quotas
                or (
                    _quota_available(
                        type_quotas,
                        accepted_by_type_count,
                        runtime_type_count,
                    )
                    and runtime_classes <= args.max_runtime_classes
                    and runtime_connected
                )
            )
            if verification["status"] == "passed" and runtime_eligible:
                if type_quotas:
                    accepted_by_type_count[runtime_type_count] = (
                        accepted_by_type_count.get(runtime_type_count, 0) + 1
                    )
                    runtime_state = {
                        "smells_total": runtime["smells_total"],
                        "smell_types": runtime["smell_types"],
                        "smell_type_count": runtime_type_count,
                        "class_count": runtime_classes,
                        "classes": runtime["classes"],
                        "classes_connected": runtime_connected,
                    }
                    draft = {
                        **draft,
                        "elements": runtime["elements"],
                        "case_id": make_case_id(
                            project,
                            list(runtime["elements"]),
                        ),
                        "start_state": runtime_state,
                    }
                case = {
                    **draft,
                    "repo_url": repo_url,
                    "commit_build_system_window": commit_build_window.to_dict(),
                    "baseline_verification": verification.get(
                        "baseline",
                        verification,
                    ),
                }
                cases.append(case)
                accepted_report.append(
                    {
                        "case_id": draft["case_id"],
                        "project": project,
                        "start_commit": draft["start_commit"],
                        "reason": "baseline_verified",
                        "runtime": runtime or None,
                    }
                )
            else:
                if verification["status"] == "passed":
                    reason = (
                        "runtime_class_count"
                        if runtime_classes > args.max_runtime_classes
                        else "runtime_classes_disconnected"
                        if not runtime_connected
                        else "runtime_quota_filled"
                    )
                else:
                    reason = verification["status"]
                rejected.append(
                    {
                        "case_id": draft["case_id"],
                        "project": project,
                        "start_commit": draft["start_commit"],
                        "reason": reason,
                        "details": verification.get("details", ""),
                        "commit_build_system_window": commit_build_window.to_dict(),
                    }
                )
        print(f"[DBG] project={project} done in {time.perf_counter() - project_start:.2f}s")
        if type_quotas and _quotas_filled(type_quotas, accepted_by_type_count):
            break

    _append_graph_fallback_cases(
        cases,
        accepted_report,
        rejected,
        graph=graph,
        repo_urls=repo_urls,
        args=args,
    )

    if getattr(args, "enrich_java_after_generation", False):
        from dataset.generation.enrich_manifest_java import enrich_case

        java_cache = Path(args.repo_cache_root) / "manifest-java"
        cases = [enrich_case(dict(case), cache_root=java_cache) for case in cases]

    generated_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    filters = {
        "config_name": getattr(args, "config_name", ""),
        "requested_projects": requested_projects,
        "projects": projects,
        "blocked_projects": blocked_projects,
        "graph_fallback_projects": list(getattr(args, "graph_fallback_projects", []) or []),
        "size_min_mib": getattr(args, "size_min_mib", None),
        "size_max_mib": getattr(args, "size_max_mib", None),
        "limit_per_project": args.limit_per_project,
        "max_elements_per_case": args.max_elements_per_case,
        "max_smells_per_case": args.max_smells_per_case,
        "elements_count": args.elements_count,
        "runtime_min_smells": args.runtime_min_smells,
        "runtime_max_smells": args.runtime_max_smells,
        "runtime_type_quotas": type_quotas,
        "max_runtime_classes": args.max_runtime_classes,
        "requires_baseline_verification": not args.skip_baseline_verification,
        "fail_if_quotas_unfilled": getattr(args, "fail_if_quotas_unfilled", True),
        "enrich_java_after_generation": getattr(args, "enrich_java_after_generation", False),
    }

    report = {
        "generated_at": generated_at,
        "filters": filters,
        "summary": {
            "draft_cases": draft_count,
            "accepted": len(cases),
            "rejected": len(rejected),
            "accepted_by_runtime_type_count": accepted_by_type_count,
            "quotas_filled": (
                _quotas_filled(type_quotas, accepted_by_type_count)
                if type_quotas
                else None
            ),
        },
        "accepted_cases": accepted_report,
        "rejected_cases": rejected,
    }

    output_path = Path(args.output)
    report_path = Path(args.report) if args.report else output_path.with_suffix(".report.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    case_count = write_manifest(output_path, cases)
    if (
        type_quotas
        and not _quotas_filled(type_quotas, accepted_by_type_count)
        and getattr(args, "fail_if_quotas_unfilled", True)
    ):
        missing = {
            type_count: required - accepted_by_type_count.get(type_count, 0)
            for type_count, required in type_quotas.items()
            if accepted_by_type_count.get(type_count, 0) < required
        }
        raise RuntimeError(f"Could not fill runtime smell type quotas: {missing}")

    elapsed = time.perf_counter() - t_run_start
    print(f"[DBG] generation complete in {elapsed:.2f}s")
    print(f"accepted {len(cases)} cases from {draft_count} draft cases")
    print(f"manifest: {output_path} ({case_count} lines)")
    print(f"report: {report_path}")
    return 0


if __name__ == "__main__":
    if __package__ is None:
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    raise SystemExit(main())
