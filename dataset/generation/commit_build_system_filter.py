from __future__ import annotations

import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from git import Repo as GitRepo

from dataset.generation.graph import Neo4jGraph
from repository.repo import repo_slug


@dataclass(frozen=True)
class CommitBuildSystemInfo:
    repo_url: str
    commit_hash: str
    has_maven: bool
    has_gradle: bool
    has_ant: bool
    primary: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class CommitWindowBuildSystemInfo:
    repo_url: str
    project: str
    start_commit_order: int
    end_commit_order: int
    commit_count: int
    all_maven: bool
    all_gradle: bool
    first_non_maven_order: int | None = None
    first_non_maven_hash: str | None = None
    first_non_maven_primary: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def classify_commit_tree(repo_url: str, commit_hash: str, paths: list[str]) -> CommitBuildSystemInfo:
    path_set = {path.strip() for path in paths if path.strip()}
    has_maven = any(path.endswith("pom.xml") for path in path_set)
    has_gradle = any(
        path.endswith("build.gradle") or path.endswith("build.gradle.kts") for path in path_set
    )
    has_ant = any(path.endswith("build.xml") for path in path_set)

    if has_maven:
        primary = "maven"
    elif has_gradle:
        primary = "gradle"
    elif has_ant:
        primary = "ant"
    else:
        primary = "unknown"

    return CommitBuildSystemInfo(
        repo_url=repo_url,
        commit_hash=commit_hash,
        has_maven=has_maven,
        has_gradle=has_gradle,
        has_ant=has_ant,
        primary=primary,
    )


def _ensure_repo(repo_url: str, cache_root: str | Path) -> Path:
    cache_dir = Path(cache_root) / repo_slug(repo_url)
    if not (cache_dir / ".git").exists():
        cache_dir.parent.mkdir(parents=True, exist_ok=True)
        GitRepo.clone_from(repo_url, cache_dir)
    else:
        GitRepo(cache_dir).remotes.origin.fetch()
    return cache_dir


def classify_commit_build_system(
    repo_url: str,
    commit_hash: str,
    *,
    cache_root: str | Path = "temp/eval_repos",
) -> CommitBuildSystemInfo:
    repo_path = _ensure_repo(repo_url, cache_root)
    result = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", commit_hash],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to inspect commit tree for {commit_hash}: {result.stderr.strip()}"
        )
    return classify_commit_tree(repo_url, commit_hash, result.stdout.splitlines())


def _commit_hashes_for_order_window(
    graph: Neo4jGraph,
    project: str,
    start_commit_order: int,
    end_commit_order: int,
) -> list[tuple[int, str]]:
    rows = graph.run(
        """
        MATCH (c:Commit)-[:BELONGS_TO]->(:Project {name: $project})
        WHERE c.order >= $start_commit_order AND c.order <= $end_commit_order
        RETURN c.order AS commit_order, c.hash AS commit_hash
        ORDER BY commit_order ASC
        """,
        project=project,
        start_commit_order=int(start_commit_order),
        end_commit_order=int(end_commit_order),
    ).data()
    return [(int(row["commit_order"]), str(row["commit_hash"])) for row in rows]


def summarize_commit_window_build_system(
    *,
    repo_url: str,
    project: str,
    start_commit_order: int,
    end_commit_order: int,
    commits: list[tuple[int, CommitBuildSystemInfo]],
) -> CommitWindowBuildSystemInfo:
    first_non_maven: tuple[int, CommitBuildSystemInfo] | None = next(
        ((order, info) for order, info in commits if info.primary != "maven"),
        None,
    )
    first_non_gradle: tuple[int, CommitBuildSystemInfo] | None = next(
        ((order, info) for order, info in commits if info.primary != "gradle"),
        None,
    )
    return CommitWindowBuildSystemInfo(
        repo_url=repo_url,
        project=project,
        start_commit_order=int(start_commit_order),
        end_commit_order=int(end_commit_order),
        commit_count=len(commits),
        all_maven=first_non_maven is None,
        all_gradle=first_non_gradle is None,
        first_non_maven_order=None if first_non_maven is None else int(first_non_maven[0]),
        first_non_maven_hash=None if first_non_maven is None else first_non_maven[1].commit_hash,
        first_non_maven_primary=None if first_non_maven is None else first_non_maven[1].primary,
    )


def classify_commit_window_build_system(
    graph: Neo4jGraph,
    project: str,
    repo_url: str,
    start_commit_order: int,
    end_commit_order: int,
    *,
    cache_root: str | Path = "temp/eval_repos",
) -> CommitWindowBuildSystemInfo:
    commit_rows = _commit_hashes_for_order_window(
        graph, project, start_commit_order, end_commit_order
    )
    if not commit_rows:
        raise RuntimeError(
            "No commit rows found for "
            f"project={project!r} order window {start_commit_order}..{end_commit_order}"
        )
    commits = [
        (order, classify_commit_build_system(repo_url, commit_hash, cache_root=cache_root))
        for order, commit_hash in commit_rows
    ]
    return summarize_commit_window_build_system(
        repo_url=repo_url,
        project=project,
        start_commit_order=start_commit_order,
        end_commit_order=end_commit_order,
        commits=commits,
    )
