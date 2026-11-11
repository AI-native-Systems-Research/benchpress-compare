"""Immutable observations and deterministic LSF job grouping."""

from __future__ import annotations

from collections import defaultdict

from .errors import PreparationError
from .identity import canonical_hash, observation_identity
from .schema import PROTOCOLS

# Measured on the 2026-11-10 baseline screening (worst seed, one level).
# Every observation of these inputs (one case, seed and arm) runs in its own
# LSF job, so no slow compile waits behind another in a sequential shard.
BIG_CIRCUITS = {
    "Q/large/bwt_n37": {"observed_seconds": 1500, "observed_peak_gb": 9},
    "Q/large/vqe_uccsd_n28": {"observed_seconds": 355, "observed_peak_gb": 2},
    "F/hwb12.qasm": {"observed_seconds": 346, "observed_peak_gb": 2.6},
    "Q/medium/bwt_n21": {"observed_seconds": 339, "observed_peak_gb": 2.5},
    "Q/medium/factor247_n15": {"observed_seconds": 338, "observed_peak_gb": 2.3},
    "F/hwb11.qasm": {"observed_seconds": 178, "observed_peak_gb": None},
}


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


def shard_bundles(specs, *, max_observations, job_wall_seconds, overhead_seconds=10):
    """Group observations into LSF jobs without runtime estimates.

    Every observation of a ``BIG_CIRCUITS`` input gets its own job, so
    baseline and evolve run separately. Every other observation is packed in
    plan order, at most ``max_observations`` per job and no more than the job
    wall limit can hold at the frozen watchdog.
    """
    groups, small = [], defaultdict(list)
    for spec in specs:
        if spec["input_id"] in BIG_CIRCUITS:
            groups.append([spec])
        else:
            small[canonical_hash(spec["runtime_profile"])].append(spec)
    for profile in sorted(small):
        rows = small[profile]
        wall = rows[0]["runtime_profile"]["wall_seconds"]
        size = max(1, min(max_observations, int((job_wall_seconds - overhead_seconds) // wall)))
        groups.extend(rows[offset : offset + size] for offset in range(0, len(rows), size))
    bundles = []
    for rows in groups:
        job = {
            "observations": rows,
            # Worst case: every observation runs to its frozen watchdog.
            "estimated_seconds": overhead_seconds
            + sum(row["runtime_profile"]["wall_seconds"] for row in rows),
            "runtime_profile": rows[0]["runtime_profile"],
        }
        bundles.append({"schema_version": 1, "bundle_id": canonical_hash(job), **job})
    return bundles
