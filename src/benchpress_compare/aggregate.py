"""Offline complete-population scoring with exact case means and classifications."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from fractions import Fraction
from typing import Iterable, Mapping

from .weights import fraction_record, read_fraction

AGGREGATION_PROTOCOL = "ratio-of-arithmetic-means-geometric-v1"
METRICS = ("N2", "D2")
_FILTERS = {
    "circuit": "input_id", "input_id": "input_id",
    "target": "target_class", "target_class": "target_class",
    "target_id": "target_id", "target_snapshot": "target_id", "target_hash": "target_hash",
    "level": "level", "family": "family", "band": "band", "recipe": "recipe",
}
_SUCCESS = {"SUCCEEDED", "SUCCESS", "COMPLETE"}


class AggregationError(ValueError):
    """The immutable case or observation data contains an integrity error."""


def normalize_query(query: Mapping | None = None) -> dict[str, list]:
    normalized: dict[str, set] = defaultdict(set)
    for key, values in (query or {}).items():
        if key not in _FILTERS:
            raise AggregationError(f"unknown slice dimension: {key}")
        field = _FILTERS[key]
        if values is None:
            continue
        if not isinstance(values, (list, tuple, set, frozenset)):
            values = [values]
        for value in values:
            if field == "level":
                if isinstance(value, str) and value in ("2", "3", "L2", "L3"):
                    value = int(value[-1])
                if type(value) is not int or value not in (2, 3):
                    raise AggregationError("level must be 2 or 3")
            elif not isinstance(value, str):
                raise AggregationError(f"{key} filter values must be strings")
            normalized[field].add(value)
    return {field: sorted(values) for field, values in sorted(normalized.items()) if values}


def select_cases(cases: Iterable[dict], query: Mapping | None = None) -> list[dict]:
    normalized = normalize_query(query)
    return sorted(
        (case for case in cases if all(case.get(field) in values for field, values in normalized.items())),
        key=lambda case: case["case_id"],
    )


def _observation_index(observations: Iterable[dict]) -> dict[tuple, dict]:
    index = {}
    for observation in observations:
        if observation.get("phase", "comparison") != "comparison":
            continue
        key = (observation["case_id"], observation["arm"], observation["seed"])
        if key[1] not in ("baseline", "evolve") or type(key[2]) is not int:
            raise AggregationError(f"invalid observation identity: {key}")
        if key in index:
            if index[key] != observation:
                raise AggregationError(f"conflicting accepted results for {key}")
            continue
        index[key] = observation
    return index


def _value(observation: dict | None, metric: str) -> tuple[int | None, str | None]:
    if observation is None:
        return None, "missing_observation"
    status = str(observation.get("status", "")).upper()
    if status not in _SUCCESS:
        return None, "observation_" + (status.lower() or "missing_status")
    value = observation.get(metric, observation.get("metrics", {}).get(metric))
    reason = observation.get("metric_reasons", {}).get(metric)
    if isinstance(value, dict):
        reason = value.get("reason", reason)
        value = value.get("value")
    if value is None:
        return None, reason or f"{metric}_unavailable"
    if type(value) is not int or value < 0:
        return None, "invalid_metric_integrity"
    return value, None


def _spread(values: list[int]) -> dict:
    if not values:
        return {"min": None, "max": None, "range": None, "population_stddev": None}
    mean = Fraction(sum(values), len(values))
    variance = sum(((Fraction(value) - mean) ** 2 for value in values), Fraction()) / len(values)
    return {"min": min(values), "max": max(values), "range": max(values) - min(values), "population_stddev": math.sqrt(float(variance))}


def _case_metric(case: dict, index: dict, metric: str) -> dict:
    seeds = case["seeds"]
    reasons: list[str] = []
    means: dict[str, Fraction | None] = {}
    raw = {}
    spread = {}
    completed_observations = 0
    for arm in ("baseline", "evolve"):
        raw[arm] = []
        values = []
        for seed in seeds:
            value, reason = _value(index.get((case["case_id"], arm, seed)), metric)
            raw[arm].append({"seed": seed, "value": value, "reason": reason})
            if reason:
                reasons.append(reason)
            else:
                values.append(value)
                completed_observations += 1
        means[arm] = Fraction(sum(values), len(seeds)) if len(values) == len(seeds) else None
        spread[arm] = _spread(values) if len(values) == len(seeds) else _spread([])
    ratio = None
    absolute = None
    baseline, evolve = means["baseline"], means["evolve"]
    if not reasons:
        absolute = "better" if evolve < baseline else "worse" if evolve > baseline else "same"
        if baseline == 0:
            reasons.append("both_zero" if evolve == 0 else "baseline_zero")
        elif evolve == 0:
            ratio = Fraction()
            reasons.append("evolve_zero")
        else:
            ratio = evolve / baseline
    return {
        "available": not reasons,
        "reasons": sorted(set(reasons)),
        "baseline_mean": fraction_record(baseline) if baseline is not None else None,
        "evolve_mean": fraction_record(evolve) if evolve is not None else None,
        "ratio": fraction_record(ratio) if ratio is not None else None,
        "ratio_display": float(ratio) if ratio is not None else None,
        "absolute_direction": absolute,
        "raw": raw,
        "spread": spread,
        "expected_observations": 2 * len(seeds),
        "usable_observations": completed_observations,
        "integrity_error": "invalid_metric_integrity" in reasons,
    }


def _geometric(ratios: list[tuple[Fraction, Fraction]]) -> float:
    weight = sum((item[1] for item in ratios), Fraction())
    logscore = math.fsum(float(w / weight) * (math.log(ratio.numerator) - math.log(ratio.denominator)) for ratio, w in ratios)
    try:
        score = math.exp(logscore)
    except OverflowError as exc:
        raise AggregationError("geometric score exceeds finite display range") from exc
    if not math.isfinite(score) or score <= 0:
        raise AggregationError("geometric score cannot be represented as a positive finite number")
    return score


def aggregate(
    cases: Iterable[dict],
    observations: Iterable[dict],
    *,
    query: Mapping | None = None,
    weight_field: str = "weight",
) -> dict:
    """Aggregate only planned cases, never select the population by result status.

    Zero-weight cases are retained as ``supplemental_cases`` and excluded from
    main circuit counts. The selected weight mass is fixed before reading data.
    """
    if weight_field not in ("weight", "sensitivity_weight"):
        raise AggregationError("unknown weight field")
    cases = list(cases)
    if len({case["case_id"] for case in cases}) != len(cases):
        raise AggregationError("duplicate planned case identity")
    for case in cases:
        seeds = case.get("seeds", [])
        if not seeds or len(set(seeds)) != len(seeds) or any(type(seed) is not int or seed < 0 for seed in seeds):
            raise AggregationError(f"invalid seed list for {case['case_id']}")
        if read_fraction(case[weight_field]) < 0:
            raise AggregationError("negative planned weight")
    normalized = normalize_query(query)
    selected = select_cases(cases, normalized)
    index = _observation_index(observations)
    weighted = [case for case in selected if read_fraction(case[weight_field]) > 0]
    weight_mass = sum((read_fraction(case[weight_field]) for case in weighted), Fraction())
    details = []
    supplemental = []
    circuit_cases = defaultdict(list)
    for case in selected:
        row = {field: case.get(field) for field in ("case_id", "input_id", "target_class", "target_id", "level", "family", "band", "recipe", "active_width", "declared_width", "occupancy")}
        row["weight"] = fraction_record(read_fraction(case[weight_field]))
        row["metrics"] = {metric: _case_metric(case, index, metric) for metric in METRICS}
        if read_fraction(case[weight_field]) > 0:
            details.append(row)
            circuit_cases[case["input_id"]].append(row)
        else:
            supplemental.append(row)
    for input_id, group in circuit_cases.items():
        if len({read_fraction(row["weight"]) for row in group}) != 1:
            raise AggregationError(f"unequal selected case weights for circuit {input_id}")
    metrics = {}
    circuit_details = {key: {"input_id": key, "case_count": len(group), "metrics": {}} for key, group in sorted(circuit_cases.items())}
    for metric in METRICS:
        counts = Counter({key: 0 for key in ("better", "same", "worse", "unclassifiable")})
        circuit_reasons = Counter()
        for input_id, group in circuit_cases.items():
            reasons = sorted({reason for row in group for reason in row["metrics"][metric]["reasons"]})
            product = Fraction(1)
            score = None
            if reasons:
                classification = "unclassifiable"
                circuit_reasons.update(reasons)
            else:
                for row in group:
                    product *= read_fraction(row["metrics"][metric]["ratio"])
                classification = "better" if product < 1 else "worse" if product > 1 else "same"
                score = _geometric([(read_fraction(row["metrics"][metric]["ratio"]), read_fraction(row["weight"])) for row in group])
            counts[classification] += 1
            circuit_details[input_id]["metrics"][metric] = {
                "classification": classification, "reasons": reasons, "score": score,
                "exact_ratio_product": fraction_record(product) if not reasons else None,
            }
        bad = [row for row in details if not row["metrics"][metric]["available"]]
        reasons = Counter(reason for row in bad for reason in row["metrics"][metric]["reasons"])
        if not details:
            reasons["empty_slice"] += 1
        score = None
        if details and not bad:
            score = _geometric([(read_fraction(row["metrics"][metric]["ratio"]), read_fraction(row["weight"])) for row in details])
        available_weight = sum((read_fraction(row["weight"]) for row in details if row["metrics"][metric]["available"]), Fraction())
        metrics[metric] = {
            "available": score is not None,
            "score": score,
            "change_percent": 100 * (score - 1) if score is not None else None,
            "reasons": dict(sorted(reasons.items())),
            "circuit_counts": dict(counts),
            "unclassifiable_circuit_reasons": dict(sorted(circuit_reasons.items())),
            "available_cases": len(details) - len(bad),
            "available_weight": fraction_record(available_weight),
            "usable_observations": sum(row["metrics"][metric]["usable_observations"] for row in details),
            "integrity_errors": sum(row["metrics"][metric]["integrity_error"] for row in details),
        }
    expected_keys = {(case["case_id"], arm, seed) for case in weighted for arm in ("baseline", "evolve") for seed in case["seeds"]}
    observed = [index[key] for key in expected_keys if key in index]
    return {
        "schema_version": 1,
        "protocol": AGGREGATION_PROTOCOL,
        "query": normalized,
        "weight_field": weight_field,
        "weight_mass": fraction_record(weight_mass),
        "expected_circuits": len(circuit_cases),
        "expected_cases": len(weighted),
        "expected_observations": len(expected_keys),
        "received_observations": len(observed),
        "successful_observations": sum(str(item.get("status", "")).upper() in _SUCCESS for item in observed),
        "failed_observations": sum(str(item.get("status", "")).upper() not in _SUCCESS for item in observed),
        "cache_hits": sum(bool(item.get("cache_hit")) for item in observed),
        "metrics": metrics,
        "cases": details,
        "circuits": list(circuit_details.values()),
        "supplemental_cases": supplemental,
    }
