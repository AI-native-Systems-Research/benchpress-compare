import os
import sys

from benchpress_compare.identity import atomic_write_json, read_json
from benchpress_compare.tasks import main


def test_interrupted_build_retains_evidence_and_recovers_final_path(tmp_path, monkeypatch):
    from benchpress_compare import builds

    destination = tmp_path / "build"
    destination.mkdir()
    (destination / "partial.log").write_text("interrupted cargo")
    rustup_home = tmp_path / "rustup"
    monkeypatch.setenv("RUSTUP_HOME", str(rustup_home))

    def build(snapshot, target, **kwargs):
        assert target == destination
        assert not target.exists()
        kwargs["lease"].assert_owned()
        assert os.environ["RUSTUP_HOME"] == str(rustup_home)
        target.mkdir()
        return {"schema_version": 1, "manifest_path": str(target / "manifest.json")}

    monkeypatch.setattr(builds, "build_release", build)
    spec = {
        "task": "build",
        "destination": str(destination),
        "snapshot": str(tmp_path / "source"),
        "python": sys.executable,
        "dependency_lock": {},
        "harness_wheel": "harness.whl",
        "rustup_home": str(rustup_home),
        "attempt_id": "second",
        "output_dir": str(tmp_path / "attempt"),
        "runtime_profile": {"wall_seconds": 10},
    }
    path = tmp_path / "spec.json"
    atomic_write_json(path, spec)
    assert main([str(path)]) == 0
    assert (tmp_path / "build.incomplete-second/partial.log").read_text() == "interrupted cargo"
    assert read_json(tmp_path / "attempt/receipt.json")["status"] == "SUCCEEDED"


def test_busy_shared_build_waits_without_mutating_owner(tmp_path, monkeypatch):
    from benchpress_compare.store import Lease

    destination = tmp_path / "build"
    destination.mkdir()
    (destination / "owner.log").write_text("still running")
    spec = {
        "task": "build",
        "destination": str(destination),
        "attempt_id": "second",
        "output_dir": str(tmp_path / "attempt"),
        "runtime_profile": {"wall_seconds": 0.01},
    }
    path = tmp_path / "spec.json"
    atomic_write_json(path, spec)
    with Lease(destination.with_suffix(".lease")):
        assert main([str(path)]) == 3
        assert read_json(tmp_path / "attempt/receipt.json")["status"] == "INFRA_FAILED"
        assert (destination / "owner.log").read_text() == "still running"
