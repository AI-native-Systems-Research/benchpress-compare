"""Resumable preparation (builds, fixtures, targets) and immutable plan publication."""

from __future__ import annotations

import importlib.metadata
import math
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

from .builds import snapshot_source, verify_snapshot
from .config import RunConfig
from .corpus import CORPUS_REVISION, load_catalogue, verify_corpus
from .eligibility import ADMISSION_POLICY
from .errors import CompareError, IntegrityError, PreparationError, ReconciliationError
from .identity import (
    atomic_write_json,
    canonical_hash,
    case_identity,
    file_hash,
    fsync_directory,
    read_json,
    harness_identity,
)
from .orchestrator import ExecutionEngine, verify_plan
from .pilot import runtime_profile
from .planner import BIG_CIRCUITS, observations_for, shard_bundles
from .profiles import load_profile, select_candidates
from .schema import PROTOCOLS, TRANSPILE_CONTRACT, validate_record
from .seeds import derive_seeds, seed_contract
from .store import Lease, Store


def _verify_harness_wheel(wheel):
    """Compare packaged code, catalogue and profiles with the coordinator before reuse."""
    import hashlib
    import json
    import zipfile

    try:
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise IntegrityError("Harness wheel contains duplicate members")
            sources = {
                name.removeprefix("benchpress_compare/"): hashlib.sha256(
                    archive.read(name)
                ).hexdigest()
                for name in names
                if name.startswith("benchpress_compare/") and name.endswith(".py")
            }
            catalogue = json.loads(archive.read("benchpress_compare/data/candidates.json"))
            profiles = json.loads(archive.read("benchpress_compare/data/profiles.json"))
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        raise IntegrityError(f"Cannot verify preparation harness wheel: {exc}") from exc
    identity = canonical_hash(
        {
            "schema_version": 1,
            "sources": sources,
            "catalogue": canonical_hash(catalogue),
            "profiles": canonical_hash(profiles),
        }
    )
    if identity != harness_identity():
        raise IntegrityError("Preparation harness wheel differs from the running coordinator")
    return wheel


def _harness_wheel(destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    wheels = sorted(destination.glob("benchpress_compare-*.whl"))
    if wheels:
        if len(wheels) != 1:
            raise IntegrityError("Ambiguous preparation harness wheel")
        return _verify_harness_wheel(wheels[0])
    package_root = Path(__file__).resolve().parents[2]
    if not (package_root / "pyproject.toml").exists():
        # An installed wheel can be reconstructed from its installed distribution,
        # excluding interpreter-specific generated scripts and bytecode.
        import zipfile

        distribution = importlib.metadata.distribution("benchpress-compare")
        wheel = destination / f"benchpress_compare-{distribution.version}-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w", zipfile.ZIP_DEFLATED) as archive:
            for entry in distribution.files or []:
                name = str(entry)
                if name.startswith("benchpress_compare/") or ".dist-info/" in name:
                    if (
                        "__pycache__" not in name
                        and not name.endswith(".pyc")
                        and ".." not in Path(name).parts
                    ):
                        archive.write(distribution.locate_file(entry), name)
        return _verify_harness_wheel(wheel)
    env = dict(os.environ, UV_CACHE_DIR=str(destination / "uv-cache"))
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(destination), str(package_root)],
        env=env,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    wheels = list(destination.glob("benchpress_compare-*.whl"))
    if len(wheels) != 1:
        raise PreparationError("Could not prepare one pinned harness wheel")
    return _verify_harness_wheel(wheels[0])


