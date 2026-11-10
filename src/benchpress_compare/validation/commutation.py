"""Independent bounded-matrix certificates for commuting gate fusion."""

from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import json


@lru_cache(maxsize=4096)
def _operator(encoded, width):
    from qiskit.quantum_info import Operator
    from benchpress_compare.corpus.ir import IR_PROTOCOL, import_circuit

    return Operator(import_circuit({
        "schema_version": 1, "protocol": IR_PROTOCOL, "num_qubits": width,
        "num_clbits": 0, "qregs": [], "cregs": [], "global_phase": 0,
        "operations": [json.loads(encoded)],
    })).data


def normalize_commuting_rewrites(circuit, *, max_support=4):
    """Fuse same-support gates only across independently proven commutations.

    Operator matrices, including their wire order, determine commutation and
    fusion. Gate display names are never treated as algebraic evidence. Both
    adjacent fusion and moves past overlapping gates carry conservative numerical
    error bounds. Classical operations and unsupported gates remain barriers.
    """
    import numpy as np
    from qiskit import QuantumCircuit
    from qiskit.quantum_info import Operator

    result = deepcopy(circuit)
    counts = {"commutations": 0, "fusions": 0, "identities": 0}
    error_bound = 0.0
    epsilon = np.finfo(float).eps

    def matrix(operation):
        qs = operation.get("qargs", [])
        if (not 0 < len(qs) <= max_support or operation.get("cargs")
                or operation.get("condition") or operation.get("blocks")):
            return None
        ordered = sorted(qs)
        remap = {q: i for i, q in enumerate(ordered)}
        encoded = deepcopy(operation)
        encoded["qargs"] = [remap[q] for q in qs]
        try:
            value = _operator(json.dumps(encoded, sort_keys=True, separators=(",", ":")), len(qs))
        except Exception:
            return None
        if not np.all(np.isfinite(value)):
            return None
        if float(np.max(np.abs(value.conj().T @ value - np.eye(len(value))))) > 1e-12:
            return None
        return value

    def serialize(qs, value):
        return {"name": "unitary", "kind": "unitary", "qargs": list(qs), "cargs": [],
                "params": [], "matrix": [[[float(z.real), float(z.imag)] for z in row]
                                          for row in value]}

    def commutes(a, b):
        nonlocal error_bound
        if not set(a[0]) & set(b[0]):
            return True
        support = sorted(set(a[0]) | set(b[0]))
        if len(support) > max_support or a[1] is None or b[1] is None:
            return False
        mapping = {q: i for i, q in enumerate(support)}
        matrices = []
        for qs, value, _ in (a, b):
            qc = QuantumCircuit(len(support))
            qc.unitary(value, [mapping[q] for q in qs])
            matrices.append(Operator(qc).data)
        left, right = matrices
        residual = float(np.max(np.abs(left @ right - right @ left)))
        bound = 2 * len(left) * residual + 8 * epsilon * len(left) ** 2
        if bound > 1e-12:
            return False
        error_bound += bound
        counts["commutations"] += 1
        return True

    def visit(body):
        nonlocal error_bound
        accumulated = []
        for operation in body["operations"]:
            for child in operation.get("blocks", []):
                visit(child)
            if operation.get("kind") in {"barrier", "delay"}:
                continue
            value = matrix(operation)
            support = tuple(sorted(operation.get("qargs", [])))
            incoming = (support, value, operation)
            if value is None:
                accumulated.append(incoming)
                continue
            # Look back through the commuting suffix for a fusion partner. Each
            # proven swap is applied immediately, so its recorded error bound
            # always belongs to an actual transformation of the represented IR.
            position = len(accumulated)
            while position:
                previous = accumulated[position - 1]
                if previous[1] is not None and previous[0] == support:
                    value = value @ previous[1]
                    error_bound += 8 * epsilon * len(value) ** 2
                    counts["fusions"] += 1
                    accumulated.pop(position - 1)
                    position -= 1
                    incoming = (support, value, serialize(support, value))
                    continue
                if previous[1] is None or not commutes(previous, incoming):
                    break
                position -= 1
            # A scalar unitary has no effect on a classical branch's instrument.
            phase = value[0, 0]
            if abs(phase):
                phase /= abs(phase)
                residual = float(np.max(np.abs(value - phase * np.eye(len(value)))))
            else:
                residual = float("inf")
            bound = 2 * len(value) * residual
            if bound <= 1e-12:
                counts["identities"] += 1
                error_bound += bound
            else:
                # Give commuting gates a deterministic support order after
                # fusion. Recheck each move: a fused matrix may commute with
                # fewer gates than the original incoming factor did.
                order = (len(support), support)
                while position < len(accumulated):
                    successor = accumulated[position]
                    if ((len(successor[0]), successor[0]) >= order
                            or successor[1] is None or not commutes(incoming, successor)):
                        break
                    position += 1
                accumulated.insert(position, incoming)
        body["operations"] = [row[2] for row in accumulated]

    visit(result)
    return result, {"status": "verified" if error_bound <= 1e-8 else "unsupported",
                    "method": "bounded_commuting_fusion", "max_support": max_support,
                    "error_bound": float(error_bound), "tolerance": 1e-8, **counts}
