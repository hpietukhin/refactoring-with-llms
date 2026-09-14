"""Thin Neo4j Cypher client matching the py2neo ``Graph.run`` surface."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from config import neo4j_config, neo4j_password


@dataclass(frozen=True, slots=True)
class CypherResult:
    rows: list[dict[str, Any]]

    def data(self) -> list[dict[str, Any]]:
        return self.rows


class Neo4jGraph:
    """Run Cypher against the local Composite Refactorings Neo4j HTTP API."""

    def __init__(
        self,
        uri: str | None = None,
        *,
        user: str | None = None,
        password: str | None = None,
        auth: tuple[str, str] | None = None,
    ) -> None:
        cfg = neo4j_config()
        self.uri = str(uri or cfg["uri"]).rstrip("/")
        if auth is not None:
            self.user, password_value = auth
        else:
            self.user = str(user or cfg["user"])
            password_value = password or neo4j_password()
        self.password = password_value
        self.timeout_seconds = float(cfg["timeout_seconds"])
        self._cypher_url = f"{self.uri}{cfg['cypher_path']}"

    def run(self, query: str, **params: Any) -> CypherResult:
        payload: dict[str, Any] = {"query": query}
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
        password_manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        password_manager.add_password(None, self.uri, self.user, self.password)
        opener = urllib.request.build_opener(
            urllib.request.HTTPBasicAuthHandler(password_manager)
        )
        try:
            with opener.open(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Neo4j HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Neo4j unreachable at {self.uri}: {exc}") from exc

        parsed = json.loads(raw)
        if "message" in parsed and "columns" not in parsed:
            raise RuntimeError(f"Neo4j Cypher error: {parsed.get('message')}")

        columns = parsed.get("columns", [])
        rows = parsed.get("data", [])
        return CypherResult(
            [dict(zip(columns, row, strict=False)) for row in rows]
        )
