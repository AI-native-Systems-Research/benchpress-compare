"""Local resource inventory, worker capacity and observation runtime profiles."""

from __future__ import annotations

import math
import os
from pathlib import Path
import platform

import psutil

from .errors import PreparationError

GIB = 1024**3


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


def runtime_profile(*, native_threads=1, wall_seconds=3600, memory_gb=10, machine=None):
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
