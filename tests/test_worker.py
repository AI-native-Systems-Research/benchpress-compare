import json
import subprocess
import sys

import pytest

from benchpress_compare.identity import atomic_write_json, canonical_hash


def test_native_budget_and_instrumentation_do_not_change_compilation():
    qiskit = pytest.importorskip("qiskit")
    from benchpress_compare.targets import synthetic_target, import_target
    from benchpress_compare.worker.adapter import compile_observation, preset
    from benchpress_compare.corpus.ir import export_circuit

    circuit = qiskit.QuantumCircuit(4)
    for _ in range(3):
        for a, b in [(0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3)]:
            circuit.cx(a, b)
    target = import_target(synthetic_target("square", 4))
    output, diagnostics, artifacts = compile_observation(circuit, target, 2, 1234)
    control, _ = preset(target, 2, 1234)
    clean = control.run(circuit)
    assert export_circuit(output) == export_circuit(clean)
    assert output.layout.initial_index_layout(True) == clean.layout.initial_index_layout(True)
    assert output.layout.final_index_layout(True) == clean.layout.final_index_layout(True)
    assert diagnostics["sabre_executed"]
    assert diagnostics["inserted_swaps"] > 0
    assert diagnostics["native_calls"][0]["effective_trials"] == {
        "num_random_trials": 20,
        "num_swap_trials": 20,
    }
    assert diagnostics["protocol"] == "sabre-native-counts-v3"
    assert "routing_replay" not in diagnostics
    assert "semantic_certificate" not in diagnostics
    assert set(artifacts) == {"initialization.json", "pre-layout.json"}


def test_native_swap_counts_preserve_original_and_branch_swaps(monkeypatch):
    qiskit = pytest.importorskip("qiskit")
    import importlib
    from qiskit.circuit import Gate
    from qiskit.circuit.controlflow import IfElseOp
    from qiskit.circuit.library import SwapGate
    from qiskit.converters import circuit_to_dag
    from benchpress_compare.worker.adapter import native_instrumentation

    circuit = qiskit.QuantumCircuit(3, 1)
    circuit.swap(0, 1)
    with circuit.if_test((circuit.clbits[0], 1)):
        circuit.swap(1, 2)
    # The name alone must not make a custom operation a standard SWAP.
    circuit.append(Gate("swap", 2, []), [0, 2])
    dag = circuit_to_dag(circuit)

    def routing(dag, num_trials):
        dag.apply_operation_back(SwapGate(), [dag.qubits[0], dag.qubits[2]])
        block = qiskit.QuantumCircuit(2)
        block.swap(0, 1)
        dag.apply_operation_back(
            IfElseOp((dag.clbits[0], 1), block), [dag.qubits[1], dag.qubits[2]]
        )
        return dag, None

    module = importlib.import_module("qiskit.transpiler.passes.routing.sabre_swap")
    monkeypatch.setattr(module, "sabre_routing", routing)
    diagnostics, artifacts = {"native_calls": []}, {}
    with native_instrumentation(diagnostics, artifacts, {"status": "not_requested"}):
        module.sabre_routing(dag, num_trials=20)
    call = diagnostics["native_calls"][0]
    assert call["explicit_swaps_before"] == 2
    assert call["explicit_swaps_after"] == 4
    assert call["inserted_swaps"] == 2
    assert "replay" not in call
    assert artifacts == {}


def test_original_swap_elision_is_separate_from_inserted_routing_swaps():
    qiskit = pytest.importorskip("qiskit")
    from benchpress_compare.targets import synthetic_target, import_target
    from benchpress_compare.worker.adapter import compile_observation

    circuit = qiskit.QuantumCircuit(3)
    circuit.h(0)
    circuit.swap(0, 1)
    circuit.cx(1, 2)
    _, diagnostics, _ = compile_observation(
        circuit, import_target(synthetic_target("square", 3)), 2, 19
    )
    assert diagnostics["original_explicit_swaps"] == 1
    assert diagnostics["elided_permutations"]
    assert diagnostics["inserted_swaps"] == 0


