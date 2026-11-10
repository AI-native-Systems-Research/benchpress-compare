import pytest

pytest.importorskip("qiskit")
from qiskit import QuantumCircuit

from benchpress_compare.corpus.ir import export_circuit, import_circuit
from benchpress_compare.validation import validate_semantics
from benchpress_compare.validation.commutation import normalize_commuting_rewrites
from benchpress_compare.validation.compositional import local_equivalence


def test_rotations_fuse_across_a_wide_commuting_interaction_graph():
    from qiskit.quantum_info import Operator

    before = QuantumCircuit(6)
    before.rz(0.13, 0)
    for other in range(1, 6):
        before.cz(0, other)
    before.rz(0.37, 0)
    after = QuantumCircuit(6)
    after.rz(0.5, 0)
    for other in range(1, 6):
        after.cz(0, other)
    encoded = export_circuit(before)
    normalized, proof = normalize_commuting_rewrites(encoded)
    expected, _ = normalize_commuting_rewrites(export_circuit(after))
    assert proof["status"] == "verified" and proof["fusions"] >= 1
    assert proof["commutations"] >= 5 and proof["error_bound"] > 0
    assert encoded == export_circuit(before)
    assert Operator(before).equiv(Operator(import_circuit(normalized)))
    assert local_equivalence(normalized, expected)["status"] == "verified"


def test_shared_control_commuting_cnot_pairs_can_cancel():
    before = QuantumCircuit(3)
    before.cx(0, 1)
    before.cx(0, 2)
    before.cx(0, 1)
    normalized, proof = normalize_commuting_rewrites(export_circuit(before))
    after = QuantumCircuit(3)
    after.cx(0, 2)
    assert proof["identities"] >= 1
    assert local_equivalence(normalized, export_circuit(after))["status"] == "verified"
    assert validate_semantics(export_circuit(before), normalized, [0, 1, 2], [0, 1, 2])["status"] == "verified"


def test_noncommuting_blocker_prevents_an_incorrect_rotation_fusion():
    before = QuantumCircuit(2)
    before.rz(0.7, 1)
    before.cx(0, 1)
    before.rz(-0.7, 1)
    normalized, proof = normalize_commuting_rewrites(export_circuit(before))
    corrupt = QuantumCircuit(2)
    corrupt.cx(0, 1)
    assert proof["fusions"] == 0
    assert validate_semantics(export_circuit(before), normalized, [0, 1], [0, 1])["status"] == "verified"
    assert validate_semantics(export_circuit(corrupt), normalized, [0, 1], [0, 1])["status"] == "mismatch"


def test_custom_gate_name_cannot_fake_a_commutation_relation():
    import numpy as np
    from qiskit.circuit.library import UnitaryGate

    gate = UnitaryGate(np.array([[1, 1], [1, -1]]) / np.sqrt(2))
    gate.name = "rz"
    before = QuantumCircuit(2)
    before.append(gate, [1])
    before.cz(0, 1)
    before.append(gate, [1])
    normalized, proof = normalize_commuting_rewrites(export_circuit(before))
    assert proof["fusions"] == 0
    assert validate_semantics(export_circuit(before), normalized, [0, 1], [0, 1])["status"] == "verified"


def test_dynamic_events_remain_barriers_and_branches_normalize_independently():
    before = QuantumCircuit(2, 1)
    before.h(0)
    before.measure(0, 0)
    with before.if_test((before.clbits[0], 1)):
        before.rz(0.2, 1)
        before.rz(-0.2, 1)
        before.x(1)
    before.reset(0)
    before.x(0)
    normalized, proof = normalize_commuting_rewrites(export_circuit(before))
    assert proof["identities"] == 1
    assert validate_semantics(export_circuit(before), normalized, [0, 1], [0, 1])["status"] == "verified"
