"""Validation evidence has explicit scope; unsupported never means verified."""

from __future__ import annotations

from collections import defaultdict, deque
from copy import deepcopy

from benchpress_compare.metrics import validate_circuit
from benchpress_compare.targets import validate_target

VALIDATION_PROTOCOL = "legality-routing-instrument-v1"


def target_legality(circuit: dict, target: dict) -> dict:
    try:
        validate_circuit(circuit)
        validate_target(target)
        if circuit["num_qubits"] > target["num_qubits"]:
            return {"status": "mismatch", "reason": "target_capacity"}
        operations = {op["name"]: op for op in target["instructions"]}

        def walk(body, mapping, path):
            for index, op in enumerate(body["operations"]):
                name = op["name"]
                if name == "barrier":
                    continue
                if name not in operations:
                    return {"status": "mismatch", "reason": "unsupported_target_instruction", "operation": name, "path": [*path, index]}
                support = operations[name]
                physical = [mapping[q] for q in op.get("qargs", [])]
                if "blocks" in op:
                    if support["arity"] is not None:
                        return {"status": "mismatch", "reason": "invalid_control_flow_support"}
                    if name != "if_else":
                        return {"status": "unsupported", "reason": "unbounded_control_flow"}
                    for branch, block in enumerate(op["blocks"]):
                        error = walk(block, physical, [*path, index, branch])
                        if error:
                            return error
                elif not any(p["qargs"] is None or p["qargs"] == physical for p in support["properties"]):
                    return {"status": "mismatch", "reason": "illegal_directed_qargs", "operation": name, "qargs": physical, "path": [*path, index]}
                fixed = [p.get("value") for p in support["parameters"]]
                if len(fixed) != len(op.get("params", [])):
                    return {"status": "mismatch", "reason": "parameter_arity"}
                if any(v is not None and v != op["params"][i] for i, v in enumerate(fixed)):
                    return {"status": "mismatch", "reason": "fixed_target_parameter"}
            return None

        return walk(circuit, list(range(circuit["num_qubits"])), []) or {"status": "verified", "covers": ["recursive_native_legality", "directed_instruction_support"]}
    except (ValueError, KeyError, TypeError) as error:
        return {"status": "error", "reason": "malformed_artifact", "detail": str(error)}


def layout_validation(initial: list[int], final: list[int], input_width: int, output_width: int) -> dict:
    for name, values in (("initial", initial), ("final", final)):
        if len(values) != input_width or len(set(values)) != len(values) or any(type(v) is not int or not 0 <= v < output_width for v in values):
            return {"status": "mismatch", "reason": "invalid_layout", "field": name}
    return {"status": "verified", "covers": ["layout_injectivity", "capacity"]}


def _global_operations(circuit, qmap, cmap):
    result = []
    for op in circuit["operations"]:
        item = deepcopy(op)
        item["qargs"] = [qmap[q] for q in op.get("qargs", [])]
        item["cargs"] = [cmap[c] for c in op.get("cargs", [])]
        if op.get("condition"):
            item["condition"]["bits"] = [cmap[c] for c in op["condition"]["bits"]]
        item.pop("blocks", None)
        if "blocks" in op:
            item["branches"] = [_global_operations(b, item["qargs"], item["cargs"]) for b in op["blocks"]]
        result.append(item)
    return result


