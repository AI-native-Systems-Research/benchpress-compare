from itertools import product

import pytest

from benchpress_compare.eligibility import admit, apply_admission
from benchpress_compare.weights import LEVELS, TARGETS, read_fraction


def cases(input_id, family="F1", **extra):
    return [
        {
            "case_id": f"{input_id}/{target}/{level}",
            "input_id": input_id,
            "target_class": target,
            "level": level,
            "family": family,
            "band": "S",
            "recipe": input_id,
            "seeds": [1, 2, 3],
            **extra,
        }
        for target, level in product(TARGETS, LEVELS)
    ]


def results(planned, arm="baseline", **change):
    return [
        {
            "observation_id": f"{case['case_id']}/{arm}/{seed}",
            "case_id": case["case_id"],
            "arm": arm,
            "phase": "comparison",
            "seed": seed,
            "status": "SUCCEEDED",
            "N2": 100,
            "D2": 40,
            "validation": {
                "status": "verified",
                "checks": {"target_legality": {"status": "verified"}},
            },
            **change,
        }
        for case in planned
        for seed in case["seeds"]
    ]


def test_input_is_dropped_when_any_baseline_observation_failed_or_is_missing():
    planned = cases("a") + cases("b") + cases("c")
    observations = results(planned)
    failed = next(row for row in observations if row["case_id"].startswith("b/"))
    failed.update(status="COMPILER_FAILED", reason="native_crash", N2=None, D2=None)
    observations = [row for row in observations if row["observation_id"] != "c/square/2/baseline/1"]
    admission = admit(planned, observations)
    assert admission["admitted"] == ["a"]
    records = {row["input_id"]: row for row in admission["records"]}
    assert records["b"]["reasons"] == ["baseline_failed"]
    assert records["b"]["baseline_failures"][0]["reason"] == "native_crash"
    assert records["c"]["reasons"] == ["baseline_missing"]


def test_unverified_success_is_not_a_baseline_success():
    planned = cases("a")
    observations = results(planned)
    observations[0]["validation"] = {"status": "verified"}
    assert admit(planned, observations)["dropped"] == ["a"]


def test_evolve_results_never_affect_admission():
    planned = cases("a")
    evolve = results(planned, arm="evolve", status="COMPILER_FAILED", N2=None, D2=None)
    assert admit(planned, results(planned) + evolve)["admitted"] == ["a"]
    assert admit(planned, evolve)["dropped"] == ["a"]


def test_structural_variant_replaces_its_canonical_only_when_canonical_baseline_failed():
    planned = cases("canonical") + cases("variant", structural_canonical="canonical")
    observations = results(planned)
    records = {row["input_id"]: row for row in admit(planned, observations)["records"]}
    assert records["canonical"]["status"] == "admitted"
    assert records["variant"]["reasons"] == ["structural_duplicate"]
    observations[0]["status"] = "RESOURCE_LIMIT"
    assert admit(planned, observations)["admitted"] == ["variant"]


def test_dropped_inputs_are_zero_weight_supplemental_cases():
    planned = cases("a") + cases("b", family="F2")
    observations = results(planned)
    observations[-1]["status"] = "INVALID"
    plan = apply_admission({"cases": planned, "config": {"smoke": ["a", "b"]}}, observations)
    weights = {case["input_id"]: read_fraction(case["weight"]) for case in plan["cases"]}
    assert weights["b"] == 0 and weights["a"] > 0
    assert plan["admission"]["weights_error"] is None
    assert all(case["supplemental"] for case in plan["cases"] if case["input_id"] == "b")


def test_empty_family_in_eight_family_design_yields_no_weights_instead_of_redistribution():
    planned = cases("a")
    plan = apply_admission({"cases": planned, "config": {}}, results(planned))
    assert "empty families" in plan["admission"]["weights_error"]
    assert all(read_fraction(case["weight"]) == 0 for case in plan["cases"])
    assert plan["weights_audit"] is None


@pytest.mark.parametrize("name", ["evolve", "small"])
def test_subset_admission_reweights_inputs_and_keeps_evolve_failures(name):
    from benchpress_compare.corpus import load_catalogue
    from benchpress_compare.profiles import load_profile, select_candidates
    from benchpress_compare.report import build_report
    from benchpress_compare.weights import allocate_weights

    manifest = load_profile(name)
    inputs = select_candidates(manifest, load_catalogue()["candidates"])
    planned = [
        dict(row, case_id=row["input_id"], target_class="heavy-hex", target_id="heavy-hex",
             level=2, seeds=[1, 2, 3]) for row in inputs
    ]
    baseline = results(planned)
    evolve = results(planned, arm="evolve")
    plan = {"profile": name, "design": manifest["design"], "cases": planned,
            "profile_manifest": manifest, "config": {"profile": name}}
    admitted = apply_admission(plan, baseline + evolve)
    assert admitted["admission"]["weights_error"] is None
    assert admitted["weights_audit"]["case_count"] == len(inputs)
    assert build_report(admitted, baseline + evolve)["summary"]["metrics"]["N2"]["score"] == 1

    baseline[0]["status"] = "INVALID"
    admitted = apply_admission(plan, baseline + evolve)
    assert admitted["admission"]["dropped"] == [planned[0]["input_id"]]
    allocation = allocate_weights(inputs[1:], targets=["heavy-hex"], levels=[2])
    for case in admitted["cases"]:
        assert read_fraction(case["weight"]) == (
            0 if case["input_id"] == planned[0]["input_id"]
            else read_fraction(allocation["inputs"][case["input_id"]]["input_weight"])
        )
    evolve[3]["status"] = "COMPILER_FAILED"
    failed = apply_admission(plan, baseline + evolve)
    assert failed["admission"] == admitted["admission"]
    report = build_report(failed, baseline + evolve)
    assert report["summary"]["metrics"]["N2"]["score"] is None
    assert report["score_without_evolve_failures"]["metrics"]["N2"]["score"] == 1

    # F8 has only one selected input in small: losing it must block the score.
    without_family = [row for row in baseline if not row["case_id"].startswith("Q/large/32")]
    if name == "evolve":
        f8_ids = {row["input_id"] for row in inputs if row["family"] == "F8"}
        without_family = [row for row in baseline if row["case_id"] not in f8_ids]
    empty = apply_admission(plan, without_family + evolve)
    assert "empty families" in empty["admission"]["weights_error"]
    assert all(read_fraction(case["weight"]) == 0 for case in empty["cases"])
