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
    _revision_chain,
    _snapshot_paths,
    _structural_groups,
    _task,
    prepare,
)
from benchpress_compare.builds import snapshot_source
from benchpress_compare.weights import read_fraction


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

    state = {
        "unresolved": set(),
        "interrupt_pilot": False,
        "executed": [],
        "received": {},
        "states": [],
        "tasks": [],
    }
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
            self.site = None
            self.resources = {"usable_cpus": 8, "worker_memory_budget_bytes": 64 * 1024**3}
            from benchpress_compare.store import Store

            self.store = Store(config.store)

        def state(self, status, **details):
            state["states"].append(status)

        def run(self, specs):
            output = []
            for spec in specs:
                assert spec["arm"] == "baseline", "evolve measurement before plan freeze"
                assert spec["phase"] in ("preflight", "pilot")
                if spec["phase"] == "pilot" and state["interrupt_pilot"]:
                    state["interrupt_pilot"] = False
                    raise OSError("simulated interruption before pilot receipt")
                key = spec["observation_id"]
                if key not in state["received"]:
                    state["executed"].append(spec)
                    invalid = spec["input_id"] in state["unresolved"]
                    state["received"][key] = {
                        **spec,
                        "status": "INVALID" if invalid else "SUCCEEDED",
                        "N2": 50,
                        "D2": 20,
                        "validation": {
                            "status": "unsupported" if invalid else "verified",
                            "checks": {
                                name: {"status": "unsupported" if invalid else "verified"}
                                for name in ("target_legality", "layout", "routing", "semantics")
                            },
                        },
                        "diagnostics": {"sabre_executed": True, "inserted_swaps": 10},
                        "resources": {"elapsed_seconds": 1, "peak_rss_bytes": 100 * 1024**2},
                    }
                output.append(state["received"][key])
            return output

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

    monkeypatch.setattr(module, "ExecutionEngine", FakeEngine)
    monkeypatch.setattr(module, "_task", task)
    return state


def test_full_freeze_has_exact_work_no_evolve_measurements_and_matched_profiles(
    tmp_path, fake_preparation
):
    cfg = config(tmp_path)
    plan = prepare(cfg)
    assert plan["expected_counts"] == {
        "candidates": 8,
        "preflight": 144,
        "cases": 48,
        "main_observations": 288,
    }
    assert read_fraction(plan["weights"]["total_weight"]) == 1
    assert set(fake_preparation["states"]) == {"PREPARING", "SCREENING", "PILOTING", "FROZEN"}
    assert all(spec["arm"] == "baseline" for spec in fake_preparation["executed"])
    for case in plan["cases"]:
        observations = [row for row in plan["observations"] if row["case_id"] == case["case_id"]]
        assert {row["arm"] for row in observations} == {"baseline", "evolve"}
        assert all(row["runtime_profile"] == case["runtime_profile"] for row in observations)
        preflight_seeds = {
            row["seed"] for row in fake_preparation["executed"] if row["case_id"] == case["case_id"]
        }
        assert not set(case["seeds"]) & preflight_seeds
    before = (Path(cfg.results_root) / "plan.json").read_bytes()
    assert prepare(cfg, continue_existing=True) == plan
    assert (Path(cfg.results_root) / "plan.json").read_bytes() == before


def test_resume_interrupted_pilot_reuses_preflight_without_changing_contract(
    tmp_path, fake_preparation
):
    cfg = config(tmp_path)
    fake_preparation["interrupt_pilot"] = True
    with pytest.raises(OSError, match="simulated interruption"):
        prepare(cfg)
    root = Path(cfg.results_root)
    assert not (root / "plan.json").exists()
    before = (root / "preparation.json").read_bytes()
    prepare(cfg, continue_existing=True)
    assert before == (root / "preparation.json").read_bytes()
    assert sum(spec["phase"] == "preflight" for spec in fake_preparation["executed"]) == 144


def test_accept_unresolved_cannot_repair_empty_family(tmp_path, fake_preparation):
    cfg = config(tmp_path)
    fake_preparation["unresolved"] = {"input-8"}
    with pytest.raises(PreparationError, match="unresolved mandatory"):
        prepare(cfg)
    root = Path(cfg.results_root)
    before = (root / "preparation.json").read_bytes()
    with pytest.raises(PreparationError, match="empty families"):
        prepare(replace(cfg, accept_unresolved=True), continue_existing=True)
    assert before == (root / "preparation.json").read_bytes()
    assert not (root / "plan.json").exists()
    revisions = _revision_chain(root, canonical_hash(read_json(root / "preparation.json")))
    assert len(revisions) == 1 and revisions[0]["unresolved"] == ["input-8"]
    assert read_json(root / "coverage.json")["conditional_on_unresolved"]
    # The recorded decision survives a later ordinary continuation.
    with pytest.raises(PreparationError, match="empty families"):
        prepare(cfg, continue_existing=True)


def test_smoke_explicit_conditional_decision_preserves_original_request(tmp_path, fake_preparation):
    cfg = config(tmp_path, smoke=["input-1", "input-2"])
    fake_preparation["unresolved"] = {"input-2"}
    with pytest.raises(PreparationError):
        prepare(cfg)
    root = Path(cfg.results_root)
    request = (root / "request.json").read_bytes()
    plan = prepare(replace(cfg, accept_unresolved=True), continue_existing=True)
    assert plan["design"] == "benchpress-smoke-v1"
    assert plan["conditional_on_unresolved"]
    assert read_fraction(plan["weights"]["total_weight"]) == read_fraction("1/8")
    assert request == (root / "request.json").read_bytes()


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


def test_revision_forks_are_integrity_errors(tmp_path):
    for unresolved in (["one"], ["two"]):
        revision = {
            "schema_version": 1,
            "decision": "accept-unresolved",
            "parent_hash": "parent",
            "unresolved": unresolved,
        }
        atomic_write_json(
            tmp_path / "preparation/revisions" / (canonical_hash(revision) + ".json"), revision
        )
    with pytest.raises(IntegrityError, match="fork"):
        _revision_chain(tmp_path, "parent")


def test_build_pair_contract_rejects_toolchain_or_dependency_drift():
    attestation = {
        key: "same"
        for key in ("python", "implementation", "abi", "platform", "architecture", "libc", "os")
    }
    attestation["dependencies"] = [["qiskit", "2.0"], ["numpy", "2.1"]]
    baseline = {
        "attestation": attestation,
        "toolchain": {"rustc": "1"},
        "request_identity": {key: "same" for key in ("dependencies", "harness", "python")},
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


def test_structural_variants_without_all_contexts_remain_unresolved():
    groups = _structural_groups(
        [{"input_id": "canonical"}, {"input_id": "variant", "structural_canonical": "canonical"}],
        [],
        [],
    )
    assert groups[0]["status"] == "unresolved"


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
    assert engine.specs[0]["observation_id"] != engine.specs[1]["observation_id"]