def test_initialization_snapshot_is_captured_once_before_later_dag_mutations(monkeypatch):
    qiskit = pytest.importorskip("qiskit")
    from qiskit.circuit.library import XGate
    from qiskit.transpiler import AnalysisPass, PassManager, StagedPassManager, TransformationPass
    from benchpress_compare.corpus.ir import export_circuit
    from benchpress_compare.metrics import structural_signature
    from benchpress_compare.worker import adapter

    class AppendIdentity(TransformationPass):
        def run(self, dag):
            dag.apply_operation_back(XGate(), [dag.qubits[0]])
            dag.apply_operation_back(XGate(), [dag.qubits[0]])
            return dag

    class Unchanged(TransformationPass):
        def run(self, dag):
            return dag

    class Inspect(AnalysisPass):
        def run(self, dag):
            self.property_set["inspected"] = True

    class RemoveIdentity(TransformationPass):
        def run(self, dag):
            for node in list(dag.topological_op_nodes())[-2:]:
                dag.remove_op_node(node)
            return dag

    manager = StagedPassManager(
        stages=("init", "optimization"),
        init=PassManager([AppendIdentity(), Inspect(), Unchanged()]),
        optimization=PassManager([RemoveIdentity()]),
    )
    monkeypatch.setattr(adapter, "preset", lambda *args: (manager, []))
    exports, signatures = [], []

    def tracked_export(circuit):
        exports.append(circuit)
        return export_circuit(circuit)

    def tracked_signature(circuit):
        signatures.append(circuit)
        return structural_signature(circuit)

    monkeypatch.setattr(adapter, "export_circuit", tracked_export)
    monkeypatch.setattr(adapter, "structural_signature", tracked_signature)
    original = qiskit.QuantumCircuit(2)
    original.cx(0, 1)
    initialized = original.copy()
    initialized.x(0)
    initialized.x(0)
    output, diagnostics, artifacts = adapter.compile_observation(original, None, 2, 1)
    assert export_circuit(output) == export_circuit(original)
    assert artifacts["initialization.json"] == export_circuit(initialized)
    assert diagnostics["pre_layout_structure_hash"] == canonical_hash(
        structural_signature(export_circuit(initialized))
    )
    assert diagnostics["pre_layout_two_qubit_count"] == 1
    assert [row["name"] for row in diagnostics["passes"]] == [
        "AppendIdentity", "Inspect", "Unchanged", "RemoveIdentity",
    ]
    # Only the completed initialization stage needs serialization and hashing.
    assert len(exports) == 1
    assert len(signatures) == 1


def test_worker_fresh_process_receipt_and_artifacts(tmp_path):
    qiskit = pytest.importorskip("qiskit")
    from benchpress_compare.targets import synthetic_target
    from benchpress_compare.corpus.ir import export_circuit
    from benchpress_compare.identity import file_hash

    q = qiskit.QuantumCircuit(2)
    q.h(0)
    q.cx(0, 1)
    logical = export_circuit(q)
    fixture = {"schema_version": 1, "logical": logical, "fixture_hash": canonical_hash(logical)}
    atomic_write_json(tmp_path / "fixture.json", fixture)
    target = synthetic_target("square", 2)
    atomic_write_json(tmp_path / "target.json", target)
    spec = {
        "schema_version": 1,
        "task": "observation",
        "observation_id": "observation",
        "case_id": "case",
        "arm": "baseline",
        "phase": "preflight",
        "build_id": "integration-test",
        "attempt_id": "attempt",
        "run_id": "run",
        "submission_token": "token",
        "plan_hash": None,
        "lease_generation": 1,
        "seed": 12,
        "level": 2,
        "runtime_profile": {"native_threads": 1},
        "fixture_path": str(tmp_path / "fixture.json"),
        "target_path": str(tmp_path / "target.json"),
        "output_dir": str(tmp_path / "attempt"),
    }
    atomic_write_json(tmp_path / "spec.json", spec)
    process = subprocess.run(
        [sys.executable, "-m", "benchpress_compare.worker", str(tmp_path / "spec.json")],
        cwd=tmp_path,
        text=True,
        capture_output=True,
    )
    receipt = json.loads((tmp_path / "attempt" / "receipt.json").read_text())
    assert process.returncode == 0, receipt
    assert receipt["status"] == "SUCCEEDED"
    assert receipt["validation"]["status"] == "verified"
    assert set(receipt["validation"]["checks"]) == {"target_legality"}
    assert "semantic-passes.json" not in receipt["artifacts"]
    assert not any(name.startswith("routing-") for name in receipt["artifacts"])
    assert receipt["N2"] == 1
    for field in (
        "observation_id",
        "case_id",
        "attempt_id",
        "plan_hash",
        "lease_generation",
        "runtime_profile",
        "submission_token",
    ):
        assert receipt[field] == spec[field]
    for path, digest in receipt["artifacts"].items():
        assert file_hash(tmp_path / "attempt" / path) == digest


