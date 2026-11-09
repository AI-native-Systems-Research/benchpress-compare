"""Exact, directed target snapshots with calibration absence preserved."""

from __future__ import annotations

import importlib.metadata
import math

from benchpress_compare.identity import canonical_hash

TARGET_PROTOCOL = "directed-target-v1"
FAKE_PROVIDER_VERSION = "0.43.1"


def heavy_hex_distance(width: int) -> int:
    if type(width) is not int or width < 1:
        raise ValueError("Target width must be a positive integer")
    distance = 1
    while (5 * distance * distance - 2 * distance - 1) // 2 < width:
        distance += 2
    return distance


def square_edges(width: int) -> tuple[int, list[list[int]]]:
    if type(width) is not int or width < 1:
        raise ValueError("Target width must be a positive integer")
    side = math.isqrt(width)
    if side * side < width:
        side += 1
    edges = []
    for row in range(side):
        for col in range(side):
            q = row * side + col
            for other in ([q + 1] if col + 1 < side else []) + ([q + side] if row + 1 < side else []):
                edges.extend(([q, other], [other, q]))
    return side * side, sorted(edges)


def _seal(data: dict) -> dict:
    data["target_hash"] = canonical_hash({k: v for k, v in data.items() if k not in {"target_hash", "provenance"}})
    return data


def validate_target(data: dict) -> None:
    if data.get("schema_version") != 1 or data.get("protocol") != TARGET_PROTOCOL:
        raise ValueError("Unknown target schema")
    if type(data.get("num_qubits")) is not int or data["num_qubits"] < 1:
        raise ValueError("Invalid target capacity")
    names = set()
    for op in data["instructions"]:
        if op["name"] in names:
            raise ValueError("Duplicate target instruction")
        names.add(op["name"])
        for prop in op["properties"]:
            qs = prop["qargs"]
            if qs is not None and (len(qs) != op["arity"] or len(set(qs)) != len(qs) or
                                   any(type(q) is not int or not 0 <= q < data["num_qubits"] for q in qs)):
                raise ValueError("Invalid directed instruction support")
            for key in ("duration", "error"):
                value = prop[key]
                if value is not None and (not math.isfinite(value) or value < 0):
                    raise ValueError("Invalid instruction calibration")
    if data.get("target_hash") != _seal(dict(data))["target_hash"]:
        raise ValueError("Target content hash mismatch")


def synthetic_target(target_class: str, declared_width: int) -> dict:
    if target_class == "square":
        capacity, edges = square_edges(declared_width)
        dimensions = {"side": math.isqrt(capacity)}
    elif target_class == "heavy-hex":
        import rustworkx
        distance = heavy_hex_distance(declared_width)
        graph = rustworkx.generators.heavy_hex_graph(distance)
        capacity = graph.num_nodes()
        edges = sorted([list(e) for a, b in graph.edge_list() for e in ((a, b), (b, a))])
        dimensions = {"distance": distance}
        if capacity != (5 * distance * distance - 2 * distance - 1) // 2:
            raise ValueError("Heavy-hex generator does not match the frozen capacity formula")
    else:
        raise ValueError(f"Unknown synthetic target class {target_class}")
    instructions = []
    for name in ("cz", "id", "measure", "reset", "rz", "sx", "x"):
        qargs = edges if name == "cz" else [[q] for q in range(capacity)]
        instructions.append({"name": name, "arity": 2 if name == "cz" else 1,
                             "parameters": [{"parameter": "theta"}] if name == "rz" else [],
                             "properties": [{"qargs": qs, "present": False, "duration": None, "error": None} for qs in qargs]})
    instructions.append({"name": "if_else", "arity": None, "parameters": [], "properties": []})
    return _seal({"schema_version": 1, "protocol": TARGET_PROTOCOL, "target_class": target_class,
                  "snapshot_id": f"{target_class}-{capacity}", "num_qubits": capacity, "dimensions": dimensions,
                  "instructions": sorted(instructions, key=lambda x: x["name"]), "native_2q_names": ["cz"],
                  "dt": None, "qubit_properties": None,
                  "timing_constraints": {"granularity": 1, "min_length": 1, "pulse_alignment": 1, "acquire_alignment": 1}})


