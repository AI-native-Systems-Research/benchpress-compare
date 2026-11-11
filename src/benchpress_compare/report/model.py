"""Build the authoritative report model from a frozen plan and committed observations.

The model is plain JSON data. Everything else in this package (Markdown, CSV,
charts) is a view derived from it, so ``summary.json`` stays the single source
of truth and the views can be regenerated offline at any time.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from fractions import Fraction
from typing import Iterable, Mapping

from ..aggregate import METRICS, aggregate, normalize_query, select_cases
from ..validation import REQUIRED_CHECKS
from ..weights import fraction_record, read_fraction

REPORT_PROTOCOL = "benchpress-report-v1"
DEFAULT_DESIGN = "benchpress-eight-family-v1"
SUBSET_DESIGNS = frozenset({"benchpress-evolve-v1", "benchpress-small-v1"})
ARMS = ("baseline", "evolve")
RANKING_SIZE = 10
VALIDATION_SCOPE_NOTE = (
    "Validation covers native target legality only. Accepted circuits are compatible "
    "with the target; their behavior has not been compared with the input circuit."
)
NOTES = (
    "N2 and D2 are independent ratio-of-arithmetic-seed-means scores; lower is better.",
    "Inputs are dropped only when their own baseline failed; evolve results never change weights.",
    "An evolve failure where baseline succeeded stays in the score and makes it unavailable.",
    "Seed spread is descriptive; this report makes no significance or acceptance-threshold claim.",
)
# Slices reported by default. ``input_id`` gives one row per circuit for drill-down.
SLICE_DIMENSIONS = (
    ("level",),
    ("target_class",),
    ("family",),
    ("band",),
    ("family", "band"),
    ("family", "target_class"),
    ("input_id",),
)
_SUCCEEDED = "SUCCEEDED"


def build_report(
    plan: dict,
    observations: Iterable[dict],
    *,
    query: Mapping | None = None,
    status: str | dict | None = None,
    group_by: Iterable[str] | None = None,
) -> dict:
    """Score the planned cases and collect every diagnostic the views need."""
    cases = plan.get("cases", [])
    observations = list(observations)
    normalized = normalize_query(query)
    selected = select_cases(cases, normalized)
    summary = aggregate(cases, observations, query=normalized)
    relevant = _relevant_observations(observations, selected)
    evolve_failures = _evolve_only_failures(relevant, selected)
    routing, routing_summary = _routing(relevant)
    status_code, execution = _execution_status(status, plan)
    design = plan.get("design", plan.get("config", {}).get("design", DEFAULT_DESIGN))
    return {
        "schema_version": 1,
        "protocol": REPORT_PROTOCOL,
        "plan_hash": plan.get("plan_hash"),
        "profile": plan.get("profile", plan.get("config", {}).get("profile", "full")),
        "profile_manifest_hash": plan.get("profile_manifest_hash"),
        "design": design,
        "score_scope": score_scope(design),
        "status": status_code,
        "execution": execution,
        "query": normalized,
        "provenance": {
            key: plan.get(key)
            for key in (
                "builds",
                "sources",
                "corpus",
                "protocols",
                "runtime_profiles",
                "seed_algorithm",
                "profile_manifest",
            )
        },
        "summary": summary,
        "sensitivity": _equal_band_sensitivity(cases, observations, normalized),
        "groups": _slices(cases, observations, normalized, selected, _dimensions(group_by)),
        "rankings": _rankings(summary),
        "crossed_metric_circuits": _crossed_metric_circuits(summary),
        "validation_coverage": _validation_coverage(relevant),
        "routing_diagnostics": routing,
        "routing_summary": routing_summary,
        "observed_resources": _resources(relevant),
        "admission": plan.get("admission"),
        "failures": {"by_arm": _failures_by_arm(relevant), "evolve_only": evolve_failures},
        "score_without_evolve_failures": _score_without_evolve_failures(
            cases, observations, normalized, selected, evolve_failures
        ),
        "weights_audit": plan.get("weights_audit", plan.get("weights")),
        "concentrations": _concentrations(selected),
        "observations": _observation_rows(relevant),
        "notes": list(NOTES),
    }


def score_scope(design: str) -> str:
    if design in SUBSET_DESIGNS:
        return "subset_search_signal"
    return "smoke" if "smoke" in design else "benchmark"


def _compact(summary: dict) -> dict:
    """A summary without its per-case and per-circuit detail rows."""
    return {
        key: value
        for key, value in summary.items()
        if key not in ("cases", "circuits", "supplemental_cases")
    }


def _execution_status(status, plan: dict) -> tuple[str, dict]:
    execution = dict(status) if isinstance(status, dict) else {}
    if execution:
        return execution.get("status", "UNKNOWN"), execution
    return status or plan.get("status", "UNKNOWN"), execution


def _equal_band_sensitivity(cases: list[dict], observations: list[dict], query: dict) -> dict:
    """Re-score with every band in a family weighted equally."""
    sensitivity_cases = [
        {**case, "sensitivity_weight": case.get("sensitivity_weight", case["weight"])}
        for case in cases
    ]
    return _compact(
        aggregate(sensitivity_cases, observations, query=query, weight_field="sensitivity_weight")
    )


def _dimensions(group_by: Iterable[str] | None) -> tuple[tuple[str, ...], ...]:
    if group_by is None:
        return SLICE_DIMENSIONS
    fields = []
    for dimension in group_by:
        probe = 2 if dimension == "level" else "_"
        fields.append(next(iter(normalize_query({dimension: [probe]}))))
    if not fields:
        raise ValueError("group_by needs at least one dimension")
    return (tuple(fields),)


def _slices(
    cases: list[dict],
    observations: list[dict],
    query: dict,
    selected: list[dict],
    dimensions: tuple[tuple[str, ...], ...],
) -> dict[str, list[dict]]:
    """Score every value combination of each dimension that occurs in the selection."""
    groups = {}
    for fields in dimensions:
        combinations = sorted(
            {tuple(case.get(field) for field in fields) for case in selected},
            key=lambda values: tuple(str(value) for value in values),
        )
        rows = []
        for values in combinations:
            group_query = dict(query)
            group_query.update({field: [value] for field, value in zip(fields, values)})
            row = _compact(aggregate(cases, observations, query=group_query))
            row["group"] = dict(zip(fields, values))
            rows.append(row)
        groups["/".join(fields)] = rows
    return groups


def _rankings(summary: dict) -> dict[str, dict[str, list[dict]]]:
    """The cases with the smallest and largest ratios per metric."""
    rankings = {}
    for metric in METRICS:
        available = [row for row in summary["cases"] if row["metrics"][metric]["available"]]
        order = sorted(
            available,
            key=lambda row: (read_fraction(row["metrics"][metric]["ratio"]), row["case_id"]),
        )

        def entry(row: dict) -> dict:
            return {
                "case_id": row["case_id"],
                "input_id": row["input_id"],
                "ratio": row["metrics"][metric]["ratio"],
            }

        rankings[metric] = {
            "improvements": [
                entry(row) for row in order if read_fraction(row["metrics"][metric]["ratio"]) < 1
            ][:RANKING_SIZE],
            "regressions": [
                entry(row)
                for row in reversed(order)
                if read_fraction(row["metrics"][metric]["ratio"]) > 1
            ][:RANKING_SIZE],
        }
    return rankings


def _crossed_metric_circuits(summary: dict) -> list[str]:
    """Circuits where one metric improves while the other regresses."""
    return [
        item["input_id"]
        for item in summary["circuits"]
        if {item["metrics"][metric]["classification"] for metric in METRICS} == {"better", "worse"}
    ]


def _relevant_observations(observations: list[dict], selected: list[dict]) -> list[dict]:
    selected_ids = {case["case_id"] for case in selected}
    return [
        item
        for item in observations
        if item.get("case_id") in selected_ids and item.get("phase", "comparison") == "comparison"
    ]


def _validation_coverage(relevant: list[dict]) -> dict:
    statuses: Counter = Counter()
    scopes: Counter = Counter()
    for item in relevant:
        validation = item.get("validation", {})
        if not isinstance(validation, dict):
            statuses[str(validation)] += 1
            continue
        statuses[validation.get("status", "missing")] += 1
        checks = validation.get("checks", [])
        if isinstance(checks, dict):
            checks = [
                dict(check, name=name) if isinstance(check, dict) else check
                for name, check in checks.items()
            ]
        for check in checks:
            if isinstance(check, dict):
                scopes[str(check.get("scope", check.get("name", "unspecified")))] += 1
    return {
        "statuses": dict(sorted(statuses.items())),
        "scopes": dict(sorted(scopes.items())),
        "required_checks": list(REQUIRED_CHECKS),
        "scope_note": VALIDATION_SCOPE_NOTE,
    }


def _routing(relevant: list[dict]) -> tuple[list[dict], dict[str, dict[str, int]]]:
    """Per-observation SABRE evidence and the per-arm tallies derived from it."""
    rows = []
    for item in relevant:
        diagnostic = item.get("diagnostics", {})
        if not isinstance(diagnostic, dict):
            diagnostic = {}
        rows.append(
            {
                "observation_id": item.get("observation_id"),
                "case_id": item["case_id"],
                "arm": item["arm"],
                "seed": item["seed"],
                "sabre_executed": item.get("sabre_executed", diagnostic.get("sabre_executed")),
                "inserted_swaps": item.get("inserted_swaps", diagnostic.get("inserted_swaps")),
                "routing_burden": item.get("routing_burden", diagnostic.get("routing_burden")),
            }
        )
    rows.sort(key=lambda row: (row["case_id"], row["arm"], row["seed"]))
    tallies = {
        arm: {
            "sabre_executed": 0,
            "sabre_bypassed": 0,
            "unknown": 0,
            "inserted_swaps_sum": 0,
            "inserted_swaps_observations": 0,
        }
        for arm in ARMS
    }
    for row in rows:
        tally = tallies[row["arm"]]
        executed = row["sabre_executed"]
        if executed is True:
            tally["sabre_executed"] += 1
        elif executed is False:
            tally["sabre_bypassed"] += 1
        else:
            tally["unknown"] += 1
        count = row["inserted_swaps"]
        if type(count) is int and count >= 0:
            tally["inserted_swaps_sum"] += count
            tally["inserted_swaps_observations"] += 1
    return rows, tallies


def _resources(relevant: list[dict]) -> list[dict]:
    rows = [
        {
            "observation_id": item.get("observation_id"),
            "arm": item["arm"],
            **item.get("resources", item.get("telemetry", {})),
        }
        for item in relevant
        if item.get("resources") or item.get("telemetry")
    ]
    rows.sort(key=lambda row: (str(row.get("observation_id")), row["arm"]))
    return rows


def _concentrations(selected: list[dict]) -> list[dict]:
    """Weight carried by each input, largest first."""
    weights: dict[str, Fraction] = defaultdict(Fraction)
    for case in selected:
        weights[case["input_id"]] += read_fraction(case["weight"])
    return [
        {"input_id": key, "weight": fraction_record(value)}
        for key, value in sorted(weights.items(), key=lambda pair: (-pair[1], pair[0]))
        if value > 0
    ]


def _status(item: dict) -> str:
    return str(item.get("status", "")).upper()


def _failures_by_arm(relevant: list[dict]) -> dict[str, dict[str, int]]:
    failures = {arm: Counter() for arm in ARMS}
    for item in relevant:
        if _status(item) != _SUCCEEDED and item.get("arm") in failures:
            failures[item["arm"]][_status(item)] += 1
    return {arm: dict(sorted(counts.items())) for arm, counts in failures.items()}


def _evolve_only_failures(relevant: list[dict], selected: list[dict]) -> list[dict]:
    """Weighted evolve observations that failed where the baseline seed succeeded."""
    succeeded_baseline = {
        (item["case_id"], item["seed"])
        for item in relevant
        if item.get("arm") == "baseline" and _status(item) == _SUCCEEDED
    }
    weighted = {case["case_id"]: case for case in selected if read_fraction(case["weight"]) > 0}
    return sorted(
        (
            {
                "input_id": weighted[item["case_id"]]["input_id"],
                "case_id": item["case_id"],
                "target_class": weighted[item["case_id"]].get("target_class"),
                "level": weighted[item["case_id"]].get("level"),
                "seed": item["seed"],
                "status": item.get("status"),
                "reason": item.get("reason"),
            }
            for item in relevant
            if item.get("arm") == "evolve"
            and _status(item) != _SUCCEEDED
            and item["case_id"] in weighted
            and (item["case_id"], item["seed"]) in succeeded_baseline
        ),
        key=lambda row: (row["input_id"], row["case_id"], row["seed"]),
    )


def _score_without_evolve_failures(
    cases: list[dict],
    observations: list[dict],
    query: dict,
    selected: list[dict],
    evolve_failures: list[dict],
) -> dict:
    """A labelled partial score that leaves out circuits with evolve failures.

    The main score stays unavailable while any evolve run failed; this secondary
    score must be read together with how much weight it leaves out.
    """
    failed_inputs = sorted({row["input_id"] for row in evolve_failures})
    partial = aggregate(
        [case for case in cases if case["input_id"] not in failed_inputs],
        observations,
        query=query,
    )
    excluded_weight = sum(
        (read_fraction(case["weight"]) for case in selected if case["input_id"] in failed_inputs),
        Fraction(),
    )
    return {
        "excluded_inputs": failed_inputs,
        "excluded_weight": fraction_record(excluded_weight),
        "evolve_failed_observations": len(evolve_failures),
        "metrics": {
            metric: {
                key: partial["metrics"][metric][key]
                for key in ("available", "score", "change_percent", "reasons")
            }
            for metric in METRICS
        },
    }


def _observation_rows(relevant: list[dict]) -> list[dict]:
    rows = [{key: value for key, value in item.items() if key != "artifacts"} for item in relevant]
    rows.sort(key=lambda row: (row["case_id"], row["arm"], row["seed"]))
    return rows