def test_invalid_fixture_never_compiles_or_publishes_success(tmp_path):
    pytest.importorskip("qiskit")
    from benchpress_compare.worker import run_spec

    atomic_write_json(
        tmp_path / "fixture.json", {"schema_version": 1, "logical": {}, "fixture_hash": "wrong"}
    )
    receipt = run_spec(
        {
            "fixture_path": str(tmp_path / "fixture.json"),
            "output_dir": str(tmp_path / "attempt"),
            "task": "observation",
        }
    )
    assert receipt["status"] == "HARNESS_FAILED"
    assert "hash/schema mismatch" in receipt["error"]


def test_attempt_receipt_is_immutable(tmp_path):
    from benchpress_compare.worker import run_spec

    (tmp_path / "receipt.json").write_text("{}")
    with pytest.raises(ValueError, match="immutable"):
        run_spec({"output_dir": str(tmp_path)})


@pytest.mark.parametrize("native", [True, False])
def test_observation_checks_only_output_target_legality(tmp_path, monkeypatch, native):
    qiskit = pytest.importorskip("qiskit")
    from benchpress_compare.corpus.ir import export_circuit
    from benchpress_compare.targets import synthetic_target
    from benchpress_compare.worker import _run_task

    original = qiskit.QuantumCircuit(2)
    original.h(0)
    compiled = qiskit.QuantumCircuit(2)
    if native:
        compiled.x(0)
    else:
        compiled.h(0)
    encoded = export_circuit(original)
    atomic_write_json(tmp_path / "fixture.json", {
        "schema_version": 1, "logical": encoded, "fixture_hash": canonical_hash(encoded),
    })
    atomic_write_json(tmp_path / "target.json", synthetic_target("square", 2))
    monkeypatch.setattr(
        "benchpress_compare.worker.adapter.compile_observation",
        lambda *args, **kwargs: (compiled, {}, {}),
    )
    result, _ = _run_task({
        "fixture_path": str(tmp_path / "fixture.json"),
        "target_path": str(tmp_path / "target.json"), "level": 2, "seed": 1,
    }, tmp_path)
    assert result["status"] == ("SUCCEEDED" if native else "INVALID")
    assert set(result["validation"]["checks"]) == {"target_legality"}


def test_runtime_attestation_checks_host_abi_and_machine(tmp_path):
    qiskit = pytest.importorskip("qiskit")
    import importlib.metadata
    import platform
    import sysconfig
    from pathlib import Path
    from benchpress_compare.identity import file_hash
    from benchpress_compare.worker import _attest_runtime

    root = Path(sys.prefix).resolve()
    native = Path(importlib.import_module("qiskit._accelerate").__file__).resolve()
    attestation = {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "abi": sysconfig.get_config_var("SOABI"),
        "platform": sysconfig.get_platform(),
        "architecture": platform.machine(),
        "libc": list(platform.libc_ver()),
        "os": platform.system(),
        "qiskit_origin": str(Path(qiskit.__file__).resolve()),
        "accelerator_origin": str(native),
        "release_log_verified": True,
        "native_hashes": {str(native.relative_to(root)): file_hash(native)},
        "dependencies": sorted(
            (d.metadata["Name"].lower().replace("_", "-"), d.version)
            for d in importlib.metadata.distributions()
        ),
    }
    manifest = {"env_python": str(root / "bin/python"), "attestation": attestation}
    path = tmp_path / "manifest.json"
    atomic_write_json(path, manifest)
    spec = {
        "manifest_path": str(path),
        "runtime_profile": {"machine": f"{platform.system()}-{platform.machine()}"},
    }
    assert _attest_runtime(spec, qiskit)["build_attestation"] == "verified"
    with pytest.raises(ValueError, match="machine class"):
        _attest_runtime(dict(spec, runtime_profile={"machine": "wrong-machine"}), qiskit)
    manifest["attestation"]["abi"] = "incompatible-abi"
    atomic_write_json(path, manifest)
    with pytest.raises(ValueError, match="host abi"):
        _attest_runtime(spec, qiskit)


