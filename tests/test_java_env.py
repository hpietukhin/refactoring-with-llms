"""Tests for Maven Java-version selection."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from detection.java_env import java_env as java_environment
from detection.java_env import java_env_for_project, maven_compiler_source


def test_maven_compiler_source_reads_literal_value(tmp_path: Path) -> None:
    (tmp_path / "pom.xml").write_text(
        "<project><build><plugins><plugin><configuration>"
        "<source>1.6</source></configuration></plugin></plugins></build></project>",
        encoding="utf-8",
    )

    assert maven_compiler_source(tmp_path) == "1.6"


def test_maven_compiler_source_resolves_property_value(tmp_path: Path) -> None:
    (tmp_path / "pom.xml").write_text(
        "<project><properties><java.source.version>1.6</java.source.version>"
        "</properties><build><plugins><plugin><configuration>"
        "<source>${java.source.version}</source></configuration></plugin>"
        "</plugins></build></project>",
        encoding="utf-8",
    )

    assert maven_compiler_source(tmp_path) == "1.6"


def test_maven_compiler_source_reads_maven_compiler_property(tmp_path: Path) -> None:
    (tmp_path / "pom.xml").write_text(
        "<project><properties><maven.compiler.source>1.6</maven.compiler.source>"
        "</properties></project>",
        encoding="utf-8",
    )

    assert maven_compiler_source(tmp_path) == "1.6"


def test_java_env_marks_macos_jvms_as_background_utilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("JAVA_TOOL_OPTIONS", "-Xmx512m")

    with java_environment(()) as env:
        assert env["JAVA_TOOL_OPTIONS"] == "-Xmx512m -Dapple.awt.UIElement=true"


def test_java_env_for_project_keeps_gradle_on_repo_jdk(tmp_path: Path) -> None:
    (tmp_path / "pom.xml").write_text(
        "<project><properties><maven.compiler.source>1.6</maven.compiler.source>"
        "</properties></project>",
        encoding="utf-8",
    )
    sdkman_17 = Path.home() / ".sdkman" / "candidates" / "java" / "17.0.19-tem"
    if not sdkman_17.is_dir():
        pytest.skip("SDKMAN Java 17.0.19-tem is not installed")
    with java_env_for_project(tmp_path) as env:
        assert env["JAVA_HOME"] == str(sdkman_17)
        assert "REVAMP_TOOLCHAIN_JAVA" not in env
        assert "REVAMP_COMPILE_JAVA_HOME" not in env
