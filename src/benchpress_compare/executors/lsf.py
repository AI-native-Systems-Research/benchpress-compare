"""LSF argument-vector transport and conservative uncertain-job reconciliation."""

from __future__ import annotations

from dataclasses import asdict
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time

from ..config import SiteConfig
from ..errors import ReconciliationError
from ..identity import atomic_write_json

ACTIVE = {"PEND", "RUN", "PSUSP", "USUSP", "SSUSP", "WAIT", "PROV"}
TERMINAL = {"DONE", "EXIT", "ZOMBI"}
_NOT_FOUND = re.compile(r"not found|no matching job|no unfinished job", re.IGNORECASE)
# Scheduler termination codes that enforce the job's own resource limits. Any
# other TERM_* code is an external termination for the coordinator to classify.
LIMIT_TERMINATIONS = {
    "TERM_MEMLIMIT": "memory_limit",
    "TERM_SWAP": "memory_limit",
    "TERM_RUNLIMIT": "wall_limit",
}
_TERMINATION = re.compile(r"\bTERM_[A-Z_]+\b")


def parse_jobs(output: str) -> list[dict]:
    """Parse requested jobid/stat/job_name output, never infer absence on errors.

    A per-record ``not found`` error is an explicit statement that the scheduler
    does not list that job; it is returned as absence, unlike any other error.
    """
    text = output.strip()
    if not text:
        return []
    if text.startswith("{") or text.startswith("["):
        data = json.loads(text)
        if isinstance(data, dict) and any(key.upper() == "ERROR" for key in data):
            raise ValueError("Scheduler returned an error object")
        rows = data if isinstance(data, list) else data.get("RECORDS", data.get("records"))
        if not isinstance(rows, list):
            raise ValueError("Unrecognized scheduler JSON")
        normalized = []
        for row in rows:
            row = {key.lower(): value for key, value in row.items()}
            if "error" in row and "stat" not in row:
                if _NOT_FOUND.search(str(row["error"])):
                    continue
                raise ValueError(f"Scheduler record error: {row['error']}")
            job_id, state = row.get("jobid", row.get("job_id")), row.get("stat", row.get("status"))
            if job_id is None or state not in ACTIVE | TERMINAL | {"UNKWN"}:
                raise ValueError("Missing or unknown scheduler identity/state")
            record = {
                "job_id": str(job_id),
                "scheduler_state": state,
                "name": row.get("job_name", row.get("jobname", "")),
            }
            termination = _TERMINATION.search(str(row.get("exit_reason") or ""))
            if termination:
                record["termination"] = termination.group(0)
            normalized.append(record)
        return normalized
    rows = []
    for line in text.splitlines():
        if _NOT_FOUND.search(line):
            continue
        fields = line.split(None, 2)
        if fields and fields[0].upper() in {"JOBID", "JOB_ID"}:
            continue
        if (
            len(fields) != 3
            or not fields[0].isdigit()
            or fields[1] not in ACTIVE | TERMINAL | {"UNKWN"}
        ):
            raise ValueError(f"Unrecognized scheduler row: {line!r}")
        rows.append({"job_id": fields[0], "scheduler_state": fields[1], "name": fields[2]})
    return rows


def parse_history(output: str) -> list[dict]:
    """Parse ``bhist -l`` records; ``-o`` is unavailable on many LSF 10.1 sites.

    Wrapped continuation lines are rejoined before matching. Identifiers can be
    reused by the scheduler, so callers must match the unique job name as well.
    """
    text = re.sub(r"\n\s{10,}", "", output or "")
    rows = []
    for block in re.split(r"\n(?=Job <\d+>,)", "\n" + text):
        header = re.match(r"\s*Job <(\d+)>,(.*)", block)
        if not header:
            continue
        name = re.search(r"Job Name <([^>]*)>", header.group(2))
        if "Done successfully" in block:
            state = "DONE"
        elif re.search(r"Exited|Completed <exit>", block):
            state = "EXIT"
        elif re.search(r"Dispatched|Starting \(Pid|Running with execution home", block):
            state = "RUN"
        elif "Submitted from host" in block:
            state = "PEND"
        else:
            state = "UNKWN"
        row = {
            "job_id": header.group(1),
            "scheduler_state": state,
            "name": name.group(1) if name else "",
        }
        termination = _TERMINATION.search(block)
        if termination:
            row["termination"] = termination.group(0)
        rows.append(row)
    return rows