def _corpus(config, root):
    if config.corpus:
        mirror = Path(config.corpus)
    else:
        mirror = root / "preparation/corpus"
        if not mirror.exists():
            archive = root / "preparation/corpus.tar.gz"
            archive.parent.mkdir(parents=True, exist_ok=True)
            url = f"https://codeload.github.com/Qiskit/benchpress/tar.gz/{CORPUS_REVISION}"
            try:
                urllib.request.urlretrieve(url, archive)
                with tempfile.TemporaryDirectory(dir=archive.parent) as tmp:
                    with tarfile.open(archive) as bundle:
                        bundle.extractall(tmp, filter="data")
                    extracted = list(Path(tmp).iterdir())
                    if len(extracted) != 1:
                        raise PreparationError("Pinned corpus archive has unexpected layout")
                    verify_corpus(extracted[0])
                    os.rename(extracted[0], mirror)
            except OSError as exc:
                raise PreparationError(
                    f"Cannot fetch pinned corpus; supply --corpus for an offline mirror: {exc}"
                ) from exc
            finally:
                archive.unlink(missing_ok=True)
    manifest = verify_corpus(mirror)
    atomic_write_json(root / "preparation/corpus.json", manifest)
    return mirror, manifest


def _snapshot_paths(config, root):
    store = Store(config.store)
    references_path = root / "snapshots/references.json"
    if references_path.exists():
        references = read_json(references_path)
        for snapshot in references["paths"].values():
            verify_snapshot(snapshot)
        return references["paths"]
    baseline_staging = root / "snapshots/baseline-staging"
    # A completed snapshot pins an arm even if preparation was interrupted before
    # both references were published. Do not inspect the live source again.
    baseline = (
        verify_snapshot(baseline_staging)
        if baseline_staging.exists()
        else snapshot_source(config.baseline, baseline_staging)
    )
    destination = store.path("sources", baseline["tree_hash"])
    with store.lease("sources", baseline["tree_hash"]) as lease:
        if destination.exists():
            verify_snapshot(destination)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            stage = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=destination.parent))
            try:
                shutil.copytree(baseline_staging, stage, dirs_exist_ok=True, symlinks=True)
                verify_snapshot(stage)
                with lease.fenced():
                    os.rename(stage, destination)
                    fsync_directory(destination.parent)
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
    evolve = root / "snapshots/evolve"
    if evolve.exists():
        verify_snapshot(evolve)
    else:
        snapshot_source(config.evolve, evolve)
    paths = {"baseline": str(destination), "evolve": str(evolve)}
    atomic_write_json(references_path, {"schema_version": 1, "paths": paths})
    return paths


def _execute(engine, specs):
    options = {"retry_cancelled": True} if getattr(engine, "retry_cancelled", False) else {}
    return engine.run(specs, **options)


def _task_spec(engine, task, fields, *, python=sys.executable, profile=None):
    profile = profile or runtime_profile(
        native_threads=engine.config.native_threads,
        wall_seconds=engine.config.wall_seconds,
        memory_gb=engine.config.worker_memory_gb,
    )
    identity = {
        "task": task,
        **fields,
        "python": str(python),
        "protocols": PROTOCOLS,
        "runtime_profile": profile,
    }
    matching_builds = [
        build
        for build in getattr(engine, "builds", {}).values()
        if build["env_python"] == str(python)
    ]
    if matching_builds:
        # Workers compare the frozen identity against the manifest's produced
        # binary identity, which includes the wheel and native extension hashes.
        identity["manifest_path"] = matching_builds[0]["manifest_path"]
        identity["build_id"] = matching_builds[0].get(
            "build_id", matching_builds[0]["build_key"]
        )
    return {
        **identity,
        "schema_version": 1,
        "phase": "tasks",
        "observation_id": canonical_hash(identity),
    }


def _tasks(engine, requests):
    """Run preparation tasks together; each request is ``(task, fields, options)``.

    Submitting a batch lets the LSF executor run independent tasks concurrently
    and shard many small tasks into one job, instead of one queue wait per task.
    """
    specs = [_task_spec(engine, task, fields, **options) for task, fields, options in requests]
    results = _execute(engine, specs)
    failures = [
        (spec["task"], result.get("reason", result.get("error", "missing failure reason")))
        for spec, result in zip(specs, results)
        if result["status"] != "SUCCEEDED"
    ]
    if failures:
        suffix = f" ({len(failures)} failed tasks)" if len(failures) > 1 else ""
        raise PreparationError(f"Preparation task {failures[0][0]} failed: {failures[0][1]}{suffix}")
    return [result["result"] for result in results]


