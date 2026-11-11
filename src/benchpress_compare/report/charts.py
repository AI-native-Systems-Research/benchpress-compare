"""Chart builders that turn the report model into deterministic SVG documents.

Every chart follows one reading rule: it shows the change against baseline in
percent, negative is better, blue marks an improvement and red a regression.
Each builder returns SVG markup, or ``None`` when the report lacks the data the
chart needs (filtered queries, smoke subsets, unavailable scores). Charts never
compute scores of their own: every number they draw comes from the model.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Callable

from ..aggregate import METRICS
from ..weights import FAMILIES, TARGETS
from . import svg
from .common import (
    BAND_LABELS,
    BAND_ORDER,
    FAMILY_SHORT_NAMES,
    LEVEL_ORDER,
    METRIC_LABELS,
    circuit_rows,
    group_rows,
    ordered,
)
from .svg import Canvas, LinearScale

WIDTH = 880
PAD = 24
TITLE_Y = 26
SUBTITLE_Y = 46
HEADER = 66
RANKED = 10
BAR = 14
SUBTITLE = "Change against baseline in percent; negative is better (fewer gates, shallower)"
OUTCOMES = ("better", "same", "worse", "unclassifiable")
OUTCOME_COLORS = {
    "better": svg.BETTER,
    "same": svg.SAME,
    "worse": svg.WORSE,
    "unclassifiable": svg.UNCLASSIFIABLE,
}
CHANGE_LEGEND = [(svg.BETTER, "improvement"), (svg.WORSE, "regression")]


@dataclass(frozen=True)
class Chart:
    name: str
    title: str
    caption: str
    build: Callable[[dict], str | None]


# -- shared drawing -----------------------------------------------------------


def _header(canvas: Canvas, title: str, subtitle: str) -> None:
    canvas.rect(0, 0, canvas.width, canvas.height, svg.SURFACE)
    canvas.text(PAD, TITLE_Y, title, size=15, fill=svg.INK, weight="600")
    canvas.text(PAD, SUBTITLE_Y, subtitle, size=11.5)


def _legend(canvas: Canvas, items: list[tuple[str, str]], right: float, y: float) -> None:
    """Swatch and label pairs laid out so the row ends at ``right``."""
    size, swatch, gap = 11, 10, 16
    total = sum(swatch + 6 + svg.text_width(label, size) for _, label in items) + gap * (
        len(items) - 1
    )
    x = right - total
    for color, label in items:
        canvas.rect(x, y - swatch / 2, swatch, swatch, color, rx=2)
        canvas.text(x + swatch + 6, y + size * 0.35, label, size=size)
        x += swatch + 6 + svg.text_width(label, size) + gap


def _section_label(canvas: Canvas, x: float, y: float, label: str) -> None:
    canvas.text(x, y, label.upper(), size=10, fill=svg.INK_MUTED, spacing="0.08em")


def _panel_title(canvas: Canvas, x: float, y: float, metric: str) -> None:
    canvas.text(x, y, f"{metric} · {METRIC_LABELS[metric]}", size=12.5, fill=svg.INK, weight="600")


def _percent_axis(
    canvas: Canvas, scale: LinearScale, ticks: list[float], top: float, bottom: float
) -> None:
    """Hairline grid with percent tick labels under ``bottom`` and a zero line."""
    for tick in ticks:
        x = scale(tick)
        canvas.line(x, top, x, bottom, svg.GRID)
        canvas.text(
            x, bottom + 16, svg.percent_label(tick), size=11, fill=svg.INK_MUTED, anchor="middle"
        )
    if scale.lo <= 0 <= scale.hi:
        canvas.line(scale(0), top, scale(0), bottom, svg.REFERENCE)


def _change_bar(
    canvas: Canvas, scale: LinearScale, center: float, value: float, *, title: str
) -> None:
    """A bar from zero to ``value`` with its label just past the tip."""
    zero, tip = scale(0), scale(value)
    label = svg.change_label(value)
    if value < 0:
        canvas.bar(tip, center - BAR / 2, zero - tip, BAR, svg.BETTER, rounded="left", title=title)
        canvas.text(tip - 6, center + 4, label, size=11, anchor="end")
    elif value > 0:
        canvas.bar(zero, center - BAR / 2, tip - zero, BAR, svg.WORSE, rounded="right", title=title)
        canvas.text(tip + 6, center + 4, label, size=11)
    else:
        canvas.line(zero, center - BAR / 2, zero, center + BAR / 2, svg.INK_MUTED, width=2)
        canvas.text(zero + 6, center + 4, label, size=11)


def _change(entry: dict | None, metric: str) -> float | None:
    """Percent change of a scored entry, or ``None`` when its score is unavailable."""
    if not entry:
        return None
    result = entry["metrics"][metric]
    if result.get("change_percent") is not None:
        return result["change_percent"]
    score = result.get("score")
    return 100 * (score - 1) if score else None


def _subtitle(prefix: str) -> str:
    return f"{prefix}; {SUBTITLE[0].lower()}{SUBTITLE[1:]}"


@dataclass(frozen=True)
class Row:
    label: str
    values: dict
    emphasis: bool = False


Section = tuple[str, list[Row]]


@dataclass(frozen=True)
class PairedLayout:
    """Row labels on the left, one panel per metric to the right, rows shared."""

    label_right: float
    panels: dict[str, tuple[float, float]]
    rows: list[tuple[Row | None, str, float, int]]
    top: float
    bottom: float

    @classmethod
    def build(cls, sections: list[Section], *, label_size: float, top: float) -> "PairedLayout":
        longest = max(svg.text_width(row.label, label_size) for _, rows in sections for row in rows)
        label_right = PAD + min(300.0, longest + 8)
        left, panel_gap = label_right + 16, 40
        panel_w = (WIDTH - left - PAD - panel_gap * (len(METRICS) - 1)) / len(METRICS)
        panels = {}
        for index, metric in enumerate(METRICS):
            start = left + index * (panel_w + panel_gap)
            panels[metric] = (start, start + panel_w)
        row_h, heading_h, section_gap = 24, 26, 10
        rows: list = []
        y = top
        for index, (heading, items) in enumerate(sections):
            rows.append((None, heading, y, index))
            y += heading_h
            for row in items:
                rows.append((row, heading, y + row_h / 2, index))
                y += row_h
            y += section_gap
        return cls(label_right, panels, rows, top, y - section_gap)

    def draw_rows(self, canvas: Canvas, *, label_size: float) -> None:
        """Section headings, zebra stripes and right-aligned row labels."""
        stripe = False
        for row, heading, y, index in self.rows:
            if row is None:
                if index:
                    canvas.line(PAD, y - 4, WIDTH - PAD, y - 4, svg.GRID)
                _section_label(canvas, PAD, y + 17, heading)
                stripe = False
                continue
            if stripe:
                canvas.rect(PAD, y - 12, WIDTH - 2 * PAD, 24, svg.STRIPE)
            stripe = not stripe
            canvas.text(
                self.label_right,
                y + label_size * 0.35,
                row.label,
                size=label_size,
                anchor="end",
                weight="600" if row.emphasis else None,
                fill=svg.INK if row.emphasis else svg.INK_SECONDARY,
            )


# -- chart 1: change by slice -------------------------------------------------


def _family_label(family: str) -> str:
    if family in FAMILY_SHORT_NAMES:
        return f"{family} · {FAMILY_SHORT_NAMES[family]}"
    return str(family)


def _slice_sections(report: dict) -> list[Section]:
    overall = {metric: _change(report["summary"], metric) for metric in METRICS}
    sections: list[Section] = [("Overall", [Row("All inputs", overall, emphasis=True)])]
    dimensions = (
        ("level", "Optimization level", LEVEL_ORDER, lambda value: f"Level {value}"),
        ("target_class", "Target class", TARGETS, str),
        ("band", "Width band", BAND_ORDER, lambda value: BAND_LABELS.get(value, str(value))),
        ("family", "Circuit family", FAMILIES, _family_label),
    )
    for dimension, heading, order, label in dimensions:
        rows = group_rows(report, dimension, order)
        # A dimension with a single value (one target, one level) repeats the overall row.
        if len(rows) > 1:
            items = [
                Row(label(row["group"][dimension]), {m: _change(row, m) for m in METRICS})
                for row in rows
            ]
            sections.append((heading, items))
    return sections


def slice_change(report: dict) -> str | None:
    """Percent change of N2 and D2 for the whole benchmark and every slice."""
    sections = _slice_sections(report)
    values = [
        value
        for _, rows in sections
        for row in rows
        for value in row.values.values()
        if value is not None
    ]
    if not values:
        return None
    label_size = 12.5
    layout = PairedLayout.build(sections, label_size=label_size, top=HEADER + 30)
    canvas = Canvas(
        WIDTH,
        layout.bottom + 56,
        title="Change by slice",
        description="Bars of the percent change in N2 and D2 against baseline for the whole "
        "benchmark and for each optimization level, target class, width band and family.",
    )
    _header(canvas, "Change by slice", SUBTITLE)
    _legend(canvas, CHANGE_LEGEND, WIDTH - PAD, TITLE_Y - 4)
    layout.draw_rows(canvas, label_size=label_size)
    first_left, first_right = next(iter(layout.panels.values()))
    ticks = svg.percent_ticks(
        min(values), max(values), max(3, int((first_right - first_left) // 60))
    )
    for metric, (left, right) in layout.panels.items():
        scale = LinearScale(ticks[0], ticks[-1], left + 46, right - 46)
        _panel_title(canvas, left, HEADER + 14, metric)
        _percent_axis(canvas, scale, ticks, layout.top, layout.bottom)
        for row, _, y, _ in layout.rows:
            if row is None:
                continue
            value = row.values[metric]
            if value is None:
                canvas.text(
                    scale(0) + 6, y + 4, "unavailable", size=10.5, fill=svg.INK_MUTED, italic=True
                )
            else:
                _change_bar(canvas, scale, y, value, title=f"{row.label}: {metric} {value:+.2f}%")
    return canvas.render()


# -- chart 2: circuits better and worse ---------------------------------------


def circuit_outcomes(report: dict) -> str | None:
    """Stacked counts of circuits classified better, same or worse, per family."""
    rows = circuit_rows(report)
    if not rows:
        return None

    def counts(subset: list[dict]) -> dict[str, Counter]:
        return {
            metric: Counter(row["metrics"][metric]["classification"] for row in subset)
            for metric in METRICS
        }

    sections: list[Section] = [("Overall", [Row("All circuits", counts(rows), emphasis=True)])]
    families = ordered((row["family"] for row in rows if row.get("family")), FAMILIES)
    if families:
        items = [
            Row(_family_label(family), counts([row for row in rows if row.get("family") == family]))
            for family in families
        ]
        sections.append(("Circuit family", items))
    label_size = 12.5
    layout = PairedLayout.build(sections, label_size=label_size, top=HEADER + 30)
    canvas = Canvas(
        WIDTH,
        layout.bottom + 56,
        title="Circuits better and worse",
        description="Stacked bars counting the circuits whose N2 and D2 got better, stayed the "
        "same or got worse, overall and per family.",
    )
    _header(
        canvas,
        "Circuits better and worse",
        "Each circuit is classified by the exact product of its case ratios; "
        "bar length is the number of circuits",
    )
    present = [
        outcome
        for outcome in OUTCOMES
        if any(
            row.values[metric][outcome]
            for _, items in sections
            for row in items
            for metric in METRICS
        )
    ]
    legend = [(OUTCOME_COLORS[outcome], outcome) for outcome in (present or ["better", "worse"])]
    _legend(canvas, legend, WIDTH - PAD, TITLE_Y - 4)
    layout.draw_rows(canvas, label_size=label_size)
    ticks = svg.count_ticks(len(rows))
    label = svg.tick_formatter(ticks)
    for metric, (left, right) in layout.panels.items():
        scale = LinearScale(0, ticks[-1], left, right - 40)
        _panel_title(canvas, left, HEADER + 14, metric)
        for tick in ticks:
            x = scale(tick)
            canvas.line(x, layout.top, x, layout.bottom, svg.GRID)
            canvas.text(
                x, layout.bottom + 16, label(tick), size=11, fill=svg.INK_MUTED, anchor="middle"
            )
        for row, _, y, _ in layout.rows:
            if row is None:
                continue
            tally = row.values[metric]
            total = sum(tally[outcome] for outcome in OUTCOMES)
            drawn = [outcome for outcome in OUTCOMES if tally[outcome]]
            x = scale(0)
            for outcome in drawn:
                width = scale(tally[outcome]) - scale(0)
                segment = max(0.0, width - 2)
                color = OUTCOME_COLORS[outcome]
                title = f"{row.label}: {metric} {outcome} {tally[outcome]} of {total}"
                if outcome == drawn[-1]:
                    canvas.bar(x, y - BAR / 2, segment, BAR, color, rounded="right", title=title)
                else:
                    canvas.rect(x, y - BAR / 2, segment, BAR, color, title=title)
                text = str(tally[outcome])
                if svg.text_width(text, 10.5) + 8 <= segment:
                    canvas.text(
                        x + segment / 2,
                        y + 3.5,
                        text,
                        size=10.5,
                        fill=svg.ink_on(color),
                        anchor="middle",
                    )
                x += width
            canvas.text(x + 6, y + 4, str(total), size=10.5, fill=svg.INK_MUTED)
    canvas.text(WIDTH / 2, layout.bottom + 40, "number of circuits", size=11, anchor="middle")
    return canvas.render()


# -- chart 3: largest circuit changes -----------------------------------------


def largest_changes(report: dict) -> str | None:
    """The circuits with the largest improvement and regression per metric."""
    rows = circuit_rows(report)
    panels = []
    for metric in METRICS:
        scored = sorted(
            (change, row["input_id"])
            for row in rows
            if (change := _change(row, metric)) is not None
        )
        better = [pair for pair in scored if pair[0] < 0][:RANKED]
        worse = [pair for pair in reversed(scored) if pair[0] > 0][:RANKED]
        if better or worse:
            panels.append((metric, better, worse))
    if not panels:
        return None
    values = [change for _, better, worse in panels for change, _ in better + worse]
    label_w = 250
    left, right = PAD + label_w, WIDTH - PAD
    ticks = svg.percent_ticks(min(values), max(values), 8)
    scale = LinearScale(ticks[0], ticks[-1], left + 52, right - 52)
    row_h, group_h, gap, title_h, axis_h = 22, 24, 12, 30, 52

    def rows_height(better: list, worse: list) -> float:
        height = (group_h + len(better) * row_h) if better else 0
        height += gap if better and worse else 0
        return height + ((group_h + len(worse) * row_h) if worse else 0)

    total = HEADER + sum(title_h + rows_height(b, w) + axis_h for _, b, w in panels) + 8
    canvas = Canvas(
        WIDTH,
        total,
        title="Largest circuit changes",
        description="Per-metric bars of the circuits with the largest percent improvement and the "
        "largest percent regression against baseline.",
    )
    _header(canvas, "Largest circuit changes", _subtitle(f"Up to {RANKED} circuits per direction"))
    _legend(canvas, CHANGE_LEGEND, right, TITLE_Y - 4)
    y = HEADER + 4
    for metric, better, worse in panels:
        _panel_title(canvas, PAD, y + 14, metric)
        y += title_h
        rows_top, rows_bottom = y, y + rows_height(better, worse)
        _percent_axis(canvas, scale, ticks, rows_top, rows_bottom)
        for heading, items in (("Largest improvements", better), ("Largest regressions", worse)):
            if not items:
                continue
            if heading.endswith("regressions") and better:
                y += gap
            _section_label(canvas, PAD, y + 16, heading)
            y += group_h
            for change, input_id in items:
                center = y + row_h / 2
                canvas.text(
                    left - 12,
                    center + 4,
                    svg.shorten(input_id, 40),
                    size=11.5,
                    anchor="end",
                    title=input_id,
                )
                _change_bar(
                    canvas, scale, center, change, title=f"{input_id}: {metric} {change:+.2f}%"
                )
                y += row_h
        y = rows_bottom + axis_h
    return canvas.render()


# -- chart 4: spread of circuit changes ---------------------------------------


def change_spread(report: dict) -> str | None:
    """Histograms of per-circuit percent change, one panel per metric."""
    rows = circuit_rows(report)
    values = {
        metric: [change for row in rows if (change := _change(row, metric)) is not None]
        for metric in METRICS
    }
    everything = [value for series in values.values() for value in series]
    if len(everything) < 8:
        return None
    lo, hi = min(everything + [0.0]), max(everything + [0.0])
    bin_w = 5 if hi - lo <= 60 else 10 if hi - lo <= 160 else 20
    first = math.floor(lo / bin_w)
    last = math.floor(hi / bin_w) + 1
    edges = [float(k * bin_w) for k in range(first, last + 1)]
    counts = {}
    for metric, series in values.items():
        tally = [0] * (len(edges) - 1)
        for value in series:
            tally[min(len(tally) - 1, math.floor(value / bin_w) - first)] += 1
        counts[metric] = tally
    peak = max(max(tally) for tally in counts.values())
    yticks = svg.count_ticks(peak)
    if len(yticks) > 1 and peak > 0.8 * yticks[-1]:
        # Keep the tallest bar's count label clear of the panel title and overall marker.
        yticks.append(yticks[-1] + (yticks[-1] - yticks[-2]))
    ylabel = svg.tick_formatter(yticks)
    left, right, plot_h, panel_gap = 64, WIDTH - PAD, 160, 56
    top = HEADER + 28
    canvas = Canvas(
        WIDTH,
        top + len(METRICS) * (plot_h + panel_gap) + 20,
        title="Spread of circuit changes",
        description="Histograms of the percent change per circuit for N2 and D2, with the "
        "overall weighted change marked.",
    )
    _header(canvas, "Spread of circuit changes", _subtitle(f"Circuits per {bin_w}% bin"))
    _legend(canvas, CHANGE_LEGEND, right, TITLE_Y - 4)
    xscale = LinearScale(edges[0], edges[-1], left, right)
    every = 1 if len(edges) <= 14 else 2
    for index, metric in enumerate(METRICS):
        panel_top = top + index * (plot_h + panel_gap)
        panel_bottom = panel_top + plot_h
        yscale = LinearScale(0, yticks[-1], panel_bottom, panel_top)
        series = values[metric]
        improved = sum(value < 0 for value in series)
        regressed = sum(value > 0 for value in series)
        canvas.text(
            left,
            panel_top - 12,
            f"{metric} · {METRIC_LABELS[metric]} — {len(series)} circuits: "
            f"{improved} improved, {regressed} regressed",
            size=12.5,
            fill=svg.INK,
            weight="600",
        )
        for tick in yticks:
            y = yscale(tick)
            canvas.line(left, y, right, y, svg.GRID)
            canvas.text(left - 8, y + 3.5, ylabel(tick), size=11, fill=svg.INK_MUTED, anchor="end")
        for k, count in enumerate(counts[metric]):
            if not count:
                continue
            x0, x1 = xscale(edges[k]) + 1, xscale(edges[k + 1]) - 1
            y = yscale(count)
            color = svg.BETTER if edges[k] < 0 else svg.WORSE
            span = f"{svg.percent_label(edges[k])} to {svg.percent_label(edges[k + 1])}"
            canvas.bar(
                x0,
                y,
                x1 - x0,
                panel_bottom - y,
                color,
                rounded="top",
                radius=3,
                title=f"{span}: {count} circuits",
            )
            canvas.text((x0 + x1) / 2, y - 5, str(count), size=10.5, anchor="middle")
        canvas.line(left, panel_bottom, right, panel_bottom, svg.AXIS)
        canvas.line(xscale(0), panel_top, xscale(0), panel_bottom, svg.REFERENCE)
        overall = _change(report["summary"], metric)
        if overall is not None and edges[0] <= overall <= edges[-1]:
            x = xscale(overall)
            canvas.polygon(
                [(x - 5, panel_top - 1), (x + 5, panel_top - 1), (x, panel_top + 7)], svg.INK
            )
            anchor = "start" if x + 110 < right else "end"
            canvas.text(
                x + (8 if anchor == "start" else -8),
                panel_top + 7,
                f"overall {svg.change_label(overall)}",
                size=10.5,
                anchor=anchor,
            )
        for k, edge in enumerate(edges):
            if k % every == 0 or k == len(edges) - 1:
                canvas.text(
                    xscale(edge),
                    panel_bottom + 16,
                    svg.percent_label(edge),
                    size=11,
                    fill=svg.INK_MUTED,
                    anchor="middle",
                )
        if index == len(METRICS) - 1:
            canvas.text(
                (left + right) / 2,
                panel_bottom + 38,
                "change against baseline per circuit",
                size=11,
                anchor="middle",
            )
    return canvas.render()


CHARTS: tuple[Chart, ...] = (
    Chart(
        "slice_change",
        "Change by slice",
        "Percent change of N2 and D2 against baseline for the whole benchmark and for each "
        "optimization level, target class, width band and circuit family. Negative is better.",
        slice_change,
    ),
    Chart(
        "circuit_outcomes",
        "Circuits better and worse",
        "How many circuits got better, stayed the same or got worse on each metric, overall "
        "and per family.",
        circuit_outcomes,
    ),
    Chart(
        "largest_changes",
        "Largest circuit changes",
        "The circuits with the largest percent improvement and the largest percent regression "
        "on each metric.",
        largest_changes,
    ),
    Chart(
        "change_spread",
        "Spread of circuit changes",
        "How many circuits changed by how much on each metric; the marker shows the overall "
        "weighted change.",
        change_spread,
    ),
)
CHART_NAMES = frozenset(chart.name for chart in CHARTS)
CHART_BY_NAME = {chart.name: chart for chart in CHARTS}


def build_charts(report: dict) -> dict[str, str]:
    """SVG markup for every chart the report has data for, in display order."""
    charts = {}
    for chart in CHARTS:
        markup = chart.build(report)
        if markup:
            charts[chart.name] = markup
    return charts
