"""Tests for the Organic detector wrapper."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from config import ROOT
import detection.organic as organic
from detection.organic import OrganicDetector
from detection.organic_running import organic_running
from java.organic_types import OrganicScope
from planning.organic_smells import (
    ORGANIC_RULE_MAP,
    ORGANIC_SMELL_TYPES,
    assert_organic_smell_type,
)
import planning.rules as rules

ORGANIC_DIR = ROOT / "detection" / "organic-standalone"

EXPECTED_ORGANIC_RULE_MAP = {
    "ClassDataShouldBePrivate": "Class Data Should Be Private",
    "ComplexClass": "Complex Class",
    "FeatureEnvy": "Feature Envy",
    "GodClass": "God Class",
    "LazyClass": "Lazy Class",
    "LongMethod": "Long Method",
    "LongParameterList": "Long Parameter List",
    "MessageChain": "Message Chains",
    "RefusedBequest": "Refused Bequest",
    "SpeculativeGenerality": "Speculative Generality",
    "SpaghettiCode": "Spaghetti Code",
    "DispersedCoupling": "Dispersed Coupling",
    "IntensiveCoupling": "Intensive Coupling",
    "BrainClass": "Brain Class",
    "ShotgunSurgery": "Shotgun Surgery",
    "BrainMethod": "Brain Method",
    "DataClass": "Data Class",
}


def test_organic_rule_map_matches_supported_upstream_rules() -> None:
    assert ORGANIC_RULE_MAP == EXPECTED_ORGANIC_RULE_MAP
    for smell_type in ORGANIC_SMELL_TYPES:
        assert_organic_smell_type(smell_type)


@pytest.mark.parametrize(("organic_name", "expected"), ORGANIC_RULE_MAP.items())
def test_map_rule_covers_all_organic_smell_names(organic_name: str, expected: str) -> None:
    assert OrganicDetector(ORGANIC_DIR).map_rule(organic_name) == expected


def test_map_rule_rejects_unknown_smell_name() -> None:
    with pytest.raises(ValueError, match="Unknown Organic smell rule"):
        OrganicDetector(ORGANIC_DIR).map_rule("NotARealSmell")


def test_detect_runs_gradle_wrapper_not_missing_python_module(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organic_dir = tmp_path / "organic"
    organic_dir.mkdir()
    gradlew = organic_dir / "gradlew"
    gradlew.touch()
    source = tmp_path / "source"
    source.mkdir()
    commands: list[list[str]] = []
    environments: list[dict[str, str]] = []

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        commands.append(command)
        env = kwargs["env"]
        assert isinstance(env, dict)
        environments.append(env)
        output_arg = next(arg for arg in command if arg.startswith("--args="))
        output_path = Path(output_arg.split("'")[1])
        output_path.write_text("[]", encoding="utf-8")
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(organic.subprocess, "run", fake_run)

    assert OrganicDetector(organic_dir).detect(source) == []
    assert len(commands) == 1
    assert commands[0][:2] == [str(gradlew), "run"]
    assert "organic.detector" not in commands[0][2]
    assert "-Djava.awt.headless=true" in environments[0]["JAVA_TOOL_OPTIONS"].split()


@pytest.mark.skipif(not (ORGANIC_DIR / "gradlew").is_file(), reason="organic-standalone missing")
def test_detect_maps_organic_output_to_smells(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "Bar.java").write_text(
        "public class Bar {\n  public int x = 0;\n}\n",
        encoding="utf-8",
    )

    smells = OrganicDetector(ORGANIC_DIR).detect(source)

    assert smells
    assert all(smell.detected_by == "ORGANIC" for smell in smells)
    assert any(smell.type == rules.CLASS_DATA_SHOULD_BE_PRIVATE for smell in smells)
    assert all(" " in smell.type or smell.type[0].isupper() for smell in smells)
    assert not any(smell.type == "ClassDataShouldBePrivate" for smell in smells)


def test_feedback_detects_nested_class_overlap() -> None:
    raw = [
        {
            "fullyQualifiedName": "example.Outer",
            "sourceFile": {"fileRelativePath": "src/example/Outer.java"},
            "metricsValues": {"PublicFieldCount": 1.0},
            "smells": [
                {
                    "name": "ClassDataShouldBePrivate",
                    "reason": "PUBLIC_FIELD_COUNT = 1.0",
                    "startingLine": 1,
                    "endingLine": 20,
                }
            ],
            "methods": [],
        },
        {
            "fullyQualifiedName": "example.Outer.Inner",
            "sourceFile": {"fileRelativePath": "src/example/Outer.java"},
            "metricsValues": {"PublicFieldCount": 1.0},
            "smells": [
                {
                    "name": "ClassDataShouldBePrivate",
                    "reason": "PUBLIC_FIELD_COUNT = 1.0",
                    "startingLine": 10,
                    "endingLine": 15,
                }
            ],
            "methods": [],
        },
    ]

    feedback = OrganicDetector(ORGANIC_DIR)._parse_feedback(raw)

    outer, inner = feedback
    assert outer.scope is OrganicScope.CLASS
    assert outer.nested_overlap is True
    assert outer.nested_owners == ("example.Outer.Inner",)
    assert outer.nested_warning is not None
    assert "overlaps nested class finding(s)" in outer.nested_warning
    assert inner.nested_overlap is False


def test_feedback_marks_outer_with_own_public_field_as_overlap() -> None:
    raw = [
        {
            "fullyQualifiedName": "example.Outer",
            "sourceFile": {"fileRelativePath": "src/example/Outer.java"},
            "metricsValues": {"PublicFieldCount": 2.0},
            "smells": [
                {
                    "name": "ClassDataShouldBePrivate",
                    "reason": "PUBLIC_FIELD_COUNT = 2.0",
                    "startingLine": 1,
                    "endingLine": 20,
                }
            ],
            "methods": [],
        },
        {
            "fullyQualifiedName": "example.Outer.Inner",
            "sourceFile": {"fileRelativePath": "src/example/Outer.java"},
            "metricsValues": {"PublicFieldCount": 1.0},
            "smells": [
                {
                    "name": "ClassDataShouldBePrivate",
                    "reason": "PUBLIC_FIELD_COUNT = 1.0",
                    "startingLine": 10,
                    "endingLine": 15,
                }
            ],
            "methods": [],
        },
    ]

    feedback = OrganicDetector(ORGANIC_DIR)._parse_feedback(raw)

    assert feedback[0].nested_overlap is True
    assert feedback[0].nested_owners == ("example.Outer.Inner",)
    assert feedback[1].nested_overlap is False


@pytest.mark.skipif(not (ORGANIC_DIR / "gradlew").is_file(), reason="organic-standalone missing")
def test_organic_running_yields_detector() -> None:
    detector = OrganicDetector(ORGANIC_DIR)
    with organic_running(detector) as ready:
        assert ready is detector


def test_organic_running_fails_when_gradlew_missing(tmp_path: Path) -> None:
    detector = OrganicDetector(tmp_path)
    with pytest.raises(RuntimeError, match="Organic is not available"):
        with organic_running(detector):
            pass
