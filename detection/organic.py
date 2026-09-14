"""Run organic-standalone and map its CamelCase rules onto planning smells."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import ClassVar

from config import ROOT, data_scratch_dir, settings
from detection.java_env import java_env
from java.organic_feedback import OrganicFeedback
from java.organic_types import OrganicScope
import planning.rules as rules
from planning.organic_smells import ORGANIC_RULE_MAP
from planning.rules import SmellType
from smell.smell import Smell


class OrganicDetector:
    """Map Organic detection rules and run the organic-standalone Gradle CLI."""

    RULE_MAP = ORGANIC_RULE_MAP
    RELEVANT_METRICS: ClassVar[dict[str, tuple[str, ...]]] = {
        "ClassDataShouldBePrivate": ("PublicFieldCount",),
        "ComplexClass": ("WeightedMethodCount",),
        "FeatureEnvy": (
            "CouplingIntensity",
            "CouplingDispersion",
            "NumberOfAccessedVariables",
        ),
        "GodClass": ("ClassLinesOfCode", "TightClassCohesion"),
        "LazyClass": ("ClassLinesOfCode",),
        "LongMethod": ("MethodLinesOfCode",),
        "LongParameterList": ("ParameterCount",),
        "MessageChain": ("MaxCallChain",),
        "RefusedBequest": ("OverrideRatio",),
        "SpeculativeGenerality": ("IsAbstract",),
        "DispersedCoupling": (
            "CouplingIntensity",
            "CouplingDispersion",
            "ChangingClasses",
        ),
        "IntensiveCoupling": (
            "CouplingIntensity",
            "CouplingDispersion",
            "ChangingClasses",
        ),
        "BrainClass": (
            "WeightedMethodCount",
            "TightClassCohesion",
            "ClassLinesOfCode",
        ),
        "ShotgunSurgery": ("ChangingMethods", "ChangingClasses"),
        "BrainMethod": (
            "MethodLinesOfCode",
            "CyclomaticComplexity",
            "MaxNesting",
            "NumberOfAccessedVariables",
        ),
        "DataClass": (
            "WeighOfClass",
            "PublicFieldCount",
            "NumberOfAccessorMethods",
            "WeightedMethodCount",
        ),
    }
    RELATED_OWNER_PATTERN: ClassVar[re.Pattern[str]] = re.compile(
        r"^CALLS_TO_(?P<owner>.+?)\s*[><=]"
    )

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
        return [
            feedback.smell
            for feedback in self.detect_feedback(
                source_path,
                commit_hash=commit_hash,
            )
        ]

    def detect_feedback(
        self,
        source_path: Path,
        *,
        commit_hash: str | None = None,
    ) -> list[OrganicFeedback]:
        """Run Organic and retain its useful owner, reason, and metric context."""
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
                env = dict(env)
                java_options = env.get("JAVA_TOOL_OPTIONS", "").split()
                headless_option = "-Djava.awt.headless=true"
                if headless_option not in java_options:
                    env["JAVA_TOOL_OPTIONS"] = " ".join((*java_options, headless_option))
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
            if not isinstance(raw, list) or not all(
                isinstance(entry, dict) for entry in raw
            ):
                raise RuntimeError("Organic output root must be a list of objects")
            return self._parse_feedback(raw, commit_hash=commit_hash)
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
        return [
            feedback.smell
            for feedback in self._parse_feedback(raw, commit_hash=commit_hash)
        ]

    def _parse_feedback(
        self,
        raw: list[dict[str, object]],
        *,
        commit_hash: str | None = None,
    ) -> list[OrganicFeedback]:
        feedback: list[OrganicFeedback] = []
        for class_entry in raw:
            file_path = self._file_path(class_entry)
            class_owner = self._owner(class_entry)
            class_metrics = self._metrics(class_entry)
            for entry in class_entry.get("smells") or []:
                if isinstance(entry, dict):
                    feedback.append(
                        self._feedback(
                            entry,
                            file_path,
                            owner=class_owner,
                            scope=OrganicScope.CLASS,
                            metrics=class_metrics,
                            commit_hash=commit_hash,
                        )
                    )
            for method in class_entry.get("methods") or []:
                if not isinstance(method, dict):
                    continue
                method_owner = self._owner(method)
                scope = self._method_scope(class_owner, method_owner)
                method_metrics = self._metrics(method)
                for entry in method.get("smells") or []:
                    if isinstance(entry, dict):
                        feedback.append(
                            self._feedback(
                                entry,
                                file_path,
                                owner=method_owner,
                                scope=scope,
                                metrics=method_metrics,
                                commit_hash=commit_hash,
                            )
                        )
        return self._mark_nested_overlaps(feedback)

    def _owner(self, entry: dict[str, object]) -> str:
        owner = entry.get("fullyQualifiedName")
        return owner if isinstance(owner, str) else ""

    def _metrics(
        self,
        entry: dict[str, object],
    ) -> dict[str, float | None]:
        raw_metrics = entry.get("metricsValues")
        if not isinstance(raw_metrics, dict):
            return {}
        metrics: dict[str, float | None] = {}
        for name, value in raw_metrics.items():
            if not isinstance(name, str):
                continue
            if value is None:
                metrics[name] = None
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                metrics[name] = float(value)
        return metrics

    def _method_scope(
        self,
        class_owner: str,
        method_owner: str,
    ) -> OrganicScope:
        class_name = class_owner.rsplit(".", maxsplit=1)[-1]
        method_name = method_owner.rsplit(".", maxsplit=1)[-1]
        if class_name and method_name == class_name:
            return OrganicScope.CONSTRUCTOR
        return OrganicScope.METHOD

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

    def _feedback(
        self,
        entry: dict[str, object],
        file_path: str,
        *,
        owner: str,
        scope: OrganicScope,
        metrics: dict[str, float | None],
        commit_hash: str | None = None,
    ) -> OrganicFeedback:
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"Organic smell entry has no name: {entry!r}")
        reason_raw = entry.get("reason")
        reason = reason_raw if isinstance(reason_raw, str) and reason_raw else None
        relevant_metrics = tuple(
            (metric_name, metrics[metric_name])
            for metric_name in self.RELEVANT_METRICS.get(name, ())
            if metric_name in metrics
        )
        related_owner = None
        if reason and (match := self.RELATED_OWNER_PATTERN.match(reason)):
            related_owner = match.group("owner").strip()
        return OrganicFeedback(
            smell=self._smell(entry, file_path, commit_hash=commit_hash),
            owner=owner,
            scope=scope,
            reason=reason,
            relevant_metrics=relevant_metrics,
            related_owner=related_owner,
        )

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

    def _mark_nested_overlaps(
        self,
        feedback: list[OrganicFeedback],
    ) -> list[OrganicFeedback]:
        marked: list[OrganicFeedback] = []
        class_feedback = [
            item for item in feedback if item.scope is OrganicScope.CLASS
        ]
        for item in feedback:
            nested = [
                candidate
                for candidate in class_feedback
                if self._is_nested_match(item, candidate)
            ]
            if not nested:
                marked.append(item)
                continue
            immediate = [
                candidate
                for candidate in nested
                if not any(
                    other.owner != candidate.owner
                    and candidate.owner.startswith(f"{other.owner}.")
                    for other in nested
                )
            ]
            marked.append(
                replace(
                    item,
                    nested_overlap=True,
                    nested_owners=tuple(candidate.owner for candidate in immediate),
                )
            )
        return marked

    def _is_nested_match(
        self,
        outer: OrganicFeedback,
        inner: OrganicFeedback,
    ) -> bool:
        if outer.scope is not OrganicScope.CLASS or inner.scope is not OrganicScope.CLASS:
            return False
        if outer.smell.type != inner.smell.type:
            return False
        if outer.smell.file_path != inner.smell.file_path:
            return False
        if not inner.owner.startswith(f"{outer.owner}."):
            return False
        outer_range = outer.smell.location.range
        inner_range = inner.smell.location.range
        return (
            outer_range.start.line < inner_range.start.line
            and outer_range.end.line > inner_range.end.line
        )
