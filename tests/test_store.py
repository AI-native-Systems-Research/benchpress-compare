from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from benchpress_compare.errors import IntegrityError
from benchpress_compare.identity import atomic_write_json
from benchpress_compare.store import (
    Lease,
    LeaseBusy,
    LeaseLost,
    Store,
    publish_bundle,
    verify_bundle,
)


def test_publication_verifies_every_artifact_and_cannot_overwrite(tmp_path):
    path = tmp_path / "bundle"
    record = {"schema_version": 1, "observation_id": "abc"}
    publish_bundle(path, record, {"artifacts/output.json": b"{}"})
    assert verify_bundle(path)["record"] == record
    assert publish_bundle(path, record, {"artifacts/output.json": b"{}"})["record"] == record
    with pytest.raises(IntegrityError, match="Conflicting"):
        publish_bundle(path, record, {"artifacts/output.json": b"changed"})
    (path / "artifacts/output.json").write_text("corrupt")
    with pytest.raises(IntegrityError, match="checksum"):
        verify_bundle(path)


def test_incomplete_bundle_is_never_reusable(tmp_path):
    path = tmp_path / "incomplete"
    path.mkdir()
    (path / "manifest.json").write_text('{"schema_version":1}')
    assert verify_bundle(path) is None
    with pytest.raises(IntegrityError, match="reconciliation"):
        publish_bundle(path, {"schema_version": 1})


def test_simultaneous_publication_is_idempotent(tmp_path):
    path = tmp_path / "bundle"
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(
                publish_bundle, path, {"schema_version": 1, "key": "a"}, {"data": b"payload"}
            )
            for _ in range(4)
        ]
        assert all(future.result()["record"]["key"] == "a" for future in futures)
    assert verify_bundle(path)


def test_lease_exclusion_heartbeat_and_stale_writer_fencing(tmp_path):
    path = tmp_path / "lease"
    first = Lease(path, heartbeat=False).acquire()
    with pytest.raises(LeaseBusy):
        Lease(path, heartbeat=False).acquire()
    before = json.loads(path.read_text())
    first.heartbeat()
    assert json.loads(path.read_text())["expires_at"] >= before["expires_at"]
    before["expires_at"] = 0
    atomic_write_json(path, before)
    second = Lease(path, heartbeat=False).acquire()
    assert second.generation == first.generation + 1
    with pytest.raises(LeaseLost):
        publish_bundle(tmp_path / "bundle", {"schema_version": 1}, lease=first)
    assert not (tmp_path / "bundle").exists()
    first.release()
    second.assert_owned()
    second.release()
    assert not path.exists()


def test_store_only_caches_verified_baseline_and_owns_artifacts(tmp_path):
    store = Store(tmp_path / "shared")
    origin = tmp_path / "temporary-output"
    origin.write_text("result")
    record = {"arm": "baseline", "status": "succeeded", "validation_status": "verified"}
    store.publish("baseline/observations", "abcd", record, {"result.json": origin})
    origin.unlink()
    assert store.get("baseline/observations", "abcd")["record"]["arm"] == "baseline"
    assert (store.path("baseline/observations", "abcd") / "result.json").read_text() == "result"
    with pytest.raises(ValueError, match="baseline"):
        store.publish("baseline/observations", "dead", {**record, "arm": "evolve"})
    with pytest.raises(ValueError, match="verified"):
        store.publish(
            "baseline/observations", "dead", {**record, "validation_status": "unsupported"}
        )
    with pytest.raises(ValueError):
        store.path("evolve/observations", "abcd")
    with pytest.raises(ValueError):
        store.path("sources", "../outside")


@pytest.mark.parametrize("name", ["../outside", "/outside", "COMPLETE", "manifest.json"])
def test_artifact_paths_cannot_escape_or_replace_markers(tmp_path, name):
    with pytest.raises(ValueError):
        publish_bundle(tmp_path / "bundle", {"schema_version": 1}, {name: b"x"})


def test_corrupted_marker_is_not_trusted(tmp_path):
    path = tmp_path / "bundle"
    publish_bundle(path, {"schema_version": 1})
    (path / "manifest.json").write_text('{"schema_version":1,"record":{},"artifacts":{}}')
    with pytest.raises(IntegrityError, match="manifest checksum"):
        verify_bundle(path)
