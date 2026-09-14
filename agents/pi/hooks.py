"""Python hooks for the pi deep-refactor extension.

Commands:
  prepare --case-id <id> [--profile without-planning] [--manifest path]
  verify --repo-path <path> --elements <csv> --timeout <seconds> [--case-id <id>] [--run-log path]
  next-smell --repo-path <path> --elements <csv> --case-id <id> [--run-log path]
  complete --repo-path <path> --elements <csv> --timeout <seconds> --case-id <id>
           --stop-reason <reason> [--profile without-planning] [--run-log path]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from eliot import log_message

from agents.deep.invoke import remaining_target_feedback
from agents.deep.paths import virtual_path
from agents.deep.profiles import load_deep_agent_profile
from agents.deep.verification import run_automatic_verification
from agents.pi.logging_config import configure_pi_logging
from agents.pi.paths import (
    append_pi_event,
    create_pi_run_paths,
    pi_output_dir,
    pi_worktree_path,
    project_relative,
)
from agents.pi.smell_run import (
    SmellRunMetrics,
    SmellRunSync,
    append_metrics_to_content,
    initialize_smell_run,
    load_smell_run,
    metrics_from_queued_snapshot,
    next_queue_smell,
    run_dir_from_log,
    save_smell_run,
    smell_identity_key,
    sync_smell_run,
)
from java.organic_feedback import OrganicFeedback
from config import ROOT, langgraph_config, settings
from dataset import CaseRecord, Dataset
from java.metrics.ck_reports import run_ck_metrics
from repository.repo import Repo
from smell.smell import Smell

DEFAULT_PROFILE = "without-planning"

PI_GUIDELINES = """\
- Prefer a minimal edit that removes the reported smell.
- Each smell task includes inline refactoring advice; read it before editing.
- Read the reported line range before editing Java source.
- Inspect automatic verification feedback after every Java source edit.
- Before deleting, renaming, or inlining a Java type, inspect all source and test
  references, implementations, subclasses, and values assigned to that type.
  Replace it only with a type that accepts every existing value, and make the
  full migration before stopping.
- Do not add meaningless code or remove a useful shared interface only to raise
  a metric such as CLOC. Stop if no smaller safe design removes the smell.
- If Gradle reports compile errors after your edit, repair or restore the broken
  Java yourself until the tree compiles again.
- Do not invent unrelated members or make cosmetic-only changes.
- Never disable, ignore, remove, or comment out a test to make verification pass.
- Do not edit pom.xml, build.gradle, build.gradle.kts, or other build files.
- Do not use network search. Keep Java edits inside the case checkout; smell
  advice files are outside that checkout at each smell's `advice_path`.
"""

PI_WORKFLOW = """\
The harness sends smells one by one. Each user message contains exactly one smell.
Fix only that smell. Do not plan or edit other smells until the next message arrives.

For the current smell:
1. Read the inline refactoring advice and the reported source range.
2. If more context is necessary, locate and read only the relevant ranges.
3. Make one minimal source change with edit or write.
4. Inspect the automatic verification appended to that tool result before making
   another change.
5. Stop when this smell is fixed or no safe refactoring is possible. The harness
   will send the next smell afterward.
"""

PI_AUTOMATIC_CHECKS = """\
Automatic verification:
After every successful edit, write, deletion, move, script, or git command that
changes a *.java file under the case checkout, a Python hook runs Gradle
verification and the ORGANIC smell detector for the case elements. The results
are appended to the tool result under
"Automatic verification:". Rely on that feedback. Do not run Gradle or ORGANIC
yourself. Do not use bash to re-run builds, override Java versions, or probe the
toolchain.

Treat Gradle compilation errors after your edit as broken Java, not as a harness
or environment failure. Fix them before you continue the smell. Errors such as
"cannot find symbol", incompatible types, illegal starts, missing semicolons,
invalid overrides, private access, and constructor or method application errors
usually come from the edit. Prefer restoring the broken members with git restore
on the files you changed, then re-apply a smaller edit. Do not leave the tree
uncompilable and move on.

If a bash command times out, the case stays active. Inspect `git status` and
`git diff`, then restore only the named files or members that you changed. Never
restore the whole worktree. Make another Java edit and rely on automatic
verification. You may retry several times; do not loop on long `./gradlew test` /
integration-test commands in bash.

If Gradle or the JDK fails as a harness toolchain/environment failure, the
harness ends the whole experiment immediately. Do not edit further to retry the
build.

