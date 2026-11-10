import json
import subprocess
import sys
import time

import pytest

from benchpress_compare.config import SiteConfig
from benchpress_compare.executors.local import LocalExecutor, worker_environment
from benchpress_compare.executors.lsf import LSFExecutor, parse_history, parse_jobs
from benchpress_compare.pilot import runtime_profile


def test_sanitized_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/bad")
    monkeypatch.setenv("RAYON_NUM_THREADS", "100")
    env = worker_environment(runtime_profile(), tmp_path)
    assert "PYTHONPATH" not in env
    assert env["RAYON_NUM_THREADS"] == "1"


def test_local_wall_timeout_terminates_group(tmp_path):
    executor = LocalExecutor()
    job = executor.submit(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        tmp_path,
        runtime_profile(wall_seconds=0.05),
        name="test",
        token="t",
    )
    time.sleep(0.1)
    result = executor.poll(job)
    assert result["status"] == "RESOURCE_LIMIT"
    executor.processes["t"].wait(timeout=5)


def test_lsf_parsers_and_suspension():
    assert parse_jobs("1 SSUSP job")[0]["scheduler_state"] == "SSUSP"
    assert (
        parse_jobs(json.dumps({"RECORDS": [{"JOBID": "8", "STAT": "PEND", "JOB_NAME": "test"}]}))[
            0
        ]["job_id"]
        == "8"
    )
    with pytest.raises(ValueError):
        parse_jobs("Scheduler unavailable")


def test_lost_submission_reply_is_reconciled_without_resubmission(tmp_path):
    commands = []

    def runner(argv, **kwargs):
        commands.append(argv)
        if argv[0] == "bsub":
            raise subprocess.TimeoutExpired(argv, 1)
        return subprocess.CompletedProcess(
            argv, 0, '{"RECORDS":[{"JOBID":"42","STAT":"RUN","JOB_NAME":"unique"}]}', ""
        )

    executor = LSFExecutor(SiteConfig(queue="test"), runner)
    job = executor.submit(
        ["python", "worker.py", "space in arg"],
        tmp_path,
        runtime_profile(),
        name="unique",
        token="token",
    )
    assert job["status"] == "SUBMISSION_UNKNOWN"
    assert executor.poll(job)["job_id"] == "42"
    assert len([cmd for cmd in commands if cmd[0] == "bsub"]) == 1
    assert commands[0][-1] == "space in arg"


def test_scheduler_outage_deadline_preserves_uncertainty():
    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "outage")

    executor = LSFExecutor(SiteConfig(queue="test", visibility_grace_seconds=0.01), runner)
    job = {
        "name": "n",
        "token": "t",
        "status": "QUEUED",
        "job_id": "42",
        "uncertain_since": time.time() - 1,
    }
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

    site = SiteConfig(
        queue="test",
        memory_limit_unit="KB",
        memory_reservation_unit="MB",
        memory_per_slot=True,
        host_select="type==X86_64",
    )
    LSFExecutor(site, runner).submit(
        ["worker"], tmp_path, runtime_profile(native_threads=2, memory_gb=1), name="n", token="t"
    )
    argv = commands[0]
    assert argv[argv.index("-M") + 1] == "1048576"
    assert argv[argv.index("-R") + 1] == "select[type==X86_64] rusage[mem=512] span[hosts=1]"


BHIST = """
Job <1421546>, Job Name <bpc-probe-1>, User <ophirazu>, Project <default>, Job 
                          Priority <2>, Command <./probe.sh>, Esub <nongpu_chec
                          k>
Mon Nov  9 15:57:58 2026: Submitted from host <ccc-login5>, to Queue <normal>, 
                          CWD </tmp/probe>, Output File (overwrite) <probe.out>;
Mon Nov  9 15:57:58 2026: Dispatched 1 Task(s) on Host(s) <cccxc529>, Allocated
                           1 Slot(s) on Host(s) <cccxc529>;
Mon Nov  9 15:57:58 2026: Starting (Pid 4135966);
Mon Nov  9 15:57:59 2026: Exited with exit code 127. The CPU time used is 0.1 s
                          econds;
Mon Nov  9 15:57:59 2026: Completed <exit>;

 MEMLIMIT
      2 G 

Job <1421546>, Job Name <other-run-with-recycled-id>, User <someone>,
Fri May  1 02:51:15 2026: Submitted from host <ccc-login3>, to Queue <normal>;
Fri May  1 02:51:16 2026: Dispatched 1 Task(s) on Host(s) <cccxc589>;
Fri May  1 02:55:50 2026: Done successfully. The CPU time used is 255.2 seconds
                          ;

Job <77>, Job Name <bpc-live>, User <ophirazu>,
Mon Nov  9 15:57:58 2026: Submitted from host <ccc-login5>, to Queue <normal>;
Mon Nov  9 15:57:58 2026: Starting (Pid 1);
"""


