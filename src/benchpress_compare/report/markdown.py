"""Readable Markdown view of the report model.

The page leads with the headline scores, then widens: coverage, slices,
circuits, the largest changes, failures and diagnostics. Every number is a
rounded display of a value held exactly in ``summary.json``.
"""

from __future__ import annotations

import json
from typing import Mapping, Sequence

from ..aggregate import METRICS
from ..weights import FAMILIES, TARGETS, read_fraction
from .charts import CHART_BY_NAME, build_charts
from .common import (
    BAND_LABELS,
    BAND_ORDER,
    FAMILY_NAMES,
    LEVEL_ORDER,
    case_index,
    circuit_rows,
    circuit_sort_key,
    group_rows,
    ordered,
)
from .model import SUBSET_DESIGNS

SECTIONS = (
    "Headline",
    "Coverage and admission",
    "Slices",
    "Circuits",
    "Largest changes",
    "Failures",
    "Routing and resource diagnostics",
    "Reproduction and detailed data",
)
CASE_ID_DISPLAY = 12


def render_markdown(
    report: dict,
    charts: Mapping[str, str] | None = None,
    vectors: Mapping[str, str] | None = None,
    raster_note: str | None = None,
) -> str:
    """Render the report.

    ``charts`` maps chart names to the relative path of the image to embed;
    ``vectors`` maps the charts embedded as PNG to their SVG, which is linked
    from the caption; ``raster_note`` explains a missing PNG render.
    """
    if charts is None:
        charts = {name: f"charts/{name}.svg" for name in build_charts(report)}
    view = _View(report, charts, vectors or {}, raster_note)
    sections = (
        _title(view),
        _headline(view),
        _coverage(view),
        _slices(view),
        _circuits(view),
        _largest_changes(view),
        _failures(view),
        _diagnostics(view),
        _reproduction(view),
    )
    return "\n".join(line for section in sections for line in section).rstrip("\n") + "\n"


class _View:
    """The report plus the derived rows every section shares."""

    def __init__(
        self,
        report: dict,
        charts: Mapping[str, str],
        vectors: Mapping[str, str],
        raster_note: str | None,
    ):
        self.report = report
        self.charts = charts
        self.vectors = vectors
        self.raster_note = raster_note
        self.summary = report["summary"]
        self.circuits = sorted(circuit_rows(report), key=circuit_sort_key)
        self.cases = case_index(report)


# -- formatting ---------------------------------------------------------------


def _escape(value) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _code(value) -> str:
    return f"`{_escape(value)}`"


def _score(value) -> str:
    return "unavailable" if value is None else f"{value:.4f}"


def _change(value) -> str:
    return "unavailable" if value is None else f"{value:+.2f}"


def _ratio(record) -> str:
    return f"{float(read_fraction(record)):.4f}"


def _percent(fraction) -> str:
    return f"{100 * float(fraction):.3g}%"


def _weight(record) -> str:
    fraction = read_fraction(record)
    return f"{fraction} ({_percent(fraction)})"


def _duration(seconds: float) -> str:
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 60:.1f} min" if seconds >= 60 else f"{seconds:.1f} s"


def _bytes(count: float) -> str:
    return f"{count / 2**30:.2f} GiB" if count >= 2**30 else f"{count / 2**20:.0f} MiB"


def _anchor(title: str) -> str:
    return "#" + title.lower().replace(" ", "-")


def _heading(title: str) -> list[str]:
    return [f"## {title}", ""]


def _table(headers: Sequence[str], rows: Sequence[Sequence], align: str) -> list[str]:
    """A Markdown table; ``align`` holds one ``l``/``r`` per column."""
    separator = ["---:" if kind == "r" else "---" for kind in align]
    lines = [
        "| " + " | ".join(_escape(cell) for cell in headers) + " |",
        "| " + " | ".join(separator) + " |",
    ]
    lines += ["| " + " | ".join(_escape(cell) for cell in row) + " |" for row in rows]
    return lines + [""]


def _figure(view: _View, name: str) -> list[str]:
    if name not in view.charts:
        return []
    chart = CHART_BY_NAME[name]
    caption = f"*{chart.caption}*"
    if name in view.vectors:
        caption += f" [SVG]({view.vectors[name]})"
    return [f"![{chart.title}]({view.charts[name]})", "", caption, ""]


