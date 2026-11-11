import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from benchpress_compare.config import RunConfig, SiteConfig
from benchpress_compare.errors import IntegrityError, PreparationError, ReconciliationError
from benchpress_compare.executors.local import LocalExecutor, matching_process
from benchpress_compare.executors.lsf import LSFExecutor
from benchpress_compare.identity import atomic_write_json, canonical_hash, read_json
from benchpress_compare.journal import Journal
from benchpress_compare.orchestrator import ExecutionEngine, cancel, read_results, work_key
from benchpress_compare.pilot import runtime_profile
from benchpress_compare.schema import PROTOCOLS
from benchpress_compare.store import Lease, Store


WORKER = r"""
import hashlib, json, os, pathlib, signal, time
spec = json.loads(pathlib.Path("spec.json").read_text())
counter = pathlib.Path(spec["counter_path"])
old = counter.read_text().splitlines() if counter.exists() else []
identity = spec["arm"] + ":" + spec["observation_id"]
iteration = old.count(identity)
with counter.open("a") as output:
    output.write(identity + "\n")
    output.flush()
    os.fsync(output.fileno())
time.sleep(spec.get("fake_delay", 0))
statuses = spec.get("fake_outcomes", ["SUCCEEDED"])
status = statuses[min(iteration, len(statuses)-1)]
if status == "NO_RECEIPT":
    raise SystemExit(1)
if status == "SIGKILL":
    os.kill(os.getpid(), signal.SIGKILL)
if status == "STOP_SHARD":
    os.kill(os.getppid(), signal.SIGTERM)
    time.sleep(10)
if status == "MALFORMED":
    pathlib.Path("receipt.json").write_text("not-json")
    raise SystemExit(0)
if status == "MALFORMED_SHAPE":
    pathlib.Path("receipt.json").write_text("[]")
    raise SystemExit(0)
receipt = {key: spec[key] for key in (
    "observation_id", "case_id", "arm", "seed", "phase", "build_id",
    "attempt_id", "plan_hash", "lease_generation", "run_id", "submission_token",
    "runtime_profile", "protocols"
)}
artifact = b"frozen compiled output"
pathlib.Path("output.bin").write_bytes(artifact)
receipt.update(schema_version=1, status=status, N2=20 if status == "SUCCEEDED" else None,
    D2=10 if status == "SUCCEEDED" else None,
    validation={"status": "verified" if status == "SUCCEEDED" else "error",
        "checks": {"target_legality": {"status": "verified"}}},
    resources={"elapsed_seconds": 0.1, "peak_rss_bytes": 10_000_000},
    artifacts={"output.bin": hashlib.sha256(artifact).hexdigest()})
if spec.get("fake_bad_identity"):
    receipt["seed"] += 1
pathlib.Path("receipt.json").write_text(json.dumps(receipt))
"""


@pytest.fixture
def setup(tmp_path, monkeypatch):
    worker = tmp_path / "fake_worker.py"
    worker.write_text(WORKER)
    monkeypatch.setattr(
        "benchpress_compare.orchestrator.inventory",
        lambda **_: {
            "usable_cpus": 4,
            "worker_memory_budget_bytes": 1024**3,
        },
    )

    def make(run="run", arm="baseline", identity="first", **extra):
        root = tmp_path / run
        root.mkdir(exist_ok=True)
        config = RunConfig(
            "/unused/baseline", "/unused/evolve", str(tmp_path / "store"), str(root), workers="1"
        )
        spec = {
            "schema_version": 1,
            "observation_id": canonical_hash(identity),
            "case_id": "case-" + identity,
            "arm": arm,
            "seed": 123,
            "phase": "comparison",
            "build_id": "build-same-for-aa",
            "protocols": PROTOCOLS,
            "runtime_profile": runtime_profile(memory_gb=0.125, wall_seconds=5),
            "command": [sys.executable, str(worker)],
            "counter_path": str(tmp_path / "executions"),
            **extra,
        }
        return root, config, spec

    return make


def execute(root, config, specs, **options):
    executor = options.pop("executor", None)
    poll_interval = options.pop("poll_interval", 0.01)
    with Lease(root / "state/controller.lease") as lease:
        engine = ExecutionEngine(root, config, lease, executor=executor, plan_hash="frozen-plan")
        engine.poll_interval = poll_interval
        return engine.run(specs, **options)


def test_aa_executes_each_arm_and_resume_does_not_repeat(setup):
    root, config, baseline = setup()
    _, _, evolve = setup(arm="evolve")
    results = execute(root, config, [baseline, evolve])
    assert {row["arm"] for row in results} == {"baseline", "evolve"}
    assert all(row["N2"] == 20 for row in results)
    assert len(Path(baseline["counter_path"]).read_text().splitlines()) == 2
    assert results == execute(root, config, [baseline, evolve])
    assert len(Path(baseline["counter_path"]).read_text().splitlines()) == 2
    assert len(read_results(root)) == 2


