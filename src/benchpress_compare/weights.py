"""Exact, cohort-frozen eight-family weights and auditable marginals."""

from __future__ import annotations

from collections import Counter, defaultdict
from fractions import Fraction
from typing import Iterable, Mapping

WEIGHT_POLICY = "eight-family-recipe-band-v1"
FAMILIES = tuple(f"F{n}" for n in range(1, 9))
TARGETS = ("square", "heavy-hex", "fake_torino")
LEVELS = (2, 3)
PRIORITIES = {"S": Fraction(1, 5), "M": Fraction(3, 10), "L": Fraction(1, 2)}


class WeightError(ValueError):
    """A cohort cannot satisfy its declared allocation contract."""


def fraction_record(value: Fraction | int) -> dict[str, int]:
    value = Fraction(value)
    return {"numerator": value.numerator, "denominator": value.denominator}


def read_fraction(value: Mapping | Fraction | int | str) -> Fraction:
    if isinstance(value, Mapping):
        numerator, denominator = value.get("numerator"), value.get("denominator")
        if type(numerator) is not int or type(denominator) is not int or denominator <= 0:
            raise WeightError("fractions require integer numerator and positive denominator")
        return Fraction(numerator, denominator)
    if isinstance(value, (Fraction, str)) or type(value) is int:
        return Fraction(value)
    raise WeightError("weights must be exact fractions, never binary floats")


def width_band(active_width: int) -> str:
    if type(active_width) is not int or not 8 <= active_width <= 133:
        raise WeightError("active width must be an integer from 8 through 133")
    return "S" if active_width <= 24 else "M" if active_width <= 64 else "L"


def _inputs(inputs: Iterable[dict]) -> list[dict]:
    result = [dict(item) for item in inputs]
    seen: set[str] = set()
    structures: set[str] = set()
    aliases: set[str] = set()
    for item in result:
        key = item["input_id"]
        if key in seen or key in aliases or not isinstance(key, str) or not key:
            raise WeightError(f"duplicate or invalid canonical input: {key}")
        seen.add(key)
        new_aliases = set(item.get("aliases", []))
        if new_aliases & seen or new_aliases & aliases:
            raise WeightError(f"alias is already weighted: {key}")
        aliases.update(new_aliases)
        if item.get("family") not in FAMILIES:
            raise WeightError(f"unknown family for {key}")
        if not item.get("recipe"):
            raise WeightError(f"missing recipe for {key}")
        if "active_width" in item:
            band = width_band(item["active_width"])
            if item.get("band", band) != band:
                raise WeightError(f"band disagrees with active width for {key}")
            item["band"] = band
        if item.get("band") not in PRIORITIES:
            raise WeightError(f"unknown band for {key}")
        group = item.get("verified_structural_group")
        if group:
            if group in structures:
                raise WeightError(f"multiple weighted members of structural group {group}")
            structures.add(group)
    return sorted(result, key=lambda item: item["input_id"])


