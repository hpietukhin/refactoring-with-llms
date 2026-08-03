"""Context manager that prefers an SDKMAN Java from ``.sdkmanrc``."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from config import ROOT, settings

_LEGACY_COMPILER_SOURCES = frozenset({"1.5", "1.6", "1.7", "1.8", "5", "6", "7", "8"})


def _java_version(*search_paths: Path) -> str | None:
    for path in search_paths:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("java="):
                return line.removeprefix("java=").strip()
    return None


def _apply_sdkman_java(env: dict[str, str], version: str) -> dict[str, str]:
    java_home = Path.home() / ".sdkman" / "candidates" / "java" / version
    if not java_home.is_dir():
        return env
    updated = dict(env)
    updated["JAVA_HOME"] = str(java_home)
    updated["PATH"] = f"{java_home / 'bin'}{os.pathsep}{updated.get('PATH', '')}"
    return updated


def maven_compiler_source(project: Path) -> str | None:
    """Read ``<source>`` from the project root ``pom.xml`` when present."""
    pom = project / "pom.xml"
    if not pom.is_file():
        return None
    text = pom.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"<source>([^<]+)</source>", text)
    return match.group(1).strip() if match else None


def legacy_sdkman_java(cfg: dict[str, object] | None = None) -> str:
    """Return the configured SDKMAN Java version for legacy Maven projects."""
    java_cfg = (cfg or settings)["java"]
    return str(java_cfg.get("legacy_sdkman_java", "8.0.442-amzn"))


@contextmanager
def java_env(search_paths: Sequence[Path] | None = None) -> Iterator[dict[str, str]]:
    """Yield a subprocess env that prefers the Java pinned in ``.sdkmanrc``."""
    env = dict(os.environ)
    paths = tuple(search_paths) if search_paths is not None else (ROOT / ".sdkmanrc",)
    version = _java_version(*paths)
    if version is None:
        yield env
        return

    yield _apply_sdkman_java(env, version)


@contextmanager
def java_env_for_maven(project: Path) -> Iterator[dict[str, str]]:
    """Pick a JDK that can compile the Maven project under ``project``."""
    source = maven_compiler_source(project)
    if source in _LEGACY_COMPILER_SOURCES:
        yield _apply_sdkman_java(dict(os.environ), legacy_sdkman_java())
        return
    with java_env((project / ".sdkmanrc", ROOT / ".sdkmanrc")) as env:
        yield env
