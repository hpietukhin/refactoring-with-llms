"""Detect project Java version and matching SDKMAN identifier."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from detection.java_env import _MAVEN_PROPERTY, _resolve_maven_properties, legacy_sdkman_java

_GRADLE_COMPAT = re.compile(
    r"(?:sourceCompatibility|targetCompatibility|JavaVersion\.VERSION_(\d+(?:_\d+)?))\s*[=:]\s*[\"']?([^\"'\s]+)[\"']?",
    re.MULTILINE,
)
_GRADLE_JAVA_VERSION = re.compile(
    r"java\.version\s*=\s*[\"']?([^\"'\s]+)[\"']?",
    re.MULTILINE,
)


@dataclass(frozen=True, slots=True)
class ProjectJavaVersion:
    source: str
    sdkman_java: str
    build_system: str


def _normalize_java_source(raw: str) -> str:
    stripped = raw.strip()
    if stripped.startswith("1."):
        return stripped
    if stripped.isdigit():
        major = int(stripped)
        if major <= 8:
            return f"1.{major}"
        return str(major)
    return stripped


def _looks_like_java_source(raw: str) -> bool:
    stripped = raw.strip()
    if not stripped:
        return False
    if stripped.startswith("1."):
        suffix = stripped[2:]
        return suffix.replace(".", "").isdigit()
    if stripped.isdigit():
        return 5 <= int(stripped) <= 25
    return False


def source_major(source: str) -> int | None:
    """Return the Java language major for a compiler source string."""
    normalized = _normalize_java_source(source)
    if not _looks_like_java_source(normalized):
        return None
    if normalized.startswith("1."):
        suffix = normalized[2:]
        if suffix.isdigit():
            return int(suffix)
        return None
    if normalized.isdigit():
        return int(normalized)
    return None


def min_source_for_compiler(compiler_major: int) -> int:
    """Lowest ``-source`` still accepted by ``javac`` on this compiler (JEP 182)."""
    if compiler_major >= 20:
        return 8
    if compiler_major >= 12:
        return 7
    if compiler_major >= 9:
        return 6
    return 1


def compiler_supports_source(compiler_major: int, source: int) -> bool:
    """Return whether this compiler's javac still accepts ``source``."""
    return source <= compiler_major and source >= min_source_for_compiler(compiler_major)


def installed_sdkman_javas() -> list[tuple[int, str]]:
    """Return ``(major, identifier)`` for JDK installs under SDKMAN."""
    root = Path.home() / ".sdkman" / "candidates" / "java"
    if not root.is_dir():
        return []
    found: list[tuple[int, str]] = []
    for path in root.iterdir():
        if path.name == "current" or not path.is_dir():
            continue
        head = path.name.split(".", maxsplit=1)[0]
        if head.isdigit():
            found.append((int(head), path.name))
    return found


def sdkman_java_for_source(source: str, cfg: dict[str, object] | None = None) -> str:
    """Pick the oldest installed SDKMAN JDK whose javac can compile ``source``.

    Maven baselines used JDK 8 for Java 6–8. Newer JDKs still accept ``-source 6``
    but they dropped JAXB and other JDK 8 APIs.
    """
    needed = source_major(source)
    if needed is None:
        return legacy_sdkman_java(cfg)
    capable = [
        (major, name)
        for major, name in installed_sdkman_javas()
        if compiler_supports_source(major, needed)
    ]
    if capable:
        capable.sort(key=lambda item: (item[0], item[1]))
        return capable[0][1]
    return legacy_sdkman_java(cfg)


def _maven_compiler_source(project: Path) -> str | None:
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

    for key in ("maven.compiler.release", "maven.compiler.source", "java.version"):
        raw = properties.get(key)
        if raw and _looks_like_java_source(_normalize_java_source(raw)):
            return _normalize_java_source(_resolve_maven_properties(raw, properties))

    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "release":
            value = (element.text or "").strip()
            if value and _looks_like_java_source(_normalize_java_source(value)):
                return _normalize_java_source(value)
        if tag == "source":
            value = (element.text or "").strip()
            if not value:
                continue
            normalized = _normalize_java_source(_resolve_maven_properties(value, properties))
            if _looks_like_java_source(normalized):
                return normalized
    return None


def _gradle_java_source(project: Path) -> str | None:
    for name in ("build.gradle.kts", "build.gradle"):
        path = project / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        java_version = _GRADLE_JAVA_VERSION.search(text)
        if java_version and _looks_like_java_source(java_version.group(1)):
            return _normalize_java_source(java_version.group(1))
        for match in _GRADLE_COMPAT.finditer(text):
            raw = match.group(2) if match.lastindex and match.lastindex >= 2 else match.group(0)
            if raw.startswith("VERSION_"):
                raw = raw.removeprefix("VERSION_").replace("_", ".")
            if _looks_like_java_source(raw):
                return _normalize_java_source(raw)
    return None


def detect_project_java_version(project: Path, cfg: dict[str, object] | None = None) -> ProjectJavaVersion | None:
    """Return compiler source level and SDKMAN Java for a Maven or Gradle checkout.

    Read the POM first when both exist. ``gradle init`` rewrites
    ``sourceCompatibility`` to 1.8 and would hide Java 6 cases.
    """
    has_gradle = (project / "build.gradle").is_file() or (
        project / "build.gradle.kts"
    ).is_file()
    has_pom = (project / "pom.xml").is_file()
    if not has_gradle and not has_pom:
        return None
    source = _maven_compiler_source(project) or _gradle_java_source(project)
    if source is None:
        return None
    return ProjectJavaVersion(
        source=source,
        sdkman_java=sdkman_java_for_source(source, cfg),
        build_system="gradle" if has_gradle else "maven",
    )
