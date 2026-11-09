"""LSF argument-vector transport and conservative uncertain-job reconciliation."""

from __future__ import annotations

from dataclasses import asdict
import json
import math
from pathlib import Path
import re
import subprocess
import time

from ..config import SiteConfig
from ..errors import ReconciliationError
from ..identity import atomic_write_json

ACTIVE = {"PEND", "RUN", "PSUSP", "USUSP", "SSUSP", "WAIT", "PROV"}
TERMINAL = {"DONE", "EXIT", "ZOMBI"}


def parse_jobs(output: str) -> list[dict]:
    """Parse requested jobid/stat/job_name output, never infer absence on errors."""
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
            job_id, state = row.get("jobid", row.get("job_id")), row.get("stat", row.get("status"))
            if job_id is None or state not in ACTIVE | TERMINAL | {"UNKWN"}:
                raise ValueError("Missing or unknown scheduler identity/state")
            normalized.append(
                {
                    "job_id": str(job_id),
                    "scheduler_state": state,
                    "name": row.get("job_name", row.get("jobname", "")),
                }
            )
        return normalized
    rows = []
    for line in text.splitlines():
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


class LSFExecutor:
    def __init__(self, site: SiteConfig, runner=subprocess.run):
        self.site = site
        self.runner = runner

    def _command(self, arguments):
        return self.runner(arguments, capture_output=True, text=True, check=False, timeout=60)

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
        memory = math.ceil(profile["memory_bytes"] / limit_unit)
        reservation = math.ceil(
            profile["memory_bytes"]
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
            argv = [self.site.bhist, "-n", "0", "-a", "-o", "jobid stat job_name"]
        else:
            argv = [self.site.bjobs, "-a", "-o", "jobid stat job_name"]
        if self.site.machine_readable:
            argv.append("-json")
        else:
            argv.append("-noheader")
        if job_id:
            argv.append(str(job_id))
        elif name:
            argv.extend(["-J", name])
        try:
            result = self._command(argv)
            if result.returncode:
                raise ReconciliationError(f"Scheduler query failed: {result.stderr.strip()}")
            return parse_jobs(result.stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise ReconciliationError(f"Scheduler query unavailable: {exc}") from exc

    def poll(self, job):
        job = dict(job)
        try:
            rows = self.query(name=job["name"], job_id=job.get("job_id"))
            if not rows:
                rows = self.query(name=job["name"], job_id=job.get("job_id"), history=True)
            matches = [
                row
                for row in rows
                if row["name"] == job["name"]
                and (not job.get("job_id") or row["job_id"] == job["job_id"])
            ]
            if len(matches) > 1:
                raise ReconciliationError(f"Conflicting duplicate scheduler jobs: {job['name']}")
            if matches:
                state = matches[0]["scheduler_state"]
                job.update(matches[0])
                if state in ACTIVE:
                    job["status"] = "RUNNING" if state == "RUN" else "QUEUED"
                    job.pop("uncertain_since", None)
                elif state in {"DONE", "EXIT"}:
                    job["status"] = state
                    job.pop("uncertain_since", None)
                else:
                    job["status"] = "SUBMISSION_UNKNOWN"
            else:
                job["status"] = "SUBMISSION_UNKNOWN"
        except ReconciliationError as exc:
            job.update(status="SUBMISSION_UNKNOWN", reason=str(exc))
        if job["status"] == "SUBMISSION_UNKNOWN":
            job.setdefault("uncertain_since", time.time())
            if time.time() - job["uncertain_since"] >= self.site.visibility_grace_seconds:
                job["status"] = "NEEDS_RECONCILIATION"
        return job

    def cancel(self, job):
        if not job.get("job_id"):
            job = self.poll(job)
        if not job.get("job_id"):
            raise ReconciliationError(
                "Cannot cancel an unlocated submission; reconcile its token first"
            )
        result = self._command([self.site.bkill, str(job["job_id"])])
        if result.returncode:
            raise ReconciliationError(f"Cancellation not confirmed: {result.stderr.strip()}")
        return {**job, "status": "CANCELLING"}
