"""Deterministic SVG primitives, palette, scales and formatting for report charts.

Charts are plain text generated from the report model, so they need no plotting
library and are reproducible byte for byte. Every chart paints its own light
surface and uses one fixed, colorblind-safe palette, so it reads the same inside
any Markdown viewer, light or dark.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Sequence
from xml.sax.saxutils import escape, quoteattr

# Light-mode roles of the validated reference palette.
SURFACE = "#fcfcfb"
STRIPE = "#f3f3f0"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
REFERENCE = "#898781"
WHITE = "#ffffff"
# One color rule for every chart: blue improves, red regresses, grays are neutral.
BETTER = "#2a78d6"
WORSE = "#e34948"
SAME = "#c3c2b7"
UNCLASSIFIABLE = "#898781"
FONT = "system-ui, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"


def number(value: float) -> str:
    """Compact, stable coordinate formatting: two decimals, no trailing zeros."""
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def percent_label(value: float) -> str:
    """Axis tick text for a percent change: ``-30%``, ``0%``, ``+10%``."""
    return "0%" if value == 0 else f"{value:+.0f}%"


def change_label(value: float) -> str:
    """Bar label text for a percent change with one decimal: ``-2.5%``."""
    return f"{value:+.1f}%" if value else "0.0%"


def text_width(text: str, size: float) -> float:
    """Estimate rendered width of ``text`` in a system sans at ``size`` px.

    Slightly generous per-glyph widths keep layout decisions (label columns,
    fit checks) safe without a font renderer.
    """
    total = 0.0
    for char in text:
        if char in "iljtfrI.,:;'|!()[]{}/ -":
            total += 0.34
        elif char in "mwMW@%":
            total += 0.86
        elif char == "…":
            total += 0.9
        elif char.isupper():
            total += 0.68
        elif char.isdigit():
            total += 0.58
        else:
            total += 0.56
    return total * size


def shorten(text: str, limit: int) -> str:
    """Trim the middle of a long identifier, keeping its distinguishing suffix."""
    if len(text) <= limit:
        return text
    head = max(1, (limit - 1) * 2 // 3)
    tail = max(0, limit - 1 - head)
    return text[:head] + "…" + (text[-tail:] if tail else "")


class Canvas:
    """Collects SVG elements and renders a self-contained, accessible document."""

    def __init__(self, width: float, height: float, *, title: str, description: str):
        self.width = width
        self.height = height
        self.title = title
        self.description = description
        self._parts: list[str] = []

    def add(self, markup: str) -> None:
        self._parts.append(markup)

    @staticmethod
    def _tooltip(title: str | None) -> str:
        return f"<title>{escape(title)}</title>" if title else ""

    def rect(
        self,
        x: float,
        y: float,
        width: float,
        height: float,
        fill: str,
        *,
        rx: float = 0,
        stroke: str | None = None,
        stroke_width: float = 1,
        title: str | None = None,
    ) -> None:
        attributes = (
            f'x="{number(x)}" y="{number(y)}" width="{number(width)}" '
            f'height="{number(height)}" fill="{fill}"'
        )
        if rx:
            attributes += f' rx="{number(rx)}"'
        if stroke:
            attributes += f' stroke="{stroke}" stroke-width="{number(stroke_width)}"'
        self.add(f"<rect {attributes}>{self._tooltip(title)}</rect>")

    def bar(
        self,
        x: float,
        y: float,
        width: float,
        height: float,
        fill: str,
        *,
        rounded: str,
        radius: float = 4,
        title: str | None = None,
    ) -> None:
        """A bar square at its baseline and rounded at its data end (right, left or top)."""
        if width <= 0 or height <= 0:
            return
        r = min(radius, width / 2, height / 2)
        x1, y1 = x + width, y + height
        if rounded == "right":
            d = (
                f"M{number(x)},{number(y)} H{number(x1 - r)} A{number(r)},{number(r)} 0 0 1 "
                f"{number(x1)},{number(y + r)} V{number(y1 - r)} A{number(r)},{number(r)} 0 0 1 "
                f"{number(x1 - r)},{number(y1)} H{number(x)} Z"
            )
        elif rounded == "left":
            d = (
                f"M{number(x + r)},{number(y)} H{number(x1)} V{number(y1)} H{number(x + r)} "
                f"A{number(r)},{number(r)} 0 0 1 {number(x)},{number(y1 - r)} V{number(y + r)} "
                f"A{number(r)},{number(r)} 0 0 1 {number(x + r)},{number(y)} Z"
            )
        else:
            d = (
                f"M{number(x)},{number(y1)} V{number(y + r)} A{number(r)},{number(r)} 0 0 1 "
                f"{number(x + r)},{number(y)} H{number(x1 - r)} A{number(r)},{number(r)} 0 0 1 "
                f"{number(x1)},{number(y + r)} V{number(y1)} Z"
            )
        self.add(f'<path d="{d}" fill="{fill}">{self._tooltip(title)}</path>')

    def line(
        self, x1: float, y1: float, x2: float, y2: float, stroke: str, *, width: float = 1
    ) -> None:
        self.add(
            f'<line x1="{number(x1)}" y1="{number(y1)}" x2="{number(x2)}" y2="{number(y2)}" '
            f'stroke="{stroke}" stroke-width="{number(width)}" stroke-linecap="round"/>'
        )

    def circle(
        self,
        cx: float,
        cy: float,
        r: float,
        fill: str,
        *,
        stroke: str | None = None,
        stroke_width: float = 0,
        title: str | None = None,
    ) -> None:
        attributes = f'cx="{number(cx)}" cy="{number(cy)}" r="{number(r)}" fill="{fill}"'
        if stroke and stroke_width:
            attributes += f' stroke="{stroke}" stroke-width="{number(stroke_width)}"'
        self.add(f"<circle {attributes}>{self._tooltip(title)}</circle>")

    def polygon(self, points: Sequence[tuple[float, float]], fill: str) -> None:
        coordinates = " ".join(f"{number(x)},{number(y)}" for x, y in points)
        self.add(f'<polygon points="{coordinates}" fill="{fill}"/>')

    def text(
        self,
        x: float,
        y: float,
        content: str,
        *,
        size: float = 12,
        fill: str = INK_SECONDARY,
        anchor: str = "start",
        weight: str | None = None,
        italic: bool = False,
        spacing: str | None = None,
        rotate: float | None = None,
        title: str | None = None,
    ) -> None:
        """Text with its baseline at ``y``; callers center with ``y + 0.35 * size``."""
        attributes = f'x="{number(x)}" y="{number(y)}" font-size="{number(size)}" fill="{fill}"'
        if anchor != "start":
            attributes += f' text-anchor="{anchor}"'
        if weight:
            attributes += f' font-weight="{weight}"'
        if italic:
            attributes += ' font-style="italic"'
        if spacing:
            attributes += f' letter-spacing="{spacing}"'
        if rotate is not None:
            attributes += f' transform="rotate({number(rotate)} {number(x)} {number(y)})"'
        self.add(f"<text {attributes}>{self._tooltip(title)}{escape(content)}</text>")

    def render(self) -> str:
        head = [
            '<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{number(self.width)}" height="{number(self.height)}" '
            f'viewBox="0 0 {number(self.width)} {number(self.height)}" '
            f'role="img" aria-labelledby="title desc" font-family={quoteattr(FONT)}>',
            f'<title id="title">{escape(self.title)}</title>',
            f'<desc id="desc">{escape(self.description)}</desc>',
        ]
        return "\n".join(head + self._parts + ["</svg>"]) + "\n"


@dataclass(frozen=True)
class LinearScale:
    """Maps values in ``[lo, hi]`` onto pixels ``[start, end]``."""

    lo: float
    hi: float
    start: float
    end: float

    def __call__(self, value: float) -> float:
        span = self.hi - self.lo
        t = (value - self.lo) / span if span else 0.5
        return self.start + t * (self.end - self.start)


def percent_ticks(lo: float, hi: float, maximum: int = 8) -> list[float]:
    """Round percent ticks that cover ``[lo, hi]`` and zero, at most ``maximum`` apart."""
    low, high = min(lo, 0.0), max(hi, 0.0)
    for step in (1, 2, 5, 10, 20, 25, 50, 100, 200, 500):
        first = math.floor(low / step + 1e-9)
        last = math.ceil(high / step - 1e-9)
        if last - first <= maximum:
            return [float(k * step) for k in range(first, last + 1)]
    return [0.0]


def count_ticks(maximum_value: float, maximum: int = 5) -> list[float]:
    """Round count ticks from zero through ``maximum_value``."""
    if maximum_value <= 0:
        return [0.0]
    raw = maximum_value / maximum
    magnitude = 10 ** math.floor(math.log10(raw))
    step = magnitude
    for factor in (1, 2, 2.5, 5, 10):
        step = factor * magnitude
        if maximum_value / step <= maximum:
            break
    count = math.ceil(maximum_value / step - 1e-9)
    return [round(k * step, 10) for k in range(count + 1)]


def tick_formatter(ticks: Sequence[float]) -> Callable[[float], str]:
    """Format every tick with the fewest decimals that keep all of them exact."""
    decimals = 0
    for tick in ticks:
        while decimals < 4 and abs(round(tick, decimals) - tick) > 1e-9:
            decimals += 1
    return lambda value: f"{value:,.{decimals}f}"


def _linear(channel: int) -> float:
    c = channel / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(color: str) -> float:
    r, g, b = (_linear(int(color[i : i + 2], 16)) for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(first: str, second: str) -> float:
    a, b = luminance(first), luminance(second)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def ink_on(fill: str) -> str:
    """Label color inside a ``fill``: white wherever it clears 3:1, otherwise ink."""
    return WHITE if contrast(fill, WHITE) >= 3 else INK
