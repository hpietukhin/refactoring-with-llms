"""Tests for Java version detection from Maven and Gradle build files."""

from __future__ import annotations

from pathlib import Path

from detection.java_version import (
    _looks_like_java_source,
    compiler_supports_source,
    detect_project_java_version,
    min_source_for_compiler,
    sdkman_java_for_source,
)


def test_looks_like_java_source_rejects_source_paths() -> None:
    assert _looks_like_java_source("1.6")
    assert _looks_like_java_source("17")
    assert not _looks_like_java_source("Java-WebSocket/src/main/java")


def test_detect_maven_source_ignores_build_helper_add_source(tmp_path: Path) -> None:
    pom = tmp_path / "pom.xml"
    pom.write_text(
        """
        <project>
          <build>
            <plugins>
              <plugin>
                <artifactId>build-helper-maven-plugin</artifactId>
                <executions>
                  <execution>
                    <configuration>
                      <sources>
                        <source>Java-WebSocket/src/main/java</source>
                      </sources>
                    </configuration>
                  </execution>
                </executions>
              </plugin>
              <plugin>
                <artifactId>maven-compiler-plugin</artifactId>
                <configuration>
                  <source>1.6</source>
                </configuration>
              </plugin>
            </plugins>
          </build>
        </project>
        """,
        encoding="utf-8",
    )
    version = detect_project_java_version(tmp_path)
    assert version is not None
    assert version.source == "1.6"
    assert version.build_system == "maven"


def test_detect_prefers_pom_source_over_gradle_init(tmp_path: Path) -> None:
    (tmp_path / "pom.xml").write_text(
        "<project><properties><maven.compiler.source>1.6</maven.compiler.source>"
        "</properties></project>",
        encoding="utf-8",
    )
    (tmp_path / "build.gradle.kts").write_text(
        "java { sourceCompatibility = JavaVersion.VERSION_1_8 }\n",
        encoding="utf-8",
    )
    version = detect_project_java_version(tmp_path)
    assert version is not None
    assert version.source == "1.6"
    assert version.build_system == "gradle"


def test_jep_182_compiler_source_window() -> None:
    assert min_source_for_compiler(11) == 6
    assert min_source_for_compiler(17) == 7
    assert min_source_for_compiler(21) == 8
    assert compiler_supports_source(17, 8)
    assert compiler_supports_source(11, 6)
    assert not compiler_supports_source(17, 6)
    assert not compiler_supports_source(21, 7)


def test_sdkman_java_for_source_picks_oldest_installed_compiler_that_accepts_source() -> None:
    chosen = sdkman_java_for_source("1.6")
    assert int(chosen.split(".", maxsplit=1)[0]) == 8
    chosen_8 = sdkman_java_for_source("1.8")
    assert int(chosen_8.split(".", maxsplit=1)[0]) == 8
