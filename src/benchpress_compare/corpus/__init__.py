"""Pinned native-corpus catalogue and reproducible fixture construction.

Summit BV recipes follow Qiskit/benchpress at CORPUS_REVISION,
benchpress/qiskit_gym/circuits/circuits.py and device_transpile/test_summit.py.
Those recipes are Copyright 2017 IBM and its contributors, Apache-2.0;
see data/LICENSE-BENCHPRESS.txt. Integration and serialization are new code.
"""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import math
import subprocess
from pathlib import Path

from benchpress_compare.identity import atomic_write_json, canonical_hash, file_hash
from benchpress_compare.metrics import active_qubits
from .ir import export_circuit, import_circuit

CORPUS_REVISION = "4c04b38a53fc231a89f1a6bc9541c1b641ed6dcf"
HAMLIB_SHA256 = "c4b460e790f4c2ddcd4ff0220bcdf65d52193155c281fad3e5cdafb4d5c2516c"


def load_catalogue(path: str | Path | None = None) -> dict:
    if path is None:
        resource = importlib.resources.files("benchpress_compare").joinpath("data/candidates.json")
        if resource.is_file():
            data = json.loads(resource.read_text())
        else:
            path = Path(__file__).resolve().parents[3] / "data" / "candidates.json"
            data = json.loads(path.read_text())
    else:
        data = json.loads(Path(path).read_text())
    if data.get("schema_version") != 1 or data.get("revision") != CORPUS_REVISION:
        raise ValueError("Unsupported catalogue schema or corpus revision")
    candidates = data["candidates"]
    if len(candidates) != 149 or len({c["input_id"] for c in candidates}) != 149:
        raise ValueError("The standard catalogue requires 149 distinct candidates")
    if sum(c["category"] == "main" for c in candidates) != 138:
        raise ValueError("The standard catalogue requires 138 main-table candidates")
    return data


def verify_corpus(root: str | Path, catalogue: dict | None = None) -> dict:
    """Verify revision when Git metadata exists, and every pinned asset always.

    A hash-verified archive is supported without Git metadata: the catalogue's
    checked-in SHA-256 values are the trust anchor, not a supplied manifest.
    """
    root = Path(root).resolve()
    catalogue = catalogue or load_catalogue()
    if not root.is_dir():
        raise ValueError(f"Corpus directory does not exist: {root}")
    if (root / ".git").exists():
        revision = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
        ).strip()
        if revision != CORPUS_REVISION:
            raise ValueError(f"Corpus revision {revision} differs from {CORPUS_REVISION}")
    assets = {}
    sources = [r["source"] for r in catalogue["candidates"] + catalogue["coverage"]]
    sources += [s for r in catalogue["candidates"] for s in r.get("alias_sources", [])]
    sources += catalogue.get("supporting_assets", [])
    for source in sources:
        relative = source["path"]
        if relative in assets:
            continue
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"Corpus asset escapes mirror: {relative}")
        digest = file_hash(path)
        if digest != source["sha256"]:
            raise ValueError(f"Pinned corpus hash mismatch: {relative}")
        assets[relative] = digest
    return {
        "schema_version": 1,
        "revision": CORPUS_REVISION,
        "assets": assets,
        "catalogue_hash": canonical_hash(catalogue),
        "candidate_count": 149,
    }


def width_band(width: int) -> str:
    if type(width) is not int or not 8 <= width <= 133:
        raise ValueError(f"Active width is outside the standard 8–133 range: {width}")
    return "S" if width <= 24 else "M" if width <= 64 else "L"


