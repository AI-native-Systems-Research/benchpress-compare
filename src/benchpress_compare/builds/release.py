"""Release wheels and final-path virtual environments with checked attestations.

No settings from the developer's Cargo profile or Qiskit user configuration are
inherited. The source's optimized release profile is the default; explicit
overrides form part of the build identity.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
import tomllib
import zipfile
from pathlib import Path
from typing import Sequence

import psutil

from ..errors import IntegrityError, PreparationError
from ..identity import atomic_write_json, canonical_hash, file_hash, fsync_directory
from .snapshot import verify_snapshot

THREAD_ENVIRONMENT = {
    "QISKIT_PARALLEL": "FALSE", "QISKIT_IGNORE_USER_SETTINGS": "TRUE",
    "RAYON_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1", "VECLIB_MAXIMUM_THREADS": "1", "NUMEXPR_NUM_THREADS": "1",
    "PYTHONHASHSEED": "0", "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
}


def sanitized_environment(scratch: Path, extra: dict | None = None) -> dict[str, str]:
    scratch.mkdir(parents=True, exist_ok=True)
    keep = {"PATH", "HOME", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "LANG", "LC_ALL", "RUSTUP_HOME"}
    environment = {key: value for key, value in os.environ.items() if key in keep}
    environment.update(THREAD_ENVIRONMENT)
    environment.update({
        "UV_CACHE_DIR": str(scratch / "uv-cache"), "XDG_CACHE_HOME": str(scratch / "cache"),
        "XDG_CONFIG_HOME": str(scratch / "config"), "QISKIT_SETTINGS": str(scratch / "qiskit-settings.conf"),
        "CARGO_HOME": str(scratch / "cargo"),
    })
    environment.update(extra or {})
    return environment


def _run(command: Sequence[str], *, cwd: Path, environment: dict, log: Path, timeout: float = 14_400) -> str:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as output:
        output.write((json.dumps({"command": list(command), "cwd": str(cwd)}) + "\n").encode())
        output.flush()
        try:
            # In a supervised build task, uv/Cargo must remain in the worker's
            # process group so its frozen watchdog also contains native builds.
            process = subprocess.Popen(list(map(str, command)), cwd=cwd, env=environment, stdout=output, stderr=subprocess.STDOUT)
        except OSError as exc:
            raise PreparationError(f"Could not launch {command[0]}: {exc}") from exc
        try:
            code = process.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            try:
                parent = psutil.Process(process.pid)
                processes = [*parent.children(recursive=True), parent]
            except psutil.NoSuchProcess:
                processes = []
            for child in reversed(processes):
                try:
                    child.terminate()
                except psutil.NoSuchProcess:
                    pass
            _, alive = psutil.wait_procs(processes, timeout=5)
            for child in alive:
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            process.wait()
            raise
        if code:
            raise PreparationError(f"Command failed ({code}); see {log}")
    return log.read_text(errors="replace")


def _source_path(snapshot: Path | str) -> Path:
    snapshot = Path(snapshot).resolve()
    return snapshot / "source" if (snapshot / "snapshot.json").is_file() else snapshot


def _requirements(source: Path) -> tuple[list[str], dict]:
    try:
        configuration = tomllib.loads((source / "pyproject.toml").read_text())
    except (OSError, ValueError) as exc:
        raise PreparationError(f"Source lacks readable pyproject.toml: {source}") from exc
    build = configuration.get("build-system", {})
    if not build.get("build-backend") or not isinstance(build.get("requires"), list):
        raise PreparationError("Explicit PEP 517 build backend and requirements are required")
    requirements = list(build["requires"])
    project = configuration.get("project", {})
    requirements.extend(project.get("dependencies", []))
    if "dependencies" in project.get("dynamic", []):
        dynamic = configuration.get("tool", {}).get("setuptools", {}).get("dynamic", {}).get("dependencies", {})
        files = dynamic.get("file")
        if not files:
            raise PreparationError("Unsupported dynamic dependency metadata; cannot establish a common dependency lock")
        if isinstance(files, str):
            files = [files]
        for filename in files:
            dependency_file = (source / filename).resolve()
            if not dependency_file.is_relative_to(source):
                raise PreparationError("Dependency metadata escapes source snapshot")
            for line in dependency_file.read_text().splitlines():
                line = line.split("#", 1)[0].strip()
                if line.startswith("-"):
                    raise PreparationError(f"Nested requirements/directives need an explicit lock: {filename}")
                if line:
                    requirements.append(line)
    if not requirements:
        raise PreparationError("Empty source dependency contract")
    return requirements, build


def _rust_toolchain(source: Path) -> str | None:
    if (source / "rust-toolchain.toml").is_file():
        return tomllib.loads((source / "rust-toolchain.toml").read_text())["toolchain"]["channel"]
    if (source / "rust-toolchain").is_file():
        return (source / "rust-toolchain").read_text().strip()
    return None


def resolve_common_dependencies(
    sources: Sequence[Path | str], destination: Path | str, *, python: str | Path | None = None,
    extra_requirements: Sequence[str] = (),
) -> dict:
    """Resolve the intersection of both arms' build/runtime requirements once."""
    if len(sources) < 2:
        raise PreparationError("A common dependency lock requires both source arms")
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    requirements = {"wheel==0.45.1", "psutil==7.1.3", *extra_requirements}
    backends, toolchains = [], []
    for snapshot in sources:
        source = _source_path(snapshot)
        source_requirements, backend = _requirements(source)
        requirements.update(source_requirements)
        backends.append(backend)
        toolchains.append(_rust_toolchain(source))
    if len(set(toolchains)) != 1:
        raise PreparationError(f"Source Rust toolchains disagree: {toolchains}; choose a separately declared common build contract")
    request = "\n".join(sorted(requirements)) + "\n"
    request_path = destination / "common.in"
    lock_path = destination / "common.requirements.txt"
    request_path.write_text(request)
    environment = sanitized_environment(destination / "scratch")
    command = ["uv", "pip", "compile", "--python", str(python or sys.executable), "--generate-hashes", "--no-header", "--no-annotate", "--output-file", str(lock_path), str(request_path)]
    _run(command, cwd=destination, environment=environment, log=destination / "resolve.log")
    lock = {
        "schema_version": 1, "requirements_path": str(lock_path), "requirements_hash": file_hash(lock_path),
        "input_hash": canonical_hash(sorted(requirements)), "backends": backends,
        "rust_toolchain": toolchains[0], "python": str(python or sys.executable),
    }
    atomic_write_json(destination / "lock.json", lock)
    return lock


