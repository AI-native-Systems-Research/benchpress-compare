"""A small machine-readable result: the headline N2 and D2 scores and one run state.

``result.json`` is the file a script or search loop reads. It is derived from the
authoritative report model (``summary.json``) once a run has collected, and from
the run's ``state/status.json`` before that, so it exists in every phase of a
run and always says how its two numbers should be read.
"""

from __future__ import annotations

from pathlib import Path

from ..aggregate import METRICS
from ..identity import read_json
from ..weights import read_fraction

RESULT_SCHEMA_VERSION = 1
# Every value ``state`` can take, with its meaning. The README repeats this table.
RESULT_STATES = {
    "not_started": "The results directory has no run state yet.",
    "preparing": ("Builds, fixtures and the plan are being prepared; nothing has been measured."),
    "frozen": ("The plan is frozen but collection has not started; resume the run to collect."),
    "running": "Observations are being collected; N2 and D2 are not available yet.",
    "preparation_failed": (
        "A build, fixture or planning step failed; the explanation gives the reason."
    ),
    "interrupted": (
        "The controller was interrupted; committed results are kept and resume continues."
    ),
    "needs_reconciliation": (
        "Some scheduler jobs are in an uncertain state; resume reconciles them first."
    ),
    "cancelled": "The run was cancelled before every observation was collected; there is no score.",
    "complete": "Every planned observation succeeded; N2 and D2 are the headline scores.",
    "complete_with_exclusions": (
        "The baseline failed on some inputs, which were left out with zero weight; "
        "N2 and D2 cover the remaining circuits."
    ),
    "evolve_failed": (
        "Evolve failed where the baseline succeeded, so the main score is unavailable; "
        "the partial score leaves the failed circuits out."
    ),
    "unscored": "Collection ended but no score could be computed; the explanation lists why.",
}
SCORED_STATES = frozenset({"complete", "complete_with_exclusions"})
# Run states in which no report exists yet or the report must not be read as a score.
_RUN_STATE_TO_RESULT = {
    "NEW": "not_started",
    "PREPARING": "preparing",
    "FROZEN": "frozen",
    "RUNNING": "running",
    "PREPARATION_FAILED": "preparation_failed",
    "INTERRUPTED": "interrupted",
    "NEEDS_RECONCILIATION": "needs_reconciliation",
    "CANCELLED": "cancelled",
}
_COLLECTED = frozenset({"COMPLETE", "COMPLETE_WITH_FAILURES"})
_BASELINE_DROP_REASONS = frozenset({"baseline_failed", "baseline_missing"})
_SUCCEEDED = "SUCCEEDED"


def read_run_state(results_root: str | Path) -> dict | None:
    """The controller's ``state/status.json``, or ``None`` before the first state."""
    path = Path(results_root) / "state/status.json"
    return read_json(path) if path.exists() else None


def build_result(
    report: dict | None, state: dict | None = None, request: dict | None = None
) -> dict:
    """Reduce a report model and the live run state to the small result record.

    ``state`` is the ``state/status.json`` record, possibly with the progress
    counts ``orchestrator.status`` adds; it decides the run state. Without it the
    report's own status is used. ``request`` (``request.json``) names the profile
    when there is no report yet.
    """
    if state:
        run_status = str(state.get("status", "UNKNOWN"))
        execution = state
    elif report is not None:
        run_status = str(report.get("status", "UNKNOWN"))
        execution = report.get("execution") or {}
    else:
        run_status, execution = "NEW", {}
    request = request or {}
    scores = {metric: None for metric in METRICS}
    partial = None
    dropped = 0
    query = {}
    counts = {
        "planned": execution.get("expected_observations"),
        "completed": execution.get("completed_observations"),
        "succeeded": execution.get("succeeded"),
    }
    if report is None:
        name = _RUN_STATE_TO_RESULT.get(run_status, "unscored")
        explanation = _explain_without_report(name, run_status, execution, counts)
    elif run_status in _RUN_STATE_TO_RESULT:
        name = _RUN_STATE_TO_RESULT[run_status]
        query = report.get("query", {})
        counts = _counts(report)
        explanation = _explain_without_report(name, run_status, execution, counts)
    else:
        query = report.get("query", {})
        counts = _counts(report)
        dropped = _dropped_for_baseline(report)
        name = _collected_state(report, dropped)
        if name in SCORED_STATES:
            scores = {metric: report["summary"]["metrics"][metric]["score"] for metric in METRICS}
        partial = _partial(report) if name == "evolve_failed" else None
        explanation = _explain_collected(name, report, dropped, partial, counts)
    design = report["design"] if report is not None else request.get("design")
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "state": name,
        "scored": name in SCORED_STATES,
        "N2": scores["N2"],
        "D2": scores["D2"],
        "partial": partial,
        "explanation": explanation,
        "run_status": run_status,
        "profile": report["profile"] if report is not None else request.get("profile"),
        "design": design,
        "score_scope": report["score_scope"] if report is not None else _scope(design),
        "plan_hash": report["plan_hash"] if report is not None else request.get("plan_hash"),
        "query": query,
        "observations": counts,
        "dropped_inputs": dropped,
        "updated_at": execution.get("updated_at"),
    }