def _metric_lines(metrics: dict, fallback: str) -> list[str]:
    lines = []
    for metric in METRICS:
        result = metrics[metric]
        if result["available"]:
            lines.append(
                f"- {metric}: {_score(result['score'])}; "
                f"change {_change(result['change_percent'])}%"
            )
        else:
            lines.append(
                f"- {metric}: unavailable ({_escape(', '.join(result['reasons']) or fallback)})"
            )
    return lines


# -- sections -----------------------------------------------------------------


def _title(view: _View) -> list[str]:
    report = view.report
    lines = [
        "# Benchpress comparison",
        "",
        f"Status: **{_escape(report['status'])}**. Design: {_code(report['design'])}. "
        f"Profile: {_code(report.get('profile') or 'unknown')}."
        + (
            f" Plan: {_code(str(report['plan_hash'])[:CASE_ID_DISPLAY])}."
            if report.get("plan_hash")
            else ""
        ),
        "",
        report["validation_coverage"]["scope_note"],
        "",
    ]
    if "smoke" in report["design"]:
        lines += [
            "This is a smoke subset; it does not establish the complete eight-family score.",
            "",
        ]
    elif report["design"] in SUBSET_DESIGNS:
        lines += [
            "This subset score is a search signal. Confirm any improvement on the full "
            "profile before making a benchmark claim.",
            "",
        ]
        if report["design"] == "benchpress-small-v1":
            lines += [
                "The small profile is a quick screening check with no large-band inputs; "
                "evaluate passing candidates on the evolve profile.",
                "",
            ]
    lines += ["Contents: " + " · ".join(f"[{title}]({_anchor(title)})" for title in SECTIONS), ""]
    return lines


def _headline(view: _View) -> list[str]:
    report, summary = view.report, view.summary
    lines = _heading("Headline")
    rows = []
    for metric in METRICS:
        result = summary["metrics"][metric]
        counts = result["circuit_counts"]
        rows.append(
            (
                metric,
                _score(result["score"]),
                _change(result["change_percent"]),
                counts["better"],
                counts["same"],
                counts["worse"],
                counts["unclassifiable"],
            )
        )
    lines += _table(
        ("Metric", "Score", "Change %", "Better", "Same", "Worse", "Unclassifiable"),
        rows,
        "lrrrrrr",
    )
    lines += [
        f"N2 counts native two-qubit gates and D2 is the two-qubit depth. A score is the "
        f"weighted geometric mean of per-case `evolve / baseline` ratios over "
        f"{summary['expected_circuits']} circuits and {summary['expected_cases']} cases; below 1 "
        "is better. Better, same and worse count circuits by the exact product of their "
        "case ratios.",
        "",
    ]
    for metric in METRICS:
        reasons = summary["metrics"][metric]["reasons"]
        if reasons:
            lines += [
                f"{metric} blocking reasons: {_code(json.dumps(reasons, sort_keys=True))}.",
                "",
            ]
    lines += _figure(view, "slice_change")
    partial = report["score_without_evolve_failures"]
    if partial["excluded_inputs"]:
        lines += [
            f"Score without evolve-failed circuits (PARTIAL: leaves out "
            f"{len(partial['excluded_inputs'])} circuits, "
            f"{_percent(read_fraction(partial['excluded_weight']))} of the weight, where "
            f"{partial['evolve_failed_observations']} evolve runs failed; see Failures):",
            "",
            *_metric_lines(partial["metrics"], "no scored cases remain"),
            "",
        ]
    sensitivity = report["sensitivity"]["metrics"]
    lines += [
        "Equal-band sensitivity re-scores the same cases with every band in a family weighted "
        "equally instead of the frozen S/M/L priorities:",
        "",
    ]
    lines += _table(
        ("Metric", "Frozen weights", "Equal-band weights"),
        [
            (
                metric,
                _score_with_change(summary["metrics"][metric]),
                _score_with_change(sensitivity[metric]),
            )
            for metric in METRICS
        ],
        "lrr",
    )
    return lines


def _score_with_change(result: dict) -> str:
    if not result["available"]:
        return "unavailable" + (
            f" ({_escape(', '.join(result['reasons']))})" if result["reasons"] else ""
        )
    return f"{_score(result['score'])} ({_change(result['change_percent'])}%)"