def _load_source(candidate, root, master_seed):
    from qiskit import QuantumCircuit, qasm2
    from qiskit.circuit.library import EfficientSU2, PauliEvolutionGate, QuantumVolume
    from qiskit.quantum_info import SparsePauliOp
    from qiskit.synthesis import LieTrotter

    source = candidate["source"]
    path = (root / source["path"]).resolve()
    if not path.is_relative_to(root) or file_hash(path) != source["sha256"]:
        raise ValueError(f"Pinned source hash mismatch: {candidate['input_id']}")
    bindings = {}
    if source["kind"] == "qasm":
        # Benchpress assets use the legacy qelib1 gate vocabulary (including
        # rxx, rzz and multi-control gates). Supply Qiskit's explicit extension.
        circuit = qasm2.load(str(path), custom_instructions=qasm2.LEGACY_CUSTOM_INSTRUCTIONS)
    elif source["kind"] == "hamlib":
        records = json.loads(path.read_text())
        matches = [r for r in records if "ham_" + r["ham_instance"][1:-1] == source["base_id"]]
        if len(matches) != 1 or matches[0]["ham_instance"] != source["ham_instance"]:
            raise ValueError("HamLib record does not have a unique frozen identity")
        record = matches[0]
        terms, coefficients = (
            record["ham_hamlib_hamiltonian_terms"],
            record["ham_hamlib_hamiltonian_coefficients"],
        )
        if len(terms) != len(coefficients) or len(terms) != record["ham_terms"]:
            raise ValueError("HamLib term count mismatch")
        operator = SparsePauliOp.from_list(list(zip(terms, coefficients, strict=True)))
        circuit = QuantumCircuit(record["ham_qubits"])
        circuit.append(
            PauliEvolutionGate(operator, time=1, synthesis=LieTrotter(reps=1, preserve_order=True)),
            circuit.qubits,
        )
    elif source["kind"] == "generator":
        method = source["method"]
        if method == "test_QV_100_transpile":
            circuit = QuantumVolume(100, 100, seed=12345)
        elif method in {"test_circSU2_89_transpile", "test_circSU2_100_transpile"}:
            circuit = EfficientSU2(candidate["declared_width"], reps=3, entanglement="circular")
            mapping = {}
            for index, parameter in enumerate(circuit.parameters):
                # A dedicated generation namespace, independent of preflight,
                # pilot and comparison repetitions. Values are strictly nonzero.
                token = json.dumps(
                    ["fixture-generation-v1", master_seed, candidate["input_id"], index],
                    separators=(",", ":"),
                ).encode()
                integer = int.from_bytes(hashlib.sha256(token).digest()[:8], "big")
                value = math.pi * (integer + 1) / (2**64 + 1)
                mapping[parameter] = value
                bindings[parameter.name] = value
            circuit = circuit.assign_parameters(mapping)
        elif method == "test_BV_100_transpile":
            n = 100
            circuit = QuantumCircuit(n, n - 1)
            circuit.x(n - 1)
            circuit.h(range(n))
            circuit.cx(range(n - 1), n - 1)
            circuit.h(range(n - 1))
            circuit.measure(range(n - 1), range(n - 1))
        elif method == "test_BVlike_simplification_transpile":
            circuit = QuantumCircuit(100)
            for q in range(99):
                circuit.cx(q, 99)
            circuit.x(99)
            circuit.z(98)
            for q in reversed(range(99)):
                circuit.cx(q, 99)
        else:
            raise ValueError(f"No frozen generator recipe for {method}")
    else:
        raise ValueError(f"Unknown source kind {source['kind']}")
    return circuit, bindings


def freeze_fixture(
    candidate: dict, corpus_root: str | Path, destination: str | Path, master_seed: int = 20261109
) -> dict:
    root = Path(corpus_root).resolve()
    circuit, bindings = _load_source(candidate, root, master_seed)
    if circuit.num_qubits != candidate["declared_width"] or circuit.num_qubits > 133:
        raise ValueError(f"Declared width differs from catalogue: {candidate['input_id']}")
    logical = export_circuit(circuit)
    imported = export_circuit(import_circuit(logical))
    if imported != logical:
        raise ValueError(f"Logical fixture does not round-trip: {candidate['input_id']}")
    active_width = len(active_qubits(logical))
    aliases = []
    for alias in candidate.get("alias_sources", []):
        content = (root / alias["path"]).read_bytes()
        if file_hash(root / alias["path"]) != alias["sha256"]:
            raise ValueError("Alias asset hash mismatch")
        original = (root / candidate["source"]["path"]).read_bytes()
        if content.replace(b"\r\n", b"\n") != original.replace(b"\r\n", b"\n"):
            raise ValueError("Claimed QASM alias is not newline-normalized identical")
        aliases.append(alias["input_id"])
    fixture = {
        "schema_version": 1,
        "input_id": candidate["input_id"],
        "aliases": aliases,
        "source": candidate["source"],
        "corpus_revision": CORPUS_REVISION,
        "fixture_hash": canonical_hash(logical),
        "logical": logical,
        "declared_width": circuit.num_qubits,
        "active_width": active_width,
        "family": candidate["family"],
        "recipe": candidate["recipe"],
        "band": width_band(active_width),
        "parameter_bindings": bindings,
        "semantics": {
            "input_state": "arbitrary logical state",
            "ancillas": "zero initialized",
            "classical_outputs": "all declared classical bits",
        },
    }
    atomic_write_json(Path(destination), fixture)
    return fixture
