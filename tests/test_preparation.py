from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import sys

import pytest

from benchpress_compare.config import RunConfig
from benchpress_compare.errors import CompareError, IntegrityError, PreparationError
from benchpress_compare.identity import atomic_write_json, canonical_hash, read_json
from benchpress_compare.preparation import (
    _checked_artifact,
    _check_paired_builds,
    _harness_wheel,
    _snapshot_paths,
    _task,
    _verify_harness_wheel,
    prepare,
)
from benchpress_compare.builds import snapshot_source
from benchpress_compare.config import SiteConfig
from benchpress_compare.planner import BIG_CIRCUITS


def config(tmp_path, **kwargs):
    sources = []
    for arm in ("baseline", "evolve"):
        source = tmp_path / arm
        source.mkdir(exist_ok=True)
        (source / "pyproject.toml").write_text('[project]\nname="qiskit"\nversion="0.0.0"\n')
        sources.append(str(source))
    return RunConfig(*sources, str(tmp_path / "store"), str(tmp_path / "results"), **kwargs)


@pytest.fixture
def fake_preparation(monkeypatch):
    import benchpress_compare.preparation as module

    state = {"states": [], "tasks": [], "site": None}
    candidates = [
        {
            "input_id": f"input-{index}",
            "declared_width": 8,
            "family": f"F{index}",
            "band": "S",
            "recipe": "test",
        }
        for index in range(1, 9)
    ]
    monkeypatch.setattr(
        module,
        "load_catalogue",
        lambda: {"schema_version": 1, "candidates": candidates, "coverage": []},
    )
    monkeypatch.setattr(
        module, "_corpus", lambda config, root: (root / "corpus", {"revision": "pinned"})
    )
    monkeypatch.setattr(
        module,
        "_snapshot_paths",
        lambda config, root: {"baseline": str(root / "base"), "evolve": str(root / "evolve")},
    )
    monkeypatch.setattr(
        module,
        "_builds",
        lambda engine, snapshots: {
            arm: {
                "build_key": canonical_hash(arm),
                "env_python": sys.executable,
                "manifest_path": str(engine.root / arm / "manifest.json"),
            }
            for arm in ("baseline", "evolve")
        },
    )

    class FakeEngine:
        def __init__(self, root, config, lease, plan_hash=None):
            self.root, self.config, self.lease, self.plan_hash = root, config, lease, plan_hash
            self.site = state["site"]
            self.resources = {"usable_cpus": 8, "worker_memory_budget_bytes": 64 * 1024**3}
            from benchpress_compare.store import Store

            self.store = Store(config.store)

        def state(self, status, **details):
            state["states"].append(status)

        def run(self, specs):
            raise AssertionError("preparation must not run observations")

    def task(engine, name, fields, **kwargs):
        state["tasks"].append(name)
        if name == "freeze-fixture":
            fixture = {
                **fields["candidate"],
                "schema_version": 1,
                "active_width": 8,
                "fixture_hash": canonical_hash(fields["candidate"]),
            }
            atomic_write_json(fields["destination"], fixture)
            return fixture
        if name == "freeze-targets":
            targets = []
            for target_class, width in (("square", 9), ("heavy-hex", 19), ("fake_torino", 133)):
                target = {
                    "schema_version": 1,
                    "target_class": target_class,
                    "num_qubits": width,
                    "snapshot_id": target_class + str(width),
                    "target_hash": canonical_hash(target_class),
                }
                path = Path(fields["destination"]) / f"{target_class}.json"
                atomic_write_json(path, target)
                targets.append({**target, "path": str(path)})
            return {"targets": targets}
        if name == "validate-import":
            return {
                "status": "verified",
                "fixtures": [read_json(path)["fixture_hash"] for path in fields["fixture_paths"]],
                "targets": [read_json(path)["target_hash"] for path in fields["target_paths"]],
            }
        raise AssertionError(name)

    def tasks(engine, requests):
        return [task(engine, name, fields, **options) for name, fields, options in requests]

    monkeypatch.setattr(module, "ExecutionEngine", FakeEngine)
    monkeypatch.setattr(module, "_task", task)
    monkeypatch.setattr(module, "_tasks", tasks)
    return state


