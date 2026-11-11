import os
import stat
import threading
import time

import pytest

from benchpress_compare.config import RunConfig
from benchpress_compare.identity import atomic_write_json, read_json
from benchpress_compare.journal import Journal
from benchpress_compare.orchestrator import ExecutionEngine
from benchpress_compare.pilot import runtime_profile
from benchpress_compare.store import Lease


class ExpiringScheduler:
    """Short jobs finish on the clock, even while the controller is doing I/O."""

    def __init__(self, lifetime):
        self.lifetime = lifetime
        self.lock = threading.Lock()
        self.intervals = {}
        self.peak_jobs = 0

    def submit(self, command, directory, profile, *, name, token):
        spec = read_json(directory / "spec.json")
        atomic_write_json(directory / "receipt.json", {**spec, "status": "SUCCEEDED"})
        job = {"token": token, "status": "QUEUED", "name": name}
        atomic_write_json(directory / "job.json", job)
        with self.lock:
            now = time.monotonic()
            assert token not in self.intervals
            self.intervals[token] = (now, now + self.lifetime)
            live = sum(end > now for _, end in self.intervals.values())
            self.peak_jobs = max(self.peak_jobs, live)
        return job

    def poll_many(self, jobs):
        now = time.monotonic()
        return {
            token: {
                **job,
                "status": "DONE" if self.intervals[token][1] <= now else "RUNNING",
            }
            for token, job in jobs.items()
        }


@pytest.mark.parametrize("capacity,controller_workers", [(150, 8), (300, 16)])
def test_short_jobs_fill_capacity_despite_slow_durable_storage(
    tmp_path, monkeypatch, record_property, capacity, controller_workers
):
    # A scheduler that completes jobs only when polled can fill capacity even when
    # bookkeeping is entirely serial. These jobs expire independently, and file
    # fsync latency models the shared filesystem that caused the live short-job
    # run to drain while the controller was preparing or collecting work.
    fsync = os.fsync

    def slow_file_fsync(descriptor):
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            time.sleep(0.01)
        fsync(descriptor)

    monkeypatch.setattr(os, "fsync", slow_file_fsync)
    site = tmp_path / "site.toml"
    site.write_text(
        '[lsf]\nqueue="fake"\n'
        f"max_jobs={capacity}\nmax_cpus={capacity}\nmax_memory_gb={capacity // 150 * 2000}\n"
        f"submit_workers={controller_workers}\ncollect_workers={controller_workers}\n"
        "poll_seconds=0.2\n"
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
    profile = runtime_profile(memory_gb=10, wall_seconds=10)
    specs = [
        {
            "schema_version": 1,
            "task": "build",
            "observation_id": str(index),
            "runtime_profile": profile,
        }
        for index in range(capacity * 4)
    ]
    scheduler = ExpiringScheduler(lifetime=2.5)
    started = time.monotonic()
    with Lease(root / "state/controller.lease") as lease:
        engine = ExecutionEngine(root, config, lease, executor=scheduler)
        results = engine.run(specs)
    elapsed = time.monotonic() - started
    # Integrate actual live-job intervals after the initial capacity submissions,
    # stopping at the last submission while the controller still has a backlog.
    # This excludes startup and final draining, and catches long refill gaps
    # even if a controller briefly reaches the maximum once.
    intervals = sorted(scheduler.intervals.values())
    window_start, window_end = intervals[capacity - 1][0], intervals[-1][0]
    live_job_seconds = sum(
        max(0, min(end, window_end) - max(start, window_start)) for start, end in intervals
    )
    mean_jobs = live_job_seconds / (window_end - window_start)
    record_property("peak_jobs", scheduler.peak_jobs)
    record_property("mean_jobs_while_backlogged", mean_jobs)
    record_property("elapsed_seconds", elapsed)
    print(
        f"short-job throughput: capacity={capacity}, peak={scheduler.peak_jobs}, "
        f"backlogged_mean={mean_jobs:.2f}, elapsed={elapsed:.2f}s"
    )
    assert len(results) == len(scheduler.intervals) == len(specs)
    assert {result["status"] for result in results} == {"SUCCEEDED"}
    # Allow scheduling noise while requiring occupancy near the configured
    # limit. Serial bookkeeping reaches only about 40 jobs in this workload;
    # parallel staging and batched journal writes keep the larger budgets fed.
    assert capacity * 0.9 <= scheduler.peak_jobs <= capacity
    assert mean_jobs >= capacity * 0.8
    events = Journal(root / "state/events.jsonl").replay()
    assert sum(event["event_type"] == "submitted" for event in events) == len(specs)
    assert sum(event["event_type"] == "result_committed" for event in events) == len(specs)
