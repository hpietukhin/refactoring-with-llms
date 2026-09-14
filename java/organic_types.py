"""Enums used by filtered ORGANIC detector feedback."""

from __future__ import annotations

from enum import StrEnum


class OrganicScope(StrEnum):
    """Source element to which ORGANIC attached a finding."""

    CLASS = "class"
    METHOD = "method"
    CONSTRUCTOR = "constructor"