def test_freeze_plans_every_input_on_both_arms_without_measuring_anything(
    tmp_path, fake_preparation
):
    cfg = config(tmp_path)
    plan = prepare(cfg)
    assert plan["expected_counts"] == {
        "candidates": 8,
        "cases": 48,
        "main_observations": 288,
        "jobs": 0,
    }
    assert fake_preparation["states"] == ["PREPARING", "FROZEN"]
    assert "weights" not in plan and "pilot" not in plan
    for case in plan["cases"]:
        assert "weight" not in case and len(case["seeds"]) == cfg.seeds
        observations = [row for row in plan["observations"] if row["case_id"] == case["case_id"]]
        assert {row["arm"] for row in observations} == {"baseline", "evolve"}
        assert {row["phase"] for row in observations} == {"comparison"}
        assert all(row["runtime_profile"] == case["runtime_profile"] for row in observations)
    assert len({canonical_hash(case["runtime_profile"]) for case in plan["cases"]}) == 1
    before = (Path(cfg.results_root) / "plan.json").read_bytes()
    assert prepare(cfg, continue_existing=True) == plan
    assert (Path(cfg.results_root) / "plan.json").read_bytes() == before


def test_lsf_plan_gives_each_big_case_seed_its_own_job(tmp_path, fake_preparation, monkeypatch):
    import benchpress_compare.preparation as module

    catalogue = module.load_catalogue()
    catalogue["candidates"][0] = dict(catalogue["candidates"][0], input_id="Q/large/bwt_n37")
    monkeypatch.setattr(module, "load_catalogue", lambda: catalogue)
    fake_preparation["site"] = SiteConfig(queue="normal", job_wall_seconds=172800)
    plan = prepare(config(tmp_path))
    big = [
        bundle for bundle in plan["bundles"] if bundle["observations"][0]["input_id"] in BIG_CIRCUITS
    ]
    assert len(big) == 6 * 3
    for bundle in big:
        rows = bundle["observations"]
        assert len({(row["case_id"], row["seed"]) for row in rows}) == 1 and len(rows) == 2
    assert sum(len(bundle["observations"]) for bundle in plan["bundles"]) == 288
    assert plan["expected_counts"]["jobs"] == len(plan["bundles"])


def test_smoke_plan_and_structural_variant_identity(tmp_path, fake_preparation, monkeypatch):
    import benchpress_compare.preparation as module

    catalogue = module.load_catalogue()
    catalogue["candidates"][1] = dict(catalogue["candidates"][1], structural_canonical="input-1")
    monkeypatch.setattr(module, "load_catalogue", lambda: catalogue)
    plan = prepare(config(tmp_path, smoke=["input-1", "input-2"]))
    assert plan["design"] == "benchpress-smoke-v1"
    assert {case["input_id"] for case in plan["cases"]} == {"input-1", "input-2"}
    variants = {case.get("structural_canonical") for case in plan["cases"]}
    assert variants == {None, "input-1"}


def test_frozen_plan_rejects_new_incompatible_request(tmp_path, fake_preparation):
    cfg = config(tmp_path)
    prepare(cfg)
    with pytest.raises(CompareError, match="incompatible request"):
        prepare(replace(cfg, seeds=5))


def test_plan_resume_verifies_preparation_revisions_and_artifacts(tmp_path, fake_preparation):
    cfg = config(tmp_path)
    plan = prepare(cfg)
    fixture = Path(plan["cases"][0]["fixture_path"])
    fixture.write_text("{}")
    with pytest.raises(IntegrityError, match="artifact changed"):
        prepare(cfg, continue_existing=True)


def test_partial_snapshot_resume_uses_completed_snapshot_even_after_live_edit(tmp_path):
    cfg = config(tmp_path)
    root = Path(cfg.results_root)
    root.mkdir()
    staged = snapshot_source(cfg.baseline, root / "snapshots/baseline-staging")
    (Path(cfg.baseline) / "later.py").write_text("changed = True")
    paths = _snapshot_paths(cfg, root)
    assert read_json(Path(paths["baseline"]) / "snapshot.json")["tree_hash"] == staged["tree_hash"]
    assert not (Path(paths["baseline"]) / "source/later.py").exists()


def test_cached_external_artifact_cannot_be_substituted(tmp_path):
    path = tmp_path / "fixture.json"
    atomic_write_json(path, {"schema_version": 1, "fixture_hash": "different"})
    with pytest.raises(IntegrityError, match="receipt"):
        _checked_artifact(
            {"path": str(path), "schema_version": 1, "fixture_hash": "expected"}, kind="fixture"
        )


