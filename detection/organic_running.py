"""Context manager that checks Organic is ready to run."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from contextlib import contextmanager

from config import ROOT
from detection.java_env import java_env
from detection.organic import OrganicDetector


@contextmanager
def organic_running(detector: OrganicDetector) -> Iterator[OrganicDetector]:
    """Yield ``detector`` only when Organic's Gradle wrapper can start."""
    gradlew = detector.organic_dir / "gradlew"
    if not gradlew.is_file():
        raise RuntimeError(f"Organic is not available: missing {gradlew}")

    with java_env(
        (
            detector.organic_dir / ".sdkmanrc",
            ROOT / ".sdkmanrc",
        )
    ) as env:
        result = subprocess.run(
            [str(gradlew), "help", "-q"],
            cwd=detector.organic_dir,
            capture_output=True,
            text=True,
            check=False,
            timeout=detector.timeout,
            env=env,
        )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"Organic is not running: {detail[:500]}")

    yield detector
