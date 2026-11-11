"""Durable observation-level orchestration shared by local and LSF execution."""

from __future__ import annotations

import concurrent.futures
import math
import os
from pathlib import Path
import sys
import time
import uuid

from .config import RunConfig, SiteConfig
from .errors import IntegrityError, PreparationError, ReconciliationError
from .eligibility import _validation_verified
from .executors.local import LocalExecutor
from .executors.lsf import LSFExecutor
from .identity import atomic_write_json, canonical_hash, file_hash, read_json, harness_identity
from .journal import Journal
from .pilot import inventory, worker_capacity
from .schema import TERMINAL, PROTOCOLS, validate_record
from .store import Lease, Store, publish_bundle, verify_bundle

# Job statuses that no longer hold scheduler resources; their attempts only
# await validation and commit by the controller.
SCHEDULER_TERMINAL = frozenset({"DONE", "EXIT", "INFRA_FAILED", "RESOURCE_LIMIT", "CANCELLED"})
# Bounds the commits per controller iteration so a large batch of finished jobs
# cannot hold off dispatch; the rest are committed on the next iteration. It must
# stay above the observations one dispatch window can submit (with parallel
# submission about 50 shards of up to 6 in 20 s), or finished work piles up in
# `active` faster than it is committed and every later iteration grows longer.
# With `collect_workers` publications in flight, 500 take about 15 s.
COLLECT_PER_ITERATION = 500


def work_key(spec):
    # Equal source builds still require independent real A/A measurements.
    return f"{spec.get('arm', 'task')}-{spec['observation_id']}"


def read_results(root, *, phase="comparison", check_artifacts=True):
    results = []
    folder = Path(root) / ("observations" if phase == "comparison" else f"{phase}/observations")
    if folder.exists():
        for path in sorted(folder.iterdir()):
            if not path.is_dir() or path.name.startswith("."):
                continue
            bundle = verify_bundle(path, check_artifacts=check_artifacts)
            if bundle is not None:
                results.append(bundle["record"])
    return results


def verify_plan(root, *, check_harness=True):
    root = Path(root)
    plan = read_json(root / "plan.json")
    validate_record("RunPlan", plan)
    claimed = plan["plan_hash"]
    actual = canonical_hash({key: value for key, value in plan.items() if key != "plan_hash"})
    if claimed != actual or (root / "plan.sha256").read_text().strip() != claimed:
        raise IntegrityError("Frozen plan identity mismatch")
    if check_harness and (
        plan.get("protocols") != PROTOCOLS or plan.get("coordinator_hash") != harness_identity()
    ):
        raise IntegrityError(
            "Frozen worker protocols or coordinator implementation changed; use a new run"
        )
    for relative, digest in plan.get("artifact_hashes", {}).items():
        path = (root / relative).resolve()
        if not path.is_relative_to(root.resolve()) or file_hash(path) != digest:
            raise IntegrityError(f"Frozen artifact changed: {relative}")
    return plan