def routing_replay(logical: dict, routed: dict, initial: list[int], final: list[int] | None = None) -> dict:
    """Dependency-preserving replay with original SWAPs consumed before routing SWAPs.

    Reset and measurement are ordinary ordered events. Each conditional branch
    must produce the same exit permutation; an absent else branch is identity.
    Branch-local wire maps are translated to the enclosing physical wires.
    """
    validate_circuit(logical)
    validate_circuit(routed)
    n, physical = logical["num_qubits"], routed["num_qubits"]
    validation = layout_validation(initial, final if final is not None else initial, n, physical)
    if validation["status"] != "verified":
        return validation
    if logical["num_clbits"] != routed["num_clbits"]:
        return {"status": "mismatch", "reason": "classical_width_changed"}
    remaining = [p for p in range(physical) if p not in initial]
    full_initial = [*initial, *remaining]
    p2v = {p: v for v, p in enumerate(full_initial)}
    logical_ops = _global_operations(logical, list(range(n)), list(range(logical["num_clbits"])))
    routed_ops = _global_operations(routed, list(range(physical)), list(range(routed["num_clbits"])))

    def wires(op):
        return set([("q", q) for q in op["qargs"]] + [("c", c) for c in op["cargs"]] +
                   [("c", c) for c in op.get("condition", {}).get("bits", [])])

    def replay(expected, observed, mapping):
        queues = defaultdict(deque)
        expected = [op for op in expected if op["name"] != "barrier"]
        for i, op in enumerate(expected):
            for wire in wires(op):
                queues[wire].append(i)
        consumed, inserted = set(), 0
        for position, original in enumerate(observed):
            if original["name"] == "barrier":
                continue
            if original["name"] in {"while_loop", "for_loop", "switch_case", "box"}:
                return {"status": "unsupported", "reason": "routing_control_flow", "position": position}
            actual = deepcopy(original)
            actual["qargs"] = [mapping[q] for q in actual["qargs"]]
            candidates = sorted({queues[w][0] for w in wires(actual) if queues[w]})
            matched = None
            for candidate in candidates:
                wanted = expected[candidate]
                if not all(queues[w] and queues[w][0] == candidate for w in wires(wanted)):
                    continue
                if "branches" in actual and "branches" in wanted:
                    # Physical branch bodies can contain routing ancillas. The
                    # classical condition and ready logical container identify it.
                    if actual["name"] != wanted["name"] or actual.get("condition") != wanted.get("condition"):
                        continue
                    if not set(wanted["qargs"]).issubset(actual["qargs"]) or actual["cargs"] != wanted["cargs"]:
                        continue
                    if len(actual["branches"]) != len(wanted["branches"]):
                        return {"status": "mismatch", "reason": "branch_count_changed"}
                    exits = []
                    for eb, ob in zip(wanted["branches"], original["branches"], strict=True):
                        branch_mapping = dict(mapping)
                        result = replay(eb, ob, branch_mapping)
                        if result["status"] != "verified":
                            return result
                        inserted += result["inserted_swaps"]
                        exits.append(branch_mapping)
                    if len(exits) == 1:
                        exits.append(dict(mapping))
                    if exits[0] != exits[1]:
                        return {"status": "mismatch", "reason": "branch_exit_permutations_differ"}
                    mapping.update(exits[0])
                    matched = candidate
                    break
                if actual == wanted:
                    matched = candidate
                    break
            if matched is not None:
                consumed.add(matched)
                for wire in wires(expected[matched]):
                    queues[wire].popleft()
            elif original["name"] == "swap" and len(original["qargs"]) == 2 and not original["cargs"] and not original.get("condition") and not original.get("params"):
                a, b = original["qargs"]
                mapping[a], mapping[b] = mapping[b], mapping[a]
                inserted += 1
            else:
                return {"status": "mismatch", "reason": "unreplayable_operation", "position": position, "operation": original["name"]}
        if len(consumed) != len(expected):
            return {"status": "mismatch", "reason": "missing_logical_operations", "count": len(expected) - len(consumed)}
        return {"status": "verified", "inserted_swaps": inserted}

    result = replay(logical_ops, routed_ops, p2v)
    if result["status"] == "verified" and final is not None and any(p2v[p] != v for v, p in enumerate(final)):
        return {"status": "mismatch", "reason": "wrong_final_layout", "inserted_swaps": result["inserted_swaps"]}
    if result["status"] == "verified":
        result.update(covers=["routing", "measurement", "reset", "branch_permutations"], final_mapping=[p2v[p] for p in range(physical)])
    return result


def validate_semantics(logical: dict, output: dict, initial: list[int], final: list[int],
                       max_logical_qubits: int = 4, max_physical_qubits: int = 6) -> dict:
    """Exact bounded instrument comparison on arbitrary logical inputs.

    Every matrix unit |i><j| is propagated through all measurement/reset branches,
    grouped by the final classical register. Ancillas enter in |0>; they are
    traced out using the final layout. This proves the full quantum instrument,
    not just a zero-state output distribution. Limits are an explicit contract.
    """
    from .instrument import instrument_equivalence
    from .isometry import isometry_equivalence

    static = isometry_equivalence(logical, output, initial, final)
    if static["status"] != "unsupported" or static["reason"] != "requires_instrument_oracle":
        return static
    return instrument_equivalence(logical, output, initial, final, max_logical_qubits=max_logical_qubits,
                                  max_physical_qubits=max_physical_qubits)
