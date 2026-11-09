"""Observation-scoped preset adapter with native-boundary trial attestation."""

from __future__ import annotations

import importlib
import inspect
from contextlib import contextmanager

from benchpress_compare.corpus.ir import export_circuit
from benchpress_compare.identity import canonical_hash
from benchpress_compare.metrics import structural_signature
from benchpress_compare.validation import routing_replay

INSTRUMENTATION_PROTOCOL = "sabre-native-call-v1"


class AdapterIncompatible(RuntimeError):
    pass


def _passes(controller):
    if hasattr(controller, "tasks"):
        for task in controller.tasks:
            yield from _passes(task)
    else:
        yield controller


def preset(target, level, seed):
    from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager

    if level not in (2, 3):
        raise ValueError("The standard design supports optimization levels 2 and 3")
    manager = generate_preset_pass_manager(
        optimization_level=level, target=target, seed_transpiler=seed, approximation_degree=1.0
    )
    configured = []
    for pass_ in _passes(manager.to_flow_controller()):
        name = type(pass_).__name__
        fields = (
            ("layout_trials", "swap_trials")
            if name == "SabreLayout"
            else ("trials",)
            if name == "SabreSwap"
            else ()
        )
        for field in fields:
            if not hasattr(pass_, field):
                raise AdapterIncompatible(f"{name} lacks supported budget field {field}")
            setattr(pass_, field, 20)
        if fields:
            configured.append(
                {
                    "pass": name,
                    **{f: getattr(pass_, f) for f in fields},
                    "max_iterations": getattr(pass_, "max_iterations", None),
                    "skip_routing": getattr(pass_, "skip_routing", None),
                }
            )
    return manager, configured


@contextmanager
def native_instrumentation(diagnostics: dict, artifacts: dict):
    """Inspect actual native calls, not just configurable Python attributes.

    A build with a changed native-call ABI is a declared adapter incompatibility.
    These wrappers forward exactly the original arguments, unchanged.
    """
    from qiskit.converters import dag_to_circuit

    restored = []
    definitions = [
        (
            "qiskit.transpiler.passes.layout.sabre_layout",
            "sabre_layout_and_routing",
            {"num_random_trials": 20, "num_swap_trials": 20},
        ),
        ("qiskit.transpiler.passes.routing.sabre_swap", "sabre_routing", {"num_trials": 20}),
    ]
    for module_name, function_name, expected in definitions:
        module = importlib.import_module(module_name)
        function = getattr(module, function_name, None)
        if function is None:
            raise AdapterIncompatible(
                f"Unsupported native SABRE interface: {module_name}.{function_name}"
            )
        signature = inspect.signature(function)
        if not set(expected).issubset(signature.parameters):
            raise AdapterIncompatible(f"Unknown SABRE trial ABI: {signature}")

        def wrap(
            *args,
            __function=function,
            __signature=signature,
            __name=function_name,
            __expected=expected,
            **kwargs,
        ):
            bound = __signature.bind(*args, **kwargs)
            bound.apply_defaults()
            for name, value in __expected.items():
                if bound.arguments[name] != value:
                    raise AdapterIncompatible(
                        f"Executed {__name} {name}={bound.arguments[name]}, expected {value}"
                    )
            before_dag = bound.arguments["dag"]
            before = export_circuit(dag_to_circuit(before_dag))
            before_qubits = before_dag.num_qubits()
            native = {
                "function": __name,
                "effective_trials": dict(__expected),
                "seed": bound.arguments.get("seed"),
                "max_iterations": bound.arguments.get("max_iterations"),
                "partial_start_count": len(bound.arguments.get("partial_layouts", [])),
                "skip_routing": bound.arguments.get("skip_routing", False),
            }
            result = __function(*args, **kwargs)
            index = len(diagnostics["native_calls"])
            if __name == "sabre_layout_and_routing":
                after_dag, initial, final = result
            else:
                after_dag, final = result
                initial = bound.arguments["initial_layout"]
            if __name == "sabre_layout_and_routing":
                diagnostics["pre_layout_structure_hash"] = canonical_hash(
                    structural_signature(before)
                )
                artifacts["pre-layout.json"] = before
            if not native["skip_routing"]:
                after = export_circuit(dag_to_circuit(after_dag))
                initial_map = [initial.virtual_to_physical(q) for q in range(before_qubits)]
                final_map = [final.virtual_to_physical(q) for q in range(before_qubits)]
                replay = routing_replay(before, after, initial_map, final_map)
                native["replay"] = replay
                artifact = {
                    "schema_version": 1,
                    "before": before,
                    "after": after,
                    "initial": initial_map,
                    "final": final_map,
                    "replay": replay,
                    "native_call": __name,
                }
                artifacts[f"routing-{index}.json"] = artifact
            diagnostics["native_calls"].append(native)
            return result

        restored.append((module, function_name, function))
        setattr(module, function_name, wrap)
    try:
        yield
    finally:
        for module, name, original in reversed(restored):
            setattr(module, name, original)


