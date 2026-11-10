"""Build-specific process worker. Importing this module does not import Qiskit."""

from __future__ import annotations

import json
import os
import platform
import sys
import time
import traceback
from pathlib import Path

from benchpress_compare.identity import (
    atomic_write_json,
    atomic_write_json_gzip,
    canonical_hash,
    file_hash,
)

WORKER_PROTOCOL = "observation-worker-v2-gzip"
COMPRESSED_ARTIFACTS = {"output.json", "initialization.json", "pre-layout.json"}


def _environment(spec):
    threads = spec.get("runtime_profile", {}).get("native_threads", 1)
    if type(threads) is not int or threads < 1:
        raise ValueError("native_threads must be a positive integer")
    settings = {
        key: str(threads)
        for key in (
            "OMP_NUM_THREADS",
            "OPENBLAS_NUM_THREADS",
            "MKL_NUM_THREADS",
            "NUMEXPR_NUM_THREADS",
            "RAYON_NUM_THREADS",
        )
    }
    settings.update(
        QISKIT_NUM_PROCS="1",
        QISKIT_PARALLEL="FALSE",
        QISKIT_IN_PARALLEL="TRUE",
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONNOUSERSITE="1",
    )
    declared = spec.get("runtime_profile", {}).get("thread_environment", {})
    if any(key not in settings or value != settings[key] for key, value in declared.items()):
        raise ValueError("Frozen thread environment conflicts with the native thread contract")
    settings.update(declared)
    os.environ.update(settings)
    sys.dont_write_bytecode = True
    scratch = Path(spec["output_dir"]) / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    for key in ("XDG_CACHE_HOME", "XDG_CONFIG_HOME", "QISKIT_SETTINGS"):
        os.environ[key] = str(scratch / key.lower())
    os.environ.pop("PYTHONPATH", None)
    return settings


def _load_json(path):
    with Path(path).open() as handle:
        return json.load(handle)


def _attest_runtime(spec, qiskit):
    """Recheck the actually imported extension and dependency inventory."""
    import importlib.metadata
    import sysconfig

    accelerator = importlib.import_module("qiskit._accelerate")

    native_origin = Path(accelerator.__file__).resolve()
    host = {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "abi": sysconfig.get_config_var("SOABI"),
        "platform": sysconfig.get_platform(),
        "architecture": platform.machine(),
        "libc": list(platform.libc_ver()),
        "os": platform.system(),
    }
    machine = f"{host['os']}-{host['architecture']}"
    if spec.get("runtime_profile", {}).get("machine", machine) != machine:
        raise ValueError("Worker host does not match the frozen runtime machine class")
    evidence = {
        "accelerator_origin": str(native_origin),
        "accelerator_sha256": file_hash(native_origin),
        "host": host,
    }
    if not spec.get("manifest_path"):
        # Unattested environments are useful for explicit adapter integration
        # tests. Production specs always carry the frozen build manifest.
        evidence["build_attestation"] = "not_requested"
        return evidence
    manifest = _load_json(spec["manifest_path"])
    if (
        spec.get("build_id") is not None
        and manifest.get("build_id", manifest.get("build_key")) != spec["build_id"]
    ):
        raise ValueError("Worker build identity differs from the frozen observation")
    attestation = manifest["attestation"]
    for field, actual in host.items():
        if attestation.get(field) != actual:
            raise ValueError(f"Worker host {field} differs from the build attestation")
    environment = Path(manifest["env_python"]).parent.parent.resolve()
    if Path(sys.prefix).resolve() != environment:
        raise ValueError("Worker is not running in the attested build environment")
    if (
        str(Path(qiskit.__file__).resolve()) != attestation["qiskit_origin"]
        or str(native_origin) != attestation["accelerator_origin"]
    ):
        raise ValueError("Imported Qiskit origin differs from build attestation")
    for relative, expected in attestation["native_hashes"].items():
        path = (environment / relative).resolve()
        if not path.is_relative_to(environment) or file_hash(path) != expected:
            raise ValueError("Imported native extension hash differs from build attestation")
    if not attestation.get("release_log_verified"):
        raise ValueError("Release build was not attested")
    dependencies = sorted(
        (distribution.metadata["Name"].lower().replace("_", "-"), distribution.version)
        for distribution in importlib.metadata.distributions()
    )
    if dependencies != sorted(tuple(pair) for pair in attestation["dependencies"]):
        raise ValueError("Installed dependency inventory differs from build attestation")
    evidence["build_attestation"] = "verified"
    return evidence


