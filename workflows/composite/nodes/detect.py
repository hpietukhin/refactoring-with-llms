"""Smell detection node for the composite workflow."""

from __future__ import annotations

from pathlib import Path

from eliot import log_message

from detection.organic import OrganicDetector
from smell.smell import Smell, uri_to_path
from workflows.composite.models import CompositeWorkflowState


def _element_matches(smell: Smell, elements: set[str]) -> bool:
    if not elements:
        return True
    file_path = smell.file_path
    stem = Path(file_path).stem
    dotted = file_path.replace("/", ".").removesuffix(".java")
    for elem in elements:
        if elem == stem or elem == dotted or dotted.endswith("." + elem) or elem.endswith("." + stem):
            return True
        if elem in file_path or elem in dotted:
            return True
    return False


def filter_smells_to_elements(smells: list[Smell], elements: list[str]) -> list[Smell]:
    """Keep smells whose path or class-like name intersects ``elements``."""
    wanted = {item.strip() for item in elements if item.strip()}
    if not wanted:
        return smells
    return [smell for smell in smells if _element_matches(smell, wanted)]


def detect_smells(state: CompositeWorkflowState) -> CompositeWorkflowState:
    """Run Organic on the checked-out repository."""
    repo_path = state.get("repo_path")
    if not repo_path:
        raise ValueError("detect_smells requires state['repo_path']")
    detector = OrganicDetector()
    smells = detector.detect(
        Path(repo_path),
        commit_hash=state.get("current_commit"),
    )
    filtered = filter_smells_to_elements(smells, list(state.get("elements") or []))
    log_message(
        message_type="detect:smells",
        total=len(smells),
        filtered=len(filtered),
    )
    return {"smells": filtered}


def resolve_smell_file(repo_path: Path, smell: Smell) -> Path | None:
    """Resolve a smell location to a real file under ``repo_path``."""
    raw = uri_to_path(smell.location.uri)
    candidate = Path(raw)
    if candidate.is_file():
        return candidate
    relative = candidate.as_posix().lstrip("/")
    under_repo = repo_path / relative
    if under_repo.is_file():
        return under_repo
    matches = list(repo_path.rglob(candidate.name))
    for match in matches:
        if "target" in match.parts:
            continue
        if match.is_file():
            return match
    return None
