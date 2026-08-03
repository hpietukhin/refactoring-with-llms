"""Minimal experiment runner driven by ``config.toml``."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from eliot import log_message

from config import (
    configure_eliot,
    experiments_output_dir,
    langgraph_config,
    settings,
)
from dataset import CaseRecord, Dataset
from repository.repo import worktree_path
from workflows.composite.graph import build_graph
from workflows.composite.models import CompositeWorkflowState


def initial_state_for_case(case: CaseRecord) -> CompositeWorkflowState:
    """Build LangGraph input from a manifest case and ``config.toml`` settings."""
    lg = langgraph_config()
    return {
        "project": case.project,
        "repo_url": case.repo_url,
        "current_commit": case.start_commit,
        "elements": list(case.elements),
        "repo_path": str(worktree_path(case.repo_url)),
        "max_steps": int(lg["max_steps"]),
        "model": str(lg["model"]),
        "timeout": int(lg["timeout_seconds"]),
    }


def summarize_result(case: CaseRecord, result: CompositeWorkflowState) -> dict[str, object]:
    """Keep a small JSON-serializable summary for disk."""
    smells = list(result.get("smells") or [])
    return {
        "case_id": case.case_id,
        "project": case.project,
        "stop_reason": result.get("stop_reason"),
        "step": result.get("step"),
        "smell_count": len(smells),
        "tests_failed": result.get("tests_failed"),
        "repo_path": result.get("repo_path"),
    }


def run_case(case: CaseRecord) -> dict[str, object]:
    """Run one case through the composite workflow and return a summary."""
    log_message(message_type="experiment:case_start", case_id=case.case_id, project=case.project)
    graph = build_graph()
    result = graph.invoke(initial_state_for_case(case))
    summary = summarize_result(case, result)
    log_message(message_type="experiment:case_done", **summary)
    return summary


def write_summary(summary: dict[str, object], output_dir: Path) -> Path:
    """Write one summary JSON file under the experiments output directory."""
    output_dir.mkdir(parents=True, exist_ok=True)
    case_id = str(summary["case_id"])
    safe = "".join(ch if ch.isalnum() or ch in "-._" else "_" for ch in case_id)
    path = output_dir / f"{safe}.json"
    path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return path


def select_cases(cases: list[CaseRecord], *, case_id: str | None, limit: int | None) -> list[CaseRecord]:
    selected = cases
    if case_id is not None:
        selected = [case for case in selected if case.case_id == case_id]
        if not selected:
            raise SystemExit(f"No case with case_id={case_id!r}")
    if limit is not None:
        selected = selected[:limit]
    return selected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-id", default=None, help="Run only this manifest case_id")
    parser.add_argument("--limit", type=int, default=None, help="Run at most N cases")
    args = parser.parse_args(argv)

    log_path = configure_eliot(settings)
    output_dir = experiments_output_dir()
    cases = select_cases(Dataset().load_cases(), case_id=args.case_id, limit=args.limit)

    log_message(
        message_type="experiment:start",
        cases=len(cases),
        log_file=str(log_path),
        output_dir=str(output_dir),
    )

    for case in cases:
        summary = run_case(case)
        path = write_summary(summary, output_dir)
        print(json.dumps({"wrote": str(path), **summary}, ensure_ascii=False))

    log_message(message_type="experiment:done", cases=len(cases))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
