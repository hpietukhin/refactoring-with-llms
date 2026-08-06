"""Validated YAML profiles for DeepAgents experiments."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

PROFILE_DIRECTORY = Path(__file__).with_name("profiles")


class DeepAgentProfile(BaseModel):
    """Configuration for one DeepAgents experiment strategy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    mode: Literal["sequential", "whole-case"]
    max_model_calls: int = Field(gt=0)
    max_completion_tokens: int = Field(gt=0)
    max_smell_iterations: int = Field(gt=0)
    system_instructions: str


def load_deep_agent_profile(name: str) -> DeepAgentProfile:
    """Load and validate the named YAML profile."""
    path = PROFILE_DIRECTORY / f"{name}.yaml"
    if not path.is_file():
        raise ValueError(f"Unknown DeepAgents profile: {name!r}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return DeepAgentProfile.model_validate(data)
