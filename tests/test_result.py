"""The small ``result.json``: N2, D2 and one run state derived from the report model."""

import json
from pathlib import Path

import pytest

from benchpress_compare.identity import atomic_write_json
from benchpress_compare.report import (
    RESULT_STATES,
    SCORED_STATES,
    build_report,
    build_result,
    read_result,
    write_report,
)

DOCS = Path(__file__).resolve().parents[1] / "docs/result.md"
VERIFIED = {"status": "verified", "checks": {"target_legality": {"status": "verified"}}}


def case(case_id, input_id, weight=(1, 8)):
    record = {"numerator": weight[0], "denominator": weight[1]}
    return {
        "case_id": case_id,
        "input_id": input_id,
        "family": "F1",
        "band": "S",
        "recipe": "R",
        "level": 2,
        "target_class": "square",
        "target_id": "square9",
        "weight": record,
        "sensitivity_weight": record,
        "seeds": [1],
    }


def observation(case_id, arm, n2, d2, status="SUCCEEDED", **extra):
    return {
        "observation_id": f"{arm}-{case_id}",
        "case_id": case_id,
        "arm": arm,
        "seed": 1,
        "status": status,
        "N2": n2,
        "D2": d2,
        "validation": VERIFIED,
        **extra,
    }


def plan(cases, **extra):
    return {
        "schema_version": 1,
        "cases": cases,
        "design": "benchpress-smoke-v1",
        "plan_hash": "sha",
        "profile": "full",
        **extra,
    }


def admission(records, weights_error=None):
    return {
        "schema_version": 1,
        "policy": "post-collection-baseline-success-v2",
        "candidate_count": len(records),
        "admitted": sorted(key for key, row in records.items() if row["status"] == "admitted"),
        "dropped": sorted(key for key, row in records.items() if row["status"] == "dropped"),
        "records": [dict(row, input_id=key) for key, row in sorted(records.items())],
        "weights_error": weights_error,
    }


def one_circuit():
    return plan([case("c", "Q/circuit")]), [
        observation("c", "baseline", 10, 10),
        observation("c", "evolve", 9, 11),
    ]


def two_circuits(second_weight=(1, 8), records=None, **evolve_second):
    cases = [case("c", "Q/circuit"), case("d", "Q/other", second_weight)]
    observations = [
        observation("c", "baseline", 10, 10),
        observation("c", "evolve", 9, 11),
        observation("d", "baseline", 20, 20),
        observation("d", "evolve", 20, 20, **evolve_second),
    ]
    extra = {"admission": admission(records)} if records is not None else {}
    return plan(cases, **extra), observations


def test_complete_run_carries_the_headline_scores():
    report = build_report(*one_circuit(), status="COMPLETE")
    result = build_result(report)
    assert result["state"] == "complete"
    assert result["scored"] is True
    assert result["N2"] == 0.9
    assert result["D2"] == pytest.approx(1.1)
    assert result["partial"] is None
    assert result["dropped_inputs"] == 0
    assert result["observations"] == {"planned": 2, "completed": 2, "succeeded": 2}
    assert result["run_status"] == "COMPLETE"
    assert result["profile"] == "full"
    assert result["score_scope"] == "smoke"
    assert result["query"] == {}
    assert result["explanation"].startswith("All 2 planned observations succeeded")


def test_baseline_failures_are_exclusions_and_still_scored():
    records = {
        "Q/circuit": {"status": "admitted", "reasons": [], "baseline_failures": []},
        "Q/other": {"status": "dropped", "reasons": ["baseline_failed"], "baseline_failures": [{}]},
    }
    plan_, observations = two_circuits((0, 1), records)
    observations[2].update(status="RESOURCE_LIMIT", N2=None, D2=None)
    result = build_result(build_report(plan_, observations, status="COMPLETE_WITH_FAILURES"))
    assert result["state"] == "complete_with_exclusions"
    assert result["scored"] is True
    assert result["N2"] == 0.9
    assert result["dropped_inputs"] == 1
    assert result["observations"] == {"planned": 4, "completed": 4, "succeeded": 3}
    assert "failed on 1 input" in result["explanation"]
    assert "remaining 1 circuit" in result["explanation"]


def test_structural_duplicates_are_not_exclusions():
    records = {
        "Q/circuit": {"status": "admitted", "reasons": [], "baseline_failures": []},
        "Q/other": {
            "status": "dropped",
            "reasons": ["structural_duplicate"],
            "baseline_failures": [],
        },
    }
    result = build_result(build_report(*two_circuits((0, 1), records), status="COMPLETE"))
    assert result["state"] == "complete"
    assert result["dropped_inputs"] == 0


def test_evolve_failure_withholds_the_main_score_and_gives_the_partial_one():
    plan_, observations = two_circuits(status="COMPILER_FAILED", N2=None, D2=None, reason="crash")
    result = build_result(build_report(plan_, observations, status="COMPLETE_WITH_FAILURES"))
    assert result["state"] == "evolve_failed"
    assert result["scored"] is False
    assert result["N2"] is None and result["D2"] is None
    assert result["partial"] == {
        "N2": 0.9,
        "D2": pytest.approx(1.1),
        "excluded_inputs": 1,
        "excluded_weight": 0.125,
    }
    assert "1 observation across 1 circuit carrying 12.5% of the weight" in result["explanation"]
    assert result["observations"]["succeeded"] == 3


def test_cancelled_observations_make_the_run_cancelled_rather_than_evolve_failed():
    plan_, observations = two_circuits(status="CANCELLED", N2=None, D2=None)
    result = build_result(build_report(plan_, observations, status="COMPLETE_WITH_FAILURES"))
    assert result["state"] == "cancelled"
    assert result["scored"] is False
    assert result["partial"] is None
    assert "1 observation were cancelled" in result["explanation"]
    assert "--retry-cancelled" in result["explanation"]


