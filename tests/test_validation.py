from copy import deepcopy

import pytest

from benchpress_compare.validation import (
    routing_replay,
    layout_validation,
    target_legality,
    validate_semantics,
)
from benchpress_compare.targets import synthetic_target


def circuit(n, operations, c=0):
    return {"schema_version": 1, "num_qubits": n, "num_clbits": c, "operations": operations}


def op(name, q, c=(), **kw):
    return {"name": name, "qargs": list(q), "cargs": list(c), "params": [], **kw}


def test_routing_replay_distinguishes_original_swaps():
    logical = circuit(3, [op("swap", (0, 1)), op("cx", (0, 2))])
    routed = circuit(3, [op("swap", (0, 1)), op("swap", (1, 2)), op("cx", (0, 1))])
    evidence = routing_replay(logical, routed, [0, 1, 2], [0, 2, 1])
    assert evidence["status"] == "verified"
    assert evidence["inserted_swaps"] == 1
    assert routing_replay(logical, routed, [0, 1, 2], [0, 1, 2])["status"] == "mismatch"


def test_routing_replay_preserves_measurement_reset_and_classical_dependencies():
    logical = circuit(2, [op("measure", [0], [0]), op("reset", [0]), op("cx", [0, 1])], 1)
    routed = circuit(
        2, [op("swap", [0, 1]), op("measure", [1], [0]), op("reset", [1]), op("cx", [1, 0])], 1
    )
    assert routing_replay(logical, routed, [0, 1], [1, 0])["status"] == "verified"
    routed["operations"][1]["cargs"] = []
    assert routing_replay(logical, routed, [0, 1], [1, 0])["status"] == "mismatch"


def test_branch_routing_requires_reconciled_permutations():
    block = circuit(2, [op("cx", [0, 1])], 1)
    branch = op("if_else", [0, 1], [0], condition={"bits": [0], "value": 1}, blocks=[block])
    logical = circuit(2, [branch], 1)
    routed = deepcopy(logical)
    routed["operations"][0]["blocks"][0]["operations"] = [
        op("swap", [0, 1]),
        op("cx", [1, 0]),
        op("swap", [0, 1]),
    ]
    result = routing_replay(logical, routed, [0, 1], [0, 1])
    assert result["status"] == "verified"
    assert result["inserted_swaps"] == 2
    routed["operations"][0]["blocks"][0]["operations"].pop()
    assert (
        routing_replay(logical, routed, [0, 1], [0, 1])["reason"]
        == "branch_exit_permutations_differ"
    )


def test_illegal_native_edge_and_invalid_layout():
    target = synthetic_target("square", 4)
    assert target_legality(circuit(4, [op("cz", [0, 3])]), target)["status"] == "mismatch"
    assert target_legality(circuit(4, [op("cz", [0, 1])]), target)["status"] == "verified"
    assert layout_validation([0, 0], [0, 1], 2, 4)["status"] == "mismatch"


def test_arbitrary_input_semantics_detects_change_hidden_on_zero_state():
    qiskit = pytest.importorskip("qiskit")
    from benchpress_compare.corpus.ir import export_circuit

    a, b = qiskit.QuantumCircuit(1), qiskit.QuantumCircuit(1)
    b.z(0)
    assert (
        validate_semantics(export_circuit(a), export_circuit(b), [0], [0])["status"] == "mismatch"
    )


def test_dynamic_instrument_detects_branch_state_corruption_and_reset():
    qiskit = pytest.importorskip("qiskit")
    from benchpress_compare.corpus.ir import export_circuit

    a = qiskit.QuantumCircuit(2, 1)
    a.measure(0, 0)
    with a.if_test((a.clbits[0], 1)):
        a.x(1)
    a.reset(0)
    encoded = export_circuit(a)
    assert validate_semantics(encoded, encoded, [0, 1], [0, 1])["status"] == "verified"
    changed = deepcopy(encoded)
    changed["operations"][1]["blocks"][0]["operations"] = []
    assert validate_semantics(encoded, changed, [0, 1], [0, 1])["status"] == "mismatch"
    changed = deepcopy(encoded)
    changed["operations"].pop()
    assert validate_semantics(encoded, changed, [0, 1], [0, 1])["status"] == "mismatch"


def test_overwritten_measurement_outcome_still_dephases_quantum_state():
    qiskit = pytest.importorskip("qiskit")
    from benchpress_compare.corpus.ir import export_circuit

    logical = qiskit.QuantumCircuit(2, 1)
    logical.measure(0, 0)
    logical.measure(1, 0)
    bad = qiskit.QuantumCircuit(2, 1)
    bad.measure(1, 0)
    assert (
        validate_semantics(export_circuit(logical), export_circuit(bad), [0, 1], [0, 1])["status"]
        == "mismatch"
    )


def test_large_circuits_cannot_masquerade_as_semantically_verified():
    qiskit = pytest.importorskip("qiskit")
    from benchpress_compare.corpus.ir import export_circuit

    q = qiskit.QuantumCircuit(9)
    encoded = export_circuit(q)
    evidence = validate_semantics(encoded, encoded, list(range(9)), list(range(9)))
    assert evidence["status"] == "unsupported"
    assert evidence["reason"] == "semantic_certificate_required"