def release_policy(source: Path | str, overrides: dict | None = None) -> dict:
    source = Path(source)
    cargo = source / "Cargo.toml"
    if not cargo.is_file():
        raise PreparationError("A Cargo release profile is required for the supported Qiskit build adapter")
    parsed = tomllib.loads(cargo.read_text())
    declared = parsed.get("profile", {}).get("release", {})
    profile = {"opt-level": 3, "lto": False, "codegen-units": 16, "debug": False, **declared}
    overrides = dict(overrides or {})
    supported = {"CARGO_PROFILE_RELEASE_OPT_LEVEL": "opt-level", "CARGO_PROFILE_RELEASE_LTO": "lto", "CARGO_PROFILE_RELEASE_CODEGEN_UNITS": "codegen-units"}
    if set(overrides) - set(supported):
        raise PreparationError(f"Unsupported release overrides: {sorted(set(overrides) - set(supported))}")
    for key, value in overrides.items():
        profile[supported[key]] = value
    source_flags = {}
    for filename in [".cargo/config", ".cargo/config.toml"]:
        config_path = source / filename
        if config_path.is_file():
            config = tomllib.loads(config_path.read_text())
            source_flags[filename] = config
            profile.update(config.get("profile", {}).get("release", {}))
            serialized = json.dumps(config)
            if re.search(r"opt-level(?:=|\\?\s)+0|target-cpu|target-feature", serialized):
                raise PreparationError("Source Cargo configuration disables optimization or requests undeclared native CPU targeting")
    # Explicit environment overrides take precedence over source configuration.
    for key, value in overrides.items():
        profile[supported[key]] = value
    if str(profile["opt-level"]) not in {"1", "2", "3", "s", "z"}:
        raise PreparationError("Release optimization may not be disabled")
    for name, package_profile in profile.get("package", {}).items():
        if "opt-level" in package_profile and str(package_profile["opt-level"]) not in {"1", "2", "3", "s", "z"}:
            raise PreparationError(f"Release optimization is disabled for package {name}")
    return {
        "profile": "release", "effective_profile": profile, "explicit_overrides": overrides,
        "source_cargo_config": source_flags, "cpu_policy": "portable", "required_cpu_features": [],
        "compiler_flags": {key: "" for key in ["RUSTFLAGS", "CFLAGS", "CXXFLAGS", "LDFLAGS"]},
    }


