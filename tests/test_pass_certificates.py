from types import SimpleNamespace

import pytest

pytest.importorskip("qiskit")
from qiskit import QuantumCircuit
from qiskit.converters import circuit_to_dag

from benchpress_compare.corpus.ir import export_circuit
from benchpress_compare.validation.passes import PassCertificates


def transformation(name):
    return type(name, (), {"is_analysis_pass": False})()


def test_changed_pass_cannot_claim_equivalence_and_retains_evidence():
    original = QuantumCircuit(1)
    original.x(0)
    changed = QuantumCircuit(1)
    changed.z(0)
    artifacts = {}
    chain = PassCertificates(original, {"native_calls": []}, artifacts)
    chain.observe(transformation("FaultyOptimization"), circuit_to_dag(changed), {})
    result = chain.finish(changed)
    assert result["status"] != "verified"
    assert result["unverified"][0]["pass"] == "FaultyOptimization"
    assert artifacts["semantic-passes.json"]["boundaries"][0]["proof"]["status"] != "verified"


def test_valid_pass_chain_does_not_mask_a_wrong_reported_layout():
    original = QuantumCircuit(2)
    original.h(0)
    chain = PassCertificates(original, {"native_calls": []}, {})
    output = original.copy()
    output._layout = SimpleNamespace(
        initial_index_layout=lambda **kwargs: [0, 1],
        final_index_layout=lambda **kwargs: [1, 0],
    )
    result = chain.finish(output)
    assert result["status"] == "mismatch"
    assert result["layout"]["composed_final"] == [0, 1]
    assert result["layout"]["reported_final"] == [1, 0]


def test_native_routing_receipt_must_cover_actual_pass_boundaries():
    original = QuantumCircuit(2)
    original.cx(0, 1)
    changed = original.copy()
    changed.x(1)
    encoded = export_circuit(original)
    artifacts = {"routing-0.json": {
        "before": encoded, "after": encoded, "initial": [0, 1], "final": [0, 1],
    }}
    diagnostics = {"native_calls": []}
    chain = PassCertificates(original, diagnostics, artifacts)
    diagnostics["native_calls"].append({"replay": {"status": "verified"}})
    chain.observe(transformation("SabreLayout"), circuit_to_dag(changed), {})
    result = chain.finish(changed)
    assert result["status"] == "unsupported"
    assert "boundaries differ" in result["unverified"][0]["proof"]["detail"]


def test_composed_numerical_error_cannot_exceed_frozen_tolerance(monkeypatch):
    original = QuantumCircuit(1)
    changed = original.copy()
    changed.x(0)
    chain = PassCertificates(original, {"native_calls": []}, {})
    monkeypatch.setattr(
        "benchpress_compare.validation.passes.local_equivalence",
        lambda *args, **kwargs: {"status": "verified", "error_bound": 6e-9},
    )
    chain.observe(transformation("FirstRewrite"), circuit_to_dag(changed), {})
    chain.observe(transformation("SecondRewrite"), circuit_to_dag(original), {})
    result = chain.finish(original)
    assert result["status"] == "unsupported"
    assert result["error_bound"] == pytest.approx(12e-9)
    assert result["reason"] == "composed_numerical_error_bound_exceeded"


def test_changed_final_output_is_also_checked():
    original = QuantumCircuit(1)
    changed = original.copy()
    changed.x(0)
    chain = PassCertificates(original, {"native_calls": []}, {})
    result = chain.finish(changed)
    assert result["status"] != "verified"
    assert result["unverified"][0]["pass"] == "final_output"


def test_three_cycle_elision_composes_with_physical_layout():
    from benchpress_compare.targets import import_target, synthetic_target
    from benchpress_compare.worker.adapter import compile_observation
    from benchpress_compare.validation import validate_semantics

    original = QuantumCircuit(3)
    original.h(0)
    original.swap(0, 1)
    original.swap(1, 2)
    original.cx(2, 0)
    output, diagnostics, _ = compile_observation(
        original, import_target(synthetic_target("square", 3)), 2, 19
    )
    evidence = diagnostics["semantic_certificate"]
    assert evidence["status"] == "verified"
    assert evidence["layout"]["composed_final"] == output.layout.final_index_layout(True)
    assert validate_semantics(
        export_circuit(original), export_circuit(output),
        output.layout.initial_index_layout(True), output.layout.final_index_layout(True),
    )["status"] == "verified"
