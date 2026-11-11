"""Write the report model and every derived view atomically into ``reports/``."""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from pathlib import Path
from typing import Iterable

from ..aggregate import METRICS
from ..identity import atomic_write_json, fsync_directory
from .charts import build_charts
from .markdown import render_markdown
from .raster import RasterUnavailable, render_png
from .result import build_result, read_run_state

CASE_FIELDS = (
    "case_id",
    "input_id",
    "target_class",
    "target_id",
    "level",
    "family",
    "band",
    "recipe",
    "active_width",
    "declared_width",
    "occupancy",
    "weight",
    "metric",
    "validation_scope",
    "available",
    "baseline_mean",
    "evolve_mean",
    "ratio",
    "ratio_display",
    "absolute_direction",
    "reasons",
    "raw",
    "spread",
)
OBSERVATION_FIELDS = (
    "observation_id",
    "case_id",
    "arm",
    "seed",
    "status",
    "N2",
    "D2",
    "metrics",
    "metric_reasons",
    "cache_hit",
    "reason",
    "validation",
    "diagnostics",
    "resources",
    "accepted_attempt",
)
MANAGED_SUFFIXES = (".svg", ".png")


def write_report(results_root: str | Path, report: dict) -> dict[str, str]:
    """Write summary.json, result.json, charts, report.md and the CSV views; return their paths."""
    destination = Path(results_root) / "reports"
    destination.mkdir(parents=True, exist_ok=True)
    # Serialization first rejects accidental NaN/Infinity in any authoritative field.
    json.dumps(report, allow_nan=False)
    atomic_write_json(destination / "summary.json", report)
    # The live run state decides whether the scores may be read yet.
    atomic_write_json(
        destination / "result.json", build_result(report, read_run_state(results_root))
    )
    vectors, images, raster_note = _write_charts(destination / "charts", build_charts(report))
    embedded = {
        name: f"charts/{name}.png" if name in images else f"charts/{name}.svg" for name in vectors
    }
    markdown = render_markdown(
        report,
        charts=embedded,
        vectors={name: f"charts/{name}.svg" for name in images},
        raster_note=raster_note,
    )
    _atomic_text(destination / "report.md", markdown)
    _atomic_text(destination / "cases.csv", _csv(CASE_FIELDS, _case_rows(report)))
    _atomic_text(destination / "observations.csv", _csv(OBSERVATION_FIELDS, report["observations"]))
    paths = {
        name: str(destination / name)
        for name in ("report.md", "summary.json", "result.json", "cases.csv", "observations.csv")
    }
    paths.update({f"charts/{name}.svg": str(path) for name, path in vectors.items()})
    paths.update({f"charts/{name}.png": str(path) for name, path in images.items()})
    return paths


def _write_charts(
    directory: Path, charts: dict[str, str]
) -> tuple[dict[str, Path], dict[str, Path], str | None]:
    """Write every chart as SVG and PNG; the directory holds only the latest render.

    PNG rendering is best effort: when it is unavailable the SVGs are still
    written and the returned note says why, so ``report.md`` can fall back to
    embedding them. Leftovers from earlier renders, including renamed or
    dropped charts, are removed.
    """
    directory.mkdir(parents=True, exist_ok=True)
    vectors, images = {}, {}
    note = None
    for name, markup in charts.items():
        vectors[name] = directory / f"{name}.svg"
        _atomic_text(vectors[name], markup)
        if note:
            continue
        try:
            png = render_png(markup)
        except RasterUnavailable as exc:
            note = f"PNG versions were not rendered: {exc}."
            continue
        except Exception as exc:  # a cosmetic failure must not lose the authoritative views
            note = f"PNG versions were not rendered: {type(exc).__name__}: {exc}."
            continue
        images[name] = directory / f"{name}.png"
        _atomic_bytes(images[name], png)
    for stale in directory.iterdir():
        if stale.suffix in MANAGED_SUFFIXES and stale not in (*vectors.values(), *images.values()):
            stale.unlink()
    return vectors, images, note


def _case_rows(report: dict) -> list[dict]:
    rows = []
    scope = report["validation_coverage"]["scope_note"]
    for case in report["summary"]["cases"] + report["summary"]["supplemental_cases"]:
        for metric in METRICS:
            rows.append(
                {
                    **{key: value for key, value in case.items() if key != "metrics"},
                    "metric": metric,
                    "validation_scope": scope,
                    **case["metrics"][metric],
                }
            )
    return rows


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _atomic_text(path: Path, text: str) -> None:
    _atomic_bytes(path, text.encode("utf-8"))


def _csv(fields: Iterable[str], rows: Iterable[dict]) -> str:
    fields = list(fields)
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                field: json.dumps(
                    row[field], sort_keys=True, allow_nan=False, separators=(",", ":")
                )
                if isinstance(row.get(field), (dict, list))
                else row.get(field)
                for field in fields
            }
        )
    return stream.getvalue()
