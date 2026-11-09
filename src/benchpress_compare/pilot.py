"""Baseline-only resource estimates and explicit observation resource tiers."""

from __future__ import annotations

import math
import os
from pathlib import Path
import platform

import psutil

from .errors import PreparationError

GIB = 1024**3
MEMORY_TIERS_GB = (1, 2, 4, 8, 16, 32, 64, 128, 256, 512)
WALL_TIERS_SECONDS = (60, 300, 900, 3600, 14400, 43200, 86400, 172800)
EXPENSIVE = (
    "bwt_",
    "factor247",
    "square_root_n60",
    "hwb11",
    "hwb12",
    "vqe_uccsd_n28",
    "ham_ham_JW24",
)


def _read_limit(path: Path):
    try:
        text = path.read_text().strip()
        return None if text == "max" else int(text)
    except (ValueError, OSError):
        return None


def inventory(*, cpu_budget=None, memory_gb=None, cgroup_root=Path("/sys/fs/cgroup")) -> dict:
    cpu = float(os.cpu_count() or 1)
    if hasattr(os, "sched_getaffinity"):
        cpu = min(cpu, len(os.sched_getaffinity(0)))
    memory = psutil.virtual_memory().available
    if platform.system() == "Linux":
        # Resolve our cgroup and include ancestor constraints, including containers.
        roots = [Path(cgroup_root)]
        try:
            for line in Path("/proc/self/cgroup").read_text().splitlines():
                if line.startswith("0::"):
                    group = Path(cgroup_root) / line[3:].lstrip("/")
                    if group.is_relative_to(cgroup_root):
                        roots.extend(
                            [group, *[p for p in group.parents if p.is_relative_to(cgroup_root)]]
                        )
        except OSError:
            pass
        for root in set(roots):
            try:
                quota, period = (root / "cpu.max").read_text().split()
                if quota != "max":
                    cpu = min(cpu, int(quota) / int(period))
            except (OSError, ValueError):
                pass
            limit = _read_limit(root / "memory.max")
            current = _read_limit(root / "memory.current")
            if limit is not None:
                memory = min(memory, max(0, limit - (current or 0)))
        quota = _read_limit(Path(cgroup_root) / "cpu/cpu.cfs_quota_us")
        period = _read_limit(Path(cgroup_root) / "cpu/cpu.cfs_period_us")
        if quota and quota > 0 and period:
            cpu = min(cpu, quota / period)
        limit = _read_limit(Path(cgroup_root) / "memory/memory.limit_in_bytes")
        usage = _read_limit(Path(cgroup_root) / "memory/memory.usage_in_bytes")
        if limit:
            memory = min(memory, max(0, limit - (usage or 0)))
    if cpu_budget is not None:
        cpu = min(cpu, cpu_budget)
    if memory_gb is not None:
        memory = min(memory, int(memory_gb * GIB))
    return {
        "schema_version": 1,
        "usable_cpus": cpu,
        "available_memory_bytes": memory,
        "worker_memory_budget_bytes": int(memory * 0.8),
        "memory_reserve_fraction": 0.2,
        "system": platform.system(),
        "machine": platform.machine(),
        "enforcement": "process-tree-rss-sampling",
        "sample_interval_seconds": 0.1,
        "hard_memory_limit": False,
    }


def worker_capacity(resources: dict, profile: dict, ready_count: int, workers="auto") -> int:
    if ready_count == 0:
        return 0
    cpu_slots = math.floor(resources["usable_cpus"] / profile["native_threads"])
    memory_slots = resources["worker_memory_budget_bytes"] // profile["memory_bytes"]
    capacity = min(cpu_slots, memory_slots)
    if capacity <= 0:
        raise PreparationError(
            f"Zero worker capacity: need {profile['native_threads']} CPUs and "
            f"{profile['memory_bytes']} memory bytes per worker"
        )
    if workers != "auto" and int(workers) > capacity:
        raise PreparationError(f"Requested {workers} workers exceeds capacity {capacity}")
    return min(ready_count, capacity if workers == "auto" else int(workers))


def runtime_profile(*, native_threads=1, wall_seconds=3600, memory_gb=8, machine=None):
    return {
        "schema_version": 1,
        "native_threads": native_threads,
        "wall_seconds": wall_seconds,
        "memory_bytes": int(memory_gb * GIB),
        "machine": machine or f"{platform.system()}-{platform.machine()}",
        "enforcement": "process-tree-rss-sampling",
        "sample_interval_seconds": 0.1,
        "hard_memory_limit": False,
        "thread_environment": {
            "RAYON_NUM_THREADS": str(native_threads),
            "OMP_NUM_THREADS": str(native_threads),
            "OPENBLAS_NUM_THREADS": str(native_threads),
            "MKL_NUM_THREADS": str(native_threads),
            "NUMEXPR_NUM_THREADS": str(native_threads),
            "QISKIT_PARALLEL": "FALSE",
        },
    }


def pilot_selection(cases: list[dict]) -> list[dict]:
    chosen = {}
    strata = {}
    for case in sorted(cases, key=lambda item: item["case_id"]):
        key = (case["family"], case["band"], case["target_class"], case["level"])
        strata.setdefault(key, case)
        if any(name in case["input_id"] for name in EXPENSIVE):
            chosen[case["case_id"]] = case
    chosen.update({case["case_id"]: case for case in strata.values()})
    return [chosen[key] for key in sorted(chosen)]


def estimate(
    cases: list[dict],
    observations: list[dict],
    *,
    native_threads=1,
    max_wall_seconds=172800,
    max_memory_gb=512,
) -> dict:
    estimates = {}
    for case in cases:
        samples = [
            row
            for row in observations
            if row["case_id"] == case["case_id"]
            and row.get("status") == "SUCCEEDED"
            and row.get("arm") == "baseline"
        ]
        if not samples:
            raise PreparationError(f"No successful baseline resource evidence: {case['case_id']}")
        times = [float(row.get("resources", {}).get("elapsed_seconds", 0)) for row in samples]
        memory = [int(row.get("resources", {}).get("peak_rss_bytes", 0)) for row in samples]
        if min(times) <= 0 or min(memory) <= 0:
            raise PreparationError(f"Incomplete resource telemetry: {case['case_id']}")
        required_time, required_memory = 2 * max(times) + 10, math.ceil(1.5 * max(memory))
        wall = next((v for v in WALL_TIERS_SECONDS if v >= required_time), None)
        mem = next((v for v in MEMORY_TIERS_GB if v * GIB >= required_memory), None)
        if wall is None or mem is None or wall > max_wall_seconds or mem > max_memory_gb:
            raise PreparationError(
                f"Case {case['case_id']} needs at least {required_time:.1f}s "
                f"and {required_memory} bytes; raise preparation ceilings"
            )
        estimates[case["case_id"]] = {
            "sample_count": len(samples),
            "observed_max_seconds": max(times),
            "observed_max_rss_bytes": max(memory),
            "estimated_seconds": required_time,
            "extrapolated": False,
            "runtime_profile": runtime_profile(
                native_threads=native_threads, wall_seconds=wall, memory_gb=mem
            ),
        }
    return {
        "schema_version": 1,
        "cases": estimates,
        "memory_factor": 1.5,
        "time_factor": 2,
        "overhead_seconds": 10,
    }
