"""Immutable observations and deterministic resource-compatible job packing."""

from __future__ import annotations

from collections import defaultdict

from .errors import PreparationError
from .identity import canonical_hash, observation_identity
from .schema import PROTOCOLS


def observations_for(cases, builds, *, phase="comparison", profiles=None):
    specs = []
    arms = ("baseline", "evolve") if phase == "comparison" else ("baseline",)
    for case in sorted(cases, key=lambda c: c["case_id"]):
        for index, seed in enumerate(case["seeds"]):
            ordered = arms if (int(case["case_id"][:8], 16) + index) % 2 == 0 else arms[::-1]
            for arm in ordered:
                spec = {
                    "schema_version": 1,
                    "case_id": case["case_id"],
                    "input_id": case["input_id"],
                    "arm": arm,
                    "phase": phase,
                    "build_id": builds[arm].get("build_id", builds[arm]["build_key"]),
                    "seed": seed,
                    "level": case["level"],
                    "protocols": PROTOCOLS,
                    "fixture_path": case["fixture_path"],
                    "fixture_hash": case["fixture_hash"],
                    "target_path": case["target_path"],
                    "target_hash": case["target_hash"],
                    "runtime_profile": (profiles or {}).get(
                        case["case_id"], case.get("runtime_profile")
                    ),
                    "python": builds[arm]["env_python"],
                }
                if "manifest_path" in builds[arm]:
                    spec["manifest_path"] = builds[arm]["manifest_path"]
                if not spec["runtime_profile"]:
                    raise PreparationError("Every observation requires a frozen runtime profile")
                spec["observation_id"] = observation_identity(spec)
                specs.append(spec)
    return specs


def pack(specs, estimates, *, wall_limit, overhead_seconds=10, margin=0.1, completed=()):
    """Longest-first packing, splitting oversized cases into seed pairs then singles."""
    if wall_limit <= overhead_seconds or not 0 <= margin < 1:
        raise PreparationError("Invalid scheduler wall limit/margin")
    completed = set(completed)
    available = (wall_limit - overhead_seconds) * (1 - margin)
    grouped = defaultdict(list)
    for spec in specs:
        # Paired A/A builds can share a scientific key, but remain separate work.
        if (spec["arm"], spec["observation_id"]) not in completed:
            grouped[spec["case_id"]].append(spec)
    bundles = []
    for case_id in sorted(grouped):
        rows = grouped[case_id]
        cost = estimates[case_id]["estimated_seconds"]
        if cost > available:
            raise PreparationError(f"Observation {case_id} cannot fit scheduler wall limit")
        parts = [rows]
        if len(rows) * cost > available:
            by_seed = defaultdict(list)
            for row in rows:
                by_seed[row["seed"]].append(row)
            parts = list(by_seed.values())
        for part in parts:
            for final in [part] if len(part) * cost <= available else [[row] for row in part]:
                profile = canonical_hash(final[0]["runtime_profile"])
                if any(canonical_hash(row["runtime_profile"]) != profile for row in final):
                    raise PreparationError("Paired observations must have identical resources")
                bundles.append(
                    {"observations": final, "seconds": len(final) * cost, "profile": profile}
                )
    bins = []
    for bundle in sorted(
        bundles, key=lambda b: (-b["seconds"], b["observations"][0]["observation_id"])
    ):
        target = next(
            (
                job
                for job in bins
                if job["profile"] == bundle["profile"]
                and job["seconds"] + bundle["seconds"] <= available
            ),
            None,
        )
        if target is None:
            target = {"profile": bundle["profile"], "seconds": 0, "observations": []}
            bins.append(target)
        target["seconds"] += bundle["seconds"]
        target["observations"].extend(bundle["observations"])
    return [
        {
            "schema_version": 1,
            "bundle_id": canonical_hash(job),
            "estimated_seconds": job["seconds"] + overhead_seconds,
            "runtime_profile": job["observations"][0]["runtime_profile"],
            "observations": job["observations"],
        }
        for job in bins
    ]


def recommendations(
    specs, estimates, bundles, resources, *, workers="auto", max_jobs=None, cached_baseline_ids=()
):
    from .pilot import worker_capacity

    if not specs:
        raise PreparationError("Cannot plan an empty comparison")
    profiles = {canonical_hash(spec["runtime_profile"]): spec["runtime_profile"] for spec in specs}
    capacity = min(
        worker_capacity(resources, profile, len(specs), workers) for profile in profiles.values()
    )
    if max_jobs is not None:
        capacity = min(capacity, max_jobs)
    cached = set(cached_baseline_ids)
    costs, savings = [], []
    for spec in specs:
        cost = estimates[spec["case_id"]]["estimated_seconds"]
        if spec["arm"] == "baseline" and spec["observation_id"] in cached:
            savings.append(cost)
        else:
            costs.append((cost, spec["runtime_profile"]["native_threads"]))
    seconds = sum(cost for cost, _ in costs)
    return {
        "schema_version": 1,
        "recommended_concurrency": capacity,
        "planned_jobs": len(bundles),
        "estimated_cpu_hours": sum(t * n for t, n in costs) / 3600,
        "estimated_elapsed_seconds": {"lower": seconds / capacity, "upper": seconds},
        "cache_saved_observations": len(savings),
        "cache_saved_worker_seconds": sum(savings),
        "profiles": list(profiles.values()),
        "inventory": resources,
        "assumptions": [
            "baseline-derived estimates apply to both arms",
            "queue wait excluded",
            "conservative common-profile concurrency; heterogeneous token scheduling may improve it",
        ],
    }
