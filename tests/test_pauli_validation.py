from copy import deepcopy
from itertools import product

import pytest

pytest.importorskip("qiskit")
import numpy as np
from qiskit.quantum_info import Operator

from benchpress_compare.corpus.ir import IR_PROTOCOL, import_circuit
from benchpress_compare.validation.pauli import expand_pauli_evolution


PAULIS = {
    "I": np.eye(2), "X": np.array([[0, 1], [1, 0]]),
    "Y": np.array([[0, -1j], [1j, 0]]), "Z": np.diag([1, -1]),
}


def spec(terms, *, repetitions=1, structure="chain"):
    return {"kind": "pauli_evolution", "name": "PauliEvolution", "params": [0.37],
            "qargs": list(range(len(terms[0][0]))), "cargs": [], "groups": [terms],
            "grouped": False, "synthesis": {"class": "LieTrotter", "settings": {
                "reps": repetitions, "preserve_order": True, "cx_structure": structure}}}


def matrix(operation):
    return Operator(import_circuit({
        "schema_version": 1, "protocol": IR_PROTOCOL,
        "num_qubits": len(operation["qargs"]), "num_clbits": 0,
        "qregs": [], "cregs": [], "global_phase": 0,
        "operations": expand_pauli_evolution(operation),
    })).data


def reference(operation):
    width = len(operation["qargs"])
    eye = np.eye(2**width)
    result = eye.copy()
    repetitions = operation["synthesis"]["settings"]["reps"]
    for _ in range(repetitions):
        for group in operation["groups"]:
            for label, coefficient, _ in group:
                pauli = np.ones((1, 1))
                for axis in label:
                    pauli = np.kron(pauli, PAULIS[axis])
                angle = coefficient * operation["params"][0] / repetitions
                result = (np.cos(angle) * eye - 1j * np.sin(angle) * pauli) @ result
    return result


@pytest.mark.parametrize("structure", ["chain", "fountain"])
def test_independent_expansion_all_paulis_through_four_qubits(structure):
    for width in range(1, 5):
        for axes in product("IXYZ", repeat=width):
            operation = spec([["".join(axes), -0.61, 0]], structure=structure)
            actual, expected = matrix(operation), reference(operation)
            phase = np.vdot(expected, actual) / expected.shape[0]
            assert np.allclose(actual, phase * expected, atol=1e-10), axes


def test_noncommuting_terms_repetitions_and_coefficients_have_declared_order():
    operation = spec([["XI", 0.8, 0], ["ZZ", -0.23, 0], ["YY", 1.7, 0]], repetitions=3)
    assert np.allclose(matrix(operation), reference(operation), atol=1e-10)
    reversed_terms = deepcopy(operation)
    reversed_terms["groups"][0].reverse()
    assert not np.allclose(matrix(reversed_terms), reference(operation), atol=1e-5)


@pytest.mark.parametrize("change", [
    {"preserve_order": False}, {"reps": 0}, {"cx_structure": "unknown"},
    {"atomic_evolution": "undeclared"},
])
def test_unsupported_recipes_do_not_receive_certificate(change):
    operation = spec([["XX", 1, 0]])
    operation["synthesis"]["settings"].update(change)
    with pytest.raises(NotImplementedError):
        expand_pauli_evolution(operation)


def test_wide_expansion_is_linear_and_has_no_dense_operator():
    operation = spec([["X" + "Y" * 131 + "Z", 0.25, 0]])
    operations = expand_pauli_evolution(operation)
    assert len(operations) == 2 * 132 + 2 * 132 + 1
    assert all(len(op["qargs"]) <= 2 for op in operations)