def _fixture(path):
    from benchpress_compare.corpus.ir import import_circuit, export_circuit

    fixture = _load_json(path)
    if (
        fixture.get("schema_version") != 1
        or canonical_hash(fixture["logical"]) != fixture["fixture_hash"]
    ):
        raise ValueError("Frozen fixture hash/schema mismatch")
    circuit = import_circuit(fixture["logical"])
    if export_circuit(circuit) != fixture["logical"]:
        raise ValueError("Build changes logical fixture identity")
    return fixture, circuit


def _run_task(spec, output):
    from benchpress_compare.corpus import freeze_fixture
    from benchpress_compare.targets import freeze_fake_torino, synthetic_target, import_target

    task = spec.get("task", "observation")
    if task == "freeze-fixture":
        destination = Path(spec.get("destination", output / "fixture.json"))
        fixture = freeze_fixture(
            spec["candidate"], spec["corpus_root"], destination, spec.get("master_seed", 20261109)
        )
        return {"status": "SUCCEEDED", "result": {**fixture, "path": str(destination)}}, {}
    if task == "freeze-targets":
        destination = Path(spec.get("destination", output / "targets"))
        destination.mkdir(parents=True, exist_ok=True)
        targets = {}
        target_classes = spec.get("target_classes", ["square", "heavy-hex", "fake_torino"])
        if not target_classes or not set(target_classes) <= {"square", "heavy-hex", "fake_torino"}:
            raise ValueError("Unknown or empty target classes")
        for width in sorted(set(spec["declared_widths"])):
            for topology in target_classes:
                if topology == "fake_torino":
                    continue
                target = synthetic_target(topology, width)
                targets[target["target_hash"]] = target
        if "fake_torino" in target_classes:
            torino = freeze_fake_torino()
            targets[torino["target_hash"]] = torino
        results = []
        for digest, target in sorted(targets.items()):
            import_target(target)
            path = destination / f"{digest}.json"
            atomic_write_json(path, target)
            results.append({**target, "path": str(path)})
        return {"status": "SUCCEEDED", "result": {"targets": results}}, {}
    if task == "validate-import":
        fixtures = [_fixture(path)[0]["fixture_hash"] for path in spec.get("fixture_paths", [])]
        targets = []
        for path in spec.get("target_paths", []):
            snapshot = _load_json(path)
            import_target(snapshot)
            targets.append(snapshot["target_hash"])
        return {
            "status": "SUCCEEDED",
            "result": {"status": "verified", "fixtures": fixtures, "targets": targets},
        }, {}
    if task != "observation":
        raise ValueError(f"Unknown worker task: {task}")
    from benchpress_compare.corpus.ir import export_circuit
    from benchpress_compare.metrics import measure
    from benchpress_compare.validation import (
        summarize_validation,
        target_legality,
    )
    from .adapter import compile_observation
    from .starts import reviewed_start_source

    fixture, circuit = _fixture(spec["fixture_path"])
    snapshot = _load_json(spec["target_path"])
    for field, actual in (
        ("fixture_hash", fixture["fixture_hash"]),
        ("target_hash", snapshot["target_hash"]),
    ):
        if field in spec and spec[field] != actual:
            raise ValueError(f"Observation {field} differs from artifact")
    target = import_target(snapshot)
    try:
        compiled, diagnostics, artifacts = compile_observation(
            circuit, target, spec["level"], spec["seed"],
            native_start_source=reviewed_start_source(spec.get("manifest_path")),
        )
    except Exception as error:
        from .adapter import AdapterIncompatible
        from benchpress_compare.corpus.ir import UnsupportedCircuit

        if isinstance(error, (AdapterIncompatible, UnsupportedCircuit)):
            raise
        return {
            "status": "COMPILER_FAILED",
            "reason": f"{type(error).__name__}: {error}",
            "error": f"{type(error).__name__}: {error}",
            "traceback": traceback.format_exc(),
        }, {}
    serialized = export_circuit(compiled)
    measured = measure(serialized, snapshot["native_2q_names"])
    if compiled.layout is None:
        initial = final = list(range(circuit.num_qubits))
    else:
        initial = list(compiled.layout.initial_index_layout(filter_ancillas=True))
        final = list(compiled.layout.final_index_layout(filter_ancillas=True))
    checks = {
        "target_legality": target_legality(serialized, snapshot),
    }
    if spec.get("phase") == "preflight":
        from benchpress_compare.corpus.embedding import search_embedding

        workload = artifacts.get("pre-layout.json", artifacts.get("initialization.json"))
        diagnostics["embedding"] = (
            search_embedding(workload, snapshot, budget_seconds=1.0, max_nodes=100_000)
            if workload is not None
            else {"status": "unknown", "reason": "no_post_initialization_workload"}
        )
    validation = summarize_validation(checks)
    artifacts.update(
        {
            "output.json": serialized,
            "layout.json": {"schema_version": 1, "initial": initial, "final": final},
            "diagnostics.json": diagnostics,
            "validation.json": validation,
        }
    )
    return {
        "status": "SUCCEEDED" if validation["status"] == "verified" else "INVALID",
        "N2": measured["N2"],
        "D2": measured["D2"],
        "metric_protocol": measured["protocol"],
        "diagnostics": diagnostics,
        "validation": validation,
        "output_size": len(serialized["operations"]),
    }, artifacts


