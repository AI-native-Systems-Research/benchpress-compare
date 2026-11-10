"""Checked-in benchmark designs, independent of observation identities."""

from __future__ import annotations

import importlib.resources
import json
from pathlib import Path

from .errors import CompareError

PROFILE_NAMES = ("full", "evolve", "small")


def load_profiles() -> dict:
    resource = importlib.resources.files("benchpress_compare").joinpath("data/profiles.json")
    if resource.is_file():
        return json.loads(resource.read_text())
    return json.loads((Path(__file__).resolve().parents[2] / "data/profiles.json").read_text())


def load_profile(name: str) -> dict:
    if name not in PROFILE_NAMES:
        raise CompareError("profile must be full, evolve, or small")
    return load_profiles()[name]


def select_candidates(manifest: dict, candidates: list[dict]) -> list[dict]:
    """Preserve catalogue order and reject missing or non-main subset inputs."""
    if manifest["input_ids"] is None:
        return list(candidates)
    selected = set(manifest["input_ids"])
    if len(selected) != len(manifest["input_ids"]):
        raise CompareError("Profile contains duplicate input IDs")
    missing = selected - {row["input_id"] for row in candidates}
    if missing:
        raise CompareError(f"Unknown profile circuit IDs: {sorted(missing)}")
    rows = [row for row in candidates if row["input_id"] in selected]
    if any(row.get("category") != "main" or row.get("structural_canonical") for row in rows):
        raise CompareError("Subset profiles require main-table inputs without structural variants")
    return rows
