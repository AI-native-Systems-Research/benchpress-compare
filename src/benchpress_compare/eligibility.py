"""Baseline-only admission, explicit uncertainty, and canonical deduplication."""

from __future__ import annotations

from collections import defaultdict
from statistics import median
from typing import Iterable

from .weights import LEVELS, TARGETS
from .validation import REQUIRED_CHECKS

ADMISSION_POLICY = "baseline-three-seed-six-configuration-v1"
REQUIRED_VALIDATION_CHECKS = frozenset(REQUIRED_CHECKS)
_SUCCESS = {"SUCCEEDED", "SUCCESS", "COMPLETE"}


def _diagnostic(result: dict, key: str):
    if key in result:
        return result[key]
    for section in ("diagnostics", "routing", "passes"):
        data = result.get(section, {})
        if isinstance(data, dict) and key in data:
            return data[key]
    return None


def _validation_verified(result: dict) -> bool:
    validation = result.get("validation")
    if not isinstance(validation, dict):
        return False
    if validation.get("status") != "verified":
        return False
    checks = validation.get("checks", {})
    if isinstance(checks, list):
        if any(not isinstance(check, dict) or not check.get("name") for check in checks):
            return False
        if len({check["name"] for check in checks}) != len(checks):
            return False
        checks = {check["name"]: check for check in checks}
    if not isinstance(checks, dict) or not REQUIRED_VALIDATION_CHECKS.issubset(checks):
        return False
    for name, check in checks.items():
        if not isinstance(check, dict):
            return False
        status = check.get("status")
        # A conclusive negative from any check, disclosed or required, disqualifies.
        if status in {"mismatch", "error"}:
            return False
        if (name in REQUIRED_VALIDATION_CHECKS or check.get("required", True)) and (
            status != "verified"
        ):
            return False
    return True


def evaluate_configuration(observations: Iterable[dict], *, seeds: list[int] | None = None) -> dict:
    """Assess exactly three independent baseline preflight observations."""
    observations = list(observations)
    reasons: list[str] = []
    if any(item.get("arm") != "baseline" for item in observations):
        raise ValueError("evolve evidence is forbidden in admission")
    if any(item.get("phase") != "preflight" for item in observations):
        raise ValueError("only preflight evidence is eligible for admission")
    actual_seeds = [item.get("seed") for item in observations]
    if len(observations) != 3 or len(set(actual_seeds)) != 3:
        reasons.append("missing_or_duplicate_preflight_seeds")
    if seeds is not None:
        if len(seeds) != 3 or len(set(seeds)) != 3:
            raise ValueError("configuration requires exactly three distinct planned seeds")
        if set(actual_seeds) != set(seeds):
            reasons.append("preflight_seed_identity_mismatch")
    inserted = []
    sabre = []
    evidence = []
    for item in observations:
        evidence.append(
            {
                "observation_id": item.get("observation_id"),
                "seed": item.get("seed"),
                "result_hash": item.get("result_hash"),
            }
        )
        if str(item.get("status", "")).upper() not in _SUCCESS:
            reasons.append("preflight_" + str(item.get("status", "missing_status")).lower())
        if not _validation_verified(item):
            reasons.append("validation_unverified")
        for metric in ("N2", "D2"):
            value = item.get(metric, item.get("metrics", {}).get(metric))
            if isinstance(value, dict):
                value = value.get("value")
            if type(value) is not int or value < 0:
                reasons.append(f"{metric}_unavailable")
        executed = _diagnostic(item, "sabre_executed")
        if type(executed) is not bool:
            reasons.append("missing_sabre_instrumentation")
        sabre.append(executed)
        count = _diagnostic(item, "inserted_swaps")
        if type(count) is not int or count < 0:
            reasons.append("missing_inserted_swap_evidence")
        else:
            inserted.append(count)
    if reasons:
        status = "unresolved"
    else:
        if not all(sabre):
            reasons.append("sabre_bypassed")
        if median(inserted) < 10:
            reasons.append("inserted_swap_median_below_10")
        status = "ineligible" if reasons else "qualifying"
    return {
        "schema_version": 1,
        "status": status,
        "reasons": sorted(set(reasons)),
        "seeds": seeds if seeds is not None else actual_seeds,
        "sabre_executed": sabre,
        "inserted_swaps": inserted,
        "median_inserted_swaps": median(inserted) if len(inserted) == 3 else None,
        "evidence": evidence,
    }