def test_bhist_text_history_is_parsed_and_matched_by_unique_name():
    rows = parse_history(BHIST)
    assert [(row["job_id"], row["scheduler_state"], row["name"]) for row in rows] == [
        ("1421546", "EXIT", "bpc-probe-1"),
        ("1421546", "DONE", "other-run-with-recycled-id"),
        ("77", "RUN", "bpc-live"),
    ]
    assert parse_history("No matching job found") == []
    assert parse_jobs(
        '{"RECORDS":[{"JOBID":"5","ERROR":"Job <5> is not found"}]}'
    ) == []
    with pytest.raises(ValueError):
        parse_jobs('{"RECORDS":[{"JOBID":"5","ERROR":"mbatchd unreachable"}]}')


def test_history_fallback_uses_bhist_text_by_name():
    commands = []

    def runner(argv, **kwargs):
        commands.append(argv)
        if argv[0] == "bjobs":
            return subprocess.CompletedProcess(argv, 0, '{"RECORDS":[]}', "")
        assert argv[0] == "bhist" and "-l" in argv
        if argv[argv.index("-J") + 1] != "bpc-probe-1":
            return subprocess.CompletedProcess(argv, 255, "No matching job found", "")
        return subprocess.CompletedProcess(argv, 0, BHIST, "")

    executor = LSFExecutor(SiteConfig(queue="test"), runner)
    job = executor.poll({"name": "bpc-probe-1", "token": "t", "status": "QUEUED", "job_id": "1421546"})
    assert job["status"] == "EXIT"
    missing = executor.poll({"name": "bpc-missing", "token": "u", "status": "QUEUED"})
    assert missing["status"] == "SUBMISSION_UNKNOWN"


def test_poll_many_uses_one_listing_and_reconciles_absent_jobs_individually():
    commands = []

    def runner(argv, **kwargs):
        commands.append(argv)
        if argv[0] == "bjobs" and argv[-1] == "bpc-run-*":
            return subprocess.CompletedProcess(
                argv,
                0,
                '{"RECORDS":[{"JOBID":"1","STAT":"RUN","JOB_NAME":"bpc-run-a"},'
                '{"JOBID":"2","STAT":"DONE","JOB_NAME":"bpc-run-b"},'
                '{"JOBID":"9","STAT":"RUN","JOB_NAME":"bpc-run-a"}]}',
                "",
            )
        if argv[0] == "bjobs":
            return subprocess.CompletedProcess(argv, 0, '{"RECORDS":[]}', "")
        return subprocess.CompletedProcess(argv, 255, "No matching job found", "")

    executor = LSFExecutor(SiteConfig(queue="test"), runner)
    jobs = {
        "a": {"name": "bpc-run-a", "token": "a", "status": "QUEUED", "job_id": "1"},
        "b": {"name": "bpc-run-b", "token": "b", "status": "QUEUED", "job_id": "2"},
        "c": {"name": "bpc-run-c", "token": "c", "status": "QUEUED", "job_id": "3"},
    }
    polled = executor.poll_many(jobs)
    assert polled["a"]["status"] == "RUNNING"
    assert polled["b"]["status"] == "DONE"
    assert polled["c"]["status"] == "SUBMISSION_UNKNOWN"
    listings = [argv for argv in commands if argv[0] == "bjobs" and argv[-1] == "bpc-run-*"]
    assert len(listings) == 1