def _task(engine, task, fields, **options):
    return _tasks(engine, [(task, fields, options)])[0]


def _builds(engine, snapshots):
    from .builds import verify_build

    root, config = engine.root, engine.config
    manifests_path = root / "builds/manifests.json"
    if manifests_path.exists():
        saved = read_json(manifests_path)
        builds = {
            arm: verify_build(Path(manifest["manifest_path"]).parent)
            for arm, manifest in saved["builds"].items()
        }
        _check_paired_builds(builds)
        return builds
    wheel = _harness_wheel(root / "preparation/harness")
    job_wall_seconds = engine.site.job_wall_seconds if engine.site else 14400
    # Dependency resolution downloads and installs packages, so it gets the
    # job-level wall limit rather than the per-observation ``config.wall_seconds``.
    resolve_profile = runtime_profile(
        native_threads=config.native_threads,
        wall_seconds=job_wall_seconds,
        memory_gb=config.worker_memory_gb,
    )
    common = _task(
        engine,
        "resolve-dependencies",
        {
            "snapshots": list(snapshots.values()),
            "destination": str(root / "preparation/dependencies"),
            "extra_requirements": [
                "psutil==7.1.3",
                "jsonschema==4.25.1",
                "qiskit-ibm-runtime==0.43.1",
            ],
        },
        profile=resolve_profile,
    )
    build_profile = runtime_profile(
        native_threads=engine.site.build_cpus if engine.site else config.build_cpus,
        wall_seconds=job_wall_seconds,
        memory_gb=engine.site.build_memory_gb
        if engine.site
        else max(config.worker_memory_gb, 16),
    )
    requests = []
    for arm in ("baseline", "evolve"):
        snapshot = verify_snapshot(snapshots[arm])
        locator = canonical_hash(
            {
                "source": snapshot["tree_hash"],
                "lock": common["requirements_hash"],
                "build_constraints": common["build_constraints_hash"],
                "harness": file_hash(wheel),
                "platform": platform.platform(),
                "python": file_hash(sys.executable),
            }
        )
        destination = (
            engine.store.path("baseline/wheels", locator)
            if arm == "baseline"
            else root / "builds/evolve" / locator
        )
        requests.append(
            (
                "build",
                {
                    "snapshot": snapshots[arm],
                    "destination": str(destination),
                    "dependency_lock": common,
                    "harness_wheel": str(wheel),
                    "rustup_home": str(
                        Path(os.environ.get("RUSTUP_HOME", Path.home() / ".rustup")).resolve()
                    ),
                },
                {"profile": build_profile},
            )
        )
    # Both arms build concurrently; each is an independent durable task.
    builds = {}
    for arm, manifest in zip(("baseline", "evolve"), _tasks(engine, requests)):
        verified = verify_build(Path(manifest["manifest_path"]).parent)
        if verified != manifest:
            raise IntegrityError(f"{arm} build differs from its preparation task receipt")
        builds[arm] = verified
    _check_paired_builds(builds)
    atomic_write_json(manifests_path, {"schema_version": 1, "builds": builds})
    atomic_write_json(root / "builds/baseline.json", builds["baseline"])
    return builds


