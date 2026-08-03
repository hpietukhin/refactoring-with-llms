"""Experiment dataset: JSONL start-case manifest + Neo4j Cypher access.

JSONL is the offline/reproducible experiment input (one case per line).
Neo4j is the live Composite Refactorings 2020 graph used to inspect cases.
Smell instances come from a detector run against a ``Repo`` checkout.
Connection settings come from ``config.toml`` via ``config.neo4j_config``.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import ROOT, dataset_config, neo4j_config
from detection.organic import OrganicDetector
from repository.repo import Repo
from smell.smell import Smell

# Java FQN: package.Type or package.Type.member, optional method params.
# e.g. org.junit.runners.ParentRunner.invokeValidatorsOnMethod
#      org.tap4j.parser.Tap13Parser.checkIndentationLevel(int,String)
_JAVA_ELEMENT_FQN = re.compile(
    r"^"
    r"[A-Za-z_][A-Za-z0-9_]*"
    r"(?:\.[A-Za-z_][A-Za-z0-9_]*)+"
    r"(?:\([A-Za-z0-9_.,]*\))?"
    r"$"
)


def _parse_elements(raw: Any) -> list[str]:
    if isinstance(raw, str):
        items = [part.strip() for part in raw.split(",") if part.strip()]
    else:
        items = [str(item) for item in (raw or [])]
    for item in items:
        assert _JAVA_ELEMENT_FQN.fullmatch(item), (
            "element must use Java FQN dot notation "
            f"(e.g. org.junit.runners.ParentRunner.invokeValidatorsOnMethod), got {item!r}"
        )
    return items


@dataclass(slots=True)
class CaseRecord:
    """One experiment start case from the JSONL manifest."""

    case_id: str
    project: str
    repo_url: str
    start_commit: str
    elements: list[str]
    start_commit_order: int | None = None
    end_commit: str | None = None
    end_commit_order: int | None = None
    start_state: dict[str, Any] = field(default_factory=dict)
    range_metadata: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CaseRecord:
        start_commit = data.get("start_commit", data.get("start_commit_hash"))
        end_commit = data.get("end_commit", data.get("end_commit_hash"))
        return cls(
            case_id=str(data["case_id"]),
            project=str(data["project"]),
            repo_url=str(data["repo_url"]),
            start_commit=str(start_commit),
            elements=_parse_elements(data.get("elements")),
            start_commit_order=data.get("start_commit_order"),
            end_commit=str(end_commit) if end_commit else None,
            end_commit_order=data.get("end_commit_order"),
            start_state=dict(data.get("start_state") or {}),
            range_metadata=dict(data.get("range_metadata") or {}),
            raw=dict(data),
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "case_id": self.case_id,
            "project": self.project,
            "repo_url": self.repo_url,
            "start_commit": self.start_commit,
            "elements": self.elements,
        }
        if self.start_commit_order is not None:
            payload["start_commit_order"] = self.start_commit_order
        if self.end_commit:
            payload["end_commit"] = self.end_commit
        if self.end_commit_order is not None:
            payload["end_commit_order"] = self.end_commit_order
        if self.start_state:
            payload["start_state"] = self.start_state
        if self.range_metadata:
            payload["range_metadata"] = self.range_metadata
        return payload


class Dataset:
    """Load start cases from JSONL and query the Neo4j dataset graph."""

    def __init__(self) -> None:
        dataset_cfg = dataset_config()
        neo4j_cfg = neo4j_config()

        configured_manifest = Path(dataset_cfg["manifest"])
        if not configured_manifest.is_absolute():
            configured_manifest = ROOT / configured_manifest
        self.manifest_path = configured_manifest

        self.uri = str(neo4j_cfg["uri"]).rstrip("/")
        self.user = str(neo4j_cfg["user"])
        self.password = str(neo4j_cfg["password"])
        self.timeout_seconds = float(neo4j_cfg["timeout_seconds"])
        self._cypher_url = f"{self.uri}{neo4j_cfg['cypher_path']}"

    def iter_cases(self) -> Iterator[CaseRecord]:
        """Yield typed cases from the JSONL manifest."""
        with self.manifest_path.open(encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    payload = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"Invalid JSONL at {self.manifest_path}:{line_no}"
                    ) from exc
                if not isinstance(payload, dict):
                    raise ValueError(
                        f"JSONL line must be an object at {self.manifest_path}:{line_no}"
                    )
                yield CaseRecord.from_dict(payload)

    def load_cases(self) -> list[CaseRecord]:
        """Load all cases from the configured JSONL manifest."""
        return list(self.iter_cases())

    def write_cases(self, cases: list[CaseRecord | dict[str, Any]]) -> int:
        """Overwrite the JSONL manifest with the given cases."""
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with self.manifest_path.open("w", encoding="utf-8") as handle:
            for case in cases:
                if isinstance(case, CaseRecord):
                    payload = case.to_dict()
                else:
                    payload = case
                handle.write(json.dumps(payload, ensure_ascii=False))
                handle.write("\n")
                count += 1
        return count

    def is_available(self) -> bool:
        """Return True when the Neo4j HTTP endpoint answers."""
        request = urllib.request.Request(
            f"{self.uri}/db/data/",
            method="GET",
            headers={"Accept": "application/json"},
        )
        try:
            with self._open(request) as response:
                return 200 <= response.status < 300
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    def query(
        self,
        cypher: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Run Cypher and return rows as dictionaries."""
        payload: dict[str, Any] = {"query": cypher}
        if params:
            payload["params"] = params

        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self._cypher_url,
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with self._open(request) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Neo4j HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Neo4j unreachable at {self.uri}: {exc}") from exc

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid Neo4j JSON response: {raw[:200]}") from exc

        if "message" in parsed and "columns" not in parsed:
            raise RuntimeError(f"Neo4j Cypher error: {parsed.get('message')}")

        columns = parsed.get("columns", [])
        rows = parsed.get("data", [])
        return [dict(zip(columns, row, strict=False)) for row in rows]

    def projects(self) -> list[str]:
        """List project names known to the graph."""
        rows = self.query("MATCH (p:Project) RETURN p.name AS name ORDER BY name")
        return [str(row["name"]) for row in rows if row.get("name") is not None]

    def detect_smells_at_start(
        self,
        case: CaseRecord,
        *,
        repo: Repo,
        detector: OrganicDetector | None = None,
    ) -> list[Smell]:
        """Detect smells for a JSONL case at its start commit via the detector."""
        repo.init_experiment(case.start_commit)
        organic = detector or OrganicDetector()
        return organic.detect(repo.path, commit_hash=case.start_commit)

    def _open(self, request: urllib.request.Request):
        password_manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        password_manager.add_password(None, self.uri, self.user, self.password)
        auth_handler = urllib.request.HTTPBasicAuthHandler(password_manager)
        opener = urllib.request.build_opener(auth_handler)
        return opener.open(request, timeout=self.timeout_seconds)
