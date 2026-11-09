import pytest

from benchpress_compare.targets import (
    heavy_hex_distance,
    square_edges,
    synthetic_target,
    import_target,
    freeze_fake_torino,
)


@pytest.mark.parametrize(
    "width,distance",
    [(1, 1), (2, 3), (19, 3), (20, 5), (57, 5), (58, 7), (115, 7), (116, 9), (193, 9), (194, 11)],
)
def test_complete_heavy_hex_capacity_boundaries(width, distance):
    assert heavy_hex_distance(width) == distance


def test_square_is_complete_bidirectional_grid():
    capacity, edges = square_edges(10)
    assert capacity == 16
    assert len(edges) == 48
    assert all([b, a] in edges for a, b in edges)


@pytest.mark.parametrize("topology", ["square", "heavy-hex"])
def test_synthetic_target_roundtrip_and_nonunitary_support(topology):
    pytest.importorskip("qiskit")
    snapshot = synthetic_target(topology, 20)
    imported = import_target(snapshot)
    assert imported.num_qubits == (25 if topology == "square" else 57)
    assert {"reset", "measure", "if_else", "cz"}.issubset(imported.operation_names)
    assert all(prop is None for prop in imported["cz"].values())


def test_fake_torino_preserves_real_calibration_and_capabilities():
    pytest.importorskip("qiskit_ibm_runtime")
    snapshot = freeze_fake_torino()
    target = import_target(snapshot)
    assert target.num_qubits == 133
    assert target.dt == 4e-9
    assert "cz" in target.operation_names
    assert any(p is not None and p.error is not None for p in target["cz"].values())


def test_tampered_target_is_rejected():
    pytest.importorskip("qiskit")
    target = synthetic_target("square", 4)
    target["num_qubits"] = 5
    with pytest.raises(ValueError, match="hash mismatch"):
        import_target(target)