def _coverage(view: _View) -> list[str]:
    report, summary = view.report, view.summary
    lines = _heading("Coverage and admission")
    admission = report.get("admission")
    if admission:
        lines += [
            f"Admission ({_code(admission['policy'])}): {len(admission['admitted'])} of "
            f"{admission['candidate_count']} inputs scored; {len(admission['dropped'])} dropped "
            "because their baseline failed or they duplicate a scored structural variant.",
            "",
        ]
        if admission.get("weights_error"):
            lines += [f"No weights could be assigned: {_escape(admission['weights_error'])}.", ""]
    lines += _table(
        ("Quantity", "Value"),
        [
            ("Canonical circuits", summary["expected_circuits"]),
            ("Cases (circuit × target × level)", summary["expected_cases"]),
            ("Observations expected", summary["expected_observations"]),
            ("Observations received", summary["received_observations"]),
            (
                "Successful / failed",
                f"{summary['successful_observations']} / {summary['failed_observations']}",
            ),
            ("Cache hits", summary["cache_hits"]),
            ("Selected frozen weight mass", read_fraction(summary["weight_mass"])),
            ("Query", _code(json.dumps(report["query"], sort_keys=True))),
        ],
        "lr",
    )
    coverage = report["validation_coverage"]
    receipts = sum(coverage["statuses"].values())
    lines += [
        f"Validation receipts cover {receipts} received observations, including those of "
        "zero-weight inputs. Required certificates: "
        + (", ".join(_code(check) for check in coverage.get("required_checks", [])) or "none")
        + ". Every accepted observation requires verified native target legality.",
        "",
    ]
    lines += _table(
        ("Validation", "Observations"),
        [(f"status {_code(key)}", value) for key, value in coverage["statuses"].items()]
        + [(f"scope {_code(key)}", value) for key, value in coverage["scopes"].items()],
        "lr",
    )
    circuits = {row["input_id"]: row for row in view.circuits}
    if report["concentrations"]:
        lines += ["Largest input allocations (share of the frozen main score):", ""]
        rows = []
        for item in report["concentrations"][:8]:
            info = circuits.get(item["input_id"], {})
            rows.append(
                (
                    _code(item["input_id"]),
                    info.get("family") or "",
                    info.get("band") or "",
                    _weight(item["weight"]),
                )
            )
        lines += _table(("Input", "Family", "Band", "Weight"), rows, "lllr")
    if admission and admission["dropped"]:
        lines += ["Dropped inputs (zero weight; listed for completeness):", ""]
        rows = [
            (
                _code(record["input_id"]),
                ", ".join(record["reasons"]),
                _code(record["representative"]) if record.get("representative") else "",
            )
            for record in admission["records"]
            if record["status"] == "dropped"
        ]
        lines += _table(("Input", "Reason", "Scored representative"), rows, "lll")
    return lines


def _slice_table(view: _View, dimension: str, order, label, header: str, extra=None) -> list[str]:
    rows = group_rows(view.report, dimension, order)
    if not rows:
        return []
    table = []
    for row in rows:
        value = row["group"][dimension]
        cells = [label(value)]
        if extra:
            cells.append(extra(value))
        for metric in METRICS:
            result = row["metrics"][metric]
            cells += [_score(result["score"]), _change(result["change_percent"])]
        cells += [
            row["expected_circuits"],
            row["expected_cases"],
            read_fraction(row["weight_mass"]),
        ]
        table.append(cells)
    headers = [header] + (["Description"] if extra else [])
    headers += ["N2", "N2 change %", "D2", "D2 change %", "Circuits", "Cases", "Weight"]
    return _table(headers, table, "l" * (2 if extra else 1) + "rrrrrrr")


def _pivot(view: _View, dimension: str, order, header: str) -> list[str]:
    """Family rows against ``dimension`` columns; each cell shows ``N2 / D2``."""
    rows = view.report["groups"].get(f"family/{dimension}")
    if not rows:
        return []
    families = ordered((row["group"]["family"] for row in rows), FAMILIES)
    columns = ordered((row["group"][dimension] for row in rows), order)
    lookup = {(row["group"]["family"], row["group"][dimension]): row for row in rows}
    table = []
    for family in families:
        cells = [family]
        for column in columns:
            row = lookup.get((family, column))
            if row is None:
                cells.append("–")
            else:
                cells.append(
                    " / ".join(_score(row["metrics"][metric]["score"]) for metric in METRICS)
                )
        table.append(cells)
    return (
        _table(["Family"] + [str(column) for column in columns], table, "l" + "r" * len(columns))
        if columns
        else []
    )


