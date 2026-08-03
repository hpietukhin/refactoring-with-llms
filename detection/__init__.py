"""Smell detection backends."""

from detection.java_env import java_env
from detection.organic import OrganicDetector
from detection.organic_running import organic_running

__all__ = ["OrganicDetector", "java_env", "organic_running"]