class ExecutionEngine:
    def __init__(self, root, config: RunConfig, lease: Lease, *, executor=None, plan_hash=None):
        self.root = Path(root).resolve()
        self.config = config
        self.lease = lease
        self.journal = Journal(self.root / "state/events.jsonl", lease=lease)
        self.store = Store(config.store)
        self.site = SiteConfig.read(config.lsf_config) if config.executor == "lsf" else None
        self.executor = executor or (LSFExecutor(self.site) if self.site else LocalExecutor())
        self.plan_hash = plan_hash
        self.resources = (
            inventory(cpu_budget=config.cpu_budget, memory_gb=config.memory_gb)
            if self.site is None
            else {
                "usable_cpus": self.site.max_cpus,
                "worker_memory_budget_bytes": int(self.site.max_memory_gb * 1024**3),
            }
        )
        self.maximum_jobs = (
            self.site.max_jobs
            if self.site
            else int(config.workers)
            if config.workers != "auto"
            else 1_000_000
        )
        self.poll_interval = self.site.poll_seconds if self.site else 0.1
        # The local executor starts processes directly and its publications hit
        # a local filesystem, so it keeps the sequential controller loop.
        self.submit_workers = self.site.submit_workers if self.site else 1
        self.collect_workers = self.site.collect_workers if self.site else 1

    def state(self, status, **details):
        self.journal.append("run_state", {"status": status, **details})
        with self.lease.fenced():
            atomic_write_json(
                self.root / "state/status.json",
                {"schema_version": 1, "status": status, "updated_at": time.time(), **details},
            )

    def _result_path(self, spec):
        phase = spec.get("phase", "tasks")
        return (
            self.root
            / ("observations" if phase == "comparison" else f"{phase}/observations")
            / work_key(spec)
        )

    def _existing(self, spec):
        bundle = verify_bundle(self._result_path(spec))
        if bundle is not None:
            self._check_identity(spec, bundle["record"])
            return bundle["record"]
        if spec.get("arm") == "baseline" and spec.get("task", "observation") == "observation":
            cached = self.store.get("baseline/observations", spec["observation_id"])
            if cached is not None:
                record = cached["record"]
                self._check_identity(spec, record)
                if not _validation_verified(record) or record["status"] != "SUCCEEDED":
                    raise IntegrityError("Unverified baseline cache entry")
                source = self.store.path("baseline/observations", spec["observation_id"])
                artifacts = {name: source / name for name in cached["artifacts"]}
                record = {
                    **record,
                    "cache_hit": True,
                    "cache_key": spec["observation_id"],
                    "cache_manifest_hash": file_hash(source / "manifest.json"),
                }
                publish_bundle(self._result_path(spec), record, artifacts, lease=self.lease)
                self.journal.append("cache_hit", {"work_key": work_key(spec)})
                return record
        return None

    @staticmethod
    def _check_identity(spec, result, *, attempt=None):
        if not isinstance(result, dict):
            raise IntegrityError("Receipt must be an object")
        fields = ("observation_id", "case_id", "arm", "seed", "phase", "build_id")
        for key in fields:
            if key in spec and result.get(key) != spec[key]:
                raise IntegrityError(f"Receipt {key} mismatch for {work_key(spec)}")
        if attempt is not None:
            for key in (
                "attempt_id",
                "plan_hash",
                "lease_generation",
                "run_id",
                "submission_token",
            ):
                if key in attempt["spec"] and result.get(key) != attempt["spec"][key]:
                    raise IntegrityError(f"Receipt {key} mismatch")
        for key in ("runtime_profile", "protocols"):
            if spec.get("task", "observation") == "observation" and result.get(key) != spec.get(
                key
            ):
                raise IntegrityError(f"Receipt {key} mismatch")
        if result.get("status") not in TERMINAL:
            raise IntegrityError(
                f"Receipt has nonterminal or unknown status: {result.get('status')}"
            )
        if spec.get("task", "observation") == "observation":
            validate_record("ObservationResult", result)

    def _attempts(self, spec):
        parent = self.root / "attempts" / work_key(spec)
        return [read_json(path) for path in sorted(parent.glob("*/attempt.json"))]

    def _save_attempt(self, attempt):
        with self.lease.fenced():
            atomic_write_json(Path(attempt["directory"]) / "attempt.json", attempt)

    def _new_attempt(self, spec, *, token=None, shard_directory=None):
        attempt_id = uuid.uuid4().hex
        token = token or uuid.uuid4().hex
        directory = self.root / "attempts" / work_key(spec) / attempt_id
        run_spec = {
            **spec,
            "attempt_id": attempt_id,
            "output_dir": str(directory),
            "plan_hash": self.plan_hash,
            "lease_generation": self.lease.generation,
            "run_id": canonical_hash(str(self.root)),
            "submission_token": token,
        }
        attempt = {
            "schema_version": 1,
            "observation_id": spec["observation_id"],
            "attempt_id": attempt_id,
            "submission_token": token,
            "status": "SUBMITTING",
            "created_at": time.time(),
            "directory": str(directory),
            "spec": run_spec,
        }
        if shard_directory is not None:
            attempt["shard_directory"] = str(shard_directory)
        self._save_attempt(attempt)
        atomic_write_json(directory / "spec.json", run_spec)
        self.journal.append(
            "submission_intent",
            {"work_key": work_key(spec), "attempt_id": attempt_id, "submission_token": token},
        )
        return attempt

    @staticmethod
    def _command(spec, directory):
        return spec.get("command") or [
            spec.get("python", sys.executable),
            "-m",
            "benchpress_compare.worker"
            if spec.get("task", "observation")
            in {"observation", "freeze-fixture", "freeze-targets", "validate-import"}
            else "benchpress_compare.tasks",
            str(directory / "spec.json"),
        ]

    @staticmethod
    def _scheduler_load(active):
        """Jobs, CPUs and memory held on the scheduler by the active attempts.

        Pending, suspended and uncertain jobs count, since they hold or may hold
        resources; jobs that already ended do not, even before their commit.
        """
        owned = {}
        for spec, attempt in active.values():
            if attempt["job"]["status"] not in SCHEDULER_TERMINAL:
                owned.setdefault(attempt["job"]["token"], spec)
        return (
            len(owned),
            sum(spec["runtime_profile"]["native_threads"] for spec in owned.values()),
            sum(spec["runtime_profile"]["memory_bytes"] for spec in owned.values()),
        )

    # A launch has three steps. Intents and the shard manifest are recorded
    # first and the scheduler's reply is attached to every attempt afterwards,
    # both serially on the controller thread so the journal and attempt files
    # keep the protocol a restarted controller adopts. Only the submission in
    # between, which writes the job's own directory and runs `bsub`, overlaps
    # across a dispatch batch.

    def _job_name(self, token):
        return f"bpc-{canonical_hash(str(self.root))[:8]}-{token}"

    def _prepare_launch(self, spec):
        attempt = self._new_attempt(spec)
        directory = Path(attempt["directory"])
        return {
            "specs": [spec],
            "attempts": [attempt],
            "bundle": None,
            "token": attempt["submission_token"],
            "directory": directory,
            "command": self._command(spec, directory),
            "profile": spec["runtime_profile"],
        }

    def _prepare_bundle(self, specs, bundle):
        token = uuid.uuid4().hex
        directory = self.root / "jobs/shards" / token
        attempts = [
            self._new_attempt(spec, token=token, shard_directory=directory) for spec in specs
        ]
        profile = dict(specs[0]["runtime_profile"])
        if any(spec["runtime_profile"] != profile for spec in specs):
            raise IntegrityError("A shard mixes incompatible observation resource profiles")
        profile["wall_seconds"] = min(
            self.site.job_wall_seconds,
            max(profile["wall_seconds"], math.ceil(bundle["estimated_seconds"] / 0.9)),
        )
        manifest = {
            "schema_version": 1,
            "submission_token": token,
            "plan_hash": self.plan_hash,
            "entries": [
                {
                    "spec_path": str(Path(attempt["directory"]) / "spec.json"),
                    "spec_hash": file_hash(Path(attempt["directory"]) / "spec.json"),
                    "command": self._command(spec, Path(attempt["directory"])),
                }
                for spec, attempt in zip(specs, attempts)
            ],
        }
        atomic_write_json(directory / "shard.json", manifest)
        return {
            "specs": specs,
            "attempts": attempts,
            "bundle": bundle,
            "token": token,
            "directory": directory,
            "command": [
                sys.executable,
                "-m",
                "benchpress_compare.worker.shard",
                str(directory / "shard.json"),
            ],
            "profile": profile,
        }

    def _submit(self, prepared):
        return self.executor.submit(
            prepared["command"],
            prepared["directory"],
            prepared["profile"],
            name=self._job_name(prepared["token"]),
            token=prepared["token"],
        )

    def _submit_batch(self, batch):
        if len(batch) == 1 or self.submit_workers == 1:
            return [self._submit(prepared) for prepared in batch]
        workers = min(len(batch), self.submit_workers)
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(self._submit, batch))

    def _record_launch(self, prepared, job):
        specs, attempts, bundle = prepared["specs"], prepared["attempts"], prepared["bundle"]
        if bundle is not None:
            manifest = prepared["directory"] / "shard.json"
            job.update(
                shard=True, shard_manifest=str(manifest), shard_manifest_hash=file_hash(manifest)
            )
            # The common job belongs to all of these authorized attempts. A restart
            # can adopt the shared job even if it crashed between these writes.
            atomic_write_json(prepared["directory"] / "job.json", job)
        for spec, attempt in zip(specs, attempts):
            attempt.update(job=job, status=job["status"])
            self._save_attempt(attempt)
            data = {"work_key": work_key(spec), "attempt_id": attempt["attempt_id"], "job": job}
            if bundle is not None:
                data["bundle_id"] = bundle.get("bundle_id")
            self.journal.append("submitted", data)
        return attempts

    def _launch(self, spec):
        prepared = self._prepare_launch(spec)
        return self._record_launch(prepared, self._submit(prepared))[0]

    def _launch_bundle(self, specs, bundle):
        prepared = self._prepare_bundle(specs, bundle)
        return self._record_launch(prepared, self._submit(prepared))

    def _dispatch_batch(self, batch, active):
        """Submit a prepared batch and register its attempts; returns the job count."""
        if (self.root / "state/cancel-request.json").exists():
            # Preparation writes durable intents. Retire them before discarding
            # this batch, otherwise a restart would look for jobs never submitted.
            # Their specs stay pending so the cancellation loop publishes results.
            for prepared in batch:
                for attempt in prepared["attempts"]:
                    attempt["status"] = "CANCELLED"
                    self._save_attempt(attempt)
            batch.clear()
            return 0
        for prepared, job in zip(batch, self._submit_batch(batch)):
            for spec, attempt in zip(prepared["specs"], self._record_launch(prepared, job)):
                active[work_key(spec)] = (spec, attempt)
        count = len(batch)
        batch.clear()
        return count

    def _commit_batch(self, ready):
        """Commit collected results, overlapping their publication.

        Each commit publishes one immutable bundle (and a store copy for
        baseline results): a dozen or more fsyncs on the shared filesystem,
        about 0.2 s each, which was two thirds of the controller's time per
        shard. Every shared write inside `_commit` (bundle rename, attempt
        record, journal append, store lease) is already serialised by a file
        lock, so the publications only overlap their staging and syncing.
        """
        if len(ready) <= 1 or self.collect_workers == 1:
            return [self._commit(spec, attempt, result) for spec, attempt, result in ready]
        workers = min(len(ready), self.collect_workers)
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(lambda item: self._commit(*item), ready))

    def _adopt(self, spec, attempts):
        live = [a for a in attempts if a["status"] not in TERMINAL | {"INTERRUPTED", "SUPERSEDED"}]
        if len(live) > 1:
            raise ReconciliationError(f"Multiple current attempts for {work_key(spec)}")
        if not live:
            return None
        attempt = live[0]
        if "job" not in attempt:
            job_path = Path(attempt.get("shard_directory", attempt["directory"])) / "job.json"
            if job_path.exists():
                attempt["job"] = read_json(job_path)
                if attempt.get("shard_directory"):
                    attempt["job"]["shard"] = True
            else:
                # Crash between intent and response: never speculate that no job exists.
                attempt["job"] = {
                    "schema_version": 1,
                    "executor": self.config.executor,
                    "status": "SUBMISSION_UNKNOWN",
                    "token": attempt["submission_token"],
                    "name": f"bpc-{canonical_hash(str(self.root))[:8]}-{attempt['submission_token']}",
                    "submitted_at": attempt["created_at"],
                    "attempt_dir": attempt.get("shard_directory", attempt["directory"]),
                    "runtime_profile": spec["runtime_profile"],
                    "peak_rss_bytes": 0,
                }
        self.journal.append(
            "attempt_adopted",
            {
                "attempt_id": attempt["attempt_id"],
                "original_generation": attempt["spec"]["lease_generation"],
            },
        )
        return attempt

    def _commit(self, spec, attempt, result):
        self._check_identity(spec, result, attempt=attempt if result.get("attempt_id") else None)
        directory = Path(attempt["directory"])
        artifacts = {}
        for name, digest in result.get("artifacts", {}).items():
            path = (directory / name).resolve()
            if not path.is_relative_to(directory.resolve()) or file_hash(path) != digest:
                raise IntegrityError(f"Receipt artifact mismatch: {name}")
            artifacts[name] = path
        resources = result.setdefault("resources", {})
        job = attempt.get("job", {})
        resources["peak_rss_bytes"] = max(
            resources.get("peak_rss_bytes", 0), job.get("peak_rss_bytes", 0)
        )
        resources["elapsed_seconds"] = max(
            resources.get("elapsed_seconds", 0), job.get("elapsed_seconds", 0)
        )
        result.update(
            accepted_attempt=attempt["attempt_id"],
            runtime_profile=spec["runtime_profile"],
            protocols=spec.get("protocols"),
            cache_hit=False,
        )
        if (
            result["status"] == "SUCCEEDED"
            and spec.get("task", "observation") == "observation"
            and not _validation_verified(result)
        ):
            raise IntegrityError("Successful observation lacks required validation")
        result["validation_status"] = result.get("validation", {}).get("status")
        publish_bundle(self._result_path(spec), result, artifacts, lease=self.lease)
        if (
            result.get("arm") == "baseline"
            and result["status"] == "SUCCEEDED"
            and result.get("validation_status") == "verified"
        ):
            # Reusable store bundles own copies of every referenced artifact.
            existing = self.store.get("baseline/observations", spec["observation_id"])
            if existing is None:
                self.store.publish(
                    "baseline/observations", spec["observation_id"], result, artifacts
                )
            elif any(existing["record"].get(key) != result.get(key) for key in ("N2", "D2")):
                raise IntegrityError("Conflicting baseline result for immutable observation")
        attempt["status"] = result["status"]
        self._save_attempt(attempt)
        self.journal.append(
            "result_committed",
            {
                "work_key": work_key(spec),
                "status": result["status"],
                "attempt_id": attempt["attempt_id"],
            },
        )
        return result

    def _failed_result(self, spec, attempt, status, reason):
        return {
            **{
                key: spec[key]
                for key in ("observation_id", "case_id", "arm", "phase", "seed", "build_id")
                if key in spec
            },
            "schema_version": 1,
            "status": status,
            "N2": None,
            "D2": None,
            "reason": reason,
            "attempt_id": attempt["attempt_id"],
            "plan_hash": attempt["spec"]["plan_hash"],
            "lease_generation": attempt["spec"]["lease_generation"],
            "runtime_profile": spec["runtime_profile"],
            "protocols": spec.get("protocols"),
            **{
                key: attempt["spec"][key]
                for key in ("run_id", "submission_token")
                if key in attempt["spec"]
            },
        }

    def run(self, specs, *, retry_cancelled=False, bundles=None):
        specs = list(specs)
        keys = [work_key(spec) for spec in specs]
        if len(keys) != len(set(keys)):
            raise IntegrityError("Duplicate planned work")
        profiles = {
            canonical_hash(spec["runtime_profile"]): spec["runtime_profile"] for spec in specs
        }
        if self.site is None and self.config.workers != "auto" and len(profiles) == 1:
            worker_capacity(
                self.resources, next(iter(profiles.values())), len(specs), self.config.workers
            )
        bundle_by_key = {}
        if self.site is not None:
            if bundles is None:
                # Preparation uses the same protocol. Observations and fixture
                # tasks sharing a resource profile are sharded sequentially so
                # that the worst-case sum of their frozen watchdogs fits one job;
                # build, target and import tasks stay separate.
                grouped = {}
                for spec in specs:
                    task = spec.get("task", "observation")
                    if task in {"observation", "freeze-fixture"}:
                        key = (task, canonical_hash(spec["runtime_profile"]))
                        grouped.setdefault(key, []).append(spec)
                bundles = []
                for rows in grouped.values():
                    wall = rows[0]["runtime_profile"]["wall_seconds"]
                    size = max(
                        1,
                        min(
                            self.site.shard_max_observations,
                            int((self.site.job_wall_seconds - 10) // wall),
                        ),
                    )
                    for offset in range(0, len(rows), size):
                        part = rows[offset : offset + size]
                        bundles.append(
                            {
                                "observations": part,
                                "estimated_seconds": 10 + wall * len(part),
                            }
                        )
            expected = {work_key(spec): spec for spec in specs}
            for bundle in bundles:
                for spec in bundle["observations"]:
                    key = work_key(spec)
                    if key in bundle_by_key or key not in expected or spec != expected[key]:
                        raise IntegrityError("Shard plan duplicates or changes frozen observations")
                    bundle_by_key[key] = bundle
        active, pending, results = {}, [], {}
        for spec in specs:
            key = work_key(spec)
            existing = self._existing(spec)
            if existing:
                if existing["status"] == "CANCELLED" and retry_cancelled:
                    # Keep the old immutable receipt as history; reopening is explicit.
                    old = self._result_path(spec)
                    with self.lease.fenced():
                        os.rename(
                            old, old.with_name("." + old.name + ".cancelled." + uuid.uuid4().hex)
                        )
                    self.journal.append("cancelled_work_reopened", {"work_key": key})
                else:
                    results[key] = existing
                    continue
            attempts = self._attempts(spec)
            adopted = self._adopt(spec, attempts)
            if adopted:
                active[key] = (spec, adopted)
            else:
                # A controller can die after recording a failed attempt but
                # before publishing the terminal result. Persisted attempts,
                # rather than a fresh controller's loop, define the retry budget.
                failed = [attempt for attempt in attempts if attempt["status"] == "INFRA_FAILED"]
                if len(failed) > self.config.infrastructure_retries:
                    attempt = max(failed, key=lambda value: value["created_at"])
                    result = self._failed_result(
                        spec, attempt, "INFRA_FAILED", "infrastructure_retries_exhausted"
                    )
                    results[key] = self._commit(spec, attempt, result)
                else:
                    pending.append(spec)
        try:
            while pending or active:
                self.lease.assert_owned()
                if (self.root / "state/cancel-request.json").exists():
                    cancelled_jobs = {}
                    for key, (spec, attempt) in list(active.items()):
                        if not attempt.get("cancel_requested"):
                            token = attempt["job"]["token"]
                            if token not in cancelled_jobs:
                                cancelled_jobs[token] = self.executor.cancel(attempt["job"])
                            attempt["job"] = cancelled_jobs[token]
                            attempt["cancel_requested"] = True
                            self._save_attempt(attempt)
                    for spec in pending:
                        attempt = {
                            "attempt_id": "cancelled-before-launch",
                            "directory": str(self.root),
                            "spec": {
                                "plan_hash": self.plan_hash,
                                "lease_generation": self.lease.generation,
                            },
                        }
                        # Cancellation before submission needs no process or fabricated attempt.
                        result = self._failed_result(spec, attempt, "CANCELLED", "user_cancelled")
                        publish_bundle(self._result_path(spec), result, lease=self.lease)
                        results[work_key(spec)] = result
                    pending.clear()
                # Each iteration polls, collects, then dispatches, so the budget
                # sees fresh statuses and an uncertain poll stops dispatch at
                # once. Collection and dispatch are both bounded so neither phase
                # can starve the other while the cluster drains or fills.
                uncertain = []
                harness_failures = []
                collected = 0
                ready = []
                owned = {}
                for spec, attempt in active.values():
                    owned.setdefault(attempt["job"]["token"], attempt["job"])
                polled_jobs = self.executor.poll_many(owned) if owned else {}
                for key, (spec, attempt) in list(active.items()):
                    token = attempt["job"]["token"]
                    job = dict(polled_jobs[token])
                    # Persist only scheduler changes: finished work waiting past the
                    # collection bound is otherwise rewritten every iteration, and a
                    # few thousand such writes stall the loop for over a minute.
                    changed = attempt.get("job") != job or attempt.get("status") != job["status"]
                    attempt.update(job=job, status=job["status"])
                    if changed:
                        self._save_attempt(attempt)
                    if job["status"] in {"NEEDS_RECONCILIATION", "SUBMISSION_UNKNOWN"}:
                        if job["status"] == "NEEDS_RECONCILIATION" or self.site is None:
                            uncertain.append(key)
                        continue
                    if job["status"] not in SCHEDULER_TERMINAL:
                        continue
                    if collected >= COLLECT_PER_ITERATION:
                        # Left in active and excluded from the dispatch budget;
                        # the next poll returns it without a scheduler query.
                        continue
                    collected += 1
                    receipt = Path(attempt["directory"]) / "receipt.json"
                    supervisor_path = Path(attempt["directory"]) / "supervisor.json"
                    supervisor = read_json(supervisor_path) if supervisor_path.exists() else None
                    if supervisor is not None:
                        for field in ("attempt_id", "observation_id", "submission_token"):
                            if supervisor.get(field) != attempt["spec"][field]:
                                raise IntegrityError(f"Shard supervisor {field} mismatch")
                        job.update(supervisor["resources"])
                        job["return_code"] = supervisor.get("return_code")
                        attempt["job"] = job
                    # A scheduler that enforced the job's limits explains only the
                    # child running at the time: siblings keep their receipts and
                    # the ones the shard never reached are infrastructure casualties.
                    termination = job.get("termination")
                    limit = job.get("reason") if termination else None
                    started = (
                        supervisor["resources"].get("elapsed_seconds", 0) > 0
                        if supervisor is not None
                        else (Path(attempt["directory"]) / "job.json").exists()
                    )
                    if supervisor and supervisor["status"] in {
                        "RESOURCE_LIMIT",
                        "INFRA_FAILED",
                        "HARNESS_FAILED",
                    }:
                        result = self._failed_result(
                            spec, attempt, supervisor["status"], supervisor.get("reason")
                        )
                    elif job["status"] == "RESOURCE_LIMIT":
                        result = self._failed_result(
                            spec, attempt, "RESOURCE_LIMIT", job.get("reason")
                        )
                    elif receipt.exists():
                        try:
                            result = read_json(receipt)
                            if not isinstance(result, dict):
                                raise ValueError("Receipt must be an object")
                            if (
                                result.get("schema_version") != 1
                                or result.get("status") not in TERMINAL
                            ):
                                raise ValueError("Receipt schema or terminal status is invalid")
                            if spec.get("task", "observation") == "observation":
                                validate_record("ObservationResult", result)
                                if any(
                                    field not in result
                                    for field in ("runtime_profile", "protocols")
                                ):
                                    raise ValueError(
                                        "Observation receipt lacks runtime/protocol identity"
                                    )
                                if result["status"] == "SUCCEEDED" and any(
                                    field not in result for field in ("N2", "D2")
                                ):
                                    raise ValueError("Successful observation lacks metric fields")
                            if not isinstance(result.get("artifacts", {}), dict):
                                raise ValueError("Receipt artifact manifest must be an object")
                        except (ValueError, UnicodeDecodeError, IntegrityError) as exc:
                            result = self._failed_result(
                                spec, attempt, "INVALID", f"malformed_receipt: {exc}"
                            )
                            result["artifacts"] = {"receipt.json": file_hash(receipt)}
                        else:
                            self._check_identity(spec, result, attempt=attempt)
                    elif attempt.get("cancel_requested") or job["status"] == "CANCELLED":
                        result = self._failed_result(spec, attempt, "CANCELLED", "user_cancelled")
                    elif job.get("return_code") in {-6, -11}:
                        result = self._failed_result(
                            spec, attempt, "COMPILER_FAILED", "native_crash"
                        )
                    elif limit and started:
                        result = self._failed_result(spec, attempt, "RESOURCE_LIMIT", limit)
                    elif job["status"] == "INFRA_FAILED":
                        result = self._failed_result(
                            spec,
                            attempt,
                            "INFRA_FAILED",
                            job.get("reason", "confirmed_infrastructure_failure"),
                        )
                    elif not started:
                        result = self._failed_result(
                            spec, attempt, "INFRA_FAILED", "job_ended_before_start"
                        )
                    elif (
                        termination
                        or job.get("return_code") in {-9, -15}
                        or (supervisor and supervisor["status"] == "CANCELLED")
                    ):
                        # SIGKILL, SIGTERM and scheduler terminations come from
                        # outside the worker, so unlike a crash they say nothing
                        # about the compiler; one infrastructure retry is safe.
                        result = self._failed_result(
                            spec,
                            attempt,
                            "INFRA_FAILED",
                            (termination or "external_signal").lower(),
                        )
                    else:
                        # An unexplained process exit is not evidence of node
                        # eviction or transient infrastructure. Retrying it can
                        # otherwise hide a compiler crash behind a second sample.
                        result = self._failed_result(
                            spec, attempt, "HARNESS_FAILED", "terminal_worker_without_receipt"
                        )
                    if result["status"] == "INFRA_FAILED":
                        attempt["status"] = "INFRA_FAILED"
                        self._save_attempt(attempt)
                        count = sum(a["status"] == "INFRA_FAILED" for a in self._attempts(spec))
                        if count <= self.config.infrastructure_retries:
                            self.journal.append(
                                "infrastructure_retry", {"work_key": key, "attempt": count}
                            )
                            pending.append(spec)
                            del active[key]
                            continue
                    ready.append((key, spec, attempt, result))
                committed = self._commit_batch([item[1:] for item in ready])
                for (key, spec, attempt, result), record in zip(ready, committed):
                    results[key] = record
                    del active[key]
                    if result["status"] == "HARNESS_FAILED":
                        harness_failures.append(result.get("reason", key))
                if uncertain:
                    self.state("NEEDS_RECONCILIATION", affected=uncertain)
                    raise ReconciliationError(
                        "Uncertain owned jobs require reconciliation; no replacements submitted"
                    )
                if harness_failures:
                    raise PreparationError(f"Worker protocol incompatible: {harness_failures[0]}")
                job_count, used_cpu, used_memory = self._scheduler_load(active)
                launched = 0
                deadline = time.monotonic() + self.poll_interval
                # Membership by work key: comparing whole spec dicts against a
                # pending list of thousands made every budget-full scan quadratic.
                pending_keys = {work_key(spec) for spec in pending}
                taken = set()
                batch = []
                for spec in pending:
                    if time.monotonic() >= deadline:
                        break
                    if (self.root / "state/cancel-request.json").exists():
                        break
                    key = work_key(spec)
                    if key in taken:
                        continue
                    profile = spec["runtime_profile"]
                    if (
                        profile["native_threads"] > self.resources["usable_cpus"]
                        or profile["memory_bytes"] > self.resources["worker_memory_budget_bytes"]
                    ):
                        raise PreparationError(
                            f"Zero capacity for observation {work_key(spec)}: {profile}"
                        )
                    if not (
                        job_count < self.maximum_jobs
                        and used_cpu + profile["native_threads"] <= self.resources["usable_cpus"]
                        and used_memory + profile["memory_bytes"]
                        <= self.resources["worker_memory_budget_bytes"]
                    ):
                        continue
                    bundle = bundle_by_key.get(key)
                    selected = (
                        [
                            row
                            for row in bundle["observations"]
                            if work_key(row) in pending_keys and work_key(row) not in taken
                        ]
                        if bundle
                        else [spec]
                    )
                    # The budget is reserved before the batch is submitted, so a
                    # batch never overshoots the job, CPU or memory limits.
                    taken.update(work_key(row) for row in selected)
                    batch.append(
                        self._prepare_bundle(selected, bundle)
                        if bundle
                        else self._prepare_launch(spec)
                    )
                    job_count += 1
                    used_cpu += profile["native_threads"]
                    used_memory += profile["memory_bytes"]
                    if len(batch) >= self.submit_workers:
                        launched += self._dispatch_batch(batch, active)
                if batch:
                    launched += self._dispatch_batch(batch, active)
                if taken:
                    # Prepared jobs cancelled before submission still need the
                    # pending-work cancellation path on the next iteration.
                    pending[:] = [spec for spec in pending if work_key(spec) not in active]
                # Loop straight back while there is a backlog to work off.
                if active and not launched and not collected:
                    time.sleep(min(self.poll_interval, 30))
        except KeyboardInterrupt:
            if self.site is None:
                for spec, attempt in active.values():
                    self.executor.cancel(attempt["job"])
                    attempt["status"] = "INTERRUPTED"
                    self._save_attempt(attempt)
            self.state("INTERRUPTED")
            raise
        return [results[key] for key in keys]


def collect(root, *, retry_cancelled=False):
    root = Path(root).resolve()
    plan = verify_plan(root)
    config = RunConfig(**plan["config"])
    from .builds import verify_build

    for manifest in plan["builds"].values():
        verify_build(Path(manifest["manifest_path"]).parent)
    with Lease(root / "state/controller.lease") as lease:
        engine = ExecutionEngine(root, config, lease, plan_hash=plan["plan_hash"])
        engine.journal.replay()
        if retry_cancelled:
            with lease.fenced():
                (root / "state/cancel-request.json").unlink(missing_ok=True)
        engine.state("RUNNING")
        results = engine.run(
            plan["observations"], retry_cancelled=retry_cancelled, bundles=plan.get("bundles")
        )
        status = (
            "COMPLETE"
            if all(row["status"] == "SUCCEEDED" for row in results)
            else "COMPLETE_WITH_FAILURES"
        )
        engine.state(status)
        from .eligibility import apply_admission
        from .report import build_report, write_report

        write_report(root, build_report(apply_admission(plan, results), results, status=status))
        return status


def status(root):
    root = Path(root)
    path = root / "state/status.json"
    state = read_json(path) if path.exists() else {"schema_version": 1, "status": "NEW"}
    if (root / "plan.json").exists():
        plan = verify_plan(root, check_harness=False)
        # Status is a progress view; reports re-verify every artifact checksum.
        results = read_results(root, check_artifacts=False)
        state = {
            **state,
            "expected_observations": len(plan["observations"]),
            "completed_observations": len(results),
            "succeeded": sum(row["status"] == "SUCCEEDED" for row in results),
            "cache_hits": sum(bool(row.get("cache_hit")) for row in results),
        }
    return state


def cancel(root):
    root = Path(root).resolve()
    if not (root / "request.json").is_file():
        raise PreparationError("Cannot cancel: results directory has no owned request.json")
    atomic_write_json(
        root / "state/cancel-request.json", {"schema_version": 1, "requested_at": time.time()}
    )
    cancelled, errors = [], []
    owned = set()

    def cancel_job(job):
        if job["token"] in owned:
            return
        owned.add(job["token"])
        if job["status"] in {"DONE", "EXIT", "CANCELLED"}:
            return
        executor = (
            LocalExecutor()
            if job["executor"] == "local"
            else LSFExecutor(SiteConfig(**job["site"]))
        )
        try:
            result = executor.cancel(job)
        except ReconciliationError as exc:
            # An uncertain job must not prevent cancellation of the other
            # known jobs owned by this run.
            errors.append(f"{job['name']}: {exc}")
            return
        if result["status"] not in {"DONE", "EXIT"}:
            cancelled.append(result.get("job_id", result.get("pid")))

    # Stop dispatch before cancelling workers. The controller may still be
    # pending, in which case it has no observation attempts to discover below.
    controller_paths = [root / "jobs/controller.json"]
    controller_paths.extend(sorted((root / "jobs/controllers").glob("*/job.json")))
    for path in controller_paths:
        if path.exists():
            cancel_job(read_json(path))
    for path in sorted((root / "attempts").glob("*/*/attempt.json")):
        attempt = read_json(path)
        if attempt["status"] in TERMINAL | {"INTERRUPTED", "SUPERSEDED"}:
            continue
        job = attempt.get("job")
        if not job:
            # The scheduler reply may have been saved just before the
            # controller died without attaching it to each shard member.
            job_path = Path(attempt.get("shard_directory", attempt["directory"])) / "job.json"
            if job_path.exists():
                job = read_json(job_path)
            else:
                errors.append(f"Unlocated submission intent: {attempt['submission_token']}")
                continue
        cancel_job(job)
    if errors:
        raise ReconciliationError(
            f"Cancellation requested for {len(cancelled)} jobs; unresolved: " + "; ".join(errors)
        )
    return {"schema_version": 1, "status": "CANCELLATION_REQUESTED", "jobs": cancelled}
