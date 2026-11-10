import pytest

pytest.importorskip("qiskit")
from qiskit import QuantumCircuit

from benchpress_compare.corpus.ir import export_circuit
from benchpress_compare.validation import validate_semantics
from benchpress_compare.validation.terminal import normalize_measured_diagonals


def test_diagonal_before_all_measurements_preserves_instrument_with_wire_reuse():
    circuit = QuantumCircuit(2, 2)
    circuit.h(0)
    circuit.h(1)
    circuit.rzz(0.71, 0, 1)
    circuit.barrier()
    circuit.measure(0, 0)
    circuit.measure(1, 1)
    circuit.reset(0)
    circuit.x(1)
    before = export_circuit(circuit)
    after, proof = normalize_measured_diagonals(before)
    assert proof["removed"] == 1
    assert proof["certificates"][0]["operation"] == "rzz"
    assert before == export_circuit(circuit)
    assert validate_semantics(before, after, [0, 1], [0, 1])["status"] == "verified"


def test_diagonal_on_partially_measured_support_cannot_be_removed():
    circuit = QuantumCircuit(2, 1)
    circuit.cz(0, 1)
    circuit.measure(0, 0)
    before = export_circuit(circuit)
    after, proof = normalize_measured_diagonals(before)
    assert proof["removed"] == 0 and after == before


def test_intervening_unitary_or_condition_blocks_diagonal_removal():
    circuit = QuantumCircuit(1, 1)
    circuit.z(0)
    circuit.h(0)
    circuit.measure(0, 0)
    before = export_circuit(circuit)
    after, proof = normalize_measured_diagonals(before)
    assert proof["removed"] == 0 and after == before
    circuit = QuantumCircuit(1, 1)
    circuit.z(0)
    with circuit.if_test((circuit.clbits[0], 1)):
        circuit.measure(0, 0)
    before = export_circuit(circuit)
    after, proof = normalize_measured_diagonals(before)
    assert proof["removed"] == 0 and after == before


def test_non_diagonal_gate_is_not_removed():
    circuit = QuantumCircuit(1, 1)
    circuit.x(0)
    circuit.measure(0, 0)
    before = export_circuit(circuit)
    after, proof = normalize_measured_diagonals(before)
    assert proof["removed"] == 0 and after == before


def test_branch_local_diagonal_removal_preserves_changed_branch_detection():
    circuit = QuantumCircuit(2, 2)
    circuit.measure(0, 0)
    with circuit.if_test((circuit.clbits[0], 1)):
        circuit.z(1)
        circuit.measure(1, 1)
    before = export_circuit(circuit)
    after, proof = normalize_measured_diagonals(before)
    assert proof["removed"] == 1
    assert validate_semantics(before, after, [0, 1], [0, 1])["status"] == "verified"


@pytest.mark.parametrize("name", ["barrier", "delay", "measure"])
def test_custom_unitary_names_cannot_fake_measurement_or_identity(name):
    import numpy as np
    from qiskit.circuit.library import UnitaryGate

    gate = UnitaryGate(np.array([[1, 1], [1, -1]]) / np.sqrt(2))
    gate.name = name
    circuit = QuantumCircuit(1, 1)
    circuit.z(0)
    circuit.append(gate, [0])
    circuit.measure(0, 0)
    before = export_circuit(circuit)
    assert before["operations"][1]["kind"] == "unitary"
    after, proof = normalize_measured_diagonals(before)
    assert proof["removed"] == 0
    assert after == before
