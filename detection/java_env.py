"""Context manager that prefers an SDKMAN Java from ``.sdkmanrc``."""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from xml.etree import ElementTree

from config import ROOT, settings

_MAVEN_PROPERTY = re.compile(r"\$\{([^}]+)\}")


def _background_macos_java(env: dict[str, str]) -> dict[str, str]:
    """Mark JVM subprocesses as background utilities on macOS."""
    if sys.platform != "darwin":
        return env
    option = "-Dapple.awt.UIElement=true"
    options = env.get("JAVA_TOOL_OPTIONS", "").split()
    if option in options:
        return env
    updated = dict(env)
    updated["JAVA_TOOL_OPTIONS"] = " ".join((*options, option))
    return updated


def _java_version(*search_paths: Path) -> str | None:
    for path in search_paths:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("java="):
                return line.removeprefix("java=").strip()
    return None


def sdkman_java_home(version: str) -> Path:
    """Return the SDKMAN install path for ``version``."""
    return Path.home() / ".sdkman" / "candidates" / "java" / version


def _apply_sdkman_java(env: dict[str, str], version: str) -> dict[str, str]:
    java_home = sdkman_java_home(version)
    if not java_home.is_dir():
        return env
    updated = dict(env)
    updated["JAVA_HOME"] = str(java_home)
    updated["PATH"] = f"{java_home / 'bin'}{os.pathsep}{updated.get('PATH', '')}"
    return updated


def maven_compiler_source(project: Path) -> str | None:
    """Read and resolve the Maven compiler source from the project POM."""
    pom = project / "pom.xml"
    if not pom.is_file():
        return None
    try:
        root = ElementTree.parse(pom).getroot()
    except ElementTree.ParseError:
        return None

    properties: dict[str, str] = {}
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "properties":
            continue
        for property_element in element:
            name = property_element.tag.rsplit("}", 1)[-1]
            value = (property_element.text or "").strip()
            if value:
                properties[name] = value

    for property_name in ("maven.compiler.release", "maven.compiler.source", "java.version"):
        source = properties.get(property_name)
        if source:
            return _resolve_maven_properties(source, properties)

    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "source":
            continue
        source = (element.text or "").strip()
        if source:
            return _resolve_maven_properties(source, properties)
    return None


def _resolve_maven_properties(source: str, properties: dict[str, str]) -> str:
    """Resolve Maven ``${property}`` references using POM properties."""
    resolved = source
    for _ in range(len(properties) + 1):
        match = _MAVEN_PROPERTY.search(resolved)
        if match is None:
            break
        value = properties.get(match.group(1))
        if value is None:
            break
        resolved = resolved[: match.start()] + value + resolved[match.end() :]
    return resolved.strip()


def legacy_sdkman_java(cfg: dict[str, object] | None = None) -> str:
    """Return the fallback SDKMAN Java when no installed JDK can compile a source."""
    java_cfg = (cfg or settings)["java"]
    return str(java_cfg.get("legacy_sdkman_java", "8.0.442-amzn"))


@contextmanager
def java_env(search_paths: Sequence[Path] | None = None) -> Iterator[dict[str, str]]:
    """Yield a subprocess env that prefers the Java pinned in ``.sdkmanrc``."""
    env = _background_macos_java(dict(os.environ))
    paths = tuple(search_paths) if search_paths is not None else (ROOT / ".sdkmanrc",)
    version = _java_version(*paths)
    if version is None:
        yield env
        return

    yield _apply_sdkman_java(env, version)


@contextmanager
def java_env_for_project(project: Path) -> Iterator[dict[str, str]]:
    """Run Gradle on the repo JDK. Compile JDK is selected by toolchains.

    ``JAVA_HOME`` always comes from the repository ``.sdkmanrc``. The
    toolchain init script reads the case source level and either uses
    ``--release`` on this JDK or a second JDK that Gradle finds in SDKMAN.
    """
    del project
    with java_env((ROOT / ".sdkmanrc",)) as env:
        yield env