def test_second_run_reuses_only_baseline_and_owns_cached_artifacts(setup):
    root, config, baseline = setup()
    execute(root, config, [baseline])
    second, config2, cached = setup(run="second")
    _, _, evolve = setup(run="second", arm="evolve")
    rows = execute(second, config2, [cached, evolve])
    assert rows[0]["cache_hit"] is True
    assert rows[1]["cache_hit"] is False
    assert (
        Path(cached["counter_path"])
        .read_text()
        .splitlines()
        .count("baseline:" + cached["observation_id"])
        == 1
    )
    artifact = second / "observations" / work_key(cached) / "output.bin"
    assert artifact.read_bytes() == b"frozen compiled output"


def test_infrastructure_retries_once_with_same_seed_and_compiler_failure_never_retries(setup):
    root, config, baseline = setup(fake_outcomes=["INFRA_FAILED", "SUCCEEDED"])
    rows = execute(root, config, [baseline])
    assert rows[0]["status"] == "SUCCEEDED"
    attempts = list((root / "attempts" / work_key(baseline)).glob("*/attempt.json"))
    assert len(attempts) == 2
    assert {read_json(path)["spec"]["seed"] for path in attempts} == {123}
    root2, config2, failed = setup(
        run="compiler", identity="compiler", fake_outcomes=["COMPILER_FAILED", "SUCCEEDED"]
    )
    assert execute(root2, config2, [failed])[0]["status"] == "COMPILER_FAILED"
    assert execute(root2, config2, [failed])[0]["status"] == "COMPILER_FAILED"
    assert len(list((root2 / "attempts" / work_key(failed)).glob("*/attempt.json"))) == 1


def test_retry_budget_is_bounded_and_failed_results_survive_resume(setup):
    root, config, spec = setup(fake_outcomes=["INFRA_FAILED"])
    assert execute(root, config, [spec])[0]["status"] == "INFRA_FAILED"
    assert len(list((root / "attempts" / work_key(spec)).glob("*/attempt.json"))) == 2
    assert execute(root, config, [spec])[0]["status"] == "INFRA_FAILED"
    assert len(list((root / "attempts" / work_key(spec)).glob("*/attempt.json"))) == 2


def test_interruption_cleans_process_and_resume_preserves_committed_observation(setup):
    root, config, first = setup()
    execute(root, config, [first])
    _, _, pending = setup(identity="pending", arm="evolve", fake_delay=0.2)

    class InterruptOnce(LocalExecutor):
        def poll(self, job):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        execute(root, config, [first, pending], executor=InterruptOnce())
    attempts = list((root / "attempts" / work_key(pending)).glob("*/attempt.json"))
    interrupted = read_json(attempts[0])
    assert interrupted["status"] == "INTERRUPTED"
    assert matching_process(interrupted["job"]) is None
    assert all(row["status"] == "SUCCEEDED" for row in execute(root, config, [first, pending]))
    assert len(list((root / "attempts" / work_key(first)).glob("*/attempt.json"))) == 1
    assert len(list((root / "attempts" / work_key(pending)).glob("*/attempt.json"))) == 2


def test_cancelled_work_stays_cancelled_until_explicit_reopen(setup):
    root, config, spec = setup()
    atomic_write_json(root / "state/cancel-request.json", {"schema_version": 1})
    assert execute(root, config, [spec])[0]["status"] == "CANCELLED"
    assert execute(root, config, [spec])[0]["status"] == "CANCELLED"
    assert not Path(spec["counter_path"]).exists()
    (root / "state/cancel-request.json").unlink()
    assert execute(root, config, [spec], retry_cancelled=True)[0]["status"] == "SUCCEEDED"


def test_unknown_local_submission_is_reconciled_without_duplicate(setup):
    root, config, spec = setup()
    directory = root / "attempts" / work_key(spec) / "unknown"
    atomic_write_json(
        directory / "attempt.json",
        {
            "schema_version": 1,
            "observation_id": spec["observation_id"],
            "attempt_id": "unknown",
            "submission_token": "unknown-token",
            "status": "SUBMITTING",
            "created_at": time.time(),
            "directory": str(directory),
            "spec": {**spec, "lease_generation": 1},
        },
    )
    with pytest.raises(ReconciliationError, match="Uncertain"):
        execute(root, config, [spec])
    assert not Path(spec["counter_path"]).exists()
    assert len(list((root / "attempts" / work_key(spec)).glob("*/attempt.json"))) == 1
    assert read_json(root / "state/status.json")["status"] == "NEEDS_RECONCILIATION"