def _slices(view: _View) -> list[str]:
    report = view.report
    if not any(report["groups"].get(name) for name in ("level", "target_class", "band", "family")):
        return []
    lines = _heading("Slices")
    lines += [
        "Each slice is scored on its own frozen weight mass; the weight of absent or dropped "
        "inputs is never redistributed. Change is relative to baseline, so negative is better.",
        "",
    ]
    for dimension, order, label, header, extra in (
        ("level", LEVEL_ORDER, lambda v: f"level {v}", "Optimization level", None),
        ("target_class", TARGETS, str, "Target class", None),
        ("band", BAND_ORDER, lambda v: BAND_LABELS.get(v, str(v)), "Width band", None),
        ("family", FAMILIES, str, "Family", lambda v: FAMILY_NAMES.get(v, "")),
    ):
        table = _slice_table(view, dimension, order, label, header, extra)
        if table:
            lines += [f"### By {header.lower()}", ""] + table
    pivots = _pivot(view, "band", BAND_ORDER, "band") + _pivot(
        view, "target_class", TARGETS, "target"
    )
    if pivots:
        lines += [
            "### Family by band and by target",
            "",
            "Cells show the N2 / D2 scores of the family within that band or target class; "
            "– marks a combination with no inputs.",
            "",
        ]
        lines += pivots
    return lines


def _circuits(view: _View) -> list[str]:
    circuits = view.circuits
    if not circuits:
        return []
    lines = _heading("Circuits")
    classes = [{m: row["metrics"][m]["classification"] for m in METRICS} for row in circuits]
    both_better = sum(all(c == "better" for c in row.values()) for row in classes)
    both_worse = sum(all(c == "worse" for c in row.values()) for row in classes)
    unclassifiable = sum(any(c == "unclassifiable" for c in row.values()) for row in classes)
    opposite = len(view.report["crossed_metric_circuits"])
    other = len(circuits) - both_better - both_worse - unclassifiable - opposite
    lines += [
        f"Of {len(circuits)} scored circuits, both metrics improve on {both_better}, both regress "
        f"on {both_worse}, and {opposite} move in opposite directions"
        + (f"; {other} have one unchanged metric" if other else "")
        + (f"; {unclassifiable} are unclassifiable" if unclassifiable else "")
        + ". Each circuit's ratio is the geometric mean of its case ratios.",
        "",
    ]
    lines += _figure(view, "circuit_outcomes")
    lines += _figure(view, "change_spread")
    rows = []
    for row in circuits:
        n2, d2 = row["metrics"]["N2"], row["metrics"]["D2"]
        rows.append(
            (
                row.get("family") or "",
                row.get("band") or "",
                row.get("active_width") if row.get("active_width") is not None else "",
                _code(row["input_id"]),
                _score(n2["score"]),
                n2["classification"],
                _score(d2["score"]),
                d2["classification"],
            )
        )
    lines += _table(
        ("Family", "Band", "Width", "Input", "N2 ratio", "N2", "D2 ratio", "D2"),
        rows,
        "llrlrlrl",
    )
    crossed = view.report["crossed_metric_circuits"]
    if crossed:
        lines += [
            "<details>",
            f"<summary>Circuits with opposite N2 and D2 directions ({len(crossed)})</summary>",
            "",
            *(f"- {_code(key)}" for key in crossed),
            "",
            "</details>",
            "",
        ]
    return lines


def _ranking_table(view: _View, items: list[dict]) -> list[str]:
    rows = []
    for item in items:
        case = view.cases.get(item["case_id"], {})
        rows.append(
            (
                _code(item["input_id"]),
                case.get("target_class") or "",
                f"L{case['level']}" if case.get("level") is not None else "",
                _ratio(item["ratio"]),
                _code(item["case_id"][:CASE_ID_DISPLAY]),
            )
        )
    return _table(("Input", "Target", "Level", "Ratio", "Case"), rows, "lllrl")


