"""Fresh process groups with identity-aware recovery and process-tree watchdogs."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import time

import psutil

from ..identity import atomic_write_json
from ..errors import ReconciliationError


def worker_environment(profile: dict, scratch: Path) -> dict:
    scratch.mkdir(parents=True, exist_ok=True)
    env = {
        name: os.environ[name]
        for name in ("PATH", "SYSTEMROOT", "LANG", "LC_ALL", "TZ")
        if name in os.environ
    }
    env.update(
        {
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "HOME": str(scratch),
            "XDG_CACHE_HOME": str(scratch / "cache"),
            "XDG_CONFIG_HOME": str(scratch / "config"),
            "TMPDIR": str(scratch),
            "QISKIT_SETTINGS": str(scratch / "settings.conf"),
            **profile.get("thread_environment", {}),
        }
    )
    return env


def matching_process(job, *, include_zombie=False):
    if not job.get("pid"):
        return None
    try:
        process = psutil.Process(job["pid"])
        if abs(process.create_time() - job["process_create_time"]) > 0.01:
            return None
        if not include_zombie and process.status() == psutil.STATUS_ZOMBIE:
            return None
        return process
    except psutil.NoSuchProcess:
        return None


def _process_tree(job):
    """Refresh descendant identities before they can be reparented on exit."""
    known = {item["pid"]: item for item in job.get("descendants", [])}
    roots = [job, *known.values()]
    processes = {}
    for identity in roots:
        process = matching_process(identity, include_zombie=True)
        if process is None:
            continue
        try:
            for child in [process, *process.children(recursive=True)]:
                if child.status() != psutil.STATUS_ZOMBIE:
                    processes[child.pid] = child
                if child.pid != job.get("pid"):
                    try:
                        group = os.getpgid(child.pid)
                    except ProcessLookupError:
                        # macOS reports ESRCH for zombies; keep any earlier value.
                        group = known.get(child.pid, {}).get("process_group")
                    known[child.pid] = {
                        "pid": child.pid,
                        "process_create_time": child.create_time(),
                        "process_group": group,
                    }
        except psutil.NoSuchProcess:
            continue
    job["descendants"] = sorted(known.values(), key=lambda item: item["pid"])
    return list(processes.values())


def _group_exists(group):
    if not group:
        return False
    try:
        os.killpg(group, 0)
        return True
    except PermissionError:
        # The group exists (macOS returns EPERM when it holds only zombies).
        return True
    except ProcessLookupError:
        return False


def terminate_group(job, *, grace_seconds=2):
    processes = _process_tree(job)
    if not processes:
        if _group_exists(job.get("pid")):
            # A group without a surviving verified PID is ambiguous; never kill
            # an unrelated group after PID reuse merely because its id matches.
            zombie = matching_process(job, include_zombie=True)
            if zombie is None:
                raise ReconciliationError(
                    "Worker group remains but its process identities cannot be verified"
                )
        return
    identities = {process.pid: process.create_time() for process in processes}
    groups = set()
    for process in processes:
        try:
            group = os.getpgid(process.pid)
            if group == job.get("pid") or group in identities:
                groups.add(group)
            else:
                process.terminate()
        except ProcessLookupError:
            pass
    for group in groups:
        try:
            os.killpg(group, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline and _process_tree(job):
        time.sleep(0.05)
    # Re-check PID/create-time pairs before final escalation, including children
    # that entered their own sessions and a group whose original leader exited.
    for process in _process_tree(job):
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass


class LocalExecutor:
    def __init__(self):
        self.processes = {}
        self.started = {}

    def submit(self, command, attempt_dir, profile, *, name, token):
        attempt_dir = Path(attempt_dir).resolve()
        attempt_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "schema_version": 1,
            "executor": "local",
            "name": name,
            "token": token,
            "status": "SUBMITTING",
            "submitted_at": time.time(),
            "attempt_dir": str(attempt_dir),
            "runtime_profile": profile,
            "peak_rss_bytes": 0,
        }
        atomic_write_json(attempt_dir / "job.json", record)
        try:
            with (
                (attempt_dir / "stdout.log").open("ab") as out,
                (attempt_dir / "stderr.log").open("ab") as err,
            ):
                process = subprocess.Popen(
                    command,
                    cwd=attempt_dir,
                    stdout=out,
                    stderr=err,
                    env=worker_environment(profile, attempt_dir / "scratch"),
                    start_new_session=True,
                )
        except OSError as exc:
            record.update(status="INFRA_FAILED", reason=str(exc))
            atomic_write_json(attempt_dir / "job.json", record)
            return record
        self.processes[token] = process
        self.started[token] = time.monotonic()
        try:
            create_time = psutil.Process(process.pid).create_time()
        except psutil.NoSuchProcess:
            create_time = time.time()
        record.update(pid=process.pid, process_create_time=create_time, status="RUNNING")
        record["descendants"] = []
        atomic_write_json(attempt_dir / "job.json", record)
        return record

    def poll(self, job):
        job = dict(job)
        if job["status"] in {"INFRA_FAILED", "RESOURCE_LIMIT", "CANCELLED"}:
            return job
        token = job["token"]
        processes = _process_tree(job)
        child = self.processes.get(token)
        if child is not None:
            return_code = child.poll()
        else:
            return_code = None
        if not processes:
            if job["status"] in {"SUBMITTING", "SUBMISSION_UNKNOWN"} and not job.get("pid"):
                return {**job, "status": "SUBMISSION_UNKNOWN"}
            if matching_process(job, include_zombie=True) is None and _group_exists(job.get("pid")):
                return {
                    **job,
                    "status": "NEEDS_RECONCILIATION",
                    "reason": "unverified_surviving_process_group",
                }
            return {
                **job,
                "status": "DONE" if return_code in {0, None} else "EXIT",
                "return_code": return_code,
                "elapsed_seconds": max(0.000001, time.time() - job["submitted_at"]),
            }
        try:
            rss = sum(p.memory_info().rss for p in processes if p.is_running())
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            rss = job["peak_rss_bytes"]
        job["peak_rss_bytes"] = max(job["peak_rss_bytes"], rss)
        elapsed = (
            time.monotonic() - self.started[token]
            if token in self.started
            else max(0, time.time() - job["submitted_at"])
        )
        job["elapsed_seconds"] = elapsed
        profile = job["runtime_profile"]
        reason = (
            "wall_limit"
            if elapsed > profile["wall_seconds"]
            else "memory_limit"
            if rss > profile["memory_bytes"]
            else None
        )
        if reason:
            terminate_group(job)
            job.update(status="RESOURCE_LIMIT", reason=reason)
        return job

    def poll_many(self, jobs):
        return {token: self.poll(job) for token, job in jobs.items()}

    def cancel(self, job):
        terminate_group(job)
        return {**job, "status": "CANCELLED"}