def test_scheduler_load_counts_only_jobs_still_on_the_scheduler():
    profile = runtime_profile(memory_gb=0.5, wall_seconds=5)

    def attempt(token, status):
        return {"runtime_profile": profile}, {"job": {"token": token, "status": status}}

    active = {
        "running": attempt("a", "RUNNING"),
        "queued": attempt("b", "QUEUED"),
        "unknown": attempt("c", "SUBMISSION_UNKNOWN"),
        "shard-1": attempt("d", "NEEDS_RECONCILIATION"),
        "shard-2": attempt("d", "NEEDS_RECONCILIATION"),
        "done": attempt("e", "DONE"),
        "exit": attempt("f", "EXIT"),
        "cancelled": attempt("g", "CANCELLED"),
    }
    jobs, cpus, memory = ExecutionEngine._scheduler_load(active)
    assert jobs == 4
    assert cpus == 4 * profile["native_threads"]
    assert memory == 4 * profile["memory_bytes"]


def test_collection_is_interleaved_with_dispatch(setup):
    # Capacity for every spec at once: a dispatch-first loop would submit all of
    # them before committing anything. Slow submissions must not hold off commits.
    specs = []
    for index in range(4):
        root, config, spec = setup(identity=f"interleave-{index}")
        specs.append(spec)
    config.workers = "4"

    class SlowSubmit(LocalExecutor):
        def submit(self, *args, **kwargs):
            job = super().submit(*args, **kwargs)
            time.sleep(0.2)
            return job

    rows = execute(root, config, specs, executor=SlowSubmit())
    assert all(row["status"] == "SUCCEEDED" for row in rows)
    events = [
        json.loads(line)["event_type"]
        for line in (root / "state/events.jsonl").read_text().splitlines()
    ]
    first_commit = events.index("result_committed")
    last_submission = len(events) - 1 - events[::-1].index("submitted")
    assert first_commit < last_submission