def _check_paired_builds(builds):
    baseline, evolve = builds["baseline"], builds["evolve"]
    for field in ("dependencies", "build_constraints", "harness", "python"):
        if baseline["request_identity"][field] != evolve["request_identity"][field]:
            raise PreparationError(f"Paired build {field} identities differ")
    for field in ("python", "implementation", "abi", "platform", "architecture", "libc", "os"):
        if baseline["attestation"][field] != evolve["attestation"][field]:
            raise PreparationError(f"Paired build {field} attestations differ")
    if baseline["toolchain"] != evolve["toolchain"]:
        raise PreparationError("Paired builds used different native toolchains")
    dependencies = []
    for manifest in (baseline, evolve):
        dependencies.append(
            {
                name: version
                for name, version in manifest["attestation"]["dependencies"]
                if name != "qiskit"
            }
        )
    if dependencies[0] != dependencies[1]:
        raise PreparationError("Paired builds have different installed dependency versions")


def _checked_artifact(record, *, kind):
    """A cached task receipt must still identify the actual published artifact."""
    path = Path(record["path"])
    if not path.is_file() or read_json(path) != {
        key: value for key, value in record.items() if key != "path"
    }:
        raise IntegrityError(f"Frozen {kind} differs from its preparation receipt: {path}")
    return record


def _request_config(root, config, continue_existing):
    request_path = root / "request.json"
    if request_path.exists():
        request = read_json(request_path)
        saved = request["config"]
        manifest = load_profile(saved.get("profile", "full"))
        if (
            request.get("profile_manifest", manifest) != manifest
            or saved.get("profile_manifest_hash") not in (None, canonical_hash(manifest))
        ):
            raise IntegrityError("Profile manifest changed after the run request was frozen")
        pinned = request.get("coordinator_hash")
        if pinned is None and (root / "preparation.json").exists():
            pinned = read_json(root / "preparation.json").get("coordinator_hash")
        if pinned is not None and pinned != harness_identity():
            raise IntegrityError("Coordinator changed after the run request was frozen")
        if not continue_existing and saved != config.to_dict():
            raise CompareError(
                "Existing results directory has an incompatible request; choose a new directory"
            )
        if pinned is None:
            atomic_write_json(request_path, {**request, "coordinator_hash": harness_identity()})
        return RunConfig(**saved)
    config.validate()
    allowed = {"state", "jobs", "logs", "snapshots", "preparation"}
    if any(path.name not in allowed for path in root.iterdir()):
        raise CompareError("New results directory must be empty")
    atomic_write_json(
        request_path,
        {
            "schema_version": 1,
            "profile": config.profile,
            "design": config.design,
            "profile_manifest": load_profile(config.profile),
            "profile_manifest_hash": config.profile_manifest_hash,
            "config": config.to_dict(),
            "coordinator_hash": harness_identity(),
        },
    )
    return config