def read_result(results_root: str | Path) -> dict:
    """The result for a results directory in any phase, without writing anything.

    A collected run is read from its saved ``reports/summary.json``; when that
    file is missing the report is rebuilt in memory from the committed
    observations. Before collection ends only the run state is consulted.
    """
    root = Path(results_root)
    state = read_run_state(root)
    run_status = (state or {}).get("status", "NEW")
    summary = root / "reports/summary.json"
    if run_status in _COLLECTED and summary.exists():
        return build_result(read_json(summary), state)
    request = {}
    if (root / "request.json").exists():
        request = dict(read_json(root / "request.json"))
    if (root / "plan.json").exists():
        from ..orchestrator import read_results, status, verify_plan

        if run_status in _COLLECTED:
            from ..eligibility import apply_admission
            from .model import build_report

            results = read_results(root)
            plan = apply_admission(verify_plan(root, check_harness=False), results)
            return build_result(build_report(plan, results, status=status(root)))
        state = status(root)
        request["plan_hash"] = verify_plan(root, check_harness=False)["plan_hash"]
    return build_result(None, state, request)


def _scope(design: str | None) -> str | None:
    if design is None:
        return None
    from .model import score_scope

    return score_scope(design)


def _counts(report: dict) -> dict:
    summary = report["summary"]
    cases = summary["cases"] + summary["supplemental_cases"]
    rows = report.get("observations", [])
    return {
        "planned": sum(case["metrics"]["N2"]["expected_observations"] for case in cases),
        "completed": len(rows),
        "succeeded": sum(str(row.get("status", "")).upper() == _SUCCEEDED for row in rows),
    }


def _dropped_for_baseline(report: dict) -> int:
    """Inputs the admission left out because their baseline failed or is missing.

    Structural duplicates are also dropped, but by design rather than failure.
    """
    admission = report.get("admission") or {}
    return sum(
        row.get("status") == "dropped"
        and bool(_BASELINE_DROP_REASONS & set(row.get("reasons", [])))
        for row in admission.get("records", [])
    )


def _collected_state(report: dict, dropped: int) -> str:
    failures = report.get("failures", {})
    by_arm = failures.get("by_arm", {})
    if any("CANCELLED" in counts for counts in by_arm.values()):
        return "cancelled"
    metrics = report["summary"]["metrics"]
    if all(metrics[metric]["available"] for metric in METRICS):
        return "complete_with_exclusions" if dropped else "complete"
    if failures.get("evolve_only"):
        return "evolve_failed"
    return "unscored"


def _partial(report: dict) -> dict | None:
    without = report.get("score_without_evolve_failures") or {}
    if not without.get("evolve_failed_observations"):
        return None
    return {
        "N2": without["metrics"]["N2"]["score"],
        "D2": without["metrics"]["D2"]["score"],
        "excluded_inputs": len(without["excluded_inputs"]),
        "excluded_weight": float(read_fraction(without["excluded_weight"])),
    }


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


def _explain_collected(
    name: str, report: dict, dropped: int, partial: dict | None, counts: dict
) -> str:
    summary = report["summary"]
    circuits = summary["expected_circuits"]
    if name == "complete":
        return (
            f"All {counts['planned']} planned observations succeeded; N2 and D2 are the "
            f"headline scores over {_plural(circuits, 'circuit')}."
        )
    if name == "complete_with_exclusions":
        return (
            f"The baseline failed on {_plural(dropped, 'input')}, which were left out with zero "
            f"weight; N2 and D2 cover the remaining {_plural(circuits, 'circuit')}."
        )
    if name == "evolve_failed":
        failed = report["failures"]["evolve_only"]
        excluded = partial["excluded_inputs"] if partial else 0
        weight = partial["excluded_weight"] if partial else 0.0
        text = (
            f"Evolve failed on {_plural(len(failed), 'observation')} across "
            f"{_plural(excluded, 'circuit')} carrying {100 * weight:.1f}% of the weight where the "
            "baseline succeeded; the main score is unavailable and the partial score leaves "
            "those circuits out."
        )
    elif name == "cancelled":
        cancelled = sum(
            by_status.get("CANCELLED", 0) for by_status in report["failures"]["by_arm"].values()
        )
        text = (
            f"The run was cancelled; {_plural(cancelled, 'observation')} were cancelled and "
            "there is no score. Resume with --retry-cancelled to collect them."
        )
    else:
        reasons = sorted(
            {reason for metric in METRICS for reason in summary["metrics"][metric]["reasons"]}
        )
        parts = []
        weights_error = (report.get("admission") or {}).get("weights_error")
        if weights_error:
            parts.append(weights_error)
        if reasons:
            parts.append("case reasons: " + ", ".join(reasons))
        text = "No score could be computed" + (": " + "; ".join(parts) if parts else "") + "."
    if dropped:
        text += (
            f" The baseline also failed on {_plural(dropped, 'input')}, left out with zero weight."
        )
    return text


def _explain_without_report(name: str, run_status: str, execution: dict, counts: dict) -> str:
    if name == "running" and counts["planned"] is not None:
        return (
            f"{counts['completed'] or 0} of {counts['planned']} observations collected; "
            "N2 and D2 are not available yet."
        )
    if name == "preparation_failed" and execution.get("reason"):
        return f"Preparation failed: {execution['reason']}"
    if name == "needs_reconciliation" and execution.get("affected"):
        return (
            f"{_plural(len(execution['affected']), 'job')} in an uncertain scheduler state; "
            "resume the run to reconcile them."
        )
    if name == "unscored":
        return f"The run state is {run_status} but no report is available."
    return RESULT_STATES[name]
