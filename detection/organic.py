"""Run organic-standalone and map its CamelCase rules onto planning smells."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import ClassVar

from config import ROOT, data_scratch_dir, settings
from detection.java_env import java_env
import planning.rules as rules
from planning.organic_smells import ORGANIC_RULE_MAP
from planning.rules import SmellType
from smell.smell import Smell


class OrganicDetector:
    """Map Organic detection rules and run the organic-standalone Gradle CLI."""

    RULE_MAP = ORGANIC_RULE_MAP

    HIGH_SEVERITY: ClassVar[frozenset[SmellType]] = frozenset(
        {
            rules.LONG_METHOD,
            rules.GOD_CLASS,
            rules.LARGE_CLASS,
            rules.COMPLEX_CLASS,
            rules.BRAIN_CLASS,
            rules.BRAIN_METHOD,
            rules.SPAGHETTI_CODE,
            rules.COMPLEX_METHOD,
            rules.CONDITIONAL_COMPLEXITY,
        }
    )

    def __init__(
        self,
        organic_dir: Path = ROOT / settings["detection"]["organic_dir"],
        *,
        timeout: float = float(settings["detection"]["timeout_seconds"]),
    ) -> None:
        self.organic_dir = Path(organic_dir).expanduser().resolve()
        self.timeout = timeout

    def map_rule(self, organic_name: str) -> SmellType:
        """Map an Organic ``SmellName`` enum value onto a planning ``SmellType``."""
        if organic_name not in self.RULE_MAP:
            raise ValueError(f"Unknown Organic smell rule: {organic_name!r}")
        return self.RULE_MAP[organic_name]

    def detect(
        self,
        source_path: Path,
        *,
        commit_hash: str | None = None,
    ) -> list[Smell]:
        """Run Organic on ``source_path`` and return mapped ``Smell`` instances."""
        source = Path(source_path).expanduser().resolve()
        gradlew = self.organic_dir / "gradlew"
        if not gradlew.is_file():
            raise RuntimeError(f"gradlew not found in {self.organic_dir}")
        if not source.is_dir():
            raise RuntimeError(f"Source directory does not exist: {source}")

        scratch = data_scratch_dir()
        with tempfile.NamedTemporaryFile(
            suffix=".json",
            delete=False,
            dir=scratch,
        ) as tmp:
            output_path = Path(tmp.name)

        command = [
            str(gradlew),
            "run",
            f"--args=-sf '{output_path}' -src '{source}' -os",
        ]
        try:
            with java_env(
                (
                    self.organic_dir / ".sdkmanrc",
                    ROOT / ".sdkmanrc",
                )
            ) as env:
                result = subprocess.run(
                    command,
                    cwd=self.organic_dir,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=self.timeout,
                    env=env,
                )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()
                raise RuntimeError(
                    f"Organic exited with code {result.returncode}: {detail[:500]}"
                )
            if not output_path.exists() or output_path.stat().st_size == 0:
                return []
            raw = json.loads(output_path.read_text(encoding="utf-8"))
            return self._parse(raw, commit_hash=commit_hash)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Organic timed out after {self.timeout}s") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError("Organic output is not valid JSON") from exc
        finally:
            output_path.unlink(missing_ok=True)

    def _parse(
        self,
        raw: list[dict[str, object]],
        *,
        commit_hash: str | None = None,
    ) -> list[Smell]:
        smells: list[Smell] = []
        for class_entry in raw:
            file_path = self._file_path(class_entry)
            for entry in class_entry.get("smells") or []:
                if isinstance(entry, dict):
                    smells.append(
                        self._smell(entry, file_path, commit_hash=commit_hash)
                    )
            for method in class_entry.get("methods") or []:
                if not isinstance(method, dict):
                    continue
                for entry in method.get("smells") or []:
                    if isinstance(entry, dict):
                        smells.append(
                            self._smell(entry, file_path, commit_hash=commit_hash)
                        )
        return smells

    def _file_path(self, class_entry: dict[str, object]) -> str:
        source_file = class_entry.get("sourceFile")
        if isinstance(source_file, dict):
            relative = source_file.get("fileRelativePath")
            if isinstance(relative, str) and relative:
                return str((self.organic_dir / relative).resolve())
        fqn = class_entry.get("fullyQualifiedName")
        if isinstance(fqn, str) and fqn:
            return fqn.replace(".", "/") + ".java"
        return ""

    def _smell(
        self,
        entry: dict[str, object],
        file_path: str,
        *,
        commit_hash: str | None = None,
    ) -> Smell:
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"Organic smell entry has no name: {entry!r}")
        smell_type = self.map_rule(name)
        start_line = int(entry.get("startingLine") or 1)
        end_raw = entry.get("endingLine")
        end_line = int(end_raw) if isinstance(end_raw, int) else start_line
        return Smell.at(
            smell_type,
            file_path,
            start_line,
            end_line,
            severity="HIGH" if smell_type in self.HIGH_SEVERITY else "MEDIUM",
            detected_by="ORGANIC",
            commit_hash=commit_hash,
        )