def export_target(target, target_class: str, snapshot_id: str, provenance: dict | None = None,
                  dimensions: dict | None = None) -> dict:
    from qiskit.circuit import ParameterExpression

    instructions = []
    for name in sorted(target.operation_names):
        op = target.operation_from_name(name)
        variable = isinstance(op, type)
        parameters = [] if variable else [
            {"parameter": str(p)} if isinstance(p, ParameterExpression) and p.parameters else {"value": float(p)}
            for p in op.params
        ]
        spec = {"name": name, "arity": None if variable else op.num_qubits, "parameters": parameters, "properties": []}
        if not variable:
            for qs, prop in sorted(target[name].items(), key=lambda x: (-1,) if x[0] is None else x[0]):
                spec["properties"].append({"qargs": None if qs is None else list(qs), "present": prop is not None,
                                           "duration": None if prop is None else prop.duration,
                                           "error": None if prop is None else prop.error})
        if hasattr(target, "gate_has_angle_bounds") and target.gate_has_angle_bounds(name):
            # This adapter intentionally refuses calibration it cannot round-trip
            # via the supported public Target interface.
            raise ValueError(f"Target angle bounds are unsupported for {name}")
        instructions.append(spec)
    qubit_properties = None if target.qubit_properties is None else [
        {field: getattr(p, field) for field in ("t1", "t2", "frequency")} for p in target.qubit_properties]
    result = {"schema_version": 1, "protocol": TARGET_PROTOCOL, "target_class": target_class,
              "snapshot_id": snapshot_id, "num_qubits": target.num_qubits, "dimensions": dimensions or {},
              "instructions": instructions, "native_2q_names": ["cz"], "dt": target.dt,
              "qubit_properties": qubit_properties,
              "timing_constraints": {key: getattr(target, key) for key in ("granularity", "min_length", "pulse_alignment", "acquire_alignment")}}
    if provenance is not None:
        result["provenance"] = provenance
    return _seal(result)


def import_target(data: dict):
    from qiskit.circuit import Parameter, Delay
    from qiskit.circuit import controlflow
    from qiskit.circuit.library import get_standard_gate_name_mapping
    from qiskit.providers import QubitProperties
    from qiskit.transpiler import Target, InstructionProperties

    validate_target(data)
    target = Target(num_qubits=data["num_qubits"], dt=data["dt"],
                    qubit_properties=None if data["qubit_properties"] is None else [QubitProperties(**p) for p in data["qubit_properties"]],
                    **data["timing_constraints"])
    standard = get_standard_gate_name_mapping()
    variable = {"if_else": "IfElseOp", "for_loop": "ForLoopOp", "while_loop": "WhileLoopOp",
                "switch_case": "SwitchCaseOp", "break_loop": "BreakLoopOp", "continue_loop": "ContinueLoopOp", "box": "BoxOp"}
    for spec in data["instructions"]:
        name = spec["name"]
        if spec["arity"] is None:
            if name not in variable or not hasattr(controlflow, variable[name]):
                raise ValueError(f"Build does not support target control flow {name}")
            target.add_instruction(getattr(controlflow, variable[name]), name=name)
            continue
        params = [Parameter(p["parameter"]) if "parameter" in p else p["value"] for p in spec["parameters"]]
        if name == "delay":
            op = Delay(*params)
        elif name in standard:
            op = standard[name].base_class(*params)
        else:
            raise ValueError(f"Build does not support target operation {name}")
        properties = {None if p["qargs"] is None else tuple(p["qargs"]):
                      InstructionProperties(duration=p["duration"], error=p["error"]) if p["present"] else None
                      for p in spec["properties"]}
        target.add_instruction(op, properties=properties, name=name)
    reproduced = export_target(target, data["target_class"], data["snapshot_id"], dimensions=data.get("dimensions"))
    if reproduced["target_hash"] != data["target_hash"]:
        raise ValueError("Target import changes instruction/calibration identity")
    return target


def freeze_fake_torino() -> dict:
    version = importlib.metadata.version("qiskit-ibm-runtime")
    if version != FAKE_PROVIDER_VERSION:
        raise ValueError(f"FakeTorino provider must be pinned to {FAKE_PROVIDER_VERSION}; found {version}")
    from qiskit_ibm_runtime.fake_provider import FakeTorino

    backend = FakeTorino()
    snapshot = export_target(backend.target, "fake_torino", f"fake-torino-runtime-{version}",
                             {"package": "qiskit-ibm-runtime", "version": version, "backend": type(backend).__name__})
    if snapshot["num_qubits"] != 133 or "cz" not in {i["name"] for i in snapshot["instructions"]}:
        raise ValueError("Frozen FakeTorino must be a 133-qubit native-CZ target")
    import_target(snapshot)
    return snapshot
