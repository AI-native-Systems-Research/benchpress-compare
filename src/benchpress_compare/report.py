"""Deterministic JSON, Markdown and CSV views of committed observations."""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable, Mapping

from .aggregate import METRICS, aggregate, normalize_query, select_cases
from .identity import atomic_write_json, fsync_directory
from .weights import fraction_record, read_fraction

REPORT_PROTOCOL = "benchpress-report-v1"


def _compact(summary: dict) -> dict:
    return {key: value for key, value in summary.items() if key not in ("cases", "circuits", "supplemental_cases")}


def build_report(
    plan: dict,
    observations: Iterable[dict],
    *,
    query: Mapping | None = None,
    status: str | dict | None = None,
    group_by: Iterable[str] | None = None,
) -> dict:
    cases = plan.get("cases", [])
    observations = list(observations)
    normalized = normalize_query(query)
    summary = aggregate(cases, observations, query=normalized)
    sensitivity_cases = [{**case, "sensitivity_weight": case.get("sensitivity_weight", case["weight"])} for case in cases]
    sensitivity = aggregate(sensitivity_cases, observations, query=normalized, weight_field="sensitivity_weight")
    selected = select_cases(cases, normalized)
    groups = {}
    dimensions = [("level",), ("target_class",), ("family",), ("band",), ("family", "band"), ("family", "target_class"), ("input_id",)]
    if group_by is not None:
        fields = []
        for dimension in group_by:
            value = 2 if dimension == "level" else "_"
            fields.append(next(iter(normalize_query({dimension: [value]}))))
        dimensions = [tuple(fields)]
        if not dimensions[0]:
            raise ValueError("group_by needs at least one dimension")
    for fields in dimensions:
        combinations = sorted({tuple(case.get(field) for field in fields) for case in selected}, key=lambda values: tuple(str(value) for value in values))
        rows = []
        for values in combinations:
            group_query = dict(normalized)
            group_query.update({field: [value] for field, value in zip(fields, values)})
            row = _compact(aggregate(cases, observations, query=group_query))
            row["group"] = dict(zip(fields, values))
            rows.append(row)
        groups["/".join(fields)] = rows
    rankings = {}
    for metric in METRICS:
        available = [row for row in summary["cases"] if row["metrics"][metric]["available"]]
        order = sorted(available, key=lambda row: (read_fraction(row["metrics"][metric]["ratio"]), row["case_id"]))
        rankings[metric] = {
            "improvements": [{"case_id": row["case_id"], "input_id": row["input_id"], "ratio": row["metrics"][metric]["ratio"]} for row in order if read_fraction(row["metrics"][metric]["ratio"]) < 1][:10],
            "regressions": [{"case_id": row["case_id"], "input_id": row["input_id"], "ratio": row["metrics"][metric]["ratio"]} for row in reversed(order) if read_fraction(row["metrics"][metric]["ratio"]) > 1][:10],
        }
    crossed = [item["input_id"] for item in summary["circuits"] if {item["metrics"][metric]["classification"] for metric in METRICS} == {"better", "worse"}]
    selected_ids = {case["case_id"] for case in selected}
    relevant = [item for item in observations if item.get("case_id") in selected_ids and item.get("phase", "comparison") == "comparison"]
    validations = Counter()
    scopes = Counter()
    routing = []
    resources = []
    for item in relevant:
        validation = item.get("validation", {})
        validations[validation.get("status", "missing") if isinstance(validation, dict) else str(validation)] += 1
        if isinstance(validation, dict):
            checks = validation.get("checks", [])
            if isinstance(checks, dict):
                checks = list(checks.values())
            for check in checks:
                if isinstance(check, dict):
                    scope = check.get("scope", check.get("name", "unspecified"))
                    scopes[str(scope)] += 1
        diagnostic = item.get("diagnostics", {})
        if not isinstance(diagnostic, dict):
            diagnostic = {}
        routing.append({
            "observation_id": item.get("observation_id"), "case_id": item["case_id"], "arm": item["arm"], "seed": item["seed"],
            "sabre_executed": item.get("sabre_executed", diagnostic.get("sabre_executed")),
            "inserted_swaps": item.get("inserted_swaps", diagnostic.get("inserted_swaps")),
            "routing_burden": item.get("routing_burden", diagnostic.get("routing_burden")),
        })
        if item.get("resources") or item.get("telemetry"):
            resources.append({"observation_id": item.get("observation_id"), "arm": item["arm"], **item.get("resources", item.get("telemetry", {}))})
    routing.sort(key=lambda row: (row["case_id"], row["arm"], row["seed"]))
    resources.sort(key=lambda row: (str(row.get("observation_id")), row["arm"]))
    routing_summary = {arm: {"sabre_executed": 0, "sabre_bypassed": 0, "unknown": 0, "inserted_swaps_sum": 0, "inserted_swaps_observations": 0} for arm in ("baseline", "evolve")}
    for row in routing:
        tally = routing_summary[row["arm"]]
        executed = row["sabre_executed"]
        tally["sabre_executed" if executed is True else "sabre_bypassed" if executed is False else "unknown"] += 1
        count = row["inserted_swaps"]
        if type(count) is int and count >= 0:
            tally["inserted_swaps_sum"] += count
            tally["inserted_swaps_observations"] += 1
    input_weights = defaultdict(lambda: 0)
    for case in selected:
        input_weights[case["input_id"]] += read_fraction(case["weight"])
    concentrations = [{"input_id": key, "weight": fraction_record(value)} for key, value in sorted(input_weights.items(), key=lambda pair: (-pair[1], pair[0])) if value > 0]
    observation_rows = [{key: value for key, value in item.items() if key != "artifacts"} for item in relevant]
    observation_rows.sort(key=lambda row: (row["case_id"], row["arm"], row["seed"]))
    execution = dict(status) if isinstance(status, dict) else {}
    status_code = execution.get("status", "UNKNOWN") if execution else status or plan.get("status", "UNKNOWN")
    return {
        "schema_version": 1,
        "protocol": REPORT_PROTOCOL,
        "plan_hash": plan.get("plan_hash"),
        "design": plan.get("design", plan.get("config", {}).get("design", "benchpress-eight-family-v1")),
        "status": status_code,
        "execution": execution,
        "query": normalized,
        "provenance": {key: plan.get(key) for key in ("builds", "sources", "corpus", "protocols", "runtime_profiles", "seed_algorithm")},
        "summary": summary,
        "sensitivity": _compact(sensitivity),
        "groups": groups,
        "rankings": rankings,
        "crossed_metric_circuits": crossed,
        "validation_coverage": {"statuses": dict(sorted(validations.items())), "scopes": dict(sorted(scopes.items()))},
        "routing_diagnostics": routing,
        "routing_summary": routing_summary,
        "observed_resources": resources,
        "pilot": plan.get("pilot"),
        "coverage": plan.get("coverage"),
        "weights_audit": plan.get("weights_audit", plan.get("weights")),
        "concentrations": concentrations,
        "conditional_on_unresolved": bool(plan.get("conditional_on_unresolved", plan.get("coverage", {}).get("conditional_on_unresolved", False) if isinstance(plan.get("coverage"), dict) else False)),
        "observations": observation_rows,
        "notes": [
            "N2 and D2 are independent ratio-of-arithmetic-seed-means scores; lower is better.",
            "All planned seeds and positive ratios are required. Missing data never changes frozen weights.",
            "Seed spread is descriptive; this report makes no significance or acceptance-threshold claim.",
        ],
    }


