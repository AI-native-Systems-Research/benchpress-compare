"""Presentation vocabulary and data views shared by the Markdown and chart renderers."""

from __future__ import annotations

from fractions import Fraction
from typing import Iterable, Sequence

from ..weights import FAMILIES, read_fraction

# Family descriptions follow docs/design/benchpress_subset_spec.md.
FAMILY_NAMES = {
    "F1": "Fourier, phase estimation and factoring",
    "F2": "Register arithmetic",
    "F3": "Boolean, oracle, hub and parity-check circuits",
    "F4": "Variational and quantum-learning circuits",
    "F5": "Combinatorial optimization",
    "F6": "Spin/lattice simulation and quantum walks",
    "F7": "Electronic and vibrational chemistry",
    "F8": "Random and Clifford circuits",
}
# Compact variants for chart labels, where the full names would not fit.
FAMILY_SHORT_NAMES = {
    "F1": "Fourier, phase estimation, factoring",
    "F2": "Register arithmetic",
    "F3": "Boolean, oracle, parity-check",
    "F4": "Variational, quantum learning",
    "F5": "Combinatorial optimization",
    "F6": "Spin lattices, quantum walks",
    "F7": "Chemistry",
    "F8": "Random and Clifford",
}
BAND_ORDER = ("S", "M", "L")
BAND_LABELS = {"S": "S (8–24 qubits)", "M": "M (25–64 qubits)", "L": "L (65–133 qubits)"}
LEVEL_ORDER = (2, 3)
METRIC_LABELS = {"N2": "two-qubit gate count", "D2": "two-qubit depth"}
CIRCUIT_FIELDS = ("family", "band", "recipe", "active_width")


def ordered(values: Iterable, order: Sequence) -> list:
    """Distinct ``values`` in the canonical ``order``; unknown values follow, sorted."""
    ranks = {value: index for index, value in enumerate(order)}
    return sorted(set(values), key=lambda value: (ranks.get(value, len(ranks)), str(value)))


def circuit_rows(report: dict) -> list[dict]:
    """One row per scored circuit with its case metadata, weight and metric scores."""
    metadata: dict[str, dict] = {}
    for case in report["summary"]["cases"]:
        metadata.setdefault(case["input_id"], {field: case.get(field) for field in CIRCUIT_FIELDS})
    weights = {
        item["input_id"]: read_fraction(item["weight"]) for item in report.get("concentrations", [])
    }
    rows = []
    for circuit in report["summary"]["circuits"]:
        input_id = circuit["input_id"]
        rows.append(
            {
                "input_id": input_id,
                **metadata.get(input_id, dict.fromkeys(CIRCUIT_FIELDS)),
                "weight": weights.get(input_id, Fraction()),
                "case_count": circuit["case_count"],
                "metrics": circuit["metrics"],
            }
        )
    return rows


def circuit_sort_key(row: dict) -> tuple:
    """Family, then width band, then input id."""
    family_ranks = {family: index for index, family in enumerate(FAMILIES)}
    band_ranks = {band: index for index, band in enumerate(BAND_ORDER)}
    return (
        family_ranks.get(row.get("family"), len(family_ranks)),
        str(row.get("family")),
        band_ranks.get(row.get("band"), len(band_ranks)),
        row["input_id"],
    )


def case_index(report: dict) -> dict[str, dict]:
    summary = report["summary"]
    return {
        case["case_id"]: case for case in summary["cases"] + summary.get("supplemental_cases", [])
    }


def group_rows(report: dict, dimension: str, order: Sequence) -> list[dict]:
    """Rows of a one-dimensional slice in canonical order (empty when not computed)."""
    rows = report.get("groups", {}).get(dimension, [])
    ranks = {value: index for index, value in enumerate(order)}
    return sorted(
        rows,
        key=lambda row: (
            ranks.get(row["group"][dimension], len(ranks)),
            str(row["group"][dimension]),
        ),
    )
