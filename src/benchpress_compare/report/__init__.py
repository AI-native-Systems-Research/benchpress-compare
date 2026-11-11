"""Deterministic JSON, Markdown, CSV and SVG views of committed observations.

- :mod:`.model` builds the authoritative ``summary.json`` data.
- :mod:`.result` reduces it to the small ``result.json`` (N2, D2 and a run state).
- :mod:`.markdown` renders ``report.md`` from that model.
- :mod:`.charts` and :mod:`.svg` draw dependency-free SVG charts from it.
- :mod:`.writer` writes every view atomically into ``reports/``.
"""

from .charts import CHARTS, build_charts
from .markdown import render_markdown
from .model import REPORT_PROTOCOL, VALIDATION_SCOPE_NOTE, build_report
from .result import RESULT_STATES, SCORED_STATES, build_result, read_result
from .writer import write_report

__all__ = [
    "CHARTS",
    "REPORT_PROTOCOL",
    "RESULT_STATES",
    "SCORED_STATES",
    "VALIDATION_SCOPE_NOTE",
    "build_charts",
    "build_report",
    "build_result",
    "read_result",
    "render_markdown",
    "write_report",
]
