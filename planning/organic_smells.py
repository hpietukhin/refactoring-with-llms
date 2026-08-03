"""Organic-detectable smell types and Organic CLI name mapping."""

from __future__ import annotations

from typing import Final

from planning.rules import (
    BRAIN_CLASS,
    BRAIN_METHOD,
    CLASS_DATA_SHOULD_BE_PRIVATE,
    COMPLEX_CLASS,
    DATA_CLASS,
    DISPERSED_COUPLING,
    FEATURE_ENVY,
    GOD_CLASS,
    INTENSIVE_COUPLING,
    LAZY_CLASS,
    LONG_METHOD,
    LONG_PARAMETER_LIST,
    MESSAGE_CHAINS,
    REFUSED_BEQUEST,
    SHOTGUN_SURGERY,
    SPECULATIVE_GENERALITY,
    SPAGHETTI_CODE,
    SmellType,
)

ORGANIC_RULE_MAP: Final[dict[str, SmellType]] = {
    "ClassDataShouldBePrivate": CLASS_DATA_SHOULD_BE_PRIVATE,
    "ComplexClass": COMPLEX_CLASS,
    "FeatureEnvy": FEATURE_ENVY,
    "GodClass": GOD_CLASS,
    "LazyClass": LAZY_CLASS,
    "LongMethod": LONG_METHOD,
    "LongParameterList": LONG_PARAMETER_LIST,
    "MessageChain": MESSAGE_CHAINS,
    "RefusedBequest": REFUSED_BEQUEST,
    "SpeculativeGenerality": SPECULATIVE_GENERALITY,
    "SpaghettiCode": SPAGHETTI_CODE,
    "DispersedCoupling": DISPERSED_COUPLING,
    "IntensiveCoupling": INTENSIVE_COUPLING,
    "BrainClass": BRAIN_CLASS,
    "ShotgunSurgery": SHOTGUN_SURGERY,
    "BrainMethod": BRAIN_METHOD,
    "DataClass": DATA_CLASS,
}

ORGANIC_SMELL_TYPES: Final[frozenset[SmellType]] = frozenset(ORGANIC_RULE_MAP.values())

__all__ = ["ORGANIC_RULE_MAP", "ORGANIC_SMELL_TYPES"]