class LSFExecutor:
    def __init__(self, site: SiteConfig, runner=subprocess.run):
        self.site = site
        self.runner = runner

    def _command(self, arguments, *, timeout=60):
        return self.runner(arguments, capture_output=True, text=True, check=False, timeout=timeout)

    def submit(self, command, attempt_dir, profile, *, name, token):
        attempt_dir = Path(attempt_dir).resolve()
        attempt_dir.mkdir(parents=True, exist_ok=True)
        job = {
            "schema_version": 1,
            "executor": "lsf",
            "name": name,
            "token": token,
            "status": "SUBMITTING",
            "submitted_at": time.time(),
            "attempt_dir": str(attempt_dir),
            "runtime_profile": profile,
            "site": asdict(self.site),
        }
        atomic_write_json(attempt_dir / "job.json", job)
        units = {"KB": 1024, "MB": 1024**2, "GB": 1024**3}
        limit_unit = units[self.site.memory_limit_unit or self.site.memory_unit]
        reservation_unit = units[self.site.memory_reservation_unit or self.site.memory_unit]
        # The in-job watchdog enforces the frozen budget; the scheduler limit is
        # a backstop that must sit above it, or its cgroup (which also counts the
        # shard supervisor and page cache) kills the worker first.
        job_bytes = profile["memory_bytes"] + int(self.site.memory_headroom_gb * 1024**3)
        memory = math.ceil(job_bytes / limit_unit)
        reservation = math.ceil(
            job_bytes
            / reservation_unit
            / (profile["native_threads"] if self.site.memory_per_slot else 1)
        )
        requirement = f"rusage[mem={reservation}] span[hosts=1]"
        if self.site.host_select:
            requirement = f"select[{self.site.host_select}] " + requirement
        argv = [
            self.site.bsub,
            "-J",
            name,
            "-q",
            self.site.queue,
            "-n",
            str(profile["native_threads"]),
            "-W",
            str(math.ceil(profile["wall_seconds"] / 60)),
            "-M",
            str(memory),
            "-R",
            requirement,
            "-oo",
            str(attempt_dir / "stdout.log"),
            "-eo",
            str(attempt_dir / "stderr.log"),
            "-cwd",
            str(attempt_dir),
        ]
        for flag, value in (("-P", self.site.project), ("-U", self.site.reservation)):
            if value:
                argv.extend([flag, value])
        job["argv"] = argv + list(command)
        atomic_write_json(attempt_dir / "job.json", job)
        try:
            result = self._command(job["argv"])
            job["submit_stdout"], job["submit_stderr"] = result.stdout, result.stderr
            match = re.search(r"Job\s+<(\d+)>", result.stdout)
            # Even an error response can follow a successfully accepted submission.
            if result.returncode == 0 and match:
                job.update(status="QUEUED", job_id=match.group(1))
            else:
                job.update(status="SUBMISSION_UNKNOWN")
        except (OSError, subprocess.TimeoutExpired) as exc:
            job.update(status="SUBMISSION_UNKNOWN", reason=str(exc))
        atomic_write_json(attempt_dir / "job.json", job)
        return job

    def query(self, *, name=None, job_id=None, history=False):
        if history:
            # bhist -l is the portable history form; the unique job name is the
            # reliable key because the scheduler recycles numeric identifiers.
            argv = [self.site.bhist, "-l", "-a"]
            if name:
                argv.extend(["-J", name])
            elif job_id:
                argv.append(str(job_id))
            timeout = 300
        else:
            # exit_reason carries the TERM_* code; only JSON output can hold
            # its free text without breaking the fixed three-column parser.
            argv = [self.site.bjobs, "-a", "-o", "jobid stat job_name"]
            if self.site.machine_readable:
                argv[-1] += " exit_reason"
                argv.append("-json")
            else:
                argv.append("-noheader")
            if name:
                argv.extend(["-J", name])
            elif job_id:
                argv.append(str(job_id))
            timeout = 120
        try:
            result = self._command(argv, timeout=timeout)
            if result.returncode:
                if _NOT_FOUND.search(result.stdout + result.stderr):
                    return []
                raise ReconciliationError(f"Scheduler query failed: {result.stderr.strip()}")
            return parse_history(result.stdout) if history else parse_jobs(result.stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise ReconciliationError(f"Scheduler query unavailable: {exc}") from exc

    @staticmethod
    def _matches(job, rows):
        named = [row for row in rows if row["name"] == job["name"]]
        if len(named) > 1:
            raise ReconciliationError(f"Conflicting duplicate scheduler jobs: {job['name']}")
        return [
            row
            for row in named
            if not job.get("job_id") or row["job_id"] == job["job_id"]
        ]

    def _apply(self, job, row):
        job = dict(job)
        state = row["scheduler_state"]
        job.update(row)
        job.pop("reason", None)
        if state in ACTIVE:
            job["status"] = "RUNNING" if state == "RUN" else "QUEUED"
            job.pop("uncertain_since", None)
        elif state in {"DONE", "EXIT"}:
            job["status"] = state
            job.pop("uncertain_since", None)
            if row.get("termination") in LIMIT_TERMINATIONS:
                job["reason"] = LIMIT_TERMINATIONS[row["termination"]]
        else:
            job["status"] = "SUBMISSION_UNKNOWN"
        return job

    def _uncertain(self, job):
        job.setdefault("uncertain_since", time.time())
        if time.time() - job["uncertain_since"] >= self.site.visibility_grace_seconds:
            job["status"] = "NEEDS_RECONCILIATION"
        return job

    def poll(self, job):
        job = dict(job)
        if job["status"] in {"DONE", "EXIT"}:
            # Durable terminal evidence does not expire with scheduler history.
            return job
        try:
            rows = self.query(name=job["name"], job_id=job.get("job_id"))
            matches = self._matches(job, rows)
            if not matches:
                matches = self._matches(job, self.query(name=job["name"], history=True))
            if len(matches) > 1:
                raise ReconciliationError(f"Conflicting duplicate scheduler jobs: {job['name']}")
            if matches:
                job = self._apply(job, matches[0])
            else:
                job["status"] = "SUBMISSION_UNKNOWN"
        except ReconciliationError as exc:
            job.update(status="SUBMISSION_UNKNOWN", reason=str(exc))
        if job["status"] == "SUBMISSION_UNKNOWN":
            job = self._uncertain(job)
        return job

    def poll_many(self, jobs):
        """Poll owned jobs with one listing per cycle, then individual fallbacks.

        Names share a per-run prefix, so a single wildcard query covers the active
        set; a job absent from that listing is reconciled individually rather than
        assumed finished.
        """
        jobs = {token: dict(job) for token, job in jobs.items()}
        names = [job.get("name", "") for job in jobs.values()]
        prefix = os.path.commonprefix(names) if names else ""
        listing = None
        if len(jobs) > 1 and len(prefix) >= 8 and all(names):
            try:
                listing = self.query(name=prefix + "*")
            except ReconciliationError:
                listing = None
        results = {}
        for token, job in jobs.items():
            if job["status"] in {"DONE", "EXIT"}:
                results[token] = job
                continue
            if listing is not None:
                try:
                    matches = self._matches(job, listing)
                except ReconciliationError as exc:
                    results[token] = self._uncertain(
                        {**job, "status": "SUBMISSION_UNKNOWN", "reason": str(exc)}
                    )
                    continue
                if len(matches) == 1 and matches[0]["scheduler_state"] in ACTIVE | {"DONE", "EXIT"}:
                    results[token] = self._apply(job, matches[0])
                    continue
            results[token] = self.poll(job)
        return results

    def cancel(self, job):
        if job["status"] in {"DONE", "EXIT"}:
            return dict(job)
        # Numeric IDs are recycled. History can establish that our old job
        # finished, but only a current name/ID match authorizes killing it.
        matches = self._matches(job, self.query(name=job["name"]))
        if not matches:
            reconciled = self.poll(job)
            if reconciled["status"] in {"DONE", "EXIT"}:
                return reconciled
            raise ReconciliationError(
                "Cannot cancel an unlocated submission; verify its current name and ID first"
            )
        job = self._apply(job, matches[0])
        if job["status"] in {"DONE", "EXIT"}:
            return job
        try:
            result = self._command([self.site.bkill, str(job["job_id"])])
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReconciliationError(f"Cancellation not confirmed: {exc}") from exc
        if result.returncode:
            # A worker can finish between the listing and bkill. Confirm that
            # terminal state rather than treating an already-finished job as a
            # failed cancellation.
            reconciled = self.poll(job)
            if reconciled["status"] in {"DONE", "EXIT"}:
                return reconciled
            raise ReconciliationError(f"Cancellation not confirmed: {result.stderr.strip()}")
        return {**job, "status": "CANCELLING"}
