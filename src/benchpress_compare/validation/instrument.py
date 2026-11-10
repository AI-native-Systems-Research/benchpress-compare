"""Small exact quantum-instrument oracle, including reset and classical control."""

from __future__ import annotations

from collections import defaultdict


def _unsupported_conditions(circuit):
    """The interchange supports classical conditions only on explicit branches."""
    for op in circuit["operations"]:
        if op.get("condition") and op.get("kind") != "if_else":
            return True
        if any(_unsupported_conditions(block) for block in op.get("blocks", [])):
            return True
        if op.get("definition") and _unsupported_conditions(op["definition"]):
            return True
    return False


def instrument_equivalence(
    logical, output, initial, final, *, max_logical_qubits=4, max_physical_qubits=6
):
    import numpy as np
    from qiskit.quantum_info import Operator
    from benchpress_compare.corpus.ir import import_circuit, IR_PROTOCOL
    from benchpress_compare.metrics import validate_circuit
    from . import layout_validation

    scope = {
        "method": "exact_matrix_unit_instrument",
        "input_state": "arbitrary",
        "ancillas": "zero",
        "classical_outputs": "all",
        "max_logical_qubits": max_logical_qubits,
        "max_physical_qubits": max_physical_qubits,
    }
    if _unsupported_conditions(logical) or _unsupported_conditions(output):
        return {"status": "unsupported", "reason": "unsupported_classical_condition", "scope": scope}
    validation = layout_validation(initial, final, logical["num_qubits"], output["num_qubits"])
    if validation["status"] != "verified":
        return dict(validation, scope=scope)
    if logical["num_clbits"] != output["num_clbits"]:
        return {"status": "mismatch", "reason": "classical_width_changed", "scope": scope}
    # Idle physical wires never contribute. Compress before applying the bounded
    # oracle, preserving every used ancilla and every input/output location.
    from copy import deepcopy
    from benchpress_compare.metrics import active_qubits

    active = sorted(active_qubits(output) | set(initial) | set(final))
    if logical["num_qubits"] > max_logical_qubits or len(active) > max_physical_qubits:
        return {
            "status": "unsupported",
            "reason": "semantic_certificate_required",
            "logical_width": logical["num_qubits"],
            "active_physical_width": len(active),
            "scope": scope,
        }
    remap = {q: i for i, q in enumerate(active)}
    output = deepcopy(output)
    output["num_qubits"] = len(active)
    output["qregs"] = []
    for op in output["operations"]:
        op["qargs"] = [remap[q] for q in op["qargs"]]
    initial, final = [remap[q] for q in initial], [remap[q] for q in final]
    try:
        validate_circuit(logical)
        validate_circuit(output)
        n, m = logical["num_qubits"], output["num_qubits"]
        compiled = {}

        def full_matrix(op, width, qargs):
            key = (repr(op), width, tuple(qargs))
            if key not in compiled:
                circuit = {
                    "schema_version": 1,
                    "protocol": IR_PROTOCOL,
                    "num_qubits": width,
                    "num_clbits": 0,
                    "qregs": [],
                    "cregs": [],
                    "global_phase": 0,
                    "operations": [dict(op, qargs=qargs, cargs=[])],
                }
                compiled[key] = Operator(import_circuit(circuit)).data
            return compiled[key]

        def execute(body, states, qmap, cmap, width):
            for op in body["operations"]:
                name, kind = op["name"], op.get("kind")
                if kind in {"barrier", "delay"}:
                    continue
                if op.get("blocks") and kind != "if_else":
                    raise NotImplementedError(f"Unsupported control flow {name}")
                qs, cs = [qmap[q] for q in op["qargs"]], [cmap[c] for c in op["cargs"]]
                evolved = defaultdict(lambda: np.zeros((2**width, 2**width), dtype=complex))
                for classical, rho in states.items():
                    if kind == "if_else":
                        condition = op["condition"]
                        value = sum(
                            ((classical >> cmap[c]) & 1) << i
                            for i, c in enumerate(condition["bits"])
                        )
                        branch = 0 if value == condition["value"] else 1
                        selected = (
                            execute(op["blocks"][branch], {classical: rho}, qs, cs, width)
                            if branch < len(op["blocks"])
                            else {classical: rho}
                        )
                        for bits, matrix in selected.items():
                            evolved[bits] += matrix
                    elif kind == "standard" and name in {"measure", "reset"}:
                        if len(qs) != 1:
                            raise ValueError("Non-unary measurement/reset")
                        q = qs[0]
                        for value in (0, 1):
                            selected = np.array(
                                [int((i >> q) & 1) == value for i in range(2**width)]
                            )
                            branch = rho * selected[:, None] * selected[None, :]
                            if name == "measure":
                                bits = (classical & ~(1 << cs[0])) | (value << cs[0])
                            else:
                                bits = classical
                                if value:
                                    permutation = np.arange(2**width) ^ (1 << q)
                                    branch = branch[np.ix_(permutation, permutation)]
                            evolved[bits] += branch
                    else:
                        unitary = full_matrix(op, width, qs)
                        evolved[classical] += unitary @ rho @ unitary.conj().T
                states = dict(evolved)
            return states

        def embed_index(index, locations):
            return sum(((index >> q) & 1) << physical for q, physical in enumerate(locations))

        discarded = [q for q in range(m) if q not in final]
        extract = np.array(
            [
                [embed_index(i, final) | embed_index(a, discarded) for i in range(2**n)]
                for a in range(2 ** len(discarded))
            ]
        )
        max_error = 0.0
        for i in range(2**n):
            for j in range(2**n):
                source = np.zeros((2**n, 2**n), dtype=complex)
                source[i, j] = 1
                encoded = np.zeros((2**m, 2**m), dtype=complex)
                encoded[embed_index(i, initial), embed_index(j, initial)] = 1
                expected = execute(
                    logical, {0: source}, list(range(n)), list(range(logical["num_clbits"])), n
                )
                measured = execute(
                    output, {0: encoded}, list(range(m)), list(range(output["num_clbits"])), m
                )
                for bits in expected.keys() | measured.keys():
                    observed = measured.get(bits)
                    reduced = (
                        sum(
                            (observed[np.ix_(indices, indices)] for indices in extract),
                            np.zeros_like(source),
                        )
                        if observed is not None
                        else np.zeros_like(source)
                    )
                    error = float(
                        np.max(np.abs(expected.get(bits, np.zeros_like(source)) - reduced))
                    )
                    max_error = max(max_error, error)
                    if error > 1e-8:
                        return {
                            "status": "mismatch",
                            "reason": "quantum_instrument_changed",
                            "matrix_unit": [i, j],
                            "classical_value": bits,
                            "max_error": error,
                            "tolerance": 1e-8,
                            "scope": scope,
                        }
        return {
            "status": "verified",
            "covers": ["full_quantum_instrument"],
            "matrix_units_checked": 4**n,
            "max_error": max_error,
            "tolerance": 1e-8,
            "scope": scope,
        }
    except NotImplementedError as error:
        return {"status": "unsupported", "reason": str(error), "scope": scope}
    except Exception as error:
        return {
            "status": "error",
            "reason": "instrument_oracle_error",
            "detail": f"{type(error).__name__}: {error}",
            "scope": scope,
        }
