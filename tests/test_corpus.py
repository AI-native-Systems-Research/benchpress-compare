from collections import Counter

import pytest

from benchpress_compare.corpus import CORPUS_REVISION, load_catalogue, verify_corpus, width_band


def test_reviewed_inventory_and_source_totals():
    data = load_catalogue()
    assert data["revision"] == CORPUS_REVISION
    assert len(data["candidates"]) == 149
    main = [r for r in data["candidates"] if r["category"] == "main"]
    assert Counter(r["input_id"][0] for r in main) == {"Q": 44, "F": 32, "S": 6, "H": 56}
    assert Counter(r["family"] for r in main) == {"F1": 6, "F2": 32, "F3": 27, "F4": 6, "F5": 25, "F6": 18, "F7": 20, "F8": 4}
    assert sum("structural_canonical" in r for r in data["candidates"]) == 5
    assert len(data["coverage"]) == 8
    assert sum(len(r["aliases"]) for r in main) == 2


def test_all_pinned_assets_and_hamlib_identities_are_explicit():
    for row in load_catalogue()["candidates"]:
        source = row["source"]
        assert len(source["sha256"]) == 64
        if source["kind"] == "hamlib":
            assert "ham_" + source["ham_instance"][1:-1] == source["base_id"]
            assert source["ham_qubits"] == row["declared_width"]
    rows = {r["input_id"]: r for r in load_catalogue()["candidates"]}
    assert rows["Q/large/32"]["source"]["path"].endswith("QV_n32/32.qasm")
    assert rows["Q/medium/gcm_h6"]["source"]["path"].endswith("gcm_n13/gcm_h6.qasm")
    assert rows["Q/large/knn_129"]["source"]["path"].endswith("knn_n129/knn_129.qasm")


@pytest.mark.parametrize("width,band", [(8, "S"), (24, "S"), (25, "M"), (64, "M"), (65, "L"), (133, "L")])
def test_bands(width, band):
    assert width_band(width) == band


def test_wrong_asset_hash_is_refused(tmp_path):
    catalogue = load_catalogue()
    first = catalogue["candidates"][0]["source"]
    path = tmp_path / first["path"]
    path.parent.mkdir(parents=True)
    path.write_text("altered source")
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_corpus(tmp_path)


def test_dynamic_and_custom_definition_roundtrip():
    qiskit = pytest.importorskip("qiskit")
    from benchpress_compare.corpus.ir import export_circuit, import_circuit

    q = qiskit.QuantumCircuit(3, 1)
    q.h(0)
    q.measure(0, 0)
    with q.if_test((q.clbits[0], 1)):
        q.cx(1, 2)
        q.reset(0)
    encoded = export_circuit(q)
    assert export_circuit(import_circuit(encoded)) == encoded
    custom = qiskit.QuantumCircuit(2, name="custom")
    custom.ry(0.13, 0)
    custom.cx(0, 1)
    outer = qiskit.QuantumCircuit(2)
    outer.append(custom.to_gate(), [0, 1])
    encoded = export_circuit(outer)
    assert encoded["operations"][0]["kind"] == "definition"
    assert export_circuit(import_circuit(encoded)) == encoded


def test_pauli_order_coefficients_and_synthesis_are_preserved():
    qiskit = pytest.importorskip("qiskit")
    from qiskit.circuit.library import PauliEvolutionGate
    from qiskit.quantum_info import SparsePauliOp
    from qiskit.synthesis import LieTrotter
    from benchpress_compare.corpus.ir import export_circuit, import_circuit

    q = qiskit.QuantumCircuit(2)
    q.append(PauliEvolutionGate(SparsePauliOp.from_list([("ZZ", 0.7), ("XI", -0.31)]), time=1,
                                synthesis=LieTrotter(reps=1, preserve_order=True)), [0, 1])
    encoded = export_circuit(q)
    assert encoded["operations"][0]["groups"][0] == [["ZZ", 0.7, 0.0], ["XI", -0.31, 0.0]]
    assert export_circuit(import_circuit(encoded)) == encoded


def test_unbound_and_loop_fixtures_are_rejected():
    qiskit = pytest.importorskip("qiskit")
    from qiskit.circuit import Parameter
    from benchpress_compare.corpus.ir import export_circuit, UnsupportedCircuit
    q = qiskit.QuantumCircuit(1)
    q.rx(Parameter("theta"), 0)
    with pytest.raises(UnsupportedCircuit, match="binding"):
        export_circuit(q)
