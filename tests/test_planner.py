import pytest

from benchpress_compare.errors import PreparationError
from benchpress_compare.pilot import pilot_selection, worker_capacity
from benchpress_compare.planner import pack


def test_zero_capacity_and_override():
    profile = {"native_threads": 2, "memory_bytes": 20}
    with pytest.raises(PreparationError, match="Zero"):
        worker_capacity({"usable_cpus": 1, "worker_memory_budget_bytes": 100}, profile, 2)
    resources = {"usable_cpus": 4, "worker_memory_budget_bytes": 100}
    assert worker_capacity(resources, profile, 8) == 2
    with pytest.raises(PreparationError, match="exceeds"):
        worker_capacity(resources, profile, 8, workers="3")


def test_packing_splits_preserving_pairs_and_cache():
    specs = [
        {
            "case_id": "c",
            "observation_id": f"o{s}",
            "seed": s,
            "arm": arm,
            "runtime_profile": {"native_threads": 1},
        }
        for s in range(3)
        for arm in ("baseline", "evolve")
    ]
    estimates = {"c": {"estimated_seconds": 10}}
    jobs = pack(specs, estimates, wall_limit=36)
    assert len(jobs) == 3
    assert all(len(job["observations"]) == 2 for job in jobs)
    cached = pack(specs, estimates, wall_limit=36, completed={("baseline", "o0")})
    assert sum(len(job["observations"]) for job in cached) == 5
    with pytest.raises(PreparationError, match="cannot fit"):
        pack(specs, estimates, wall_limit=15)


def test_pilot_includes_expensive_and_each_stratum():
    cases = [
        {
            "case_id": str(i),
            "input_id": name,
            "family": "F1",
            "band": "S",
            "target_class": "square",
            "level": 2,
        }
        for i, name in enumerate(("ordinary", "also", "Q/medium/factor247_n15"))
    ]
    assert [case["case_id"] for case in pilot_selection(cases)] == ["0", "2"]