def allocate_weights(
    inputs: Iterable[dict], *, require_all_families: bool = True, targets=TARGETS, levels=LEVELS
) -> dict:
    """Allocate input and case weights; absent families are never redistributed.

    ``require_all_families=False`` is for an explicitly labelled diagnostic/smoke
    design. Its total mass is the number of represented families divided by eight.
    """
    targets, levels = _dimensions(targets, levels)
    case_count = len(targets) * len(levels)
    inputs = _inputs(inputs)
    families = {item["family"] for item in inputs}
    missing = sorted(set(FAMILIES) - families)
    if require_all_families and missing:
        raise WeightError("empty families prevent eight-family freeze: " + ", ".join(missing))
    cells: dict[tuple[str, str], dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    bands: dict[str, set[str]] = defaultdict(set)
    for item in inputs:
        cells[item["family"], item["band"]][item["recipe"]].append(item)
        bands[item["family"]].add(item["band"])
    allocations = {}
    marginals = {kind: defaultdict(Fraction) for kind in ("family", "band", "sensitivity_band")}
    for item in inputs:
        family, band, recipe = item["family"], item["band"], item["recipe"]
        z = sum((PRIORITIES[b] for b in bands[family]), Fraction())
        k = len(cells[family, band])
        n = len(cells[family, band][recipe])
        weight = Fraction(1, 8) * PRIORITIES[band] / z / k / n
        sensitivity = Fraction(1, 8 * len(bands[family]) * k * n)
        allocations[item["input_id"]] = {
            "input_weight": fraction_record(weight),
            "case_weight": fraction_record(weight / case_count),
            "sensitivity_input_weight": fraction_record(sensitivity),
            "sensitivity_case_weight": fraction_record(sensitivity / case_count),
            "factors": {
                "family": fraction_record(Fraction(1, 8)),
                "band_priority": fraction_record(PRIORITIES[band]),
                "supported_band_priority_sum": fraction_record(z),
                "recipe_count": k,
                "recipe_input_count": n,
                "target": fraction_record(Fraction(1, len(targets))),
                "level": fraction_record(Fraction(1, len(levels))),
            },
        }
        marginals["family"][family] += weight
        marginals["band"][band] += weight
        marginals["sensitivity_band"][band] += sensitivity
    total = sum(marginals["family"].values(), Fraction())
    expected = Fraction(len(families), 8)
    if total != expected or any(
        weight != Fraction(1, 8) for weight in marginals["family"].values()
    ):
        raise WeightError("internal allocation invariant failed")
    concentrations = sorted(
        ({"input_id": key, "weight": value["input_weight"]} for key, value in allocations.items()),
        key=lambda item: (-read_fraction(item["weight"]), item["input_id"]),
    )
    band_mass = marginals["band"]
    return {
        "schema_version": 1,
        "policy": WEIGHT_POLICY,
        "complete_eight_family": not missing,
        "missing_families": missing,
        "total_weight": fraction_record(total),
        "inputs": allocations,
        "supported_bands": {
            family: sorted(value, key=("S", "M", "L").index)
            for family, value in sorted(bands.items())
        },
        "marginals": {
            name: {key: fraction_record(value) for key, value in sorted(mapping.items())}
            for name, mapping in marginals.items()
        },
        "concentrations": concentrations,
        "large_is_largest": bool(band_mass)
        and band_mass["L"] > max(band_mass["S"], band_mass["M"]),
    }


def assign_case_weights(
    cases: Iterable[dict], allocation: dict | None = None, *, require_all_families: bool = True,
    targets=TARGETS, levels=LEVELS,
) -> list[dict]:
    targets, levels = _dimensions(targets, levels)
    cases = [dict(case) for case in cases]
    inputs = {}
    for case in cases:
        if case.get("supplemental", False):
            continue
        key = case["input_id"]
        metadata = {field: case[field] for field in ("input_id", "family", "band", "recipe")}
        for field in ("active_width", "aliases", "verified_structural_group"):
            if field in case:
                metadata[field] = case[field]
        if key in inputs and inputs[key] != metadata:
            raise WeightError(f"conflicting metadata for input {key}")
        inputs[key] = metadata
    allocation = allocation or allocate_weights(
        inputs.values(), require_all_families=require_all_families, targets=targets, levels=levels
    )
    for case in cases:
        if case.get("supplemental", False):
            case["weight"] = case["sensitivity_weight"] = fraction_record(0)
        else:
            entry = allocation["inputs"][case["input_id"]]
            case["weight"] = dict(entry["case_weight"])
            case["sensitivity_weight"] = dict(entry["sensitivity_case_weight"])
            case["weight_factors"] = entry["factors"]
    audit_case_weights(
        cases, require_all_families=require_all_families, targets=targets, levels=levels
    )
    return cases


def _dimensions(targets, levels):
    targets, levels = tuple(targets), tuple(levels)
    if (
        not targets or not levels
        or len(set(targets)) != len(targets) or len(set(levels)) != len(levels)
        or not set(targets) <= set(TARGETS) or not set(levels) <= set(LEVELS)
    ):
        raise WeightError("invalid target/level dimensions")
    return targets, levels


def audit_case_weights(
    cases: Iterable[dict], *, require_all_families: bool = True, targets=TARGETS, levels=LEVELS
) -> dict:
    targets, levels = _dimensions(targets, levels)
    cases = list(cases)
    ids = [case["case_id"] for case in cases]
    if len(set(ids)) != len(ids):
        raise WeightError("duplicate case identity")
    weighted = [case for case in cases if read_fraction(case["weight"]) > 0]
    grouped = defaultdict(list)
    for case in cases:
        for field in ("weight", "sensitivity_weight"):
            if read_fraction(case[field]) < 0:
                raise WeightError("negative weight")
        if case.get("supplemental") and (
            read_fraction(case["weight"]) or read_fraction(case["sensitivity_weight"])
        ):
            raise WeightError("supplemental case has positive weight")
    for case in weighted:
        grouped[case["input_id"]].append(case)
    for key, group in grouped.items():
        configurations = Counter((case["target_class"], case["level"]) for case in group)
        if configurations != Counter((target, level) for target in targets for level in levels):
            count = "all six" if targets == TARGETS and levels == LEVELS else "the declared"
            raise WeightError(f"input {key} must have exactly {count} target/level cases")
        for field in ("weight", "sensitivity_weight"):
            if len({read_fraction(case[field]) for case in group}) != 1:
                raise WeightError(f"unequal case weights within input {key}")
        for case in group:
            seeds = case.get("seeds", [])
            if (
                not seeds
                or len(seeds) != len(set(seeds))
                or any(type(seed) is not int or seed < 0 for seed in seeds)
            ):
                raise WeightError(f"invalid comparison seed list: {case['case_id']}")
    if weighted and len({len(case["seeds"]) for case in weighted}) != 1:
        raise WeightError("comparison seed count differs across cases")
    families = {case["family"] for case in weighted}
    if require_all_families and families != set(FAMILIES):
        raise WeightError("all eight families are required")
    expected = Fraction(len(families), 8)
    output = {}
    for field in ("weight", "sensitivity_weight"):
        totals = {
            key: defaultdict(Fraction)
            for key in ("family", "target", "level", "family_target_level")
        }
        total = Fraction()
        for case in weighted:
            weight = read_fraction(case[field])
            total += weight
            totals["family"][case["family"]] += weight
            totals["target"][case["target_class"]] += weight
            totals["level"][str(case["level"])] += weight
            totals["family_target_level"][
                f"{case['family']}/{case['target_class']}/{case['level']}"
            ] += weight
        checks = {
            "family": Fraction(1, 8),
            "target": expected / len(targets),
            "level": expected / len(levels),
            "family_target_level": Fraction(1, 8 * len(targets) * len(levels)),
        }
        if total != expected or any(
            value != checks[name] for name, values in totals.items() for value in values.values()
        ):
            raise WeightError(f"incorrect exact {field} marginals")
        output[field] = {
            "total": fraction_record(total),
            **{
                name: {key: fraction_record(value) for key, value in sorted(values.items())}
                for name, values in totals.items()
            },
        }
    return {
        "schema_version": 1,
        "case_count": len(weighted),
        "input_count": len(grouped),
        "marginals": output,
    }
