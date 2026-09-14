"""Organic-only projection of the planning dependency rules."""

from __future__ import annotations

from planning.organic_smells import ORGANIC_SMELL_TYPES
from planning.rules import Rule, SmellType, rules

organic_rules: dict[SmellType, Rule] = {
    smell_type: Rule(
        target=rule.target,
        positive=frozenset(rule.positive & ORGANIC_SMELL_TYPES),
        negative=frozenset(rule.negative & ORGANIC_SMELL_TYPES),
    )
    for smell_type, rule in rules.items()
    if smell_type in ORGANIC_SMELL_TYPES
}

__all__ = ["organic_rules"]
