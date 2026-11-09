import json
import subprocess
import sys
import time

import pytest

from benchpress_compare.config import SiteConfig
from benchpress_compare.executors.local import LocalExecutor, worker_environment
from benchpress_compare.executors.lsf import LSFExecutor, parse_jobs
from benchpress_compare.pilot import runtime_profile


def test_sanitized_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/bad")
    monkeypatch.setenv("RAYON_NUM_THREADS", "100")
    env = worker_environment(runtime_profile(), tmp_path)
    assert "PYTHONPATH" not in env
    assert env["RAYON_NUM_THREADS"] == "1"


def test_local_wall_timeout_terminates_group(tmp_path):
    executor = LocalExecutor()
    job = executor.submit([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path,
                          runtime_profile(wall_seconds=.05), name="test", token="t")
    time.sleep(.1)
    result = executor.poll(job)
    assert result["status"] == "RESOURCE_LIMIT"
    executor.processes["t"].wait(timeout=5)


def test_lsf_parsers_and_suspension():
    assert parse_jobs('1 SSUSP job')[0]["scheduler_state"] == "SSUSP"
    assert parse_jobs(json.dumps({"RECORDS": [{"JOBID": "8", "STAT": "PEND",
                                              "JOB_NAME": "test"}]}))[0]["job_id"] == "8"
    with pytest.raises(ValueError):
        parse_jobs("Scheduler unavailable")


def test_lost_submission_reply_is_reconciled_without_resubmission(tmp_path):
    commands = []

    def runner(argv, **kwargs):
        commands.append(argv)
        if argv[0] == "bsub":
            raise subprocess.TimeoutExpired(argv, 1)
        return subprocess.CompletedProcess(argv, 0, '{"RECORDS":[{"JOBID":"42","STAT":"RUN","JOB_NAME":"unique"}]}', "")

    executor = LSFExecutor(SiteConfig(queue="test"), runner)
    job = executor.submit(["python", "worker.py", "space in arg"], tmp_path, runtime_profile(),
                          name="unique", token="token")
    assert job["status"] == "SUBMISSION_UNKNOWN"
    assert executor.poll(job)["job_id"] == "42"
    assert len([cmd for cmd in commands if cmd[0] == "bsub"]) == 1
    assert commands[0][-1] == "space in arg"


def test_scheduler_outage_deadline_preserves_uncertainty():
    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "outage")

    executor = LSFExecutor(SiteConfig(queue="test", visibility_grace_seconds=.01), runner)
    job = {"name": "n", "token": "t", "status": "QUEUED", "job_id": "42",
           "uncertain_since": time.time() - 1}
    assert executor.poll(job)["status"] == "NEEDS_RECONCILIATION"


def test_duplicate_job_names_are_conflicts():
    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, "1 RUN n\n2 PEND n", "")
    executor = LSFExecutor(SiteConfig(queue="test", machine_readable=False), runner)
    result = executor.poll({"name": "n", "token": "t", "status": "SUBMISSION_UNKNOWN"})
    assert result["status"] == "SUBMISSION_UNKNOWN"
    assert "Conflicting" in result["reason"]


def test_lsf_separate_memory_limit_and_reservation_units(tmp_path):
    commands = []
    def runner(argv, **kwargs):
        commands.append(argv)
        return subprocess.CompletedProcess(argv, 0, "Job <42> is submitted", "")
    site = SiteConfig(queue="test", memory_limit_unit="KB", memory_reservation_unit="MB",
                      memory_per_slot=True, host_select="type==X86_64")
    LSFExecutor(site, runner).submit(["worker"], tmp_path, runtime_profile(native_threads=2, memory_gb=1),
                                    name="n", token="t")
    argv = commands[0]
    assert argv[argv.index("-M") + 1] == "1048576"
    assert argv[argv.index("-R") + 1] == "select[type==X86_64] rusage[mem=512] span[hosts=1]"
