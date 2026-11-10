import pytest

from benchpress_compare.errors import IntegrityError
from benchpress_compare.identity import atomic_write_json, canonical_hash, file_hash
from benchpress_compare.worker.starts import native_start_templates, reviewed_start_source


@pytest.fixture
def reviewed_source(tmp_path, monkeypatch):
    from benchpress_compare.worker import starts

    relative = starts.SOURCE_FILE
    native = tmp_path / "snapshot/source" / relative
    native.parent.mkdir(parents=True)
    native.write_text("\n".join(
        f"if num_physical_qubits == {width} {{ starting_layouts.push([0, 1, 2]); }}"
        for width in (127, 133, 156)
    ))
    digest = file_hash(native)
    monkeypatch.setattr(starts, "REVIEWED_SOURCE_HASHES", {digest})
    files = [{"path": relative, "sha256": digest, "type": "file"}]
    atomic_write_json(tmp_path / "snapshot/snapshot.json", {
        "schema_version": 1, "files": files, "tree_hash": canonical_hash(files),
    })
    manifest = tmp_path / "build.json"
    atomic_write_json(manifest, {
        "snapshot_path": str(tmp_path / "snapshot"), "snapshot_hash": canonical_hash(files),
    })
    return manifest, native


def test_native_policy_requires_reviewed_and_unchanged_source(reviewed_source, monkeypatch):
    from benchpress_compare.worker import starts

    manifest, native = reviewed_source
    evidence = reviewed_start_source(manifest)
    assert evidence["status"] == "verified"
    assert evidence["ring_templates"]["133"] == [0, 1, 2]
    monkeypatch.setattr(starts, "REVIEWED_SOURCE_HASHES", set())
    assert reviewed_start_source(manifest)["status"] == "unsupported"
    native.write_text(native.read_text() + "\nchanged")
    with pytest.raises(IntegrityError, match="pinned snapshot"):
        reviewed_start_source(manifest)


def test_installed_integration_wheel_cannot_claim_reviewed_native_source():
    source = reviewed_start_source()
    assert source["status"] == "not_requested"
    assert native_start_templates(None, 4, {"num_random_trials": 20}, source)["status"] == "unsupported"


@pytest.mark.integration
@pytest.mark.parametrize("width,total", [(9, 23), (133, 24)])
def test_connected_targets_record_all_implicit_templates(reviewed_source, width, total):
    pytest.importorskip("qiskit")
    from qiskit.circuit.library import CXGate
    from qiskit.transpiler import Target

    target = Target(num_qubits=width)
    target.add_instruction(CXGate(), {(q, q + 1): None for q in range(width - 1)})
    source = reviewed_start_source(reviewed_source[0])
    result = native_start_templates(target, 4, {"num_random_trials": 20}, source)
    assert result["status"] == "verified"
    assert result["total_layout_trials"] == total
    assert [row["name"] for row in result["implicit_templates"][:3]] == ["dense", "ascending", "descending"]
    assert result["implicit_templates"][1]["layout"] == list(range(width))
    assert result["implicit_templates"][2]["layout"] == list(reversed(range(width)))
    if width == 133:
        assert result["implicit_templates"][3] == {"name": "ring_133", "layout": [0, 1, 2]}
    partial = [[0, None, None, 3]]
    with_partial = native_start_templates(
        target, 4, {"num_random_trials": 20, "partial_layouts": partial}, source
    )
    assert with_partial["supplied_partial_templates"] == partial
    assert with_partial["total_layout_trials"] == total + 1


@pytest.mark.integration
def test_full_presets_match_frozen_arbitrary_input_contract():
    pytest.importorskip("qiskit")
    from benchpress_compare.schema import TRANSPILE_CONTRACT
    from benchpress_compare.targets import import_target, synthetic_target
    from benchpress_compare.worker.adapter import _passes, preset

    for level in (2, 3):
        manager, _ = preset(import_target(synthetic_target("square", 4)), level, 123)
        synthesis = [
            item for item in _passes(manager.to_flow_controller())
            if type(item).__name__ == "HighLevelSynthesis"
        ]
        assert synthesis
        assert TRANSPILE_CONTRACT["qubits_initially_zero"] is False
        assert all(item.qubits_initially_zero is False for item in synthesis)