def compile_observation(circuit, target, level, seed):
    from qiskit.converters import dag_to_circuit

    manager, configured = preset(target, level, seed)
    original = export_circuit(circuit)

    def original_swaps(body):
        return sum(
            int(op["name"] == "swap") + sum(original_swaps(b) for b in op.get("blocks", []))
            for op in body["operations"]
        )

    diagnostics = {
        "schema_version": 1,
        "protocol": INSTRUMENTATION_PROTOCOL,
        "configured_passes": configured,
        "native_calls": [],
        "passes": [],
        "sabre_executed": False,
        "inserted_swaps": None,
        "original_explicit_swaps": original_swaps(original),
        "elided_permutations": [],
    }
    artifacts = {}
    stage_map = {}
    for stage in manager.expanded_stages:
        stage_manager = getattr(manager, stage)
        if stage_manager is not None:
            for pass_ in _passes(stage_manager.to_flow_controller()):
                stage_map[id(pass_)] = stage

    def callback(**kwargs):
        pass_, dag = kwargs["pass_"], kwargs["dag"]
        name = type(pass_).__name__
        stage = stage_map.get(id(pass_))
        diagnostics["passes"].append({"name": name, "stage": stage, "seconds": kwargs.get("time")})
        if name in {"SabreLayout", "SabreSwap"}:
            diagnostics["sabre_executed"] = True
        if stage == "init":
            initialized = export_circuit(dag_to_circuit(dag))
            diagnostics["pre_layout_structure_hash"] = canonical_hash(
                structural_signature(initialized)
            )
            artifacts["initialization.json"] = initialized
        if name in {"VF2Layout", "VF2PostLayout"}:
            diagnostics.setdefault("vf2", []).append(
                {"pass": name, "status": str(kwargs["property_set"].get("VF2Layout_stop_reason"))}
            )
        permutation = kwargs["property_set"].get("virtual_permutation_layout")
        if permutation is not None:
            indices = kwargs["property_set"].get("original_qubit_indices") or {
                q: i for i, q in enumerate(circuit.qubits)
            }
            mapped = sorted([[indices[q], p] for q, p in permutation.get_virtual_bits().items()])
            if (
                not diagnostics["elided_permutations"]
                or diagnostics["elided_permutations"][-1]["mapping"] != mapped
            ):
                diagnostics["elided_permutations"].append(
                    {
                        "after_pass": name,
                        "mapping": mapped,
                        "convention": "virtual wire at boundary to original input index",
                    }
                )

    with native_instrumentation(diagnostics, artifacts):
        output = manager.run(circuit, callback=callback)
    replay = [call["replay"] for call in diagnostics["native_calls"] if "replay" in call]
    if diagnostics["sabre_executed"] and not diagnostics["native_calls"]:
        raise AdapterIncompatible("Executed SABRE pass did not enter an attested native boundary")
    if replay and all(r["status"] == "verified" for r in replay):
        diagnostics["inserted_swaps"] = sum(r["inserted_swaps"] for r in replay)
        diagnostics["routing_replay"] = {
            "status": "verified",
            "covers": ["all_executed_sabre_boundaries"],
            "boundaries": len(replay),
        }
    elif replay:
        diagnostics["routing_replay"] = next(r for r in replay if r["status"] != "verified")
    elif not diagnostics["sabre_executed"]:
        # Bypass is valid for evolve. A full end-to-end semantic certificate is
        # still required before this observation is accepted.
        diagnostics["inserted_swaps"] = 0
        diagnostics["routing_replay"] = {
            "status": "verified",
            "covers": [],
            "reason": "sabre_bypassed_no_boundary",
        }
    else:
        diagnostics["routing_replay"] = {"status": "unsupported", "reason": "no_routing_boundary"}
    before = artifacts.get("pre-layout.json", artifacts.get("initialization.json"))
    workload = len(structural_signature(before)) if before is not None else None
    diagnostics["pre_layout_two_qubit_count"] = workload
    swaps = diagnostics["inserted_swaps"]
    diagnostics["relative_routing_burden"] = {
        "inserted_swaps": swaps,
        "pre_layout_two_qubit_count": workload,
        "ratio": swaps / workload if swaps is not None and workload else None,
        "reason": "zero_pre_layout_workload"
        if workload == 0
        else "unavailable_evidence"
        if workload is None or swaps is None
        else None,
    }
    return output, diagnostics, artifacts
