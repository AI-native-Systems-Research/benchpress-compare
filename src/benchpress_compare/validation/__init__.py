"""Recursive native target legality for accepted benchmark observations."""

from __future__ import annotations

from benchpress_compare.metrics import validate_circuit
from benchpress_compare.targets import validate_target

VALIDATION_PROTOCOL = "target-legality-v4"
REQUIRED_CHECKS = ("target_legality",)
DISCLOSED_CHECKS = ()


def summarize_validation(checks: dict) -> dict:
    """Combine check evidence under the frozen contract.

    Target legality must be ``verified``. Unsupported legality never passes.
    Conclusive negatives from additional checks also invalidate the observation.
    """
    combined = {}
    statuses = set()
    for name, check in checks.items():
        if not isinstance(check, dict) or check.get("status") not in {
            "verified",
            "mismatch",
            "unsupported",
            "error",
        }:
            raise ValueError(f"Malformed validation check: {name}")
        record = dict(check)
        record["required"] = name in REQUIRED_CHECKS or record["status"] != "unsupported"
        if record["required"]:
            statuses.add(record["status"])
        combined[name] = record
    if any(name not in combined for name in REQUIRED_CHECKS):
        statuses.add("error")
    status = next((s for s in ("error", "mismatch", "unsupported") if s in statuses), "verified")
    return {
        "schema_version": 1,
        "protocol": VALIDATION_PROTOCOL,
        "status": status,
        "required_checks": list(REQUIRED_CHECKS),
        "disclosed_checks": list(DISCLOSED_CHECKS),
        "checks": combined,
    }


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
                if name == "barrier" and op.get("kind", "barrier") == "barrier":
                    continue
                if name not in operations:
                    return {
                        "status": "mismatch",
                        "reason": "unsupported_target_instruction",
                        "operation": name,
                        "path": [*path, index],
                    }
                support = operations[name]
                expected_kind = "if_else" if name == "if_else" else "delay" if name == "delay" else "standard"
                if op.get("kind", expected_kind) != expected_kind:
                    return {"status": "mismatch", "reason": "non_native_operation_identity",
                            "operation": name, "kind": op.get("kind")}
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
                elif not any(
                    p["qargs"] is None or p["qargs"] == physical for p in support["properties"]
                ):
                    return {
                        "status": "mismatch",
                        "reason": "illegal_directed_qargs",
                        "operation": name,
                        "qargs": physical,
                        "path": [*path, index],
                    }
                fixed = [p.get("value") for p in support["parameters"]]
                if len(fixed) != len(op.get("params", [])):
                    return {"status": "mismatch", "reason": "parameter_arity"}
                if any(v is not None and v != op["params"][i] for i, v in enumerate(fixed)):
                    return {"status": "mismatch", "reason": "fixed_target_parameter"}
            return None

        return walk(circuit, list(range(circuit["num_qubits"])), []) or {
            "status": "verified",
            "covers": ["recursive_native_legality", "directed_instruction_support"],
        }
    except (ValueError, KeyError, TypeError) as error:
        return {"status": "error", "reason": "malformed_artifact", "detail": str(error)}
