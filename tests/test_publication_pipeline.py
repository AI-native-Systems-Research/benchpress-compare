import threading

import pytest

from benchpress_compare.config import RunConfig
from benchpress_compare.identity import atomic_write_json, read_json
from benchpress_compare.journal import Journal
from benchpress_compare.orchestrator import ExecutionEngine, read_results
from benchpress_compare.pilot import runtime_profile
from benchpress_compare.store import Lease


def pipeline_run(tmp_path, count, *, max_jobs=1):
    site = tmp_path / "site.toml"
    site.write_text(
        f'[lsf]\nqueue="fake"\nmax_jobs={max_jobs}\nsubmit_workers=2\ncollect_workers=2\n'
        "poll_seconds=0.01\n"
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
    specs = [
        {
            "schema_version": 1,
            "task": "build",
            "phase": "comparison",
            "observation_id": str(index),
            "runtime_profile": runtime_profile(memory_gb=0.125, wall_seconds=5),
        }
        for index in range(count)
    ]
    return root, config, specs


class Scheduler:
    """Completes jobs on poll without worker processes or scheduler access."""

    def __init__(self, *, on_submit=None, on_cancel=None):
        self.on_submit = on_submit
        self.on_cancel = on_cancel
        self.submitted = []
        self.jobs = {}
        self.cancelled = []

    def submit(self, command, directory, profile, *, name, token):
        spec = read_json(directory / "spec.json")
        identity = spec["observation_id"]
        self.submitted.append(identity)
        successful = self.on_submit(identity) if self.on_submit else True
        job = {"token": token, "name": name, "status": "QUEUED", "identity": identity}
        self.jobs[token] = {**job, "status": "DONE" if successful else "RUNNING"}
        if successful:
            atomic_write_json(directory / "receipt.json", {**spec, "status": "SUCCEEDED"})
        atomic_write_json(directory / "job.json", job)
        return job

    def poll_many(self, jobs):
        return {token: dict(self.jobs[token]) for token in jobs}

    def cancel(self, job):
        self.cancelled.append(job["identity"])
        if self.on_cancel:
            self.on_cancel(job["identity"])
        self.jobs[job["token"]]["status"] = "CANCELLED"
        return dict(self.jobs[job["token"]])


def hold_first_publication(monkeypatch, started, release, *, failure=None):
    import benchpress_compare.orchestrator as module

    publish = module.publish_bundle

    def held(destination, *args, **kwargs):
        if destination.name == "task-0":
            started.set()
            assert release.wait(timeout=5), "Dispatch blocked behind result publication"
            if failure:
                raise failure
        return publish(destination, *args, **kwargs)

    monkeypatch.setattr(module, "publish_bundle", held)
    return publish


def test_dispatch_continues_while_a_finished_result_is_publishing(tmp_path, monkeypatch):
    root, config, specs = pipeline_run(tmp_path, 2)
    started, release = threading.Event(), threading.Event()
    hold_first_publication(monkeypatch, started, release)

    def submit(identity):
        if identity == "1":
            assert started.wait(timeout=5)
            release.set()
        return True

    scheduler = Scheduler(on_submit=submit)
    with Lease(root / "state/controller.lease") as lease:
        engine = ExecutionEngine(root, config, lease, executor=scheduler)
        rows = engine.run(specs)
    assert [row["status"] for row in rows] == ["SUCCEEDED", "SUCCEEDED"]
    assert scheduler.submitted == ["0", "1"]
    assert len(read_results(root)) == 2
    events = Journal(root / "state/events.jsonl").replay()
    assert sorted(
        event["data"]["work_key"] for event in events if event["event_type"] == "result_committed"
    ) == ["task-0", "task-1"]


def test_cancellation_does_not_mutate_an_attempt_being_published(tmp_path, monkeypatch):
    root, config, specs = pipeline_run(tmp_path, 3)
    started, release = threading.Event(), threading.Event()
    hold_first_publication(monkeypatch, started, release)

    def submit(identity):
        if identity == "1":
            assert started.wait(timeout=5)
            atomic_write_json(root / "state/cancel-request.json", {"schema_version": 1})
            return False
        return True

    def cancel(identity):
        assert identity == "1", "A completed attempt was cancelled during publication"
        release.set()

    scheduler = Scheduler(on_submit=submit, on_cancel=cancel)
    with Lease(root / "state/controller.lease") as lease:
        engine = ExecutionEngine(root, config, lease, executor=scheduler)
        rows = engine.run(specs)
    assert [row["status"] for row in rows] == ["SUCCEEDED", "CANCELLED", "CANCELLED"]
    assert scheduler.submitted == ["0", "1"]
    assert scheduler.cancelled == ["1"]
    first = read_json(next((root / "attempts/task-0").glob("*/attempt.json")))
    assert first["status"] == "SUCCEEDED"
    assert not first.get("cancel_requested")
    assert len(read_results(root)) == 3


def test_publication_exception_propagates_and_submitted_work_survives_resume(tmp_path, monkeypatch):
    import benchpress_compare.orchestrator as module

    root, config, specs = pipeline_run(tmp_path, 2)
    started, release = threading.Event(), threading.Event()
    publish = hold_first_publication(
        monkeypatch, started, release, failure=OSError("publication failed")
    )

    def submit(identity):
        if identity == "1":
            assert started.wait(timeout=5)
            release.set()
        return True

    scheduler = Scheduler(on_submit=submit)
    with Lease(root / "state/controller.lease") as lease:
        engine = ExecutionEngine(root, config, lease, executor=scheduler)
        with pytest.raises(OSError, match="publication failed"):
            engine.run(specs)
        lease.assert_owned()
    # Already submitted jobs have durable attempts and are adopted by the next
    # owner after the failed publication propagates out of the controller.
    assert scheduler.submitted == ["0", "1"]
    monkeypatch.setattr(module, "publish_bundle", publish)
    with Lease(root / "state/controller.lease") as lease:
        resumed = ExecutionEngine(root, config, lease, executor=scheduler)
        rows = resumed.run(specs)
    assert [row["status"] for row in rows] == ["SUCCEEDED"] * 2
    assert scheduler.submitted == ["0", "1"]
    assert len(read_results(root)) == 2


def test_failed_publication_batch_still_journals_successful_siblings(tmp_path, monkeypatch):
    import benchpress_compare.orchestrator as module

    root, config, specs = pipeline_run(tmp_path, 2, max_jobs=2)
    publish = module.publish_bundle

    def fail_first(destination, *args, **kwargs):
        if destination.name == "task-0":
            raise OSError("one result could not be published")
        return publish(destination, *args, **kwargs)

    monkeypatch.setattr(module, "publish_bundle", fail_first)
    scheduler = Scheduler()
    with Lease(root / "state/controller.lease") as lease:
        engine = ExecutionEngine(root, config, lease, executor=scheduler)
        with pytest.raises(OSError, match="one result"):
            engine.run(specs)
    assert [row["observation_id"] for row in read_results(root)] == ["1"]
    events = Journal(root / "state/events.jsonl").replay()
    assert [
        event["data"]["work_key"] for event in events if event["event_type"] == "result_committed"
    ] == ["task-1"]
    monkeypatch.setattr(module, "publish_bundle", publish)
    with Lease(root / "state/controller.lease") as lease:
        resumed = ExecutionEngine(root, config, lease, executor=scheduler)
        rows = resumed.run(specs)
    assert [row["status"] for row in rows] == ["SUCCEEDED"] * 2
    assert sorted(scheduler.submitted) == ["0", "1"]
