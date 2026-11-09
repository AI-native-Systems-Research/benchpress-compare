"""Bounded exact isometry check for unitary circuits with terminal measurement."""

from __future__ import annotations

from copy import deepcopy


def isometry_equivalence(
    logical, output, initial, final, max_logical_qubits=8, max_physical_qubits=14
):
    import numpy as np
    from qiskit.quantum_info import Operator, Statevector
    from benchpress_compare.corpus.ir import import_circuit
    from benchpress_compare.metrics import active_qubits

    scope = {
        "method": "exact_basis_isometry",
        "input_state": "arbitrary",
        "ancillas": "zero",
        "max_logical_qubits": max_logical_qubits,
        "max_physical_qubits": max_physical_qubits,
    }

    def unitary_prefix(circuit, locations):
        result = deepcopy(circuit)
        measured = set()
        classical = []
        body = []
        inverse = {physical: logical for logical, physical in enumerate(locations)}
        for op in circuit["operations"]:
            if op["name"] in {
                "reset",
                "if_else",
                "while_loop",
                "for_loop",
                "switch_case",
            } or op.get("condition"):
                return None
            if op["name"] == "measure":
                if len(op["qargs"]) != 1 or len(op["cargs"]) != 1 or op["qargs"][0] not in inverse:
                    return None
                measured.add(op["qargs"][0])
                # Preserve every measurement event, including a dephasing whose
                # classical result is overwritten by a later measurement.
                classical.append([op["cargs"][0], inverse[op["qargs"][0]]])
            elif op["name"] not in {"barrier", "delay"}:
                if measured.intersection(op["qargs"]):
                    return None
                body.append(op)
        result["operations"] = body
        result["num_clbits"] = 0
        result["cregs"] = []
        return result, classical

    a, b = (
        unitary_prefix(logical, list(range(logical["num_qubits"]))),
        unitary_prefix(output, final),
    )
    if a is None or b is None:
        return {"status": "unsupported", "reason": "requires_instrument_oracle", "scope": scope}
    if a[1] != b[1] or logical["num_clbits"] != output["num_clbits"]:
        return {"status": "mismatch", "reason": "terminal_measurement_map_changed", "scope": scope}
    logical, output = a[0], b[0]
    active = sorted(active_qubits(output) | set(initial) | set(final))
    n, m = logical["num_qubits"], len(active)
    if n > max_logical_qubits or m > max_physical_qubits:
        return {
            "status": "unsupported",
            "reason": "semantic_certificate_required",
            "scope": scope,
            "logical_width": n,
            "active_physical_width": m,
        }
    mapping = {q: i for i, q in enumerate(active)}
    initial, final = [mapping[q] for q in initial], [mapping[q] for q in final]
    output["num_qubits"] = m
    output["qregs"] = []
    for op in output["operations"]:
        op["qargs"] = [mapping[q] for q in op["qargs"]]
    try:
        reference = Operator(import_circuit(logical)).data
        compiled = import_circuit(output)
        ancillas = [q for q in range(m) if q not in final]

        def encode(index, positions):
            return sum(((index >> q) & 1) << p for q, p in enumerate(positions))

        indices = np.array(
            [
                [encode(q, final) | encode(a, ancillas) for a in range(2 ** (m - n))]
                for q in range(2**n)
            ]
        )
        ancilla_state = None
        max_error = 0.0
        for column in range(2**n):
            state = (
                Statevector.from_int(encode(column, initial), dims=2**m)
                .evolve(compiled)
                .data[indices]
            )
            expected = reference[:, column]
            if ancilla_state is None:
                ancilla_state = expected.conj() @ state
                if abs(float(np.vdot(ancilla_state, ancilla_state).real) - 1) > 1e-8:
                    return {
                        "status": "mismatch",
                        "reason": "logical_isometry_changed",
                        "column": column,
                        "scope": scope,
                    }
            error = float(np.max(np.abs(state - np.outer(expected, ancilla_state))))
            max_error = max(error, max_error)
            if error > 1e-8:
                return {
                    "status": "mismatch",
                    "reason": "logical_isometry_changed",
                    "column": column,
                    "max_error": error,
                    "scope": scope,
                }
        return {
            "status": "verified",
            "covers": ["full_quantum_instrument"],
            "columns_checked": 2**n,
            "max_error": max_error,
            "tolerance": 1e-8,
            "scope": scope,
        }
    except Exception as error:
        return {
            "status": "error",
            "reason": "isometry_oracle_error",
            "detail": f"{type(error).__name__}: {error}",
            "scope": scope,
        }
