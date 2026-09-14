"""Java verification nodes for the composite workflow."""

from __future__ import annotations

from eliot import log_message

from agents.java_test.analysis import run_java_test_analysis
from agents.java_test.models import TestRunSummary
from java.metrics.ck_reports import run_ck_metrics
from repository.repo import Repo
from workflows.composite.models import CompositeWorkflowState


def _tests_failed_from_result(result: dict[str, object]) -> bool:
    if result.get("error"):
        return True
    summary = result.get("summary")
    if summary is None:
        return True
    if isinstance(summary, TestRunSummary):
        return not summary.success
    return True


def _ck_state(repo_path: str, *, timeout: int) -> CompositeWorkflowState:
    """Collect CK metrics for a checkout that just passed Java tests."""
    try:
        summary = run_ck_metrics(Repo(repo_path), timeout=float(timeout))
    except (OSError, RuntimeError) as exc:
        log_message(message_type="java_verification:ck_failed", exception=str(exc))
        return {
            "ck_classes": None,
            "ck_mean_cbo": None,
            "ck_mean_lcom": None,
            "ck_mean_wmc": None,
            "ck_total_loc": None,
        }
    totals = summary.totals
    log_message(
        message_type="java_verification:ck",
        success=summary.success,
        exit_code=summary.exit_code,
        classes=totals.classes,
        mean_cbo=totals.mean_cbo,
        mean_lcom=totals.mean_lcom,
        mean_wmc=totals.mean_wmc,
        total_loc=totals.total_loc,
    )
    return {
        "ck_classes": totals.classes,
        "ck_mean_cbo": totals.mean_cbo,
        "ck_mean_lcom": totals.mean_lcom,
        "ck_mean_wmc": totals.mean_wmc,
        "ck_total_loc": totals.total_loc,
    }


def java_verification(state: CompositeWorkflowState) -> CompositeWorkflowState:
    """Run Maven tests; on success, collect CK metrics for the checkout."""
    repo_path = state.get("repo_path")
    if not repo_path:
        raise ValueError("java_verification requires state['repo_path']")
    timeout = int(state.get("timeout") or 120)
    result = run_java_test_analysis(
        repo_path,
        clean=True,
        timeout=timeout,
        code_agent_model=state.get("model") or None,
    )
    failed = _tests_failed_from_result(result)
    log_message(
        message_type="java_verification:result",
        failed=failed,
        error=result.get("error"),
    )
    update: CompositeWorkflowState = {"tests_failed": failed}
    if not failed:
        update.update(_ck_state(repo_path, timeout=timeout))
    return update


def route_after_java_tests(state: CompositeWorkflowState) -> str:
    """Route after java_tests: retry refactor or continue to replan."""
    if state.get("tests_failed", False):
        retries_used = int(state.get("retries_used") or 0)
        max_retries = int(state.get("max_retries") or 1)
        if retries_used < max_retries:
            return "refactor_smell"
        return "replan_after_action"
    return "replan_after_action"