def run_spec(spec: dict) -> dict:
    output = Path(spec["output_dir"]).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output / "receipt.json").exists():
        raise ValueError("An attempt receipt is immutable; use a new attempt directory")
    identities = (
        "observation_id",
        "case_id",
        "arm",
        "seed",
        "phase",
        "build_id",
        "attempt_id",
        "plan_hash",
        "lease_generation",
        "run_id",
        "submission_token",
        "runtime_profile",
        "protocols",
        "input_id",
        "target_class",
        "target_hash",
        "fixture_hash",
        "level",
    )
    receipt = {
        "schema_version": 1,
        "worker_protocol": WORKER_PROTOCOL,
        **{k: spec[k] for k in identities if k in spec},
    }
    started = time.monotonic()
    try:
        if spec.get("schema_version", 1) != 1:
            raise ValueError("Unsupported worker specification version")
        settings = _environment(spec)
        import qiskit

        receipt["runtime"] = {
            "python": sys.version,
            "executable": sys.executable,
            "platform": platform.platform(),
            "qiskit_version": qiskit.__version__,
            "qiskit_origin": str(Path(qiskit.__file__).resolve()),
            "environment": settings,
            **_attest_runtime(spec, qiskit),
        }
        result, artifacts = _run_task(spec, output)
        receipt.update(result)
        receipt["artifacts"] = {}
        for name, data in sorted(artifacts.items()):
            writer = atomic_write_json
            if name in COMPRESSED_ARTIFACTS:
                name += ".gz"
                writer = atomic_write_json_gzip
            path = output / name
            writer(path, data)
            receipt["artifacts"][name] = file_hash(path)
    except Exception as error:
        receipt.update(
            status="HARNESS_FAILED",
            reason=f"{type(error).__name__}: {error}",
            error=f"{type(error).__name__}: {error}",
            traceback=traceback.format_exc(),
            artifacts={},
        )
    receipt["resources"] = {
        "elapsed_seconds": time.monotonic() - started,
        "persisted_artifact_bytes": sum(
            (output / name).stat().st_size for name in receipt.get("artifacts", {})
        ),
    }
    try:
        import psutil

        receipt["resources"]["rss_bytes_at_exit"] = psutil.Process().memory_info().rss
    except ImportError:
        pass
    atomic_write_json(output / "receipt.json", receipt)
    return receipt