def test_weights_error_leaves_the_run_unscored_with_the_reason():
    records = {
        "Q/circuit": {"status": "admitted", "reasons": [], "baseline_failures": []},
        "Q/other": {"status": "dropped", "reasons": ["baseline_failed"], "baseline_failures": [{}]},
    }
    plan_, observations = two_circuits((0, 1), records)
    plan_["cases"][0]["weight"] = plan_["cases"][0]["sensitivity_weight"] = {
        "numerator": 0,
        "denominator": 1,
    }
    plan_["admission"]["weights_error"] = "empty families: F2"
    observations[2].update(status="RESOURCE_LIMIT", N2=None, D2=None)
    result = build_result(build_report(plan_, observations, status="COMPLETE_WITH_FAILURES"))
    assert result["state"] == "unscored"
    assert result["scored"] is False
    assert result["N2"] is None
    assert "empty families: F2" in result["explanation"]
    assert "empty_slice" in result["explanation"]
    assert "also failed on 1 input" in result["explanation"]


def test_live_run_state_overrides_a_report_that_has_scores():
    report = build_report(*one_circuit(), status="COMPLETE")
    running = build_result(report, {"status": "RUNNING", "updated_at": 5.0})
    assert running["state"] == "running"
    assert running["scored"] is False
    assert running["N2"] is None and running["D2"] is None
    assert running["observations"] == {"planned": 2, "completed": 2, "succeeded": 2}
    assert (
        running["explanation"] == "2 of 2 observations collected; N2 and D2 are not available yet."
    )
    assert running["updated_at"] == 5.0
    assert running["plan_hash"] == "sha"
    uncertain = build_result(report, {"status": "NEEDS_RECONCILIATION", "affected": ["j1", "j2"]})
    assert uncertain["state"] == "needs_reconciliation"
    assert uncertain["explanation"].startswith("2 jobs in an uncertain scheduler state")


def test_states_before_a_report_exists():
    assert build_result(None)["state"] == "not_started"
    assert build_result(None)["explanation"] == RESULT_STATES["not_started"]
    request = {"profile": "small", "design": "benchpress-small-v1"}
    failed = build_result(None, {"status": "PREPARATION_FAILED", "reason": "boom"}, request)
    assert failed["state"] == "preparation_failed"
    assert failed["explanation"] == "Preparation failed: boom"
    assert failed["profile"] == "small"
    assert failed["design"] == "benchpress-small-v1"
    assert failed["score_scope"] == "subset_search_signal"
    assert failed["scored"] is False
    assert failed["observations"] == {"planned": None, "completed": None, "succeeded": None}
    for run_status, name in (
        ("PREPARING", "preparing"),
        ("FROZEN", "frozen"),
        ("INTERRUPTED", "interrupted"),
        ("CANCELLED", "cancelled"),
    ):
        result = build_result(None, {"status": run_status})
        assert result["state"] == name
        assert result["explanation"] == RESULT_STATES[name]
    progress = build_result(
        None,
        {
            "status": "RUNNING",
            "expected_observations": 10,
            "completed_observations": 4,
            "succeeded": 4,
        },
    )
    assert (
        progress["explanation"]
        == "4 of 10 observations collected; N2 and D2 are not available yet."
    )
    assert progress["observations"] == {"planned": 10, "completed": 4, "succeeded": 4}


def test_result_keys_are_stable_and_json_clean():
    report = build_report(*one_circuit(), status="COMPLETE")
    results = [
        build_result(report),
        build_result(report, {"status": "RUNNING"}),
        build_result(None),
        build_result(None, {"status": "PREPARATION_FAILED", "reason": "x"}),
    ]
    assert len({tuple(sorted(result)) for result in results}) == 1
    for result in results:
        json.dumps(result, allow_nan=False)
        assert result["scored"] == (result["state"] in SCORED_STATES)
        assert result["scored"] == (result["N2"] is not None)


def test_write_report_writes_result_json_from_the_live_run_state(tmp_path):
    report = build_report(*one_circuit(), status="COMPLETE")
    paths = write_report(tmp_path, report)
    assert Path(paths["result.json"]) == tmp_path / "reports/result.json"
    assert json.load(open(paths["result.json"]))["state"] == "complete"
    atomic_write_json(tmp_path / "state/status.json", {"schema_version": 1, "status": "RUNNING"})
    write_report(tmp_path, report)
    written = json.load(open(paths["result.json"]))
    assert written["state"] == "running"
    assert written["N2"] is None


def test_read_result_uses_the_saved_report_of_a_collected_run(tmp_path):
    assert read_result(tmp_path / "absent")["state"] == "not_started"
    report = build_report(*one_circuit(), status="COMPLETE")
    atomic_write_json(tmp_path / "reports/summary.json", report)
    atomic_write_json(
        tmp_path / "state/status.json",
        {"schema_version": 1, "status": "COMPLETE", "updated_at": 7.0},
    )
    result = read_result(tmp_path)
    assert result["state"] == "complete"
    assert result["N2"] == 0.9
    assert result["updated_at"] == 7.0
    atomic_write_json(
        tmp_path / "state/status.json", {"schema_version": 1, "status": "INTERRUPTED"}
    )
    assert read_result(tmp_path)["state"] == "interrupted"


def test_every_state_is_documented():
    text = DOCS.read_text()
    for name in RESULT_STATES:
        assert f"`{name}`" in text, name
    assert SCORED_STATES <= set(RESULT_STATES)
    assert "benchpress-compare result" in text
    assert "docs/result.md" in (DOCS.parents[1] / "README.md").read_text()
