import pytest

from benchpress_compare.errors import PreparationError
from benchpress_compare.pilot import estimate, pilot_selection, worker_capacity
from benchpress_compare.planner import pack, recommendations


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


def resource_sample(size, *, arm="baseline", phase="preflight"):
    return {
        "case_id": "c",
        "status": "SUCCEEDED",
        "arm": arm,
        "phase": phase,
        "resources": {
            "elapsed_seconds": 1,
            "peak_rss_bytes": 1024,
            "persisted_artifact_bytes": size,
        },
    }


def test_artifact_estimate_uses_maximum_baseline_preflight_and_pilot_size():
    observed = estimate(
        [{"case_id": "c"}],
        [resource_sample(100), resource_sample(150, phase="pilot"), resource_sample(999, arm="evolve")],
    )["cases"]["c"]
    assert observed["artifact_size_status"] == "available"
    assert observed["artifact_size_sample_count"] == 2
    assert observed["observed_max_artifact_bytes"] == 150


@pytest.mark.parametrize("missing", [None, -1, True])
def test_missing_or_invalid_artifact_telemetry_is_explicitly_unavailable(missing):
    observed = estimate(
        [{"case_id": "c"}], [resource_sample(100), resource_sample(missing)]
    )["cases"]["c"]
    assert observed["artifact_size_status"] == "unavailable"
    assert observed["artifact_size_sample_count"] == 1
    assert observed["observed_max_artifact_bytes"] is None


@pytest.mark.parametrize("artifact_bytes", [100, None])
def test_disk_forecast_counts_retained_copies_and_reports_missing_evidence(artifact_bytes):
    specs = [
        {
            "case_id": "c",
            "observation_id": f"o{seed}",
            "arm": arm,
            "runtime_profile": {"native_threads": 1, "memory_bytes": 20},
        }
        for seed in range(2)
        for arm in ("baseline", "evolve")
    ]
    estimates = {
        "c": {
            "estimated_seconds": 10,
            "observed_max_artifact_bytes": artifact_bytes,
            "artifact_size_status": "available" if artifact_bytes is not None else "unavailable",
        }
    }
    recommendation = recommendations(
        specs, estimates, [], {"usable_cpus": 4, "worker_memory_budget_bytes": 100},
        cached_baseline_ids=["o0"],
    )
    assert recommendation["cache_saved_observations"] == 1
    if artifact_bytes is None:
        assert recommendation["estimated_additional_artifact_bytes"] is None
        assert recommendation["artifact_estimate_status"] == "unavailable"
        assert recommendation["artifact_estimate_missing_cases"] == ["c"]
    else:
        # One cached baseline result, one new baseline with three copies,
        # and two evolved observations with two copies each.
        assert recommendation["estimated_additional_artifact_bytes"] == 800
        assert recommendation["artifact_estimate_status"] == "available"
        assert recommendation["artifact_estimate_missing_cases"] == []