def prepare(config: RunConfig, *, continue_existing=False, retry_cancelled=False):
    root = Path(config.results_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with Lease(root / "state/controller.lease") as lease:
        config = _request_config(root, config, continue_existing)
        if (root / "plan.json").exists():
            return verify_plan(root)
        engine = ExecutionEngine(root, config, lease, plan_hash=canonical_hash(config.to_dict()))
        engine.retry_cancelled = retry_cancelled
        if retry_cancelled:
            with lease.fenced():
                (root / "state/cancel-request.json").unlink(missing_ok=True)
            engine.journal.append("preparation_cancelled_work_reopened", {})
        try:
            engine.state("PREPARING")
            catalogue = load_catalogue()
            manifest = read_json(root / "request.json").get("profile_manifest") or load_profile(
                config.profile
            )
            candidates = select_candidates(manifest, catalogue["candidates"])
            if config.smoke:
                unknown = set(config.smoke) - {row["input_id"] for row in candidates}
                if unknown:
                    raise CompareError(f"Unknown smoke circuit IDs: {sorted(unknown)}")
                candidates = [row for row in candidates if row["input_id"] in config.smoke]
            mirror, corpus_manifest = _corpus(config, root)
            snapshots = _snapshot_paths(config, root)
            builds = _builds(engine, snapshots)
            engine.builds = builds
            fixture_requests = [
                (
                    "freeze-fixture",
                    {
                        "candidate": candidate,
                        "corpus_root": str(mirror),
                        "destination": str(
                            root
                            / "fixtures/inputs"
                            / (canonical_hash(candidate["input_id"]) + ".json")
                        ),
                        "master_seed": config.master_seed,
                    },
                    {"python": builds["baseline"]["env_python"]},
                )
                for candidate in candidates
            ]
            fixtures = []
            for request, fixture in zip(fixture_requests, _tasks(engine, fixture_requests)):
                fixtures.append({**fixture, "path": request[1]["destination"]})
                _checked_artifact(fixtures[-1], kind="fixture")
            targets_result = _task(
                engine,
                "freeze-targets",
                {
                    "declared_widths": sorted({fixture["declared_width"] for fixture in fixtures}),
                    "target_classes": manifest["targets"],
                    "destination": str(root / "fixtures/targets"),
                },
                python=builds["baseline"]["env_python"],
            )
            targets = targets_result["targets"]
            for target in targets:
                _checked_artifact(target, kind="target")
            imports = _tasks(
                engine,
                [
                    (
                        "validate-import",
                        {
                            "fixture_paths": [row["path"] for row in fixtures],
                            "target_paths": [row["path"] for row in targets],
                        },
                        {"python": builds[arm]["env_python"]},
                    )
                    for arm in ("baseline", "evolve")
                ],
            )
            for arm, imported in zip(("baseline", "evolve"), imports):
                if (
                    imported.get("status") != "verified"
                    or imported.get("fixtures") != [row["fixture_hash"] for row in fixtures]
                    or imported.get("targets") != [row["target_hash"] for row in targets]
                ):
                    raise IntegrityError(
                        f"{arm} importer did not verify the expected frozen artifacts"
                    )
            cases = []
            contract_hash = canonical_hash(TRANSPILE_CONTRACT)
            # One common profile for every case and both arms: nothing is
            # measured before the comparison, so limits come from the request.
            profile = runtime_profile(
                native_threads=config.native_threads,
                wall_seconds=config.wall_seconds,
                memory_gb=config.worker_memory_gb,
            )
            structural = {
                row["input_id"]: row["structural_canonical"]
                for row in candidates
                if row.get("structural_canonical")
            }
            for fixture in fixtures:
                from .targets import heavy_hex_distance

                width = fixture["declared_width"]
                side = math.isqrt(width - 1) + 1
                distance = heavy_hex_distance(width)
                capacities = {
                    "square": side * side,
                    "heavy-hex": (5 * distance * distance - 2 * distance - 1) // 2,
                    "fake_torino": 133,
                }
                for target_class in manifest["targets"]:
                    matches = [
                        t
                        for t in targets
                        if t["target_class"] == target_class
                        and t["num_qubits"] == capacities[target_class]
                    ]
                    if len(matches) != 1:
                        raise IntegrityError(
                            f"Expected exactly one {target_class} snapshot at width {width}"
                        )
                    target = matches[0]
                    for level in manifest["levels"]:
                        case_id = case_identity(
                            fixture["fixture_hash"], target["target_hash"], level, contract_hash
                        )
                        case = {
                            "schema_version": 1,
                            "case_id": case_id,
                            "input_id": fixture["input_id"],
                            "fixture_hash": fixture["fixture_hash"],
                            "fixture_path": fixture["path"],
                            "target_hash": target["target_hash"],
                            "target_path": target["path"],
                            "target_class": target_class,
                            "target_id": target["snapshot_id"],
                            "level": level,
                            "family": fixture["family"],
                            "recipe": fixture["recipe"],
                            "band": fixture["band"],
                            "active_width": fixture["active_width"],
                            "declared_width": width,
                            "active_occupancy": fixture["active_width"] / target["num_qubits"],
                            "declared_occupancy": width / target["num_qubits"],
                            "occupancy": {
                                "active": fixture["active_width"] / target["num_qubits"],
                                "declared": width / target["num_qubits"],
                            },
                            "seeds": derive_seeds(
                                case_id, "comparison", config.seeds, config.master_seed
                            ),
                            "runtime_profile": profile,
                        }
                        canonical = structural.get(fixture["input_id"])
                        if canonical:
                            case["structural_canonical"] = canonical
                        validate_record("Case", case)
                        cases.append(case)
            contract = {
                "schema_version": 1,
                "profile": config.profile,
                "design": config.design,
                "profile_manifest": manifest,
                "profile_manifest_hash": canonical_hash(manifest),
                "config": config.to_dict(),
                "protocols": PROTOCOLS,
                "coordinator_hash": harness_identity(),
                "builds": builds,
                "catalogue_hash": canonical_hash(catalogue),
                "corpus": corpus_manifest,
                "fixtures": fixtures,
                "targets": targets,
                "cases": cases,
                "transpile_contract": TRANSPILE_CONTRACT,
                "validation_policy": {
                    "protocol": PROTOCOLS["validation"],
                    "required": ["target_legality"],
                    "disclosed": [],
                    "target_legality": "recursive native target legality; "
                    "unsupported, mismatch or error invalidates the observation",
                },
                "admission_policy": ADMISSION_POLICY,
            }
            prep_path = root / "preparation.json"
            if prep_path.exists():
                if read_json(prep_path) != contract:
                    raise IntegrityError("Preparation contract changed on resume")
            else:
                atomic_write_json(prep_path, contract)
            # Every planned circuit is compared. Inputs are admitted to the
            # score only after collection, from baseline results alone.
            observations = observations_for(cases, builds)
            bundles = (
                shard_bundles(
                    observations,
                    max_observations=engine.site.shard_max_observations,
                    job_wall_seconds=engine.site.job_wall_seconds,
                )
                if engine.site
                else []
            )
            plan = {
                "schema_version": 1,
                "profile": config.profile,
                "design": config.design,
                "profile_manifest": manifest,
                "profile_manifest_hash": canonical_hash(manifest),
                "config": config.to_dict(),
                "cases": cases,
                "coordinator_hash": harness_identity(),
                "corpus": corpus_manifest,
                "sources": snapshots,
                "seed_algorithm": seed_contract(config.master_seed),
                "runtime_profiles": {case["case_id"]: case["runtime_profile"] for case in cases},
                "observations": observations,
                "builds": builds,
                "admission_policy": ADMISSION_POLICY,
                "big_circuits": BIG_CIRCUITS,
                "bundles": bundles,
                "preparation_hash": canonical_hash(contract),
                "expected_counts": {
                    "candidates": len(candidates),
                    "cases": len(cases),
                    "main_observations": len(observations),
                    "jobs": len(bundles),
                },
                "protocols": PROTOCOLS,
                "transpile_contract": TRANSPILE_CONTRACT,
                "artifact_hashes": {
                    str(Path(row["path"]).relative_to(root)): file_hash(row["path"])
                    for row in fixtures + targets
                },
            }
            plan["artifact_hashes"]["preparation.json"] = file_hash(prep_path)
            plan["plan_hash"] = canonical_hash(plan)
            validate_record("RunPlan", plan)
            with lease.fenced():
                # Plan is the final publication point; digest appears first.
                digest_path = root / "plan.sha256"
                with digest_path.open("w") as digest_file:
                    digest_file.write(plan["plan_hash"] + "\n")
                    digest_file.flush()
                    os.fsync(digest_file.fileno())
                atomic_write_json(root / "plan.json", plan)
            engine.state("FROZEN")
            return plan
        except (CompareError, ValueError, OSError, subprocess.SubprocessError) as exc:
            if not isinstance(exc, ReconciliationError):
                engine.state("PREPARATION_FAILED", reason=str(exc))
            raise
