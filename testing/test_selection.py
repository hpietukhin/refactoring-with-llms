"""Derive targeted Maven -Dtest selectors from changed Java files."""

from __future__ import annotations

from itertools import product
from pathlib import Path


def resolve_targeted_tests(project_path: Path, changed_files: list[str]) -> list[str]:
    """Map changed Java files to their corresponding test classes.

    Returns fully-qualified class names suitable for ``mvn -Dtest=A,B test``.
    Falls back to an empty list when no matching tests are found.
    """
    if not changed_files:
        return []

    project_root = project_path.resolve()
    candidates: list[str] = []

    for target_file in changed_files:
        file_path = Path(target_file)
        if not file_path.is_absolute():
            file_path = (project_root / file_path).resolve()

        if file_path.suffix != ".java":
            continue

        direct = _to_test_class(file_path, project_root)
        if direct:
            _add_unique(candidates, direct)
            continue

        for test_class in _find_tests_for_source(file_path.stem, project_root):
            _add_unique(candidates, test_class)

    return candidates


def _add_unique(candidates: list[str], value: str) -> None:
    if value not in candidates:
        candidates.append(value)


def _to_test_class(file_path: Path, project_root: Path) -> str | None:
    """Return a dotted class name if the file itself is a test."""
    try:
        rel = file_path.relative_to(project_root)
    except ValueError:
        rel = file_path

    rel_posix = rel.as_posix()
    if not rel_posix.endswith(".java"):
        return None

    stem = rel.stem
    if not stem.endswith("Test") and not stem.endswith("Tests"):
        return None

    marker = "src/test/java/"
    lower = rel_posix.lower()
    if marker in lower:
        class_part = rel_posix[lower.index(marker) + len(marker) : -5]
        return class_part.replace("/", ".")

    marker2 = "test/"
    if marker2 in lower:
        idx = lower.index(marker2)
        class_part = rel_posix[idx + len(marker2) : -5]
        return class_part.replace("/", ".")

    return stem


def _find_tests_for_source(class_stem: str, project_root: Path) -> list[str]:
    """Find test classes matching a production class by name convention."""
    globs = (
        f"**/{class_stem}Test.java",
        f"**/{class_stem}Tests.java",
    )
    test_roots = [
        root_name
        for root_name in ("src/test/java", "test/java", "src/test", "test")
        if (project_root / root_name).exists()
    ]
    results: list[str] = []
    for root_name, pattern in product(test_roots, globs):
        for test_file in (project_root / root_name).glob(pattern):
            test_class = _to_test_class(test_file, project_root)
            if test_class and test_class not in results:
                results.append(test_class)
    return results