class FakeLSF:
    """Scheduler transport that runs real fresh worker subprocesses."""

    def __init__(self, *, lose_submission=False, terminations=None):
        self.jobs = {}
        self.submissions = 0
        self.lose_submission = lose_submission
        # Process exit code -> scheduler exit reason reported for that job.
        self.terminations = terminations or {}
        # The controller submits a dispatch batch from several threads.
        self.lock = threading.Lock()

    def __call__(self, argv, **kwargs):
        if argv[0] == "bsub":
            with self.lock:
                self.submissions += 1
                identity = str(self.submissions)
                name = argv[argv.index("-J") + 1]
                cwd_index = argv.index("-cwd")
                directory = Path(argv[cwd_index + 1])
                process = subprocess.Popen(
                    argv[cwd_index + 2 :],
                    cwd=directory,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self.jobs[identity] = {"name": name, "process": process}
                if self.lose_submission:
                    self.lose_submission = False
                    raise subprocess.TimeoutExpired(argv, 60)
            return subprocess.CompletedProcess(argv, 0, f"Job <{identity}> is submitted", "")
        if argv[0] in {"bjobs", "bhist"}:
            records = []
            for identity, job in self.jobs.items():
                code = job["process"].poll()
                record = {
                    "JOBID": identity,
                    "STAT": "RUN" if code is None else "DONE" if code == 0 else "EXIT",
                    "JOB_NAME": job["name"],
                }
                if code is not None and code in self.terminations:
                    record.update(STAT="EXIT", EXIT_REASON=self.terminations[code])
                records.append(record)
            return subprocess.CompletedProcess(argv, 0, json.dumps({"RECORDS": records}), "")
        raise AssertionError(argv)


class Overlap:
    """Wraps a callable to hold it for ``delay`` seconds and record concurrency."""

    def __init__(self, delay):
        self.delay = delay
        self.lock = threading.Lock()
        self.current = 0
        self.peak = 0

    def wrap(self, function):
        def wrapper(*args, **kwargs):
            with self.lock:
                self.current += 1
                self.peak = max(self.peak, self.current)
            try:
                time.sleep(self.delay)
                return function(*args, **kwargs)
            finally:
                with self.lock:
                    self.current -= 1

        return wrapper


class SlowFakeLSF(FakeLSF):
    """FakeLSF whose bsub takes ``delay`` seconds and records scheduler load."""

    def __init__(self, *, delay, **kwargs):
        super().__init__(**kwargs)
        self.overlap = Overlap(delay)
        self.peak_load = 0

    def __call__(self, argv, **kwargs):
        if argv[0] != "bsub":
            return super().__call__(argv, **kwargs)
        with self.lock:
            running = sum(job["process"].poll() is None for job in self.jobs.values())
            self.peak_load = max(self.peak_load, running + self.overlap.current + 1)
        return self.overlap.wrap(super().__call__)(argv, **kwargs)


def lsf_site(tmp_path, **options):
    lines = [f"{key}={value}" for key, value in options.items()]
    path = tmp_path / "site.toml"
    path.write_text(
        '[lsf]\nqueue="fake"\npoll_seconds=0.01\nvisibility_grace_seconds=0.1\n'
        + "\n".join(lines)
        + "\n"
    )
    return path, SiteConfig.read(path)


@pytest.mark.parametrize("submit_workers,overlap", [(1, 1), (4, 2)])
def test_lsf_submissions_overlap_within_the_job_budget(setup, tmp_path, submit_workers, overlap):
    specs = []
    for index in range(6):
        root, config, spec = setup(identity=f"parallel-{index}", fake_delay=0.5)
        specs.append(spec)
    # One job per observation, so six specs mean six bsub calls under a budget of four.
    site_path, site = lsf_site(
        tmp_path, max_jobs=4, shard_max_observations=1, submit_workers=submit_workers
    )
    config.executor, config.lsf_config = "lsf", str(site_path)
    scheduler = SlowFakeLSF(delay=0.2)
    rows = execute(
        root, config, specs, executor=LSFExecutor(site, runner=scheduler), poll_interval=1.0
    )
    assert [row["status"] for row in rows] == ["SUCCEEDED"] * 6
    assert scheduler.submissions == 6
    # One worker is the sequential controller; a batch overlaps its bsub calls
    # but never holds more jobs on the scheduler than max_jobs allows.
    assert overlap <= scheduler.overlap.peak <= submit_workers
    assert scheduler.peak_load <= 4
    for spec in specs:
        assert len(list((root / "attempts" / work_key(spec)).glob("*/attempt.json"))) == 1
    events = [json.loads(line) for line in (root / "state/events.jsonl").read_text().splitlines()]
    submitted = [
        event["data"]["work_key"] for event in events if event["event_type"] == "submitted"
    ]
    assert sorted(submitted) == sorted(work_key(spec) for spec in specs)


@pytest.mark.parametrize("collect_workers,overlap", [(1, 1), (4, 2)])
def test_lsf_results_are_published_in_parallel(
    setup, tmp_path, monkeypatch, collect_workers, overlap
):
    import benchpress_compare.orchestrator as orchestrator

    specs = []
    for index in range(4):
        for arm in ("baseline", "evolve"):
            root, config, spec = setup(identity=f"collect-{index}", arm=arm)
            specs.append(spec)
    site_path, site = lsf_site(
        tmp_path, max_jobs=8, shard_max_observations=1, collect_workers=collect_workers
    )
    config.executor, config.lsf_config = "lsf", str(site_path)
    publication = Overlap(0.1)
    monkeypatch.setattr(
        orchestrator, "publish_bundle", publication.wrap(orchestrator.publish_bundle)
    )
    scheduler = FakeLSF()
    rows = execute(
        root, config, specs, executor=LSFExecutor(site, runner=scheduler), poll_interval=1.0
    )
    assert [row["status"] for row in rows] == ["SUCCEEDED"] * 8
    assert overlap <= publication.peak <= collect_workers
    assert len(read_results(root)) == 8
    events = Journal(root / "state/events.jsonl").replay()
    committed = [
        event["data"]["work_key"] for event in events if event["event_type"] == "result_committed"
    ]
    assert sorted(committed) == sorted(work_key(spec) for spec in specs)
    # The journal chain stays intact when commits are appended from threads.
    for previous, event in zip(events, events[1:]):
        assert event["previous"] == previous["event_hash"]
        assert event["sequence"] == previous["sequence"] + 1
    # A/A observations have equal identities across arms, but only the baseline
    # may enter the shared cache. A restarted controller must reuse both local
    # publications without submitting either arm again.
    store = Store(config.store)
    for spec in specs:
        cached = store.get("baseline/observations", spec["observation_id"])
        assert cached["record"]["arm"] == "baseline"
    assert execute(root, config, specs, executor=LSFExecutor(site, runner=scheduler)) == rows
    assert scheduler.submissions == 8


def test_parallel_submissions_are_adopted_after_interruption_before_recording(setup, tmp_path):
    specs = []
    for index in range(4):
        root, config, spec = setup(identity=f"unrecorded-{index}")
        specs.append(spec)
    site_path, site = lsf_site(tmp_path, max_jobs=4, shard_max_observations=1, submit_workers=4)
    config.executor, config.lsf_config = "lsf", str(site_path)
    scheduler = FakeLSF()

    class InterruptedRecording(ExecutionEngine):
        def _record_launch(self, prepared, job):
            raise KeyboardInterrupt

    with Lease(root / "state/controller.lease") as lease:
        engine = InterruptedRecording(
            root,
            config,
            lease,
            executor=LSFExecutor(site, runner=scheduler),
            plan_hash="frozen-plan",
        )
        engine.poll_interval = 10
        with pytest.raises(KeyboardInterrupt):
            engine.run(specs)
    assert scheduler.submissions == 4
    for spec in specs:
        paths = list((root / "attempts" / work_key(spec)).glob("*/attempt.json"))
        assert len(paths) == 1
        assert "job" not in read_json(paths[0])

    rows = execute(root, config, specs, executor=LSFExecutor(site, runner=scheduler))
    assert [row["status"] for row in rows] == ["SUCCEEDED"] * 4
    assert scheduler.submissions == 4
    events = Journal(root / "state/events.jsonl").replay()
    assert sum(event["event_type"] == "attempt_adopted" for event in events) == 4
    assert sum(event["event_type"] == "result_committed" for event in events) == 4


def test_lost_lsf_submit_reply_is_reconciled_and_matches_local(setup, tmp_path):
    root, config, spec = setup()
    local = execute(root, config, [spec])[0]
    remote, remote_config, remote_spec = setup(run="cluster", arm="evolve")
    site = SiteConfig(queue="fake", poll_seconds=0.01, visibility_grace_seconds=0.1)
    site_path = tmp_path / "site.toml"
    site_path.write_text('[lsf]\nqueue="fake"\npoll_seconds=0.01\nvisibility_grace_seconds=0.1\n')
    remote_config.executor, remote_config.lsf_config = "lsf", str(site_path)
    scheduler = FakeLSF(lose_submission=True)
    cluster = execute(
        remote, remote_config, [remote_spec], executor=LSFExecutor(site, runner=scheduler)
    )[0]
    assert (cluster["N2"], cluster["D2"], cluster["status"]) == (
        local["N2"],
        local["D2"],
        local["status"],
    )
    assert scheduler.submissions == 1
    assert len(list((remote / "attempts" / work_key(remote_spec)).glob("*/attempt.json"))) == 1


def test_bad_identity_is_not_accepted_or_retried(setup):
    root, config, spec = setup(fake_bad_identity=True)
    with pytest.raises(IntegrityError, match="seed mismatch"):
        execute(root, config, [spec])
    assert len(list((root / "attempts" / work_key(spec)).glob("*/attempt.json"))) == 1
    assert read_results(root) == []


def test_malformed_receipt_is_terminal_invalid_and_retains_evidence(setup):
    root, config, spec = setup(fake_outcomes=["MALFORMED", "SUCCEEDED"])
    assert execute(root, config, [spec])[0]["status"] == "INVALID"
    assert execute(root, config, [spec])[0]["status"] == "INVALID"
    assert len(list((root / "attempts" / work_key(spec)).glob("*/attempt.json"))) == 1
    assert (root / "observations" / work_key(spec) / "receipt.json").read_text() == "not-json"


def test_valid_json_wrong_shape_is_terminal_invalid(setup):
    root, config, spec = setup(fake_outcomes=["MALFORMED_SHAPE", "SUCCEEDED"])
    assert execute(root, config, [spec])[0]["status"] == "INVALID"
    assert execute(root, config, [spec])[0]["status"] == "INVALID"
    assert len(list((root / "attempts" / work_key(spec)).glob("*/attempt.json"))) == 1


def test_resume_does_not_reset_exhausted_infrastructure_budget(setup):
    root, config, spec = setup()
    for index in range(2):
        directory = root / "attempts" / work_key(spec) / f"failed-{index}"
        attempt_id = directory.name
        atomic_write_json(
            directory / "attempt.json",
            {
                "schema_version": 1,
                "observation_id": spec["observation_id"],
                "attempt_id": attempt_id,
                "submission_token": attempt_id,
                "status": "INFRA_FAILED",
                "created_at": index,
                "directory": str(directory),
                "spec": {
                    **spec,
                    "attempt_id": attempt_id,
                    "plan_hash": "frozen-plan",
                    "lease_generation": 1,
                },
            },
        )
    assert execute(root, config, [spec])[0]["status"] == "INFRA_FAILED"
    assert len(list((root / "attempts" / work_key(spec)).glob("*/attempt.json"))) == 2
    assert not Path(spec["counter_path"]).exists()


def test_new_controller_adopts_previous_generation_receipt(setup):
    root, config, spec = setup(arm="evolve", fake_delay=0.05)
    with Lease(root / "state/controller.lease") as first:
        engine = ExecutionEngine(root, config, first, plan_hash="frozen-plan")
        attempt = engine._launch(spec)
        original_generation = first.generation
    rows = execute(root, config, [spec])
    assert rows[0]["status"] == "SUCCEEDED"
    assert rows[0]["lease_generation"] == original_generation
    assert rows[0]["accepted_attempt"] == attempt["attempt_id"]
    assert len(list((root / "attempts" / work_key(spec)).glob("*/attempt.json"))) == 1


def test_known_done_job_is_committed_after_controller_restart(setup):
    root, config, spec = setup(arm="evolve")
    with Lease(root / "state/controller.lease") as lease:
        engine = ExecutionEngine(root, config, lease, plan_hash="frozen-plan")
        attempt = engine._launch(spec)
        engine.executor.processes[attempt["submission_token"]].wait(timeout=5)
        attempt["job"] = engine.executor.poll(attempt["job"])
        attempt["status"] = "DONE"
        engine._save_attempt(attempt)
    assert execute(root, config, [spec])[0]["status"] == "SUCCEEDED"
    assert len(list((root / "attempts" / work_key(spec)).glob("*/attempt.json"))) == 1


def test_unexplained_missing_receipt_does_not_resample_compiler(setup):
    root, config, spec = setup(fake_outcomes=["NO_RECEIPT", "SUCCEEDED"])
    with pytest.raises(PreparationError, match="protocol incompatible"):
        execute(root, config, [spec])
    assert len(list((root / "attempts" / work_key(spec)).glob("*/attempt.json"))) == 1
    assert read_results(root)[0]["status"] == "HARNESS_FAILED"


def test_external_kill_without_receipt_is_retried_once_as_infrastructure(setup):
    root, config, spec = setup(fake_outcomes=["SIGKILL", "SUCCEEDED"])
    rows = execute(root, config, [spec])
    assert rows[0]["status"] == "SUCCEEDED"
    attempts = sorted(
        (read_json(path) for path in (root / "attempts" / work_key(spec)).glob("*/attempt.json")),
        key=lambda attempt: attempt["created_at"],
    )
    assert [attempt["status"] for attempt in attempts] == ["INFRA_FAILED", "SUCCEEDED"]
    assert attempts[0]["job"]["return_code"] == -9
    exhausted, config2, killed = setup(run="killed", identity="killed", fake_outcomes=["SIGKILL"])
    row = execute(exhausted, config2, [killed])[0]
    assert (row["status"], row["reason"]) == ("INFRA_FAILED", "external_signal")
    assert len(list((exhausted / "attempts" / work_key(killed)).glob("*/attempt.json"))) == 2


def test_scheduler_memory_kill_is_a_resource_limit_and_siblings_keep_receipts(setup, tmp_path):
    root, config, baseline = setup(fake_outcomes=["SIGKILL"])
    _, _, evolve = setup(arm="evolve")
    site = SiteConfig(queue="fake", poll_seconds=0.01, visibility_grace_seconds=0.1)
    site_path = tmp_path / "site.toml"
    site_path.write_text('[lsf]\nqueue="fake"\npoll_seconds=0.01\nvisibility_grace_seconds=0.1\n')
    config.executor, config.lsf_config = "lsf", str(site_path)
    # The shard itself finishes, yet the scheduler reports the job as killed for
    # its memory limit, exactly as cgroup enforcement does on LSF.
    scheduler = FakeLSF(
        terminations={0: "TERM_MEMLIMIT: job killed after reaching LSF memory usage limit"}
    )
    bundle = {"bundle_id": "oom", "observations": [baseline, evolve], "estimated_seconds": 12}
    rows = execute(
        root,
        config,
        [baseline, evolve],
        bundles=[bundle],
        executor=LSFExecutor(site, runner=scheduler),
    )
    assert (rows[0]["status"], rows[0]["reason"]) == ("RESOURCE_LIMIT", "memory_limit")
    assert rows[1]["status"] == "SUCCEEDED"
    assert scheduler.submissions == 1
    assert len(list((root / "attempts" / work_key(baseline)).glob("*/attempt.json"))) == 1
    assert read_results(root) == execute(
        root, config, [baseline, evolve], bundles=[bundle], executor=LSFExecutor(site, scheduler)
    )


def test_scheduler_kill_retries_children_the_shard_never_reached(setup, tmp_path):
    root, config, baseline = setup(fake_outcomes=["STOP_SHARD"])
    _, _, evolve = setup(arm="evolve")
    site = SiteConfig(queue="fake", poll_seconds=0.01, visibility_grace_seconds=0.1)
    site_path = tmp_path / "site.toml"
    site_path.write_text('[lsf]\nqueue="fake"\npoll_seconds=0.01\nvisibility_grace_seconds=0.1\n')
    config.executor, config.lsf_config = "lsf", str(site_path)
    # The first child's SIGTERM to the shard stands in for the scheduler ending
    # the job; the shard exits 3 after cancelling it and skipping the second.
    scheduler = FakeLSF(
        terminations={3: "TERM_RUNLIMIT: job killed after reaching LSF run time limit"}
    )
    bundle = {"bundle_id": "cut", "observations": [baseline, evolve], "estimated_seconds": 12}
    rows = execute(
        root,
        config,
        [baseline, evolve],
        bundles=[bundle],
        executor=LSFExecutor(site, runner=scheduler),
    )
    assert (rows[0]["status"], rows[0]["reason"]) == ("RESOURCE_LIMIT", "wall_limit")
    assert rows[1]["status"] == "SUCCEEDED"
    assert scheduler.submissions == 2
    assert len(list((root / "attempts" / work_key(baseline)).glob("*/attempt.json"))) == 1
    attempts = sorted(
        (read_json(path) for path in (root / "attempts" / work_key(evolve)).glob("*/attempt.json")),
        key=lambda attempt: attempt["created_at"],
    )
    assert [attempt["status"] for attempt in attempts] == ["INFRA_FAILED", "SUCCEEDED"]


def test_confirmed_launch_failure_keeps_infrastructure_classification(setup):
    root, config, spec = setup(command=["/definitely/nonexistent-benchpress-command"])
    assert execute(root, config, [spec])[0]["status"] == "INFRA_FAILED"
    assert len(list((root / "attempts" / work_key(spec)).glob("*/attempt.json"))) == 2


def test_resource_and_protocol_receipt_mismatch_is_rejected(setup):
    _, _, spec = setup()
    receipt = {
        **spec,
        "status": "SUCCEEDED",
        "runtime_profile": {**spec["runtime_profile"], "native_threads": 42},
    }
    with pytest.raises(IntegrityError, match="runtime_profile"):
        ExecutionEngine._check_identity(spec, receipt)
    receipt["runtime_profile"] = spec["runtime_profile"]
    receipt["protocols"] = {**spec["protocols"], "metric": "unsupported-new-metric"}
    with pytest.raises(IntegrityError, match="protocols"):
        ExecutionEngine._check_identity(spec, receipt)


def test_frozen_lsf_bundle_runs_paired_arms_in_one_job(setup, tmp_path):
    root, config, baseline = setup()
    _, _, evolve = setup(arm="evolve")
    site = SiteConfig(queue="fake", poll_seconds=0.01, visibility_grace_seconds=0.1, max_jobs=1)
    site_path = tmp_path / "site.toml"
    site_path.write_text(
        '[lsf]\nqueue="fake"\nmax_jobs=1\npoll_seconds=0.01\nvisibility_grace_seconds=0.1\n'
    )
    config.executor, config.lsf_config = "lsf", str(site_path)
    scheduler = FakeLSF()
    bundle = {
        "bundle_id": "frozen-pair",
        "observations": [baseline, evolve],
        "estimated_seconds": 12,
    }
    rows = execute(
        root,
        config,
        [baseline, evolve],
        bundles=[bundle],
        executor=LSFExecutor(site, runner=scheduler),
    )
    assert all(row["status"] == "SUCCEEDED" for row in rows)
    assert scheduler.submissions == 1
    attempts = [read_json(path) for path in (root / "attempts").glob("*/*/attempt.json")]
    assert len({attempt["job"]["job_id"] for attempt in attempts}) == 1
    assert len({attempt["attempt_id"] for attempt in attempts}) == 2
    assert len({attempt["submission_token"] for attempt in attempts}) == 1
    assert len(list((root / "attempts").glob("*/*/supervisor.json"))) == 2


def test_lsf_partial_bundle_retry_does_not_repeat_successful_arm(setup, tmp_path):
    root, config, baseline = setup(fake_outcomes=["INFRA_FAILED", "SUCCEEDED"])
    _, _, evolve = setup(arm="evolve")
    site = SiteConfig(queue="fake", poll_seconds=0.01, visibility_grace_seconds=0.1)
    site_path = tmp_path / "site.toml"
    site_path.write_text('[lsf]\nqueue="fake"\npoll_seconds=0.01\nvisibility_grace_seconds=0.1\n')
    config.executor, config.lsf_config = "lsf", str(site_path)
    scheduler = FakeLSF()
    bundle = {"bundle_id": "partial", "observations": [baseline, evolve], "estimated_seconds": 12}
    rows = execute(
        root,
        config,
        [baseline, evolve],
        bundles=[bundle],
        executor=LSFExecutor(site, runner=scheduler),
    )
    assert all(row["status"] == "SUCCEEDED" for row in rows)
    assert scheduler.submissions == 2
    assert len(list((root / "attempts" / work_key(baseline)).glob("*/attempt.json"))) == 2
    assert len(list((root / "attempts" / work_key(evolve)).glob("*/attempt.json"))) == 1
    assert (
        Path(evolve["counter_path"])
        .read_text()
        .splitlines()
        .count("evolve:" + evolve["observation_id"])
        == 1
    )


def test_lsf_valid_child_receipts_survive_later_scheduler_exit(setup, tmp_path):
    root, config, baseline = setup()
    _, _, evolve = setup(arm="evolve")
    site = SiteConfig(queue="fake", poll_seconds=0.01, visibility_grace_seconds=0.1)
    site_path = tmp_path / "site.toml"
    site_path.write_text('[lsf]\nqueue="fake"\npoll_seconds=0.01\nvisibility_grace_seconds=0.1\n')
    config.executor, config.lsf_config = "lsf", str(site_path)

    class ExitAfterReceipts(FakeLSF):
        def __call__(self, argv, **kwargs):
            result = super().__call__(argv, **kwargs)
            if argv[0] in {"bjobs", "bhist"}:
                result.stdout = result.stdout.replace('"STAT": "DONE"', '"STAT": "EXIT"')
            return result

    scheduler = ExitAfterReceipts()
    rows = execute(root, config, [baseline, evolve], executor=LSFExecutor(site, runner=scheduler))
    assert all(row["status"] == "SUCCEEDED" for row in rows)
    assert scheduler.submissions == 1


def test_success_summary_without_required_checks_cannot_be_committed(setup):
    root, config, spec = setup()
    with Lease(root / "state/controller.lease") as lease:
        engine = ExecutionEngine(root, config, lease, plan_hash="frozen-plan")
        attempt = engine._new_attempt(spec)
        result = {
            **attempt["spec"],
            "status": "SUCCEEDED",
            "N2": 20,
            "D2": 10,
            "validation": {"status": "verified"},
            "schema_version": 1,
        }
        with pytest.raises(IntegrityError, match="required validation"):
            engine._commit(spec, attempt, result)
    assert read_results(root) == []


def test_cancel_includes_pending_controller_and_continues_past_uncertain_jobs(setup, monkeypatch):
    root, config, _ = setup()
    atomic_write_json(root / "request.json", {"schema_version": 1, "config": config.to_dict()})
    cancelled = []

    class Executor:
        def cancel(self, job):
            cancelled.append(job["name"])
            if job["name"] == "uncertain":
                raise ReconciliationError("scheduler cannot locate this job")
            return {**job, "status": "CANCELLING"}

    monkeypatch.setattr("benchpress_compare.orchestrator.LSFExecutor", lambda *_: Executor())

    def job(name, job_id):
        return {
            "name": name, "token": name, "job_id": job_id,
            "status": "QUEUED", "executor": "lsf", "site": {"queue": "test"},
        }

    atomic_write_json(root / "jobs/controller.json", job("controller", "1"))
    for name, job_id in (("uncertain", "2"), ("worker", "3")):
        atomic_write_json(
            root / "attempts" / name / "attempt/attempt.json",
            {"status": "QUEUED", "job": job(name, job_id)},
        )
    with pytest.raises(ReconciliationError, match="Cancellation requested for 2 jobs"):
        cancel(root)
    assert cancelled == ["controller", "uncertain", "worker"]
    assert (root / "state/cancel-request.json").is_file()


def test_cancel_recovers_shard_job_saved_before_attempt_update(setup, monkeypatch):
    root, config, _ = setup()
    atomic_write_json(root / "request.json", {"schema_version": 1, "config": config.to_dict()})
    cancelled = []

    class Executor:
        def cancel(self, job):
            cancelled.append(job["job_id"])
            return {**job, "status": "CANCELLING"}

    monkeypatch.setattr("benchpress_compare.orchestrator.LSFExecutor", lambda *_: Executor())
    shard = root / "jobs/shards/shared"
    atomic_write_json(shard / "job.json", {
        "name": "shard", "token": "shared", "job_id": "42", "status": "QUEUED",
        "executor": "lsf", "site": {"queue": "test"},
    })
    for name in ("a", "b"):
        directory = root / "attempts" / name / "attempt"
        atomic_write_json(directory / "attempt.json", {
            "status": "SUBMITTING", "submission_token": "shared",
            "directory": str(directory), "shard_directory": str(shard),
        })
    assert cancel(root)["jobs"] == ["42"]
    assert cancelled == ["42"]
