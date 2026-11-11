import json
import sys
import time

import psutil

from benchpress_compare.executors.local import LocalExecutor, matching_process
from benchpress_compare.pilot import runtime_profile


def running(pid):
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def wait_until(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("process state did not settle")


def launch_tree(tmp_path):
    script = tmp_path / "worker.py"
    script.write_text("""
import json, pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
pathlib.Path("child.json").write_text(json.dumps({"pid": child.pid}))
while not pathlib.Path("exit-parent").exists():
    time.sleep(0.01)
""")
    executor = LocalExecutor()
    directory = tmp_path / "attempt"
    job = executor.submit(
        [sys.executable, str(script)],
        directory,
        runtime_profile(memory_gb=0.125, wall_seconds=10),
        name="test",
        token="owned-token",
    )
    wait_until(lambda: (directory / "child.json").exists())
    child_pid = json.loads((directory / "child.json").read_text())["pid"]
    job = executor.poll(job)
    assert any(record["pid"] == child_pid for record in job["descendants"])
    return executor, job, child_pid, directory


def test_cancel_kills_child_that_created_a_new_session(tmp_path):
    executor, job, child_pid, _ = launch_tree(tmp_path)
    try:
        executor.cancel(job)
        wait_until(lambda: not running(child_pid))
        assert matching_process(job) is None
    finally:
        if running(child_pid):
            psutil.Process(child_pid).kill()
        executor.processes[job["token"]].wait(timeout=5)


def test_dead_leader_does_not_hide_live_detached_descendant(tmp_path):
    executor, job, child_pid, directory = launch_tree(tmp_path)
    try:
        (directory / "exit-parent").touch()
        executor.processes[job["token"]].wait(timeout=5)
        job = executor.poll(job)
        assert job["status"] == "RUNNING"
        assert running(child_pid)
        executor.cancel(job)
        wait_until(lambda: not running(child_pid))
    finally:
        if running(child_pid):
            psutil.Process(child_pid).kill()


def test_restarted_supervisor_can_cancel_persisted_descendants(tmp_path):
    executor, job, child_pid, directory = launch_tree(tmp_path)
    try:
        (directory / "exit-parent").touch()
        executor.processes[job["token"]].wait(timeout=5)
        restored = json.loads(json.dumps(job))
        LocalExecutor().cancel(restored)
        wait_until(lambda: not running(child_pid))
    finally:
        if running(child_pid):
            psutil.Process(child_pid).kill()


def test_poll_tolerates_unreaped_zombie_descendant(tmp_path):
    # The worker spawns a child that exits immediately and is never reaped, so
    # it lingers as a zombie; os.getpgid() raises ESRCH for zombies on macOS.
    script = tmp_path / "worker.py"
    script.write_text("""
import json, pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "pass"])
pathlib.Path("child.json").write_text(json.dumps({"pid": child.pid}))
while not pathlib.Path("exit-parent").exists():
    time.sleep(0.01)
""")
    executor = LocalExecutor()
    directory = tmp_path / "attempt"
    job = executor.submit(
        [sys.executable, str(script)],
        directory,
        runtime_profile(memory_gb=0.125, wall_seconds=10),
        name="test",
        token="zombie-token",
    )
    try:
        wait_until(lambda: (directory / "child.json").exists())
        child_pid = json.loads((directory / "child.json").read_text())["pid"]
        wait_until(lambda: psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE)
        job = executor.poll(job)
        assert job["status"] == "RUNNING"
        assert any(record["pid"] == child_pid for record in job["descendants"])
    finally:
        (directory / "exit-parent").write_text("")
        executor.cancel(job)
