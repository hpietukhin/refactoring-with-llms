"""Tests for testing.test_selection."""

from __future__ import annotations

from pathlib import Path

from testing.test_selection import resolve_targeted_tests


def test_direct_test_file(tmp_path: Path) -> None:
    test_dir = tmp_path / "src" / "test" / "java" / "com" / "example"
    test_dir.mkdir(parents=True)
    (test_dir / "FooTest.java").write_text("class FooTest {}", encoding="utf-8")
    result = resolve_targeted_tests(tmp_path, ["src/test/java/com/example/FooTest.java"])
    assert result == ["com.example.FooTest"]


def test_production_file_finds_matching_test(tmp_path: Path) -> None:
    src = tmp_path / "src" / "main" / "java" / "com" / "example"
    src.mkdir(parents=True)
    (src / "Foo.java").write_text("class Foo {}", encoding="utf-8")
    test_dir = tmp_path / "src" / "test" / "java" / "com" / "example"
    test_dir.mkdir(parents=True)
    (test_dir / "FooTest.java").write_text("class FooTest {}", encoding="utf-8")
    result = resolve_targeted_tests(tmp_path, ["src/main/java/com/example/Foo.java"])
    assert result == ["com.example.FooTest"]


def test_no_matching_test_returns_empty(tmp_path: Path) -> None:
    src = tmp_path / "src" / "main" / "java" / "com" / "example"
    src.mkdir(parents=True)
    (src / "Bar.java").write_text("class Bar {}", encoding="utf-8")
    result = resolve_targeted_tests(tmp_path, ["src/main/java/com/example/Bar.java"])
    assert result == []


def test_empty_input(tmp_path: Path) -> None:
    assert resolve_targeted_tests(tmp_path, []) == []


def test_deduplicates(tmp_path: Path) -> None:
    test_dir = tmp_path / "src" / "test" / "java" / "org"
    test_dir.mkdir(parents=True)
    (test_dir / "XTest.java").write_text("class XTest {}", encoding="utf-8")
    result = resolve_targeted_tests(
        tmp_path,
        ["src/test/java/org/XTest.java", "src/test/java/org/XTest.java"],
    )
    assert result == ["org.XTest"]
