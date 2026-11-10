"""Independent expansion of the frozen ordered Lie-Trotter input recipe.

This certifies the requested product formula, not exact time evolution under
the sum of noncommuting Hamiltonian terms. No synthesis code from either arm is
used: each Pauli exponential follows basis rotation and parity conjugation.
Global identity-term phases are immaterial for these uncontrolled channels.
"""

from __future__ import annotations

import math


def expand_pauli_evolution(operation):
    synthesis = operation["synthesis"]
    settings = synthesis["settings"]
    allowed = {"reps", "insert_barriers", "cx_structure", "wrap", "preserve_order"}
    if (
        operation.get("kind") != "pauli_evolution"
        or operation.get("condition")
        or operation.get("cargs")
        or synthesis["class"] != "LieTrotter"
        or set(settings) - allowed
        or settings.get("preserve_order", True) is not True
    ):
        raise NotImplementedError("Only the frozen ordered Lie-Trotter recipe is certified")
    repetitions = settings.get("reps", 1)
    structure = settings.get("cx_structure", "chain")
    time = operation["params"][0]
    if (
        type(repetitions) is not int or repetitions <= 0
        or structure not in {"chain", "fountain"}
        or not isinstance(time, (int, float)) or not math.isfinite(time)
    ):
        raise NotImplementedError("Unsupported product-formula parameters")
    qargs = operation["qargs"]
    terms = [term for group in operation["groups"] for term in group]
    output = []

    def gate(name, qubits, params=()):
        return {"kind": "standard", "name": name, "qargs": list(qubits),
                "cargs": [], "params": list(params)}

    for _ in range(repetitions):
        for label, real, imaginary in terms:
            if (
                len(label) != len(qargs) or set(label) - set("IXYZ")
                or imaginary != 0 or not math.isfinite(real)
            ):
                raise NotImplementedError("Expected real, width-matched Pauli terms")
            active = [(qargs[i], axis) for i, axis in enumerate(reversed(label)) if axis != "I"]
            angle = 2 * real * time / repetitions
            if not active:
                continue
            if len(active) == 1:
                qubit, axis = active[0]
                output.append(gate("r" + axis.lower(), [qubit], [angle]))
                continue
            if len(active) == 2:
                (qa, a), (qb, b) = active
                if a == b:
                    output.append(gate("r" + (a + b).lower(), [qa, qb], [angle]))
                    continue
                if {a, b} == {"X", "Z"}:
                    output.append(gate("rzx", [qa, qb] if a == "Z" else [qb, qa], [angle]))
                    continue
            # H† Z H = X and SX† Z SX = Y. Conjugate a Z rotation by
            # these local basis changes and a reversible parity network.
            output.extend(gate("h" if p == "X" else "sx", [q])
                          for q, p in active if p != "Z")
            qubits = [q for q, _ in active]
            if structure == "chain":
                parity = [(qubits[i], qubits[i - 1]) for i in range(len(qubits) - 1, 0, -1)]
            else:
                parity = [(q, qubits[0]) for q in qubits[1:]]
            output.extend(gate("cx", edge) for edge in parity)
            output.append(gate("rz", [qubits[0]], [angle]))
            output.extend(gate("cx", edge) for edge in reversed(parity))
            output.extend(gate("h" if p == "X" else "sxdg", [q])
                          for q, p in active if p != "Z")
    return output
