"""Post-collection, baseline-only admission and canonical deduplication."""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from .validation import REQUIRED_CHECKS
from .weights import WeightError, assign_case_weights, audit_case_weights

ADMISSION_POLICY = "post-collection-baseline-success-v2"
REQUIRED_VALIDATION_CHECKS = frozenset(REQUIRED_CHECKS)
_SUCCESS = {"SUCCEEDED", "SUCCESS", "COMPLETE"}


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


def admit(cases: Iterable[dict], observations: Iterable[dict]) -> dict:
    """Decide which inputs are scored, from comparison baseline results only.

    An input is dropped when any planned baseline observation of any of its
    cases did not succeed or is missing. Evolve results are never read here, so
    an evolve failure keeps its input in the score, where it counts against
    evolve. Declared structural variants share one weight: the first of
    ``[canonical, *sorted(variants)]`` whose baseline succeeded is scored.
    """
    cases = list(cases)
    baseline = {
        (item["case_id"], item["seed"]): item
        for item in observations
        if item.get("arm") == "baseline" and item.get("phase", "comparison") == "comparison"
    }
    failures: dict[str, list[dict]] = defaultdict(list)
    canonicals: dict[str, str | None] = {}
    for case in cases:
        canonicals[case["input_id"]] = case.get("structural_canonical")
        for seed in case["seeds"]:
            item = baseline.get((case["case_id"], seed))
            if item is None:
                status, reason = "MISSING", "no_committed_result"
            elif str(item.get("status", "")).upper() in _SUCCESS and _validation_verified(item):
                continue
            else:
                status, reason = item.get("status"), item.get("reason")
            failures[case["input_id"]].append(
                {
                    "case_id": case["case_id"],
                    "target_class": case["target_class"],
                    "level": case["level"],
                    "seed": seed,
                    "status": status,
                    "reason": reason,
                }
            )
    records = {}
    for key in sorted(canonicals):
        problems = failures.get(key, [])
        records[key] = {
            "input_id": key,
            "status": "dropped" if problems else "admitted",
            "reasons": sorted(
                {
                    "baseline_missing" if row["status"] == "MISSING" else "baseline_failed"
                    for row in problems
                }
            ),
            "baseline_failures": problems,
        }
    groups: dict[str, list[str]] = defaultdict(list)
    for key, canonical in canonicals.items():
        if canonical:
            groups[canonical].append(key)
    for canonical, variants in sorted(groups.items()):
        priority = ([canonical] if canonical in records else []) + sorted(variants)
        representative = next(
            (key for key in priority if records[key]["status"] == "admitted"), None
        )
        for key in priority:
            if key == representative:
                records[key]["structural_group"] = "|".join(priority)
            elif records[key]["status"] == "admitted":
                records[key].update(
                    status="dropped",
                    reasons=["structural_duplicate"],
                    representative=representative,
                )
    return {
        "schema_version": 1,
        "policy": ADMISSION_POLICY,
        "candidate_count": len(records),
        "admitted": sorted(key for key, row in records.items() if row["status"] == "admitted"),
        "dropped": sorted(key for key, row in records.items() if row["status"] == "dropped"),
        "records": [records[key] for key in sorted(records)],
    }


def apply_admission(plan: dict, observations: Iterable[dict]) -> dict:
    """Return the plan with post-collection admission and exact case weights.

    Dropped inputs stay in the report as zero-weight supplemental cases. If
    the admitted inputs cannot satisfy the design (for example an empty family
    in the eight-family design), every case gets zero weight and the reason is
    recorded, so the report shows no score instead of a redistributed one.
    """
    observations = list(observations)
    admission = admit(plan["cases"], observations)
    records = {row["input_id"]: row for row in admission["records"]}
    cases = []
    for case in plan["cases"]:
        record = records[case["input_id"]]
        case = dict(case, supplemental=record["status"] != "admitted")
        if "structural_group" in record:
            case["verified_structural_group"] = record["structural_group"]
        cases.append(case)
    config = plan.get("config", {})
    require_all = not (config.get("smoke") or config.get("allow_missing_families"))
    try:
        cases = assign_case_weights(cases, require_all_families=require_all)
        audit = audit_case_weights(cases, require_all_families=require_all)
        admission["weights_error"] = None
    except WeightError as exc:
        zero = {"numerator": 0, "denominator": 1}
        cases = [dict(case, weight=zero, sensitivity_weight=zero) for case in cases]
        audit = None
        admission["weights_error"] = str(exc)
    return {**plan, "cases": cases, "admission": admission, "weights_audit": audit}
