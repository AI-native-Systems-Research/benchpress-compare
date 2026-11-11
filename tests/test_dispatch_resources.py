import threading

import pytest

from benchpress_compare.config import RunConfig
from benchpress_compare.identity import atomic_write_json, read_json
from benchpress_compare.orchestrator import ExecutionEngine
from benchpress_compare.pilot import runtime_profile
from benchpress_compare.store import Lease


@pytest.mark.parametrize(
    "max_cpus,max_memory_gb,native_threads,max_jobs,submit_workers,count,capacity",
    [(4, 10, 2, 8, 4, 6, 2), (20, 0.5, 1, 8, 4, 6, 2), (150, 2000, 1, 150, 8, 166, 150)],
    ids=["cpu-bound", "memory-bound", "150-jobs"],
)
def test_parallel_submissions_fill_capacity_without_overshooting_budgets(
    tmp_path, max_cpus, max_memory_gb, native_threads, max_jobs, submit_workers, count, capacity
):
    site = tmp_path / "site.toml"
    site.write_text(
        '[lsf]\nqueue="fake"\n'
        f"max_jobs={max_jobs}\nsubmit_workers={submit_workers}\n"
        f"max_cpus={max_cpus}\nmax_memory_gb={max_memory_gb}\n"
    )
    root = tmp_path / "run"
    config = RunConfig(
        "/unused/baseline",
        "/unused/evolve",
        str(tmp_path / "store"),
        str(root),
        executor="lsf",
        lsf_config=str(site),
    )
    profile = runtime_profile(native_threads=native_threads, memory_gb=0.25)
    specs = [
        {
            "schema_version": 1,
            "task": "build",
            "observation_id": str(index),
            "runtime_profile": profile,
        }
        for index in range(count)
    ]

    class Scheduler:
        def __init__(self):
            self.lock = threading.Lock()
            self.pair = threading.Barrier(2)
            self.live = {}
            self.submissions = 0
            self.peak_jobs = 0
            self.poll_loads = []

        def submit(self, command, directory, profile, *, name, token):
            with self.lock:
                self.live[token] = profile
                self.submissions += 1
                self.peak_jobs = max(self.peak_jobs, len(self.live))
                assert len(self.live) <= max_jobs
                assert sum(p["native_threads"] for p in self.live.values()) <= max_cpus
                assert sum(p["memory_bytes"] for p in self.live.values()) <= max_memory_gb * 1024**3
            # Require submissions to overlap in pairs. The 150-job case uses
            # eight controller threads, independent of scheduler concurrency.
            self.pair.wait(timeout=5)
            spec = read_json(directory / "spec.json")
            atomic_write_json(directory / "receipt.json", {**spec, "status": "SUCCEEDED"})
            return {"token": token, "status": "QUEUED", "name": name}

        def poll_many(self, jobs):
            # Jobs keep their resources until the controller polls, even after
            # submit returns, so later submissions cannot escape the budget.
            with self.lock:
                assert set(jobs) == set(self.live)
                self.poll_loads.append(len(self.live))
                self.live.clear()
            return {token: {**job, "status": "DONE"} for token, job in jobs.items()}

    scheduler = Scheduler()
    with Lease(root / "state/controller.lease") as lease:
        engine = ExecutionEngine(root, config, lease, executor=scheduler)
        engine.poll_interval = 60
        results = engine.run(specs)
    assert [result["status"] for result in results] == ["SUCCEEDED"] * count
    assert scheduler.submissions == count
    assert scheduler.peak_jobs == capacity
    assert scheduler.poll_loads == [
        min(capacity, count - offset) for offset in range(0, count, capacity)
    ]