@pytest.mark.integration
def test_execution_engine_runs_real_aa_and_reuses_only_committed_baseline(tmp_path, monkeypatch):
    qiskit = pytest.importorskip("qiskit")
    from benchpress_compare.config import RunConfig
    from benchpress_compare.corpus.ir import export_circuit
    from benchpress_compare.orchestrator import ExecutionEngine, read_results, work_key
    from benchpress_compare.pilot import runtime_profile
    from benchpress_compare.schema import PROTOCOLS
    from benchpress_compare.store import Lease
    from benchpress_compare.targets import synthetic_target

    monkeypatch.setattr(
        "benchpress_compare.orchestrator.inventory",
        lambda **_: {
            "usable_cpus": 4,
            "worker_memory_budget_bytes": 4 * 1024**3,
        },
    )
    circuit = qiskit.QuantumCircuit(3)
    circuit.h(0)
    circuit.cx(0, 1)
    circuit.cx(0, 2)
    logical = export_circuit(circuit)
    fixture = {"schema_version": 1, "logical": logical, "fixture_hash": canonical_hash(logical)}
    target = synthetic_target("square", 3)
    atomic_write_json(tmp_path / "fixture.json", fixture)
    atomic_write_json(tmp_path / "target.json", target)
    profile = runtime_profile(memory_gb=1, wall_seconds=60)
    spec = {
        "schema_version": 1,
        "observation_id": canonical_hash("real-aa-worker"),
        "case_id": "real-aa-case",
        "task": "observation",
        "phase": "comparison",
        "seed": 123,
        "level": 2,
        "build_id": "adapter-integration-only",
        "protocols": PROTOCOLS,
        "runtime_profile": profile,
        "fixture_path": str(tmp_path / "fixture.json"),
        "target_path": str(tmp_path / "target.json"),
        "python": sys.executable,
    }
    root = tmp_path / "first"
    root.mkdir()
    config = RunConfig(
        "/unused/baseline", "/unused/evolve", str(tmp_path / "store"), str(root), workers="1"
    )
    specs = [dict(spec, arm=arm) for arm in ("baseline", "evolve")]
    with Lease(root / "state/controller.lease") as lease:
        engine = ExecutionEngine(root, config, lease, plan_hash="adapter-integration-plan")
        first = engine.run(specs)
        attempts_before = sorted(str(p) for p in (root / "attempts").glob("*/*/attempt.json"))
        resumed = engine.run(specs)
    assert len(attempts_before) == 2
    assert attempts_before == sorted(str(p) for p in (root / "attempts").glob("*/*/attempt.json"))
    assert all(row["status"] == "SUCCEEDED" for row in first), first
    assert first == resumed
    assert first[0]["N2"] == first[1]["N2"] == 2
    assert first[0]["D2"] == first[1]["D2"]
    assert len(read_results(root)) == 2
    second_root = tmp_path / "second"
    second_root.mkdir()
    second_config = RunConfig(
        "/unused/baseline", "/unused/evolve", str(tmp_path / "store"), str(second_root), workers="1"
    )
    with Lease(second_root / "state/controller.lease") as lease:
        reused = ExecutionEngine(second_root, second_config, lease, plan_hash="second-plan").run(
            specs
        )
    assert reused[0]["cache_hit"] is True
    assert reused[1]["cache_hit"] is False
    assert len(list((second_root / "attempts" / work_key(specs[0])).glob("*/attempt.json"))) == 0
    assert len(list((second_root / "attempts" / work_key(specs[1])).glob("*/attempt.json"))) == 1