_ATTEST = r'''
import hashlib, importlib.metadata, json, pathlib, platform, sys, sysconfig
import qiskit
import qiskit._accelerate
from qiskit import QuantumCircuit, transpile
root = pathlib.Path(sys.prefix).resolve()
origin = pathlib.Path(qiskit.__file__).resolve()
assert origin.is_relative_to(root), (str(origin), str(root))
native = {}
for path in origin.parent.rglob("*"):
    if path.is_file() and (path.suffix in {".so", ".pyd", ".dylib"}):
        native[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
assert native, "Qiskit has no native extensions"
accelerate = pathlib.Path(qiskit._accelerate.__file__).resolve()
assert str(accelerate.relative_to(root)) in native, "Imported accelerator is not an attested native binary"
circuit = QuantumCircuit(3)
circuit.h(0)
circuit.cx(0, 2)
output = transpile(circuit, basis_gates=["id", "sx", "x", "rz", "cz"], optimization_level=2, seed_transpiler=7, approximation_degree=1.0)
assert output.num_qubits == 3
print("BENCHPRESS_ATTEST=" + json.dumps({
    "python": platform.python_version(), "implementation": platform.python_implementation(),
    "abi": sysconfig.get_config_var("SOABI"), "platform": sysconfig.get_platform(),
    "architecture": platform.machine(), "libc": list(platform.libc_ver()), "os": platform.system(),
    "qiskit_origin": str(origin), "qiskit_version": qiskit.__version__,
    "native_hashes": native, "accelerator_origin": str(accelerate),
    "dependencies": sorted((distribution.metadata["Name"].lower().replace("_", "-"), distribution.version) for distribution in importlib.metadata.distributions()),
    "smoke_transpile": {"status": "verified", "qubits": output.num_qubits, "operations": dict(output.count_ops())},
}, sort_keys=True))
'''


def _environment_manifest(environment: Path) -> dict[str, str]:
    records = {}
    for path in sorted(environment.rglob("*")):
        if path.is_symlink():
            relative = path.relative_to(environment)
            if relative.parts[0] == "bin" and path.name.startswith("python"):
                records[relative.as_posix()] = canonical_hash({"link": os.readlink(path), "binary": file_hash(path)})
            elif path.is_dir() and path.resolve().is_relative_to(environment):
                # uv may use an in-environment lib64 -> lib compatibility link.
                records[relative.as_posix()] = canonical_hash({"link": os.readlink(path)})
            else:
                raise IntegrityError(f"Unattested environment symlink: {path}")
        elif path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            records[path.relative_to(environment).as_posix()] = file_hash(path)
    return records


def binary_identity(build_key: str, wheel_hash: str, native_hashes: dict[str, str]) -> str:
    """Observation identity includes the produced binaries, not only inputs."""
    return canonical_hash({"schema_version": 1, "build_key": build_key,
                           "wheel_hash": wheel_hash, "native_hashes": native_hashes})


def verify_build(directory: Path | str) -> dict:
    directory = Path(directory).resolve()
    try:
        marker = json.loads((directory / "READY").read_text())
        manifest = json.loads((directory / "manifest.json").read_text())
    except (OSError, ValueError) as exc:
        raise IntegrityError(f"Build is incomplete: {directory}") from exc
    if marker.get("schema_version") != 1 or manifest.get("schema_version") != 1:
        raise IntegrityError("Unsupported build schema")
    if marker.get("manifest_hash") != file_hash(directory / "manifest.json"):
        raise IntegrityError("Build manifest checksum mismatch")
    for name, expected in manifest["artifacts"].items():
        path = directory / name
        if not path.resolve().is_relative_to(directory) or not path.is_file() or file_hash(path) != expected:
            raise IntegrityError(f"Build artifact checksum mismatch: {path}")
    environment = directory / "env"
    if _environment_manifest(environment) != manifest["environment_files"]:
        raise IntegrityError("Installed environment contents or dependency files changed")
    if file_hash(Path(manifest["wheel"])) != manifest["wheel_hash"]:
        raise IntegrityError("Produced wheel identity changed")
    for name, expected in manifest["attestation"]["native_hashes"].items():
        native = (environment / name).resolve()
        if not native.is_relative_to(environment) or file_hash(native) != expected:
            raise IntegrityError("Attested native binary changed")
    if manifest["build_id"] != binary_identity(manifest["build_key"], manifest["wheel_hash"], manifest["attestation"]["native_hashes"]):
        raise IntegrityError("Produced binary identity does not match its build attestation")
    python = Path(manifest["env_python"])
    if not python.is_file() or file_hash(python) != manifest["interpreter_hash"]:
        raise IntegrityError("Build interpreter changed")
    if manifest["release"]["profile"] != "release" or not manifest["attestation"].get("release_log_verified"):
        raise IntegrityError("Missing optimized release evidence")
    host = manifest["attestation"]
    if (host["architecture"] != platform.machine() or host["implementation"] != platform.python_implementation()
            or host["platform"] != sysconfig.get_platform() or host["os"] != platform.system()
            or host["libc"] != list(platform.libc_ver())):
        raise IntegrityError("Build platform is incompatible with this host")
    return manifest


