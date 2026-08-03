"""Tests for the Organic detector wrapper."""

from __future__ import annotations

from pathlib import Path

import pytest

from config import ROOT
from detection.organic import OrganicDetector
from detection.organic_running import organic_running
from planning.organic_smells import ORGANIC_RULE_MAP
import planning.rules as rules

ORGANIC_DIR = ROOT / "detection" / "organic-standalone"


@pytest.mark.parametrize(("organic_name", "expected"), ORGANIC_RULE_MAP.items())
def test_map_rule_covers_all_organic_smell_names(organic_name: str, expected: str) -> None:
    assert OrganicDetector(ORGANIC_DIR).map_rule(organic_name) == expected


def test_map_rule_rejects_unknown_smell_name() -> None:
    with pytest.raises(ValueError, match="Unknown Organic smell rule"):
        OrganicDetector(ORGANIC_DIR).map_rule("NotARealSmell")


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