def evaluate_input(
    input_id: str,
    observations: Iterable[dict],
    *,
    embedding: dict | None = None,
    expected_cases: Iterable[dict] | None = None,
) -> dict:
    observations = list(observations)
    if any(item.get("input_id", input_id) != input_id for item in observations):
        raise ValueError("observation input identity mismatch")
    expected = (
        list(expected_cases)
        if expected_cases is not None
        else [{"target_class": target, "level": level} for target in TARGETS for level in LEVELS]
    )
    configs = [(case["target_class"], case["level"]) for case in expected]
    if sorted(configs) != sorted((target, level) for target in TARGETS for level in LEVELS):
        raise ValueError("admission requires exactly six standard configurations")
    by_case = {case["case_id"]: case for case in expected if "case_id" in case}
    grouped = defaultdict(list)
    for item in observations:
        case = by_case.get(item.get("case_id"), item)
        configuration = (case.get("target_class"), case.get("level"))
        if configuration not in configs:
            raise ValueError(f"unexpected preflight configuration: {configuration}")
        grouped[configuration].append(item)
    records = []
    for case in expected:
        result = evaluate_configuration(
            grouped[case["target_class"], case["level"]],
            seeds=case.get("preflight_seeds", case.get("seeds")),
        )
        records.append(
            {
                "case_id": case.get("case_id"),
                "target_class": case["target_class"],
                "level": case["level"],
                **result,
            }
        )
    embedding = dict(
        embedding or {"status": "unknown", "reason": "no_verified_embedding_certificate"}
    )
    if (
        embedding.get("status") in {"verified", "verified_zero_routing"}
        and embedding.get("zero_routing", True) is True
    ):
        if not embedding.get("evidence"):
            raise ValueError("verified zero-routing embedding requires evidence")
        status, reasons = "ineligible", ["verified_zero_routing_embedding"]
    elif any(record["status"] == "unresolved" for record in records):
        status, reasons = "unresolved", ["unresolved_configuration"]
    elif any(record["status"] == "ineligible" for record in records):
        status, reasons = "ineligible", ["nonqualifying_configuration"]
    else:
        status, reasons = "admitted", []
    return {
        "schema_version": 1,
        "policy": ADMISSION_POLICY,
        "input_id": input_id,
        "status": status,
        "reasons": reasons,
        "embedding": embedding,
        "configurations": records,
        "qualifying_configurations": [
            {"target_class": record["target_class"], "level": record["level"]}
            for record in records
            if record["status"] == "qualifying"
        ],
    }


def resolve_candidates(
    candidates: Iterable[dict],
    eligibility: Iterable[dict],
    *,
    structural_groups: Iterable[dict] = (),
    accept_unresolved: bool = False,
) -> dict:
    """Select verified structural representatives using a predeclared priority.

    Structural group records contain ``status='verified'``, ``members`` and an
    ordered ``priority`` listing every member, plus ``evidence``. Unverified
    suspected groups block normal freeze rather than silently double-weighting.
    """
    candidates = list(candidates)
    candidate_ids = [item["input_id"] for item in candidates]
    if len(set(candidate_ids)) != len(candidate_ids):
        raise ValueError("duplicate candidate identity")
    records = {}
    for record in eligibility:
        key = record["input_id"]
        if key in records or key not in candidate_ids:
            raise ValueError(f"unexpected or duplicate eligibility record: {key}")
        records[key] = dict(record)
    for key in candidate_ids:
        if key not in records:
            records[key] = {
                "schema_version": 1,
                "input_id": key,
                "status": "unresolved",
                "reasons": ["missing_eligibility"],
            }
    assigned: set[str] = set()
    for group in structural_groups:
        members = list(group["members"])
        priority = list(group["priority"])
        if (
            set(priority) != set(members)
            or len(priority) != len(members)
            or len(set(members)) != len(members)
        ):
            raise ValueError("structural priority must list every group member exactly once")
        if set(members) - set(candidate_ids) or assigned & set(members):
            raise ValueError("unknown or overlapping structural group members")
        assigned.update(members)
        if group.get("status") != "verified" or not group.get("evidence"):
            for key in members:
                records[key] = {
                    **records[key],
                    "status": "unresolved",
                    "reasons": ["unresolved_structural_equivalence"],
                }
            continue
        representative = next(
            (key for key in priority if records[key]["status"] == "admitted"), None
        )
        for key in members:
            if key == representative:
                records[key]["verified_structural_group"] = group.get(
                    "group_id", "|".join(priority)
                )
            elif records[key]["status"] == "admitted":
                records[key] = {
                    **records[key],
                    "status": "ineligible",
                    "reasons": ["structural_duplicate"],
                    "representative": representative,
                }
    unresolved = sorted(key for key, record in records.items() if record["status"] == "unresolved")
    admitted = sorted(key for key, record in records.items() if record["status"] == "admitted")
    return {
        "schema_version": 1,
        "policy": ADMISSION_POLICY,
        "candidate_count": len(candidates),
        "admitted": admitted,
        "unresolved": unresolved,
        "freeze_allowed": not unresolved or accept_unresolved,
        "conditional_on_unresolved": bool(unresolved and accept_unresolved),
        "requires_preparation_revision": bool(unresolved and accept_unresolved),
        "records": [records[key] for key in sorted(records)],
    }
