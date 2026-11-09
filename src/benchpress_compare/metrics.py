"""Versioned, Qiskit-independent metrics on the typed logical circuit format.

N2 is static native two-qubit size. D2 is a longest dependency path whose
two-qubit instructions have cost one; all other operations have cost zero.
Conditional boundaries conservatively synchronize their participating wires.
"""

from __future__ import annotations

METRIC_PROTOCOL = "native-n2-weighted-d2-v1"
CONTROL_FLOW = {"if_else", "while_loop", "for_loop", "switch_case", "box"}


class UnsupportedMetric(ValueError):
    """The frozen metric convention does not cover this construct."""


def validate_circuit(circuit: dict) -> None:
    if circuit.get("schema_version") != 1:
        raise ValueError("Unsupported logical circuit schema")
    for field in ("num_qubits", "num_clbits"):
        if type(circuit.get(field)) is not int or circuit[field] < 0:
            raise ValueError(f"Invalid {field}")
    if not isinstance(circuit.get("operations"), list):
        raise ValueError("operations must be a list")
    for op in circuit["operations"]:
        if not isinstance(op.get("name"), str) or not op["name"]:
            raise ValueError("Operation name is required")
        for field, limit in (("qargs", circuit["num_qubits"]), ("cargs", circuit["num_clbits"])):
            indices = op.get(field, [])
            if not isinstance(indices, list) or len(set(indices)) != len(indices):
                raise ValueError(f"Invalid {field}")
            if any(type(i) is not int or not 0 <= i < limit for i in indices):
                raise ValueError(f"Out-of-range {field}")
        condition = op.get("condition")
        if condition is not None:
            bits = condition.get("bits", [])
            if not bits or any(type(i) is not int or not 0 <= i < circuit["num_clbits"] for i in bits):
                raise ValueError("Invalid classical condition bits")
            if type(condition.get("value")) is not int or not 0 <= condition["value"] < 2 ** len(bits):
                raise ValueError("Invalid classical condition value")
        for block in op.get("blocks", []):
            validate_circuit(block)
            if block["num_qubits"] != len(op.get("qargs", [])) or block["num_clbits"] != len(op.get("cargs", [])):
                raise ValueError("Block wire map does not match container")


def active_qubits(circuit: dict) -> set[int]:
    """Count used logical wires, mapping nested blocks to their enclosing wires."""
    validate_circuit(circuit)
    active: set[int] = set()
    for op in circuit["operations"]:
        if op["name"] == "barrier":
            continue
        if "blocks" in op:
            for block in op["blocks"]:
                active.update(op["qargs"][q] for q in active_qubits(block))
        else:
            active.update(op.get("qargs", []))
    return active


def measure(circuit: dict, native_2q_names: tuple[str, ...] | list[str] = ("cz",)) -> dict:
    validate_circuit(circuit)
    native = set(native_2q_names)

    def traverse(body, qdepth, cdepth):
        count = 0
        for op in body["operations"]:
            name = op["name"]
            qs, cs = op.get("qargs", []), op.get("cargs", [])
            reads = op.get("condition", {}).get("bits", [])
            touched_c = set(cs) | set(reads)
            incoming = max([qdepth[q] for q in qs] + [cdepth[c] for c in touched_c] + [0])
            if name in CONTROL_FLOW or "blocks" in op:
                if name != "if_else" or not 1 <= len(op.get("blocks", [])) <= 2:
                    raise UnsupportedMetric(f"Unsupported control flow: {name}")
                if not op.get("condition"):
                    raise UnsupportedMetric("Conditional expression is not an equality condition")
                boundary = incoming
                for block in op["blocks"]:
                    bq = [incoming] * len(qs)
                    bc = [incoming] * len(cs)
                    n = traverse(block, bq, bc)
                    count += n
                    boundary = max([boundary, *bq, *bc])
                for q in qs:
                    qdepth[q] = boundary
                for c in touched_c:
                    cdepth[c] = boundary
                continue
            if len(qs) == 2 and name != "barrier" and name not in native:
                raise UnsupportedMetric(f"Non-native two-qubit operation: {name}")
            if len(qs) > 2 and name != "barrier":
                raise UnsupportedMetric(f"Non-native multi-qubit operation: {name}")
            cost = int(name in native and len(qs) == 2)
            count += cost
            outgoing = incoming + cost
            for q in qs:
                qdepth[q] = outgoing
            # Condition reads are dependencies too. Conservatively retain the
            # ordering of repeated read/write accesses to a classical wire.
            for c in touched_c:
                cdepth[c] = outgoing
        return count

    qdepth = [0] * circuit["num_qubits"]
    cdepth = [0] * circuit["num_clbits"]
    n2 = traverse(circuit, qdepth, cdepth)
    return {"N2": n2, "D2": max([0, *qdepth, *cdepth]), "protocol": METRIC_PROTOCOL}


def structural_signature(circuit: dict) -> list:
    """Ordered pre-layout two-qubit sequence, including branch context.

    Coefficients, one-qubit operations, and rotation angles deliberately do not
    identify a routing workload. This is not a semantic circuit identity.
    """
    validate_circuit(circuit)
    result = []

    def walk(body, mapping, context):
        for op in body["operations"]:
            qs = [mapping[q] for q in op.get("qargs", [])]
            if "blocks" in op:
                for branch, block in enumerate(op["blocks"]):
                    walk(block, qs, [*context, [op["name"], branch, op.get("condition")]])
            elif len(qs) == 2 and op["name"] != "barrier":
                result.append([context, op["name"], qs])

    walk(circuit, list(range(circuit["num_qubits"])), [])
    return result
