"""Durable asynchronous LSF controller launch and client-side progress following."""

from __future__ import annotations

from pathlib import Path
import platform
import sys
import time
import uuid

from .config import RunConfig, SiteConfig
from .errors import CompareError, ReconciliationError
from .executors.lsf import LSFExecutor
from .identity import atomic_write_json, canonical_hash, harness_identity, read_json
from .orchestrator import status, verify_plan
from .pilot import runtime_profile
from .store import Lease


def submit_controller(config: RunConfig, *, prepare_only=False, retry_cancelled=False):
    root = Path(config.results_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    site = SiteConfig.read(config.lsf_config)
    if platform.system() != "Linux" and not site.controller_python:
        raise CompareError(
            "A non-Linux launch host requires LSF controller_python pointing to the cluster's installed coordinator environment"
        )
    executor = LSFExecutor(site)
    with Lease(root / "state/launch.lease") as lease:
        request_path = root / "request.json"
        if request_path.exists():
            request = read_json(request_path)
            if request.get("coordinator_hash", harness_identity()) != harness_identity():
                raise CompareError(
                    "Coordinator implementation changed after launch; use a new results directory"
                )
        if (root / "plan.json").exists():
            verify_plan(root)
            if prepare_only:
                return {
                    "schema_version": 1,
                    "executor": "lsf",
                    "status": "FROZEN",
                    "submitted_at": time.time(),
                    "job_id": None,
                }
        path = root / "jobs/controller.json"
        options = {
            "prepare_only": prepare_only,
            "retry_cancelled": retry_cancelled,
            "accept_unresolved": config.accept_unresolved,
        }
        if path.exists():
            previous = read_json(path)
            run_state = status(root)
            if (run_state["status"] == "COMPLETE" and not retry_cancelled) or (
                prepare_only and run_state["status"] == "FROZEN"
            ):
                return previous
            job_path = Path(previous["attempt_dir"]) / "job.json"
            if previous["status"] == "SUBMITTING" and job_path.exists():
                previous = {
                    **read_json(job_path),
                    "controller_options": previous.get("controller_options", {}),
                }
            # Persisted terminal scheduler evidence remains valid after the
            # scheduler ages the job out of its query/history windows.
            current = (
                previous if previous["status"] in {"DONE", "EXIT"} else executor.poll(previous)
            )
            with lease.fenced():
                atomic_write_json(path, current)
            if current["status"] in {"SUBMISSION_UNKNOWN", "NEEDS_RECONCILIATION"}:
                raise ReconciliationError(
                    "Previous controller submission is uncertain; reconcile before launching a replacement"
                )
            if current["status"] not in {"DONE", "EXIT"}:
                if current.get("controller_options", options) != options:
                    raise CompareError(
                        "The active controller has different requested actions; resume after it finishes"
                    )
                return current
        # Snapshot on the launch host; workers never depend on live source trees.
        from .preparation import _snapshot_paths, _harness_wheel

        if not request_path.exists():
            config.validate()
            with lease.fenced():
                atomic_write_json(
                    request_path,
                    {
                        "schema_version": 1,
                        "config": config.to_dict(),
                        "coordinator_hash": harness_identity(),
                    },
                )
        else:
            saved = RunConfig(**read_json(request_path)["config"])
            saved_values, requested_values = saved.to_dict(), config.to_dict()
            if config.accept_unresolved:
                saved_values["accept_unresolved"] = True
            if saved_values != requested_values:
                raise CompareError("LSF continuation must use its saved configuration")
            if "coordinator_hash" not in request:
                with lease.fenced():
                    atomic_write_json(
                        request_path, {**request, "coordinator_hash": harness_identity()}
                    )
        _snapshot_paths(config, root)
        _harness_wheel(root / "preparation/harness")
        token = uuid.uuid4().hex
        directory = root / "jobs/controllers" / token
        name = f"bpc-controller-{canonical_hash(str(root))[:8]}-{token}"
        profile = runtime_profile(
            native_threads=site.controller_cpus,
            wall_seconds=site.job_wall_seconds,
            memory_gb=site.controller_memory_gb,
        )
        command = [
            site.controller_python or sys.executable,
            "-m",
            "benchpress_compare",
            "_controller",
            "--results-root",
            str(root),
        ]
        if prepare_only:
            command.append("--prepare-only")
        if retry_cancelled:
            command.append("--retry-cancelled")
        if config.accept_unresolved:
            command.append("--accept-unresolved")
        intent = {
            "schema_version": 1,
            "status": "SUBMITTING",
            "executor": "lsf",
            "name": name,
            "token": token,
            "attempt_dir": str(directory),
            "submitted_at": time.time(),
            "runtime_profile": profile,
            "controller_options": options,
            "site": vars(site),
        }
        with lease.fenced():
            atomic_write_json(path, intent)
        job = executor.submit(command, directory, profile, name=name, token=token)
        job["controller_options"] = options
        with lease.fenced():
            atomic_write_json(path, job)
        if job["status"] == "SUBMISSION_UNKNOWN":
            raise ReconciliationError(
                "Controller submission reply is uncertain; next invocation will reconcile its unique name"
            )
        return job


def follow_controller(config, job, *, prepare_only=False):
    if prepare_only and job["status"] == "FROZEN":
        return "FROZEN"
    executor = LSFExecutor(SiteConfig.read(config.lsf_config))
    previous_status = None
    while True:
        current = status(config.results_root)
        if current != previous_status:
            print(
                f"{current['status']}: {current.get('completed_observations', 0)}/{current.get('expected_observations', '?')} observations",
                flush=True,
            )
            previous_status = current
        belongs_to_submission = current.get("updated_at", 0) >= job["submitted_at"]
        if belongs_to_submission and current["status"] in {
            "COMPLETE",
            "COMPLETE_WITH_FAILURES",
            "PREPARATION_FAILED",
            "NEEDS_RECONCILIATION",
            "CANCELLED",
        }:
            return current["status"]
        if belongs_to_submission and prepare_only and current["status"] == "FROZEN":
            return "FROZEN"
        job = executor.poll(job)
        if job["status"] in {"DONE", "EXIT", "NEEDS_RECONCILIATION"}:
            # A scheduler DONE without a terminal coordinator state is not success.
            raise ReconciliationError(
                "Controller ended without a terminal run state; resume to reconcile its workers"
            )
        time.sleep(min(executor.site.poll_seconds, 30))
