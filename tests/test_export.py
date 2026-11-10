import json
import tarfile

import pytest

from benchpress_compare.errors import CompareError, IntegrityError
from benchpress_compare.export import export_run
from benchpress_compare.identity import atomic_write_json, file_hash
from benchpress_compare.store import Lease, LeaseBusy, Store, publish_bundle


@pytest.fixture
def comparison(tmp_path):
    root = tmp_path / "run"
    store = Store(tmp_path / "store")
    atomic_write_json(
        root / "request.json", {"schema_version": 1, "config": {"store": str(tmp_path / "store")}}
    )
    return root, store


def test_export_materializes_verified_cache_in_all_phases(comparison, tmp_path):
    root, store = comparison
    for phase, key in [("comparison", "abc"), ("preflight", "def")]:
        record = {
            "schema_version": 1,
            "arm": "baseline",
            "status": "SUCCEEDED",
            "phase": phase,
            "observation_id": key,
            "validation_status": "verified",
        }
        store.publish("baseline/observations", key, record, {"compiled.bin": b"compiled"})
        cached = store.path("baseline/observations", key)
        receipt = {
            **record,
            "cache_key": key,
            "cache_manifest_hash": file_hash(cached / "manifest.json"),
        }
        parent = root / "observations" if phase == "comparison" else root / phase / "observations"
        publish_bundle(parent / key, receipt, {"compiled.bin": b"compiled"})
    output = tmp_path / "export.tar"
    result = export_run(root, output)
    assert result["store_references"] == 2
    with tarfile.open(output) as archive:
        names = set(archive.getnames())
        assert "results/request.json" in names
        assert "shared-store/baseline/observations/abc/compiled.bin" in names
        assert "shared-store/baseline/observations/def/compiled.bin" in names
        metadata = json.load(archive.extractfile("export.json"))
        assert len(metadata["references"]) == 2
        assert "results/state/controller.lease" not in names


def test_export_rejects_corrupt_committed_result(comparison, tmp_path):
    root, _ = comparison
    bundle = root / "observations/abc"
    publish_bundle(bundle, {"schema_version": 1}, {"output": b"valid"})
    (bundle / "output").write_bytes(b"corrupted")
    with pytest.raises(IntegrityError, match="checksum"):
        export_run(root, tmp_path / "corrupt.tar")
    assert not (tmp_path / "corrupt.tar").exists()


def test_export_refuses_incomplete_cache_reference(comparison, tmp_path):
    root, _ = comparison
    publish_bundle(
        root / "observations/abc",
        {"schema_version": 1, "cache_key": "abc", "cache_manifest_hash": "missing"},
    )
    with pytest.raises(IntegrityError, match="incomplete"):
        export_run(root, tmp_path / "missing.tar")


def test_export_cannot_race_active_controller_or_overwrite_archive(comparison, tmp_path):
    root, _ = comparison
    with Lease(root / "state/controller.lease"):
        with pytest.raises(LeaseBusy):
            export_run(root, tmp_path / "busy.tar")
    output = tmp_path / "exists.tar"
    output.write_bytes(b"previous archive")
    with pytest.raises(CompareError, match="already exists"):
        export_run(root, output)
    assert output.read_bytes() == b"previous archive"


def test_export_rejects_external_symlinks_and_preserves_internal_links(comparison, tmp_path):
    root, _ = comparison
    (root / "inside").write_text("inside")
    (root / "link").symlink_to("inside")
    result = export_run(root, tmp_path / "safe.tar")
    with tarfile.open(result["path"]) as archive:
        assert archive.getmember("results/link").linkname == "inside"
    (tmp_path / "external").write_text("outside")
    (root / "external-link").symlink_to(tmp_path / "external")
    with pytest.raises(IntegrityError, match="external file"):
        export_run(root, tmp_path / "unsafe.tar")
    assert not (tmp_path / "unsafe.tar").exists()


def test_export_rejects_destination_inside_run(comparison):
    root, _ = comparison
    with pytest.raises(CompareError, match="outside"):
        export_run(root, root / "export.tar")


def test_export_uses_paired_build_references_and_excludes_both_environments(
    comparison, tmp_path, monkeypatch
):
    root, store = comparison
    baseline = store.path("baseline/wheels", "abc")
    evolve = root / "builds/evolve/def"
    interpreter = tmp_path / "python"
    interpreter.write_bytes(b"external interpreter")
    builds = {}
    for arm, directory in (("baseline", baseline), ("evolve", evolve)):
        (directory / "env/bin").mkdir(parents=True)
        (directory / "env/bin/python").symlink_to(interpreter)
        (directory / "release.whl").write_bytes(arm.encode())
        builds[arm] = {"schema_version": 1, "manifest_path": str(directory / "manifest.json")}
        atomic_write_json(directory / "manifest.json", builds[arm])
    atomic_write_json(root / "builds/manifests.json", {"schema_version": 1, "builds": builds})
    checked = []

    def verify(directory):
        checked.append(directory)
        return json.loads((directory / "manifest.json").read_text())

    monkeypatch.setattr("benchpress_compare.export.verify_build", verify)
    result = export_run(root, tmp_path / "builds.tar")
    assert checked == [baseline, evolve]
    with tarfile.open(result["path"]) as archive:
        names = set(archive.getnames())
        assert "shared-store/baseline/wheels/abc/release.whl" in names
        assert "results/builds/evolve/def/release.whl" in names
        assert not any("/env" in name for name in names)
        metadata = json.load(archive.extractfile("export.json"))
        assert metadata["excluded_environments"] == sorted(
            [str(baseline / "env"), str(evolve / "env")]
        )
