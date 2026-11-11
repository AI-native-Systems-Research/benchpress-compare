"""Deterministic JSON, Markdown, CSV and SVG views of committed observations.

- :mod:`.model` builds the authoritative ``summary.json`` data.
- :mod:`.markdown` renders ``report.md`` from that model.
- :mod:`.charts` and :mod:`.svg` draw dependency-free SVG charts from it.
- :mod:`.writer` writes every view atomically into ``reports/``.
"""

from .charts import CHARTS, build_charts
from .markdown import render_markdown
from .model import REPORT_PROTOCOL, VALIDATION_SCOPE_NOTE, build_report
from .writer import write_report

__all__ = [
    "CHARTS",
    "REPORT_PROTOCOL",
    "VALIDATION_SCOPE_NOTE",
    "build_charts",
    "build_report",
    "render_markdown",
    "write_report",
]
