"""Rasterize chart SVGs to PNG with a bundled font, so the bytes match on every host.

Rendering uses resvg through the ``resvg-py`` wheel, which needs no system
libraries. The DejaVu Sans faces shipped under ``data/fonts`` are the only fonts
the renderer sees, so a PNG never depends on which fonts a machine happens to
have installed and is reproducible byte for byte.
"""

from __future__ import annotations

import importlib.resources
from pathlib import Path

FONT_FAMILY = "DejaVu Sans"
FONT_FILES = ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf")
# Device pixels per CSS pixel: crisp on high-density screens and in slides.
SCALE = 2.0


class RasterUnavailable(RuntimeError):
    """PNG rendering cannot run here; SVG charts are still written."""


def font_paths() -> list[Path]:
    """Bundled font files, from the installed package or the source checkout."""
    paths = []
    for name in FONT_FILES:
        resource = importlib.resources.files("benchpress_compare").joinpath(f"data/fonts/{name}")
        if resource.is_file():
            paths.append(Path(str(resource)))
        else:
            paths.append(Path(__file__).resolve().parents[3] / "data" / "fonts" / name)
    return paths


def render_png(svg: str, *, scale: float = SCALE) -> bytes:
    """PNG bytes for ``svg`` at ``scale`` device pixels per CSS pixel."""
    try:
        import resvg_py
    except ImportError as exc:
        raise RasterUnavailable(f"resvg-py is not installed ({exc})") from exc
    fonts = [path for path in font_paths() if path.is_file()]
    if not fonts:
        raise RasterUnavailable("bundled fonts are missing from data/fonts")
    return resvg_py.svg_to_bytes(
        svg_string=svg,
        zoom=scale,
        skip_system_fonts=True,
        font_files=[str(path) for path in fonts],
        font_family=FONT_FAMILY,
        sans_serif_family=FONT_FAMILY,
    )