def _largest_changes(view: _View) -> list[str]:
    lines = _heading("Largest changes")
    lines += _figure(view, "largest_changes")
    lines += [
        "Per case (one circuit on one target at one optimization level), ranked by ratio. "
        f"The case column holds the first {CASE_ID_DISPLAY} characters of the ids in `cases.csv`.",
        "",
    ]
    for metric in METRICS:
        rankings = view.report["rankings"].get(metric, {})
        for direction in ("improvements", "regressions"):
            items = rankings.get(direction, [])
            lines += [f"### {metric} {direction}", ""]
            if items:
                lines += _ranking_table(view, items)
            else:
                lines += [f"{metric} {direction}: none with a usable positive ratio.", ""]
    return lines


def _failures(view: _View) -> list[str]:
    report = view.report
    lines = _heading("Failures")
    lines += _table(
        ("Arm", "Failed observations"),
        [
            (arm, ", ".join(f"{_escape(key)} {value}" for key, value in counts.items()) or "none")
            for arm, counts in report["failures"]["by_arm"].items()
        ],
        "ll",
    )
    evolve_only = report["failures"]["evolve_only"]
    lines += [
        f"Evolve failed where baseline succeeded: {len(evolve_only)} observations"
        + (" (these count against evolve and are never dropped):" if evolve_only else "."),
        "",
    ]
    if evolve_only:
        lines += _table(
            ("Input", "Target", "Level", "Seed", "Status", "Reason"),
            [
                (
                    _code(row["input_id"]),
                    row["target_class"],
                    f"L{row['level']}",
                    row["seed"],
                    row["status"],
                    row["reason"],
                )
                for row in evolve_only
            ],
            "llllll",
        )
    admission = report.get("admission")
    if admission and admission["dropped"]:
        lines += [
            f"{len(admission['dropped'])} inputs were dropped at admission; see "
            f"[Coverage and admission]({_anchor('Coverage and admission')}).",
            "",
        ]
    return lines


def _diagnostics(view: _View) -> list[str]:
    report = view.report
    lines = _heading("Routing and resource diagnostics")
    lines += _table(
        (
            "Arm",
            "SABRE executed",
            "SABRE bypassed",
            "Unknown",
            "Inserted SWAP sum",
            "Observations with SWAP evidence",
        ),
        [
            (
                arm,
                tally["sabre_executed"],
                tally["sabre_bypassed"],
                tally["unknown"],
                tally["inserted_swaps_sum"],
                tally["inserted_swaps_observations"],
            )
            for arm, tally in report["routing_summary"].items()
        ],
        "lrrrrr",
    )
    lines += [
        "SWAP sums are diagnostics over received observations and are not an N2/D2 score.",
        "",
    ]
    rows = []
    for arm in ("baseline", "evolve"):
        resources = [row for row in report["observed_resources"] if row["arm"] == arm]
        elapsed = [
            r["elapsed_seconds"]
            for r in resources
            if isinstance(r.get("elapsed_seconds"), (int, float))
        ]
        memory = [
            r["peak_rss_bytes"]
            for r in resources
            if isinstance(r.get("peak_rss_bytes"), (int, float))
        ]
        rows.append(
            (
                arm,
                len(resources),
                f"{_duration(sum(elapsed))} ({sum(elapsed):.1f} s)" if elapsed else "unavailable",
                f"{_bytes(max(memory))} ({max(memory)} bytes)" if memory else "unavailable",
            )
        )
    lines += _table(
        ("Arm", "Resource receipts", "Observed compute time", "Peak process-tree RSS"), rows, "lrrr"
    )
    lines += [
        "Admission records, runtime profiles, build provenance, and artifact references are "
        "retained in `summary.json`. Timings are resource-planning diagnostics.",
        "",
    ]
    return lines


def _reproduction(view: _View) -> list[str]:
    lines = _heading("Reproduction and detailed data")
    lines += [
        "`summary.json` is authoritative. `cases.csv` contains exact means, ratios and raw "
        "per-seed measurements; `observations.csv` retains accepted observation outcomes."
        + (
            " Charts under `charts/` are drawn from `summary.json` alone, as SVG and PNG."
            if view.charts
            else ""
        ),
        "",
        "```bash",
        "benchpress-compare status --results-root /path/to/results",
        "benchpress-compare resume --results-root /path/to/results",
        "benchpress-compare report --results-root /path/to/results",
        "```",
        "",
    ]
    if view.raster_note:
        lines += [view.raster_note, ""]
    lines += [f"- {note}" for note in view.report["notes"]]
    return lines + [""]