def _escape(value) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _display(value, digits: int = 6) -> str:
    return "unavailable" if value is None else f"{value:.{digits}g}"


def render_markdown(report: dict) -> str:
    summary = report["summary"]
    lines = ["# Benchpress comparison", "", f"Status: **{_escape(report['status'])}**. Design: `{_escape(report['design'])}`.", ""]
    if "smoke" in report["design"]:
        lines += ["This is a smoke subset; it does not establish the complete eight-family score.", ""]
    if report["conditional_on_unresolved"]:
        lines += ["The cohort is conditional on unresolved screening candidates; see coverage gaps.", ""]
    lines += [
        f"Expected: {summary['expected_circuits']} canonical circuits, {summary['expected_cases']} cases, {summary['expected_observations']} observations. "
        f"Received: {summary['received_observations']}; successful: {summary['successful_observations']}; failed: {summary['failed_observations']}; cache hits: {summary['cache_hits']}.",
        "", f"Selected frozen weight mass: `{read_fraction(summary['weight_mass'])}`. Query: `{_escape(json.dumps(report['query'], sort_keys=True))}`.", "",
        "| Metric | Score | Change % | Better | Same | Worse | Unclassifiable |", "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for metric in METRICS:
        result = summary["metrics"][metric]
        counts = result["circuit_counts"]
        lines.append(f"| {metric} | {_display(result['score'])} | {_display(result['change_percent'])} | {counts['better']} | {counts['same']} | {counts['worse']} | {counts['unclassifiable']} |")
    lines += ["", "Equal-band sensitivity:", ""]
    for metric in METRICS:
        result = report["sensitivity"]["metrics"][metric]
        lines.append(f"- {metric}: {_display(result['score'])}; change {_display(result['change_percent'])}%" if result["available"] else f"- {metric}: unavailable ({_escape(', '.join(result['reasons']))})")
    for metric in METRICS:
        reasons = summary["metrics"][metric]["reasons"]
        if reasons:
            lines += ["", f"{metric} blocking reasons: {_escape(json.dumps(reasons, sort_keys=True))}."]
    lines += ["", "## Slices", "", "| Group | N2 | D2 | Circuits | Cases | Weight |", "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for name, rows in report["groups"].items():
        if name == "input_id":
            continue
        for row in rows:
            label = ", ".join(f"{key}={value}" for key, value in row["group"].items())
            lines.append(f"| {_escape(label)} | {_display(row['metrics']['N2']['score'])} | {_display(row['metrics']['D2']['score'])} | {row['expected_circuits']} | {row['expected_cases']} | {read_fraction(row['weight_mass'])} |")
    lines += ["", "## Validation and coverage", "", f"Validation statuses: `{_escape(json.dumps(report['validation_coverage']['statuses'], sort_keys=True))}`.", "", f"Validation scopes: `{_escape(json.dumps(report['validation_coverage']['scopes'], sort_keys=True))}`.", ""]
    if report.get("coverage"):
        lines += ["Coverage and exclusions are retained in `summary.json` under `coverage`.", ""]
    if report["concentrations"]:
        lines += ["Largest input allocations (fraction of the frozen main score):", ""]
        for item in report["concentrations"][:5]:
            lines.append(f"- `{_escape(item['input_id'])}`: {100 * float(read_fraction(item['weight'])):.6g}%.")
    if report["crossed_metric_circuits"]:
        lines += ["", "Opposite N2/D2 directions: " + ", ".join(f"`{_escape(key)}`" for key in report["crossed_metric_circuits"]) + "."]
    lines += ["", "## Circuit summaries", "", "| Canonical input | N2 ratio | N2 classification | D2 ratio | D2 classification |", "| --- | ---: | --- | ---: | --- |"]
    for item in summary["circuits"]:
        n2, d2 = item["metrics"]["N2"], item["metrics"]["D2"]
        lines.append(f"| {_escape(item['input_id'])} | {_display(n2['score'])} | {n2['classification']} | {_display(d2['score'])} | {d2['classification']} |")
    lines += ["", "## Largest changes", ""]
    for metric, rankings in report["rankings"].items():
        for direction, items in rankings.items():
            label = f"{metric} {direction}"
            if not items:
                lines += [f"{label}: none with a usable positive ratio.", ""]
                continue
            lines += [f"{label}:", ""]
            for item in items:
                lines.append(f"- `{_escape(item['input_id'])}` / `{_escape(item['case_id'])}`: {_display(float(read_fraction(item['ratio'])))}.")
            lines.append("")
    lines += ["## Routing and resource diagnostics", "", "| Arm | SABRE executed | SABRE bypassed | Unknown | Inserted SWAP sum | Observations with SWAP evidence |", "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for arm, tally in report["routing_summary"].items():
        lines.append(f"| {arm} | {tally['sabre_executed']} | {tally['sabre_bypassed']} | {tally['unknown']} | {tally['inserted_swaps_sum']} | {tally['inserted_swaps_observations']} |")
    lines += ["", "SWAP sums are diagnostics over received observations and are not an N2/D2 score.", ""]
    for arm in ("baseline", "evolve"):
        resources = [row for row in report["observed_resources"] if row["arm"] == arm]
        elapsed = [row["elapsed_seconds"] for row in resources if isinstance(row.get("elapsed_seconds"), (int, float))]
        memory = [row["peak_rss_bytes"] for row in resources if isinstance(row.get("peak_rss_bytes"), (int, float))]
        lines.append(f"- {arm}: resource receipts {len(resources)}; total observed seconds {_display(sum(elapsed)) if elapsed else 'unavailable'}; peak process-tree RSS bytes {max(memory) if memory else 'unavailable'}.")
    lines += ["", "Pilot predictions, runtime profiles, build provenance, and artifact references are retained in `summary.json`. Timings are resource-planning diagnostics."]
    lines += ["", "## Reproduction and detailed data", "", "`summary.json` is authoritative. `cases.csv` contains exact means, ratios and raw per-seed measurements; `observations.csv` retains accepted observation outcomes.", "", "```bash", "benchpress-compare status --results-root /path/to/results", "benchpress-compare resume --results-root /path/to/results", "benchpress-compare report --results-root /path/to/results", "```", ""]
    lines += [f"- {note}" for note in report["notes"]]
    return "\n".join(lines) + "\n"


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _csv(fields: list[str], rows: Iterable[dict]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: json.dumps(row[field], sort_keys=True, allow_nan=False, separators=(",", ":")) if isinstance(row.get(field), (dict, list)) else row.get(field) for field in fields})
    return stream.getvalue()


def write_report(results_root: str | Path, report: dict) -> dict[str, str]:
    destination = Path(results_root) / "reports"
    destination.mkdir(parents=True, exist_ok=True)
    # Serialization first rejects accidental NaN/Infinity in any authoritative field.
    json.dumps(report, allow_nan=False)
    atomic_write_json(destination / "summary.json", report)
    _atomic_text(destination / "report.md", render_markdown(report))
    rows = []
    for case in report["summary"]["cases"] + report["summary"]["supplemental_cases"]:
        for metric in METRICS:
            data = case["metrics"][metric]
            rows.append({**{key: value for key, value in case.items() if key != "metrics"}, "metric": metric, **data})
    fields = ["case_id", "input_id", "target_class", "target_id", "level", "family", "band", "recipe", "active_width", "declared_width", "occupancy", "weight", "metric", "available", "baseline_mean", "evolve_mean", "ratio", "ratio_display", "absolute_direction", "reasons", "raw", "spread"]
    _atomic_text(destination / "cases.csv", _csv(fields, rows))
    fields = ["observation_id", "case_id", "arm", "seed", "status", "N2", "D2", "metrics", "metric_reasons", "cache_hit", "reason", "validation", "diagnostics", "resources", "accepted_attempt"]
    _atomic_text(destination / "observations.csv", _csv(fields, report["observations"]))
    return {name: str(destination / name) for name in ("report.md", "summary.json", "cases.csv", "observations.csv")}