A one-line syntax error from your last edit may clear on the next Java edit.
Do not change pom.xml, build.gradle, build.gradle.kts, or other build files.
Fix only the reported smell in Java source, or stop if no safe refactoring is possible.
"""


def build_pi_system_prompt(
    system_instructions: str,
    *,
    repo_path: Path,
) -> str:
    """Build the system-prompt section appended by the pi extension."""
    return (
        "Your only objective is refactoring Java code to fix code smells. "
        "Automatic verification runs after each successful change to a *.java "
        "file. Prefer fixes that keep tests green, but stop when the "
        "harness reports a toolchain/environment failure or when no safe "
        "refactoring is possible — do not chase build-environment issues.\n\n"
        f"Repository checkout: {repo_path}\n"
        "Work only inside this checkout. Prefer paths relative to that root, or "
        "absolute paths under it.\n\n"
        f"Guidelines:\n{PI_GUIDELINES}\n"
        f"Workflow:\n{PI_WORKFLOW}\n"
        f"{PI_AUTOMATIC_CHECKS}\n"
        f"Profile instructions:\n{system_instructions}"
    )


def _read_smell_advice_text(smell: Smell) -> str:
    """Load bundled refactoring guidance for one smell."""
    advice_path = ROOT / smell.advice
    if not advice_path.is_file():
        return f"(advice file missing: {smell.advice.as_posix()})"
    return advice_path.read_text(encoding="utf-8")


def _smell_detail_block(repo: Repo, feedback: OrganicFeedback) -> str:
    """Format filtered ORGANIC context for a pi user message."""
    smell = feedback.smell
    source_range = smell.location.range
    relative = virtual_path(repo.path, smell.file_path).lstrip("/")
    lines = [
        f"type={smell.type}",
        f"severity={smell.severity}",
        f"owner={feedback.owner}",
        f"scope={feedback.scope.value}",
        f"file_path={relative}",
        f"start_line={source_range.start.line + 1}",
        f"start_character={source_range.start.character}",
        f"end_line={source_range.end.line + 1}",
        f"end_character={source_range.end.character}",
    ]
    if feedback.reason:
        lines.append(f"reason={feedback.reason}")
    if feedback.relevant_metrics:
        metrics = ", ".join(
            f"{name}={value}" for name, value in feedback.relevant_metrics
        )
        lines.append(f"relevant_metrics={metrics}")
    if feedback.related_owner:
        lines.append(f"related_owner={feedback.related_owner}")
    if feedback.nested_warning:
        lines.append(f"nested_warning={feedback.nested_warning}")
    lines.extend(
        (
            f"detected_by={smell.detected_by}",
            f"advice_path={smell.advice.as_posix()}",
        )
    )
    return "\n".join(lines)


def build_pi_smell_task(
    repo: Repo,
    feedback: OrganicFeedback,
    *,
    index: int,
    total: int,
) -> str:
    """Build a single-smell task with repo-relative paths for pi tools."""
    smell = feedback.smell
    advice_text = _read_smell_advice_text(smell)
    return (
        f"Repository checkout: {repo.path}\n\n"
        f"Smell {index} of {total}:\n\n"
        f"{_smell_detail_block(repo, feedback)}\n\n"
        f"Refactoring advice ({smell.advice.as_posix()}):\n\n"
        f"{advice_text}\n\n"
        "Focus only on this smell. Read the inline refactoring advice and the "
        "reported source range, make the minimal semantic edit, and inspect "
        "automatic verification. Do not work on other smells until the next task "
        "is sent."
    )


def build_pi_smell_tasks(
    repo: Repo,
    feedback: tuple[OrganicFeedback, ...],
) -> list[str]:
    """Build one task message per smell, in detection order."""
    total = len(feedback)
    return [
        build_pi_smell_task(repo, item, index=index, total=total)
        for index, item in enumerate(feedback, start=1)
    ]


def load_cases(manifest: str | None = None) -> list[CaseRecord]:
    """Load manifest cases from config or an explicit JSONL path."""
    if manifest is None:
        return Dataset().load_cases()
    return Dataset.from_manifest(manifest).load_cases()


def _prepare(
    case_id: str,
    profile_name: str,
    *,
    manifest: str | None = None,
) -> dict[str, object]:
    cases = [case for case in load_cases(manifest) if case.case_id == case_id]
    if not cases:
        raise SystemExit(f"No case with case_id={case_id!r}")
    case = cases[0]
    run_dir, run_log = create_pi_run_paths(case.repo_url)
    configure_pi_logging(str(run_log))
    profile = load_deep_agent_profile(profile_name)
    timeout = int(langgraph_config()["timeout_seconds"])
    checkout = pi_worktree_path(case.repo_url)
    repo = Repo.for_experiment(
        case.repo_url,
        case.start_commit,
        checkout_path=checkout,
    )
    elements = list(case.elements)
    feedback = remaining_target_feedback(repo, elements)
    smell_state = initialize_smell_run(
        run_dir,
        case_id=case.case_id,
        start_commit=case.start_commit,
        feedback=feedback,
    )
    tasks = build_pi_smell_tasks(repo, feedback) if feedback else []
    task = tasks[0] if tasks else ""
    current_smell_key = (
        smell_identity_key(feedback[0], case.start_commit) if feedback else ""
    )
    system_prompt = build_pi_system_prompt(
        profile.system_instructions,
        repo_path=repo.path,
    )
    output_dir = pi_output_dir()
    stop_reason = "smells_cleared" if not feedback else "ready"
    log_message(
        message_type="deep:case_start",
        case_id=case.case_id,
        elements=elements,
        repo_path=str(repo.path),
        profile=profile.name,
        harness="pi",
        delivery="one_by_one",
        smell_count=len(feedback),
    )
    if task:
        log_message(
            message_type="deep:prompt",
            case_id=case.case_id,
            profile=profile.name,
            system_prompt=system_prompt,
            task_prompt=task,
            harness="pi",
            smell_index=1,
            smell_count=len(feedback),
        )
    append_pi_event(
        "prepare",
        {
            "project": case.project,
            "profile": profile.name,
            "repo_path": project_relative(repo.path),
            "output_dir": project_relative(output_dir),
            "run_dir": project_relative(run_dir),
            "run_log": project_relative(run_log),
            "smell_count": len(feedback),
            "current_smell_key": current_smell_key,
            "stop_reason": stop_reason,
            "delivery": "one_by_one",
            "smells_json": project_relative(run_dir / "smells.json"),
        },
        case_id=case.case_id,
    )
    return {
        "case_id": case.case_id,
        "project": case.project,
        "repo_path": str(repo.path),
        "elements": elements,
        "timeout": timeout,
        "profile": profile.name,
        "system_instructions": profile.system_instructions,
        "system_prompt_append": system_prompt,
        "task": task,
        "tasks": tasks,
        "smell_count": len(feedback),
        "current_smell_key": current_smell_key,
        "stop_reason": stop_reason,
        "output_dir": str(output_dir),
        "run_dir": str(run_dir),
        "run_log": str(run_log),
    }


def parse_remaining_smells(content: str) -> int | None:
    """Parse ``remaining_smells=N`` from automatic verification content."""
    for line in content.splitlines():
        if line.startswith("remaining_smells="):
            raw = line.removeprefix("remaining_smells=").strip()
            try:
                return int(raw)
            except ValueError:
                return None
    return None


def parse_introduced_smells(content: str) -> int | None:
    """Parse ``introduced_smells=N`` from automatic verification content."""
    for line in content.splitlines():
        if line.startswith("introduced_smells="):
            raw = line.removeprefix("introduced_smells=").strip()
            try:
                return int(raw)
            except ValueError:
                return None
    return None


def _metrics_payload(metrics: SmellRunMetrics) -> dict[str, int]:
    return {
        "remaining_smells": metrics.remaining_smells,
        "introduced_smells": metrics.introduced_smells,
        "fixed_smells": metrics.fixed_smells,
        "original_smells": metrics.original_smells,
        "total_introduced_smells": metrics.total_introduced_smells,
        "fixed_introduced_smells": metrics.fixed_introduced_smells,
    }


def _introduced_smell_log_entry(
    repo: Repo,
    feedback: OrganicFeedback,
    *,
    start_commit: str,
) -> dict[str, object]:
    source_range = feedback.smell.location.range
    return {
        "identity_key": smell_identity_key(feedback, start_commit),
        "type": feedback.smell.type,
        "owner": feedback.owner,
        "scope": feedback.scope.value,
        "file_path": virtual_path(repo.path, feedback.smell.file_path).lstrip("/"),
        "start_line": source_range.start.line + 1,
        "end_line": source_range.end.line + 1,
        "reason": feedback.reason,
    }


def _append_newly_introduced_to_content(
    content: str,
    repo: Repo,
    sync: SmellRunSync,
    *,
    start_commit: str,
) -> str:
    if not sync.newly_introduced:
        return content
    lines = [content, "", "New ORGANIC smells (not in baseline):"]
    for item in sync.newly_introduced:
        entry = _introduced_smell_log_entry(repo, item, start_commit=start_commit)
        lines.append(
            f"{entry['type']}: /{entry['file_path']}:{entry['start_line']} "
            f"owner={entry['owner']}"
        )
    return "\n".join(lines)


def _log_newly_introduced_smells(
    repo: Repo,
    *,
    case_id: str | None,
    start_commit: str,
    sync: SmellRunSync,
) -> None:
    if not sync.newly_introduced:
        return
    smells = [
        _introduced_smell_log_entry(repo, item, start_commit=start_commit)
        for item in sync.newly_introduced
    ]
    log_message(
        message_type="pi:smells_introduced",
        case_id=case_id,
        harness="pi",
        introduced_smells=sync.metrics.introduced_smells,
        newly_introduced_count=len(sync.newly_introduced),
        smells=smells,
    )
    append_pi_event(
        "smells_introduced",
        {
            "repo_path": project_relative(repo.path),
            "introduced_smells": sync.metrics.introduced_smells,
            "newly_introduced_count": len(sync.newly_introduced),
            "smells": smells,
        },
        case_id=case_id,
    )


def _verify(
    repo_path: str,
    elements: list[str],
    timeout: int,
    *,
    case_id: str | None,
    run_log: str | None,
    current_smell_key: str | None = None,
) -> dict[str, object]:
    repo = Repo(repo_path)
    run_dir = run_dir_from_log(run_log)
    smell_state = load_smell_run(run_dir)
    feedback = run_automatic_verification(repo, elements=elements, timeout=timeout)
    content = feedback.content
    current_smell_present: bool | None = None
    metrics: SmellRunMetrics | None = None
    if feedback.tests_acceptable:
        current_feedback = feedback.remaining_feedback
        sync = sync_smell_run(smell_state, current_feedback)
        _log_newly_introduced_smells(
            repo,
            case_id=case_id,
            start_commit=smell_state.start_commit,
            sync=sync,
        )
        save_smell_run(run_dir, smell_state)
        metrics = sync.metrics
        content = append_metrics_to_content(content, metrics)
        content = _append_newly_introduced_to_content(
            content,
            repo,
            sync,
            start_commit=smell_state.start_commit,
        )
        if current_smell_key:
            current_smell_present = current_smell_key in {
                smell_identity_key(item, smell_state.start_commit)
                for item in current_feedback
            }
    elif smell_state.entries:
        metrics = metrics_from_queued_snapshot(smell_state)
        content = append_metrics_to_content(content, metrics)
    metric_fields = _metrics_payload(metrics) if metrics else {
        "remaining_smells": parse_remaining_smells(content),
        "introduced_smells": parse_introduced_smells(content),
        "fixed_smells": None,
        "original_smells": smell_state.original_smells,
    }
    log_message(
        message_type="pi:verify",
        case_id=case_id,
        repo_path=str(repo.path),
        passed=feedback.passed,
        tests_acceptable=feedback.tests_acceptable,
        test_status=feedback.test_status,
        flaky_tests=list(feedback.flaky_tests),
        current_smell_present=current_smell_present,
        fatal_environment_failure=feedback.fatal_environment_failure,
        failure_kind=feedback.failure_kind,
        content=content,
        harness="pi",
        **{
            key: value
            for key, value in metric_fields.items()
            if value is not None
        },
    )
    append_pi_event(
        "verify",
        {
            "repo_path": project_relative(repo.path),
            "passed": feedback.passed,
            "tests_acceptable": feedback.tests_acceptable,
            "test_status": feedback.test_status,
            "flaky_tests": list(feedback.flaky_tests),
            "current_smell_present": current_smell_present,
            "fatal_environment_failure": feedback.fatal_environment_failure,
            "failure_kind": feedback.failure_kind,
            "content": content,
            **{
                key: value
                for key, value in metric_fields.items()
                if value is not None
            },
        },
        case_id=case_id,
    )
    return {
        "content": content,
        "passed": feedback.passed,
        "tests_acceptable": feedback.tests_acceptable,
        "test_status": feedback.test_status,
        "flaky_tests": list(feedback.flaky_tests),
        "current_smell_present": current_smell_present,
        "fatal_environment_failure": feedback.fatal_environment_failure,
        "failure_kind": feedback.failure_kind,
        **metric_fields,
    }


def _next_smell(
    repo_path: str,
    _elements: list[str],
    *,
    case_id: str,
    run_log: str | None,
    skip_smell_keys: set[str] | None = None,
) -> dict[str, object]:
    """Select the next smell from the last synced ORGANIC snapshot."""
    repo = Repo(repo_path)
    run_dir = run_dir_from_log(run_log)
    smell_state = load_smell_run(run_dir)
    skipped = skip_smell_keys or set()
    next_feedback, sync = next_queue_smell(
        smell_state,
        repo_path=repo_path,
        skip_identity_keys=skipped,
    )
    metric_fields = _metrics_payload(sync.metrics)
    if next_feedback is None:
        stop_reason = (
            "smells_cleared"
            if sync.metrics.remaining_smells == 0
            else "smells_exhausted"
        )
        append_pi_event(
            "next_smell",
            {
                **metric_fields,
                "stop_reason": stop_reason,
                "smell_key": "",
            },
            case_id=case_id,
        )
        return {
            "task": "",
            **metric_fields,
            "stop_reason": stop_reason,
            "smell_key": "",
        }
    next_key = smell_identity_key(next_feedback, smell_state.start_commit)
    task = build_pi_smell_task(
        repo,
        next_feedback,
        index=sync.metrics.fixed_smells + 1,
        total=sync.metrics.original_smells + len(smell_state.introduced_logical_keys),
    )
    append_pi_event(
        "next_smell",
        {
            **metric_fields,
            "stop_reason": "ready",
            "next_type": next_feedback.smell.type,
            "smell_key": next_key,
        },
        case_id=case_id,
    )
    return {
        "task": task,
        **metric_fields,
        "stop_reason": "ready",
        "smell_key": next_key,
    }


def _usage_fields(usage: dict[str, object] | None) -> dict[str, object]:
    if not usage:
        return {}
    fields: dict[str, object] = {}
    for key in (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "total_tokens",
        "cost_usd",
        "turn_count",
    ):
        value = usage.get(key)
        if value is not None:
            fields[key] = value
    return fields


def log_case_completion_metrics(
    repo: Repo,
    *,
    case_id: str,
    timeout: int,
    metrics: SmellRunMetrics,
    stop_reason: str,
    profile: str,
    elements: list[str],
    usage: dict[str, object] | None = None,
) -> dict[str, object]:
    """Log case summary and CK metrics after a whole-case refactor finishes.

    Called once when the harness finishes a case (smells cleared or stopped),
    not after individual Java edits.
    """
    metric_fields = _metrics_payload(metrics)
    log_message(
        message_type="deep:case_summary",
        case_id=case_id,
        profile=profile,
        stop_reason=stop_reason,
        harness="pi",
        elements=elements,
        repo_path=str(repo.path),
        **metric_fields,
        **_usage_fields(usage),
    )
    try:
        summary = run_ck_metrics(repo, timeout=float(timeout))
    except (OSError, RuntimeError) as exc:
        log_message(
            message_type="pi:ck_failed",
            case_id=case_id,
            harness="pi",
            exception=str(exc),
            stop_reason=stop_reason,
        )
        append_pi_event(
            "complete",
            {
                "repo_path": project_relative(repo.path),
                "stop_reason": stop_reason,
                **metric_fields,
                "ck_error": str(exc),
            },
            case_id=case_id,
        )
        return {
            "case_id": case_id,
            "stop_reason": stop_reason,
            **metric_fields,
            "ck_error": str(exc),
            "ck": None,
        }

    totals = summary.totals
    ck_payload: dict[str, object] = {
        "success": summary.success,
        "exit_code": summary.exit_code,
        "classes": totals.classes,
        "methods": totals.methods,
        "mean_cbo": totals.mean_cbo,
        "mean_lcom": totals.mean_lcom,
        "mean_wmc": totals.mean_wmc,
        "total_loc": totals.total_loc,
    }
    log_message(
        message_type="pi:ck",
        case_id=case_id,
        harness="pi",
        stop_reason=stop_reason,
        repo_path=str(repo.path),
        **metric_fields,
        **ck_payload,
    )
    append_pi_event(
        "complete",
        {
            "repo_path": project_relative(repo.path),
            "stop_reason": stop_reason,
            **metric_fields,
            "ck": ck_payload,
        },
        case_id=case_id,
    )
    return {
        "case_id": case_id,
        "stop_reason": stop_reason,
        **metric_fields,
        "ck_error": None,
        "ck": ck_payload,
    }


def _complete(
    repo_path: str,
    elements: list[str],
    timeout: int,
    *,
    case_id: str,
    stop_reason: str,
    profile: str,
    run_log: str | None,
    usage: dict[str, object] | None = None,
) -> dict[str, object]:
    """Finalize a pi case: count remaining smells, log Eliot CK metrics."""
    repo = Repo(repo_path)
    run_dir = run_dir_from_log(run_log)
    smell_state = load_smell_run(run_dir)
    current_feedback = remaining_target_feedback(repo, elements)
    sync = sync_smell_run(smell_state, current_feedback)
    _log_newly_introduced_smells(
        repo,
        case_id=case_id,
        start_commit=smell_state.start_commit,
        sync=sync,
    )
    save_smell_run(run_dir, smell_state)
    return log_case_completion_metrics(
        repo,
        case_id=case_id,
        timeout=timeout,
        metrics=sync.metrics,
        stop_reason=stop_reason,
        profile=profile,
        elements=elements,
        usage=usage,
    )


def _parse_elements(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def _parse_string_array(raw: str) -> set[str]:
    """Parse a JSON array containing only strings."""
    value: object = json.loads(raw)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise argparse.ArgumentTypeError("expected a JSON array of strings")
    return set(value)


def _parse_json_object(raw: str) -> dict[str, object]:
    """Parse a JSON object payload."""
    value: object = json.loads(raw)
    if not isinstance(value, dict):
        raise argparse.ArgumentTypeError("expected a JSON object")
    return value


def _add_run_log_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--run-log",
        default=None,
        help="Per-run Eliot log file created by prepare (data/pi/runs/{repo}/{uuid}/all.log)",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="Checkout a dataset case and build the task")
    prepare.add_argument("--case-id", required=True)
    prepare.add_argument("--profile", default=DEFAULT_PROFILE)
    prepare.add_argument(
        "--manifest",
        default=None,
        help="JSONL manifest path (default: config.toml [dataset].manifest)",
    )

    verify = sub.add_parser("verify", help="Run Gradle + ORGANIC automatic verification")
    verify.add_argument("--repo-path", required=True)
    verify.add_argument("--elements", required=True)
    verify.add_argument("--timeout", type=int, required=True)
    verify.add_argument("--case-id", default=None)
    verify.add_argument("--current-smell-key", default=None)
    _add_run_log_argument(verify)

    nxt = sub.add_parser(
        "next-smell",
        help="Pick the next smell from the last synced ORGANIC snapshot",
    )
    nxt.add_argument("--repo-path", required=True)
    nxt.add_argument("--elements", required=True)
    nxt.add_argument("--case-id", required=True)
    nxt.add_argument("--skip-smell-keys", type=_parse_string_array, default=set())
    _add_run_log_argument(nxt)

    complete = sub.add_parser(
        "complete",
        help="Log case summary + CK metrics after whole-case refactoring finishes",
    )
    complete.add_argument("--repo-path", required=True)
    complete.add_argument("--elements", required=True)
    complete.add_argument("--timeout", type=int, required=True)
    complete.add_argument("--case-id", required=True)
    complete.add_argument("--stop-reason", required=True)
    complete.add_argument("--profile", default=DEFAULT_PROFILE)
    complete.add_argument(
        "--usage-json",
        type=_parse_json_object,
        default=None,
        help="Optional pi session token/cost totals from the TypeScript harness",
    )
    _add_run_log_argument(complete)

    args = parser.parse_args(argv)
    if args.command != "prepare":
        configure_pi_logging(args.run_log)
    match args.command:
        case "prepare":
            payload = _prepare(args.case_id, args.profile, manifest=args.manifest)
        case "verify":
            payload = _verify(
                args.repo_path,
                _parse_elements(args.elements),
                args.timeout,
                case_id=args.case_id,
                run_log=args.run_log,
                current_smell_key=args.current_smell_key,
            )
        case "next-smell":
            payload = _next_smell(
                args.repo_path,
                _parse_elements(args.elements),
                case_id=args.case_id,
                run_log=args.run_log,
                skip_smell_keys=args.skip_smell_keys,
            )
        case "complete":
            payload = _complete(
                args.repo_path,
                _parse_elements(args.elements),
                args.timeout,
                case_id=args.case_id,
                stop_reason=args.stop_reason,
                profile=args.profile,
                run_log=args.run_log,
                usage=args.usage_json,
            )
        case _:
            raise SystemExit(f"Unknown command: {args.command}")
    json.dump(payload, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
