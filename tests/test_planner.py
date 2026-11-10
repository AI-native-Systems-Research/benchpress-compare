import pytest

from benchpress_compare.corpus import load_catalogue
from benchpress_compare.errors import PreparationError
from benchpress_compare.pilot import worker_capacity
from benchpress_compare.planner import BIG_CIRCUITS, shard_bundles


def test_zero_capacity_and_override():
    profile = {"native_threads": 2, "memory_bytes": 20}
    with pytest.raises(PreparationError, match="Zero"):
        worker_capacity({"usable_cpus": 1, "worker_memory_budget_bytes": 100}, profile, 2)
    resources = {"usable_cpus": 4, "worker_memory_budget_bytes": 100}
    assert worker_capacity(resources, profile, 8) == 2
    with pytest.raises(PreparationError, match="exceeds"):
        worker_capacity(resources, profile, 8, workers="3")


def test_big_circuits_are_catalogue_inputs():
    ids = {row["input_id"] for row in load_catalogue()["candidates"]}
    assert set(BIG_CIRCUITS) <= ids


def specs(input_id, cases, *, wall=100):
    return [
        {
            "case_id": f"{input_id}#{case}",
            "input_id": input_id,
            "observation_id": f"{input_id}#{case}/{arm}/{seed}",
            "seed": seed,
            "arm": arm,
            "runtime_profile": {"native_threads": 1, "wall_seconds": wall},
        }
        for case in range(cases)
        for seed in range(3)
        for arm in ("baseline", "evolve")
    ]


def test_each_big_observation_is_its_own_job_and_small_work_is_chunked():
    big = specs("Q/large/bwt_n37", 6)
    small = specs("Q/small/ordinary", 5)
    bundles = shard_bundles(big + small, max_observations=6, job_wall_seconds=10_000)
    big_jobs = [b for b in bundles if b["observations"][0]["input_id"] in BIG_CIRCUITS]
    assert len(big_jobs) == 6 * 3 * 2
    assert all(len(job["observations"]) == 1 for job in big_jobs)
    small_jobs = [b for b in bundles if b not in big_jobs]
    assert [len(b["observations"]) for b in small_jobs] == [6] * 5
    assert all(len({row["case_id"] for row in b["observations"]}) == 1 for b in small_jobs)
    assert sum(len(b["observations"]) for b in bundles) == len(big + small)
    assert small_jobs[0]["estimated_seconds"] == 10 + 6 * 100


def test_job_wall_limit_bounds_small_shards():
    bundles = shard_bundles(specs("Q/small/ordinary", 1), max_observations=24, job_wall_seconds=310)
    assert [len(b["observations"]) for b in bundles] == [3, 3]


def test_jobs_never_mix_profiles():
    rows = specs("Q/small/ordinary", 1)
    rows[0] = dict(rows[0], runtime_profile={"native_threads": 2, "wall_seconds": 100})
    bundles = shard_bundles(rows, max_observations=24, job_wall_seconds=10_000)
    assert sorted(len(b["observations"]) for b in bundles) == [1, 5]
    for bundle in bundles:
        assert all(
            row["runtime_profile"] == bundle["runtime_profile"] for row in bundle["observations"]
        )
