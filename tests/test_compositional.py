"""Adversarial checks for the bounded causal-region semantic certificate."""

from copy import deepcopy

import pytest

from benchpress_compare.corpus.ir import export_circuit
from benchpress_compare.validation.compositional import local_equivalence

pytestmark = pytest.mark.integration
qiskit = pytest.importorskip("qiskit")
QuantumCircuit, transpile = qiskit.QuantumCircuit, qiskit.transpile
UnitaryGate = pytest.importorskip("qiskit.circuit.library").UnitaryGate
Operator = pytest.importorskip("qiskit.quantum_info").Operator


@pytest.mark.parametrize("name", ["barrier", "delay", "swap"])
def test_nonstandard_operation_names_cannot_erase_semantics(name):
    logical = QuantumCircuit(2)
    changed = QuantumCircuit(2)
    misleading = UnitaryGate(Operator.from_label("IX").data)
    misleading.name = name
    changed.append(misleading, [0, 1])
    final = [1, 0] if name == "swap" else None
    result = local_equivalence(export_circuit(logical), export_circuit(changed), final=final)
    assert result["status"] != "verified"


def test_conditional_definition_cannot_be_inlined_unconditionally():
    definition = QuantumCircuit(1)
    definition.x(0)
    logical = export_circuit(QuantumCircuit(1, 1))
    logical["operations"] = [{
        "kind": "definition", "name": "conditional_x", "qargs": [0], "cargs": [],
        "params": [], "definition": export_circuit(definition),
        "condition": {"bits": [0], "value": 1},
    }]
    changed = QuantumCircuit(1, 1)
    changed.x(0)
    result = local_equivalence(logical, export_circuit(changed))
    assert result["status"] != "verified"


def test_dynamic_condition_and_measurement_destinations_are_semantic():
    circuit = QuantumCircuit(2, 2)
    circuit.h(0)
    circuit.measure(0, 0)
    with circuit.if_test((circuit.clbits[0], 1)):
        circuit.x(1)
    circuit.measure(1, 1)
    logical = export_circuit(circuit)
    assert local_equivalence(logical, logical)["status"] == "verified"
    changed = deepcopy(logical)
    changed["operations"][2]["condition"]["value"] = 0
    assert local_equivalence(logical, changed)["status"] != "verified"
    changed = deepcopy(logical)
    changed["operations"][1]["cargs"] = [1]
    assert local_equivalence(logical, changed)["status"] != "verified"


def test_changed_reset_and_branch_operations_are_not_certified():
    circuit = QuantumCircuit(2, 1)
    circuit.h(0)
    circuit.measure(0, 0)
    circuit.reset(0)
    with circuit.if_test((circuit.clbits[0], 1)):
        circuit.cx(0, 1)
    logical = export_circuit(circuit)
    changed = deepcopy(logical)
    changed["operations"].pop(2)
    assert local_equivalence(logical, changed)["status"] != "verified"
    changed = deepcopy(logical)
    changed["operations"][-1]["blocks"][0]["operations"][0]["qargs"].reverse()
    assert local_equivalence(logical, changed)["status"] != "verified"


def test_unitary_local_certificate_agrees_with_independent_dense_operator():
    from random import Random

    rng = Random(72265)
    for _ in range(30):
        circuit = QuantumCircuit(3)
        for _ in range(10):
            gate = rng.choice(["h", "s", "cx", "swap", "rz"])
            if gate in {"cx", "swap"}:
                getattr(circuit, gate)(*rng.sample(range(3), 2))
            elif gate == "rz":
                circuit.rz(rng.random(), rng.randrange(3))
            else:
                getattr(circuit, gate)(rng.randrange(3))
        lowered = transpile(circuit, basis_gates=["rz", "sx", "x", "cx"], optimization_level=1)
        logical, output = export_circuit(circuit), export_circuit(lowered)
        assert Operator(circuit).equiv(Operator(lowered))
        result = local_equivalence(logical, output)
        assert result["status"] == "verified", result
        changed = lowered.copy()
        changed.x(rng.randrange(3))
        assert not Operator(circuit).equiv(Operator(changed))
        assert local_equivalence(logical, export_circuit(changed))["status"] != "verified"


def test_wide_basis_rewrite_uses_causal_cuts_across_interleaved_gate_expansions():
    circuit = QuantumCircuit(11)
    for _ in range(2):
        for q in range(10):
            circuit.cx(q, q + 1)
            circuit.h(q)
            circuit.swap(q, (q + 2) % 11)
    lowered = transpile(
        circuit, basis_gates=["rz", "sx", "cz"], optimization_level=0,
        qubits_initially_zero=False,
    )
    proof = local_equivalence(export_circuit(circuit), export_circuit(lowered))
    assert proof["status"] == "verified", proof
    assert proof["regions"] > 0
    assert proof["scope"]["max_support"] == 4
    assert proof["error_bound"] < 1e-8
    altered = lowered.copy()
    altered.rz(0.125, 5)
    assert local_equivalence(export_circuit(circuit), export_circuit(altered))["status"] != "verified"


def test_per_region_tolerance_does_not_hide_accumulated_circuit_error():
    logical, changed = QuantumCircuit(20), QuantumCircuit(20)
    for q in range(20):
        changed.rz(1e-9, q)
    proof = local_equivalence(export_circuit(logical), export_circuit(changed))
    assert proof["status"] != "verified"
    assert proof["reason"] == "certificate_numerical_budget_exhausted"
