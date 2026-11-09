from pathlib import Path
import time

import pytest

from benchpress_compare.config import RunConfig
from benchpress_compare.controller import follow_controller, submit_controller
from benchpress_compare.errors import CompareError, ReconciliationError
from benchpress_compare.identity import atomic_write_json, read_json


class Scheduler:
    def __init__(self):
        self.submitted = []
        self.polls = 0
        self.poll_status = "RUNNING"
        self.submit_status = "QUEUED"

    def poll(self, job):
        self.polls += 1
        return {**job, "status": self.poll_status}

    def submit(self, command, directory, profile, *, name, token):
        self.submitted.append(command)
        result = {"schema_version": 1, "executor": "lsf", "name": name, "token": token,
                  "job_id": str(len(self.submitted)), "status": self.submit_status,
                  "attempt_dir": str(directory), "runtime_profile": profile,
                  "submitted_at": time.time()}
        atomic_write_json(Path(directory) / "job.json", result)
        return result


@pytest.fixture
def launch(tmp_path, monkeypatch):
    root = tmp_path / "run"
    root.mkdir()
    site = tmp_path / "site.toml"
    site.write_text('[lsf]\nqueue="test"\ncontroller_python="/cluster/coordinator/bin/python"\n')
    config = RunConfig("/source/baseline", "/source/evolve", str(tmp_path / "store"), str(root), executor="lsf", lsf_config=str(site))
    atomic_write_json(root / "request.json", {"schema_version": 1, "config": config.to_dict()})
    scheduler = Scheduler()
    monkeypatch.setattr("benchpress_compare.controller.LSFExecutor", lambda *_: scheduler)
    monkeypatch.setattr("benchpress_compare.preparation._snapshot_paths", lambda *_: {})
    monkeypatch.setattr("benchpress_compare.preparation._harness_wheel", lambda *_: tmp_path / "harness.whl")
    return root, config, scheduler


def test_repeated_launch_attaches_to_same_controller(launch):
    root, config, scheduler = launch
    first = submit_controller(config)
    second = submit_controller(config)
    assert first["job_id"] == second["job_id"]
    assert len(scheduler.submitted) == 1
    assert scheduler.submitted[0][0] == "/cluster/coordinator/bin/python"
    assert read_json(root / "jobs/controller.json")["status"] == "RUNNING"


def test_lost_submission_response_never_launches_duplicate(launch):
    root, config, scheduler = launch
    scheduler.submit_status = "SUBMISSION_UNKNOWN"
    with pytest.raises(ReconciliationError):
        submit_controller(config)
    scheduler.poll_status = "SUBMISSION_UNKNOWN"
    with pytest.raises(ReconciliationError):
        submit_controller(config)
    assert len(scheduler.submitted) == 1
    scheduler.poll_status = "RUNNING"
    assert submit_controller(config)["status"] == "RUNNING"
    assert len(scheduler.submitted) == 1


def test_known_terminal_controller_need_not_remain_in_scheduler_history(launch):
    root, config, scheduler = launch
    submit_controller(config)
    previous = read_json(root / "jobs/controller.json")
    previous["status"] = "DONE"
    atomic_write_json(root / "jobs/controller.json", previous)
    scheduler.poll_status = "NEEDS_RECONCILIATION"
    assert submit_controller(config)["status"] == "QUEUED"
    assert scheduler.polls == 0
    assert len(scheduler.submitted) == 2


def test_continuation_forwards_explicit_unresolved_decision(launch):
    root, config, scheduler = launch
    config.accept_unresolved = True
    submit_controller(config)
    assert "--accept-unresolved" in scheduler.submitted[0]
    assert read_json(root / "request.json")["config"]["accept_unresolved"] is False
    assert read_json(root / "jobs/controller.json")["controller_options"]["accept_unresolved"] is True


def test_active_controller_actions_cannot_be_silently_changed(launch):
    _, config, scheduler = launch
    submit_controller(config, prepare_only=True)
    with pytest.raises(CompareError, match="different requested actions"):
        submit_controller(config, prepare_only=False)
    assert len(scheduler.submitted) == 1


def test_complete_run_does_not_submit_another_controller(launch):
    root, config, scheduler = launch
    submit_controller(config)
    atomic_write_json(root / "state/status.json", {"schema_version": 1, "status": "COMPLETE", "updated_at": time.time()})
    submit_controller(config)
    assert len(scheduler.submitted) == 1


def test_follow_ignores_terminal_state_from_older_controller(launch, monkeypatch):
    _, config, scheduler = launch
    states = iter([
        {"status": "PREPARATION_FAILED", "updated_at": 1},
        {"status": "COMPLETE", "updated_at": 3},
    ])
    monkeypatch.setattr("benchpress_compare.controller.status", lambda *_: next(states))
    monkeypatch.setattr("benchpress_compare.controller.time.sleep", lambda _: None)
    scheduler.site = type("Site", (), {"poll_seconds": .01})()
    assert follow_controller(config, {"submitted_at": 2}) == "COMPLETE"
    assert scheduler.polls == 1