def build_release(
    snapshot_directory: Path | str, destination: Path | str, *, python: Path | str | None = None,
    dependency_lock: Path | str | dict | None = None, harness_wheel: Path | str | None = None,
    release_overrides: dict | None = None, timeout: float = 14_400,
    lease=None, build_jobs: int = 1,
) -> dict:
    """Build a source wheel, install it at its final path and publish READY last."""
    snapshot_directory, destination = Path(snapshot_directory).resolve(), Path(destination).resolve()
    if type(build_jobs) is not int or build_jobs < 1:
        raise PreparationError("Build jobs must be a positive integer")
    snapshot = verify_snapshot(snapshot_directory)
    if lease is not None:
        lease.assert_owned()
    source = snapshot_directory / "source"
    policy = release_policy(source, release_overrides)
    if dependency_lock is None or harness_wheel is None:
        raise PreparationError("A common dependency lock and a pinned harness wheel are required")
    if isinstance(dependency_lock, dict):
        lock_info = dependency_lock
        dependency_lock = Path(lock_info["requirements_path"])
        if file_hash(dependency_lock) != lock_info["requirements_hash"]:
            raise IntegrityError("Common dependency lock changed")
    dependency_lock, harness_wheel = Path(dependency_lock).resolve(), Path(harness_wheel).resolve()
    python = str(python or sys.executable)
    requested_identity = {
        "tree_hash": snapshot["tree_hash"], "dependencies": file_hash(dependency_lock),
        "release": policy, "harness": file_hash(harness_wheel), "python": file_hash(python),
    }
    if (destination / "READY").exists():
        existing = verify_build(destination)
        if existing["request_identity"] != requested_identity:
            raise IntegrityError("Build destination has an incompatible identity")
        return existing
    if destination.exists() and any(destination.iterdir()):
        raise PreparationError(f"Incomplete build needs a new attempt destination: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    for ancestor in destination.parents:
        for filename in [".cargo/config", ".cargo/config.toml"]:
            if (ancestor / filename).is_file():
                raise PreparationError(f"Build would inherit undeclared Cargo configuration: {ancestor / filename}")
    scratch = destination / "scratch"
    environment = sanitized_environment(scratch, {"QISKIT_BUILD_PROFILE": "release", "RUST_DEBUG": "0",
                    "CARGO_BUILD_JOBS": str(build_jobs), **policy["explicit_overrides"]})
    toolchain = _rust_toolchain(source)
    if toolchain:
        environment["RUSTUP_TOOLCHAIN"] = toolchain
    toolchain_output = _run(["rustc", "-vV"], cwd=destination, environment=environment, log=destination / "logs" / "rustc.log")
    compiler_output = _run(["cc", "--version"], cwd=destination, environment=environment, log=destination / "logs" / "cc.log")
    (destination / "common.requirements.txt").write_bytes(dependency_lock.read_bytes())
    shutil.copyfile(harness_wheel, destination / harness_wheel.name)
    # Virtual environments cannot be moved after installation: entry-point
    # scripts contain the absolute interpreter path.
    env_dir = destination / "env"
    _run(["uv", "venv", "--python", python, str(env_dir)], cwd=destination, environment=environment, log=destination / "logs" / "environment.log")
    env_python = env_dir / "bin" / "python"
    _run(["uv", "pip", "sync", "--python", str(env_python), "--require-hashes", str(destination / "common.requirements.txt")], cwd=destination, environment=environment, log=destination / "logs" / "environment.log")
    build_source = destination / "build-source"
    shutil.copytree(source, build_source, symlinks=True)
    before_locks = {str(path.relative_to(build_source)): file_hash(path) for path in build_source.rglob("*") if path.name in {"Cargo.lock", "uv.lock", "poetry.lock"}}
    wheels = destination / "wheels"
    wheels.mkdir()
    command = ["uv", "build", "--wheel", "--no-build-isolation", "--python", str(env_python), "--out-dir", str(wheels), "--verbose", str(build_source)]
    build_log = _run(command, cwd=destination, environment=environment, log=destination / "logs" / "build.log", timeout=timeout)
    after_locks = {str(path.relative_to(build_source)): file_hash(path) for path in build_source.rglob("*") if path.name in {"Cargo.lock", "uv.lock", "poetry.lock"}}
    if before_locks != after_locks:
        raise PreparationError("Build modified or created source dependency lockfiles")
    release_evidence = bool(re.search(r"Finished\s+[`']?release[`']?\s+profile\s+\[optimized", build_log))
    if not release_evidence:
        raise PreparationError("Build log does not establish an optimized Cargo release; no environment was published")
    outputs = list(wheels.glob("*.whl"))
    if len(outputs) != 1:
        raise PreparationError(f"Expected one release wheel, found {len(outputs)}")
    wheel = outputs[0]
    with zipfile.ZipFile(wheel) as contents:
        native_wheel = {name: hashlib.sha256(contents.read(name)).hexdigest() for name in contents.namelist() if name.endswith((".so", ".pyd", ".dylib"))}
    if not native_wheel:
        raise PreparationError("Qiskit wheel contains no native extension")
    _run(["uv", "pip", "install", "--python", str(env_python), "--no-deps", "--reinstall", str(wheel), str(destination / harness_wheel.name)], cwd=destination, environment=environment, log=destination / "logs" / "environment.log")
    _run(["uv", "pip", "check", "--python", str(env_python)], cwd=destination, environment=environment, log=destination / "logs" / "dependencies.log")
    script = destination / "attest.py"
    script.write_text(_ATTEST)
    output = _run([str(env_python), "-I", str(script)], cwd=destination, environment=environment, log=destination / "logs" / "attest.log")
    records = [line.removeprefix("BENCHPRESS_ATTEST=") for line in output.splitlines() if line.startswith("BENCHPRESS_ATTEST=")]
    if len(records) != 1:
        raise PreparationError("Missing or ambiguous native import attestation")
    attestation = json.loads(records[0])
    native_installed = attestation["native_hashes"]
    if len(native_installed) != len(native_wheel):
        raise PreparationError("Installed Qiskit native binaries differ from the release wheel inventory")
    for name, expected in native_wheel.items():
        matches = [actual for installed, actual in native_installed.items() if installed.endswith("/" + name)]
        if matches != [expected]:
            raise PreparationError(f"Imported native binary does not match release wheel: {name}")
    attestation["release_log_verified"] = True
    toolchain_identity = {"rustc": toolchain_output.splitlines()[1:], "cc": compiler_output.splitlines()[1:]}
    build_key = canonical_hash({**requested_identity, "toolchain": toolchain_identity, "abi": attestation["abi"], "platform": attestation["platform"], "architecture": attestation["architecture"], "libc": attestation["libc"]})
    artifacts = {path.relative_to(destination).as_posix(): file_hash(path) for path in [wheel, destination / harness_wheel.name, destination / "common.requirements.txt", script, *(destination / "logs").glob("*.log")]}
    manifest = {
        "schema_version": 1, "build_key": build_key,
        "build_id": binary_identity(build_key, file_hash(wheel), native_installed),
        "manifest_path": str(destination / "manifest.json"),
        "environment_key": canonical_hash({"build": build_key, "wheel": file_hash(wheel), "harness": file_hash(harness_wheel), "interpreter": file_hash(env_python)}),
        "snapshot_hash": snapshot["tree_hash"], "request_identity": requested_identity,
        "snapshot_path": str(snapshot_directory),
        "env_python": str(env_python), "wheel": str(wheel), "wheel_hash": file_hash(wheel),
        "interpreter_hash": file_hash(env_python), "release": policy, "toolchain": toolchain_identity,
        "build_command": command, "attestation": attestation, "artifacts": artifacts,
        "build_resources": {"cargo_build_jobs": build_jobs, "timeout_seconds": timeout},
        "environment_files": _environment_manifest(env_dir),
    }
    with lease.fenced() if lease is not None else contextlib.nullcontext():
        atomic_write_json(destination / "manifest.json", manifest)
        atomic_write_json(destination / "READY", {"schema_version": 1, "manifest_hash": file_hash(destination / "manifest.json")})
        fsync_directory(destination)
    return verify_build(destination)
