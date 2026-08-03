"""Repository object shared by build-system adapters and experiments."""

from __future__ import annotations

from pathlib import Path

from git import BadName, Repo as GitRepo

from config import ROOT, resolve_path, settings

type BuildSystem = "maven"


def repo_slug(repo_url: str) -> str:
    """Stable directory name derived from a clone URL."""
    trimmed = repo_url.rstrip("/").removesuffix(".git")
    owner, name = trimmed.rsplit("/", maxsplit=1)
    return f"{owner.rsplit('/', maxsplit=1)[-1]}_{name}"


def worktrees_root(cfg: dict[str, object] | None = None) -> Path:
    """Return the configured experiment worktrees directory."""
    git_cfg = (cfg or settings)["git"]
    return resolve_path(str(git_cfg.get("worktrees_dir", "experiments/worktrees")))


def worktree_path(repo_url: str, cfg: dict[str, object] | None = None) -> Path:
    """Return the on-disk checkout path for ``repo_url``."""
    return worktrees_root(cfg) / repo_slug(repo_url)


class Repo:
    """Own a GitPython repository and expose its checkout as a stable object.

    Consumers should pass this object around instead of separately passing a
    path and a GitPython repository. Git operations remain available through
    ``git_repo`` for repository-level code; build adapters use ``path``.
    """

    def __init__(
        self,
        repo_path: str | Path | None = None,
        *,
        git_repo: GitRepo | None = None,
        build_system: BuildSystem = "maven",
    ) -> None:
        configured_path = settings["git"]["repo_path"]
        self.path = Path(repo_path or configured_path).expanduser().resolve()
        self.git_repo = git_repo or GitRepo(self.path)
        self.build_system = build_system

    @classmethod
    def open(cls, repo_path: str | Path | None = None) -> "Repo":
        """Open an existing checkout and wrap its GitPython repository."""
        return cls(repo_path)

    @classmethod
    def for_experiment(
        cls,
        repo_url: str,
        commit_hash: str,
        *,
        checkout_path: Path | None = None,
    ) -> "Repo":
        """Clone ``repo_url`` when needed and move the checkout to ``commit_hash``."""
        path = checkout_path or worktree_path(repo_url)
        if not (path / ".git").exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            GitRepo.clone_from(repo_url, path)
        repo = cls(path)
        repo.init_experiment(commit_hash)
        return repo

    @property
    def repo_path(self) -> Path:
        """Compatibility alias for callers that use the configuration name."""
        return self.path

    def changed_files(self, *, include_untracked: bool = False) -> tuple[Path, ...]:
        """Return changed paths relative to the checkout root."""
        paths = {Path(item.a_path) for item in self.git_repo.index.diff(None)}
        paths.update(Path(item.a_path) for item in self.git_repo.index.diff("HEAD"))
        if include_untracked:
            paths.update(Path(path) for path in self.git_repo.untracked_files)
        return tuple(sorted(paths))

    def filter_java_edits(self, *, include_untracked: bool = False) -> tuple[Path, ...]:
        """Return changed Java paths, excluding generated ``target`` files."""
        return tuple(
            path
            for path in self.changed_files(include_untracked=include_untracked)
            if path.suffix == ".java" and "target" not in path.parts
        )

    def init_experiment(self, commit_hash: str) -> None:
        """Move this checkout to the requested commit for an experiment."""
        try:
            self.git_repo.commit(commit_hash)
        except BadName:
            self.git_repo.remotes.origin.fetch()
        self.git_repo.git.checkout("--force", commit_hash)
