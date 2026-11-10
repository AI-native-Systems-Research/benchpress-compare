"""Independent certificates for phases erased by computational measurements."""

from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import json


@lru_cache(maxsize=4096)
def _diagonal_error(encoded: str, width: int) -> float | None:
    """Bound the channel error from replacing this gate by a diagonal unitary."""
    import numpy as np
    from qiskit.quantum_info import Operator
    from benchpress_compare.corpus.ir import IR_PROTOCOL, import_circuit

    operation = json.loads(encoded)
    operation["qargs"] = list(range(width))
    circuit = {
        "schema_version": 1, "protocol": IR_PROTOCOL,
        "num_qubits": width, "num_clbits": 0, "qregs": [], "cregs": [],
        "global_phase": 0, "operations": [operation],
    }
    try:
        matrix = Operator(import_circuit(circuit)).data
    except Exception:
        return None
    diagonal = np.diag(matrix)
    if not np.all(np.isfinite(matrix)):
        return None
    off_diagonal = matrix - np.diag(diagonal)
    # Spectral norm <= dimension * maximum entry magnitude. Account for
    # non-unit diagonal amplitudes too, and use the factor two channel bound.
    bound = 2 * (
        len(matrix) * float(np.max(np.abs(off_diagonal)))
        + float(np.max(np.abs(np.abs(diagonal) - 1)))
    )
    return bound if bound <= 1e-12 else None


def normalize_measured_diagonals(circuit: dict, *, max_support: int = 4) -> tuple[dict, dict]:
    """Erase only independently verified phases followed by full measurement.

    Every affected wire must next undergo unconditional computational-basis
    measurement. Projecting all diagonal-gate wires turns its phase into a scalar
    within each measurement branch, so the full quantum instrument is preserved.
    Measured wires can subsequently be reused. A measurement on only part of the
    support is insufficient. Barriers and delays have identity semantics here.
    """
    normalized = deepcopy(circuit)
    certificates = []
    error_bound = 0.0

    def visit(body, path):
        nonlocal error_bound
        for index, operation in enumerate(body["operations"]):
            for branch, child in enumerate(operation.get("blocks", [])):
                visit(child, [*path, index, branch])
        following = {}
        retained = []
        for index in reversed(range(len(body["operations"]))):
            operation = body["operations"][index]
            name, qubits = operation["name"], operation.get("qargs", [])
            if operation.get("kind") in {"barrier", "delay"}:
                retained.append(operation)
                continue
            removable = (
                0 < len(qubits) <= max_support
                and not operation.get("cargs")
                and not operation.get("condition")
                and not operation.get("blocks")
                and name not in {"measure", "reset", "initialize"}
                and all(following.get(q) == "unconditional_measure" for q in qubits)
            )
            bound = None
            if removable:
                encoded = deepcopy(operation)
                encoded["qargs"] = list(range(len(qubits)))
                bound = _diagonal_error(
                    json.dumps(encoded, sort_keys=True, separators=(",", ":")), len(qubits)
                )
            if bound is not None:
                certificates.append({
                    "path": [*path, index], "operation": name,
                    "qargs": qubits, "channel_error_bound": bound,
                })
                error_bound += bound
                continue
            retained.append(operation)
            successor = (
                "unconditional_measure" if name == "measure"
                and operation.get("kind") == "standard"
                and len(qubits) == 1 and len(operation.get("cargs", [])) == 1
                and not operation.get("condition") and not operation.get("blocks")
                else "other"
            )
            for q in qubits:
                following[q] = successor
        body["operations"] = list(reversed(retained))

    visit(normalized, [])
    return normalized, {
        "status": "verified", "method": "diagonal_before_full_measurement",
        "removed": len(certificates), "certificates": certificates,
        "error_bound": error_bound, "max_support": max_support,
        "covers": ["arbitrary_input_instrument", "measurement_outcomes", "postmeasurement_state"],
    }
