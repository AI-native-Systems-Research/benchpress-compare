"""Regression checks for typed operations and terminal measurement ordering."""

from copy import deepcopy

import pytest

from benchpress_compare.corpus.ir import export_circuit
from benchpress_compare.validation.instrument import instrument_equivalence
from benchpress_compare.validation.isometry import isometry_equivalence
from benchpress_compare.validation import validate_semantics

pytestmark = pytest.mark.integration
QuantumCircuit = pytest.importorskip("qiskit").QuantumCircuit
UnitaryGate = pytest.importorskip("qiskit.circuit.library").UnitaryGate
Operator = pytest.importorskip("qiskit.quantum_info").Operator


@pytest.mark.parametrize("oracle", [isometry_equivalence, instrument_equivalence])
@pytest.mark.parametrize("name", ["barrier", "delay", "measure", "reset", "if_else"])
def test_bounded_oracle_uses_typed_semantics_not_custom_names(oracle, name):
    logical = QuantumCircuit(1)
    changed = QuantumCircuit(1)
    gate = UnitaryGate(Operator.from_label("X").data)
    gate.name = name
    changed.append(gate, [0])
    assert oracle(export_circuit(logical), export_circuit(changed), [0], [0])["status"] == "mismatch"


@pytest.mark.parametrize("oracle", [isometry_equivalence, instrument_equivalence])
def test_nested_unsupported_conditions_cannot_be_discarded(oracle):
    body = QuantumCircuit(1, 1)
    body.x(0)
    encoded = export_circuit(body)
    encoded["operations"][0]["condition"] = {"bits": [0], "value": 1}
    nested = export_circuit(QuantumCircuit(1, 1))
    nested["operations"] = [{
        "name": "custom", "kind": "definition", "qargs": [0], "cargs": [0],
        "params": [], "definition": encoded,
    }]
    unconditional = deepcopy(nested)
    unconditional["operations"][0]["definition"]["operations"][0].pop("condition")
    assert oracle(nested, unconditional, [0], [0])["status"] != "verified"


def test_isometry_allows_disjoint_terminal_measurement_reordering():
    logical = QuantumCircuit(2, 2)
    logical.h(0)
    logical.cx(0, 1)
    logical.measure(0, 0)
    logical.measure(1, 1)
    encoded = export_circuit(logical)
    reordered = deepcopy(encoded)
    reordered["operations"][-2:] = reversed(reordered["operations"][-2:])
    assert isometry_equivalence(encoded, reordered, [0, 1], [0, 1])["status"] == "verified"


def test_isometry_preserves_classical_overwrite_order():
    logical = QuantumCircuit(2, 1)
    logical.measure(0, 0)
    logical.measure(1, 0)
    encoded = export_circuit(logical)
    reordered = deepcopy(encoded)
    reordered["operations"].reverse()
    assert isometry_equivalence(encoded, reordered, [0, 1], [0, 1])["status"] != "verified"
    assert validate_semantics(encoded, reordered, [0, 1], [0, 1])["status"] == "mismatch"


def test_isometry_compares_measurements_in_logical_wire_order():
    logical = QuantumCircuit(2, 2)
    logical.h(0)
    logical.measure(0, 0)
    logical.measure(1, 1)
    physical = QuantumCircuit(3, 2)
    physical.h(2)
    physical.measure(0, 1)
    physical.measure(2, 0)
    assert isometry_equivalence(
        export_circuit(logical), export_circuit(physical), [2, 0], [2, 0]
    )["status"] == "verified"


def test_terminal_measurement_can_erase_a_diagonal_unitary():
    logical = QuantumCircuit(1, 1)
    logical.z(0)
    logical.measure(0, 0)
    reduced = QuantumCircuit(1, 1)
    reduced.measure(0, 0)
    a, b = export_circuit(logical), export_circuit(reduced)
    assert isometry_equivalence(a, b, [0], [0])["reason"] == "requires_instrument_oracle"
    assert validate_semantics(a, b, [0], [0])["status"] == "verified"


def test_wrong_measured_wire_still_fails_full_instrument_oracle():
    logical = QuantumCircuit(2, 1)
    logical.measure(0, 0)
    wrong = QuantumCircuit(2, 1)
    wrong.measure(1, 0)
    assert validate_semantics(
        export_circuit(logical), export_circuit(wrong), [0, 1], [0, 1]
    )["status"] == "mismatch"
