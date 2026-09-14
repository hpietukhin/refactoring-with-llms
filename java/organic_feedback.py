"""Compact, agent-facing feedback from one ORGANIC finding."""

from __future__ import annotations

from dataclasses import dataclass

from java.organic_types import OrganicScope
from smell.smell import Smell


@dataclass(frozen=True, slots=True)
class OrganicFeedback:
    """Keep only the ORGANIC fields that help locate and fix one smell."""

    smell: Smell
    owner: str
    scope: OrganicScope
    reason: str | None
    relevant_metrics: tuple[tuple[str, float | None], ...]
    related_owner: str | None = None
    nested_overlap: bool = False
    nested_owners: tuple[str, ...] = ()

    @property
    def nested_warning(self) -> str | None:
        """Warn when this finding overlaps findings on nested classes."""
        if not self.nested_overlap:
            return None
        owners = ", ".join(self.nested_owners)
        return f"This finding overlaps nested class finding(s): {owners}."