def test_build_pair_contract_rejects_toolchain_or_dependency_drift():
    attestation = {
        key: "same"
        for key in ("python", "implementation", "abi", "platform", "architecture", "libc", "os")
    }
    attestation["dependencies"] = [["qiskit", "2.0"], ["numpy", "2.1"]]
    baseline = {
        "attestation": attestation,
        "toolchain": {"rustc": "1"},
        "request_identity": {
            key: "same" for key in ("dependencies", "build_constraints", "harness", "python")
        },
    }
    evolve = deepcopy(baseline)
    evolve["attestation"]["dependencies"][0][1] = "2.2"
    _check_paired_builds({"baseline": baseline, "evolve": evolve})
    evolve["toolchain"]["rustc"] = "2"
    with pytest.raises(PreparationError, match="toolchains"):
        _check_paired_builds({"baseline": baseline, "evolve": evolve})
    evolve["toolchain"]["rustc"] = "1"
    evolve["attestation"]["dependencies"][1][1] = "2.3"
    with pytest.raises(PreparationError, match="dependency versions"):
        _check_paired_builds({"baseline": baseline, "evolve": evolve})
    evolve = deepcopy(baseline)
    evolve["request_identity"]["build_constraints"] = "different"
    with pytest.raises(PreparationError, match="build_constraints"):
        _check_paired_builds({"baseline": baseline, "evolve": evolve})


def test_task_identity_includes_limits_and_build_attestation(tmp_path):
    class TaskEngine:
        def __init__(self):
            self.config = config(tmp_path)
            self.builds = {
                "baseline": {
                    "env_python": "/env/bin/python",
                    "manifest_path": "/build/manifest.json",
                    "build_key": "build",
                }
            }
            self.specs = []

        def run(self, specs):
            self.specs.extend(specs)
            return [{"status": "SUCCEEDED", "result": {"verified": True}}]

    engine = TaskEngine()
    _task(engine, "validate-import", {"fixture_paths": []}, python="/env/bin/python")
    engine.config.wall_seconds = 7200
    _task(engine, "validate-import", {"fixture_paths": []}, python="/env/bin/python")
    assert engine.specs[0]["manifest_path"] == "/build/manifest.json"
    assert engine.specs[0]["build_id"] == "build"
    assert engine.specs[0]["observation_id"] != engine.specs[1]["observation_id"]
    # A verified manifest carries the produced binary identity, which is what
    # the worker compares against; the task must freeze that, not the build key.
    engine.builds["baseline"]["build_id"] = "binary"
    _task(engine, "validate-import", {"fixture_paths": []}, python="/env/bin/python")
    assert engine.specs[2]["build_id"] == "binary"


def test_preparation_rejects_coordinator_changes_before_first_build(tmp_path, monkeypatch):
    import benchpress_compare.preparation as module

    cfg = config(tmp_path).validate()
    root = Path(cfg.results_root)
    root.mkdir()
    module._request_config(root, cfg, False)
    monkeypatch.setattr(module, "harness_identity", lambda: "changed-after-submission")
    with pytest.raises(IntegrityError, match="Coordinator changed"):
        prepare(cfg, continue_existing=True)
    assert not (root / "snapshots").exists()


def test_cached_harness_wheel_must_match_actual_coordinator(tmp_path):
    import json
    import zipfile
    import benchpress_compare
    from benchpress_compare.corpus import load_catalogue

    package = Path(benchpress_compare.__file__).parent
    wheel = tmp_path / "benchpress_compare-0.1.0-py3-none-any.whl"

    def write_wheel(*, corrupt=False):
        with zipfile.ZipFile(wheel, "w") as archive:
            for path in package.rglob("*.py"):
                if "__pycache__" in path.parts:
                    continue
                content = path.read_bytes()
                if corrupt and path.name == "preparation.py":
                    content += b"\n# stale implementation\n"
                archive.writestr("benchpress_compare/" + path.relative_to(package).as_posix(), content)
            archive.writestr("benchpress_compare/data/candidates.json", json.dumps(load_catalogue()))

    write_wheel()
    assert _verify_harness_wheel(wheel) == wheel
    assert _harness_wheel(tmp_path) == wheel
    write_wheel(corrupt=True)
    with pytest.raises(IntegrityError, match="differs from the running coordinator"):
        _harness_wheel(tmp_path)
