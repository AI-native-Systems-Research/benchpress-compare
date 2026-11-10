from copy import deepcopy
from itertools import product

import pytest

from benchpress_compare.eligibility import (
    evaluate_configuration,
    evaluate_input,
    resolve_candidates,
)
from benchpress_compare.weights import LEVELS, TARGETS


def records(counts=(9, 10, 11)):
    return [
        {
            "observation_id": f"obs-{seed}",
            "input_id": "input",
            "arm": "baseline",
            "phase": "preflight",
            "seed": seed,
            "status": "SUCCEEDED",
            "N2": 100,
            "D2": 40,
            "validation": {
                "status": "verified",
                "checks": {
                    name: {"status": "verified"}
                    for name in ("target_legality", "layout", "routing", "semantics")
                },
            },
            "sabre_executed": True,
            "inserted_swaps": count,
        }
        for seed, count in enumerate(counts, 1)
    ]


def complete_input():
    return [
        {**row, "target_class": target, "level": level}
        for target, level in product(TARGETS, LEVELS)
        for row in records()
    ]


def test_median_boundary_and_every_seed_sabre():
    assert evaluate_configuration(records())["status"] == "qualifying"
    assert evaluate_configuration(records((8, 9, 100)))["status"] == "ineligible"
    observations = records()
    observations[0]["sabre_executed"] = False
    result = evaluate_configuration(observations)
    assert result["status"] == "ineligible"
    assert "sabre_bypassed" in result["reasons"]


def test_summary_label_does_not_substitute_for_required_validation_evidence():
    observations = records()
    observations[0]["validation"] = {"status": "verified"}
    assert evaluate_configuration(observations)["status"] == "unresolved"
    # An observation cannot declare a required certificate optional to bypass
    # the admission contract, including when the semantic oracle is unsupported.
    observations = records()
    observations[0]["validation"]["checks"]["semantics"] = {
        "status": "unsupported",
        "required": False,
    }
    assert evaluate_configuration(observations)["status"] == "unresolved"
    observations = records()
    observations[0]["validation"]["checks"]["semantics"] = {
        "status": "mismatch",
        "required": False,
    }
    assert evaluate_configuration(observations)["status"] == "unresolved"
    observations = records()
    observations[0]["validation"]["checks"]["routing"] = {
        "status": "unsupported",
        "required": False,
    }
    assert evaluate_configuration(observations)["status"] == "unresolved"


@pytest.mark.parametrize(
    "change",
    [
        {"status": "RESOURCE_LIMIT"},
        {"N2": None},
        {"D2": -1},
        {"validation": {"status": "unsupported"}},
        {"inserted_swaps": None},
        {"sabre_executed": None},
        {"routing_replay": {"status": "mismatch"}},
    ],
)
def test_failures_are_unresolved_not_evidence_of_locality(change):
    observations = records()
    observations[0].update(change)
    assert evaluate_configuration(observations)["status"] == "unresolved"


def test_seed_contract_and_evolve_rejection():
    assert evaluate_configuration(records()[:2])["status"] == "unresolved"
    assert evaluate_configuration(records(), seeds=[4, 5, 6])["status"] == "unresolved"
    observations = records()
    observations[0]["arm"] = "evolve"
    with pytest.raises(ValueError, match="evolve"):
        evaluate_configuration(observations)


def test_all_six_configurations_and_embedding_unknown_disclosure():
    result = evaluate_input("input", complete_input())
    assert result["status"] == "admitted"
    assert result["embedding"]["status"] == "unknown"
    assert evaluate_input("input", complete_input()[:-1])["status"] == "unresolved"
    result = evaluate_input(
        "input",
        complete_input(),
        embedding={"status": "verified", "zero_routing": True, "evidence": "mapping-sha256"},
    )
    assert result["status"] == "ineligible"
    with pytest.raises(ValueError, match="requires evidence"):
        evaluate_input("input", complete_input(), embedding={"status": "verified"})


def test_structural_priority_and_fallback():
    candidates = [{"input_id": "canonical"}, {"input_id": "variant"}]
    evidence = [
        {"input_id": "canonical", "status": "admitted"},
        {"input_id": "variant", "status": "admitted"},
    ]
    groups = [
        {
            "members": ["canonical", "variant"],
            "priority": ["canonical", "variant"],
            "status": "verified",
            "evidence": "six-context-fingerprints",
        }
    ]
    result = resolve_candidates(candidates, evidence, structural_groups=groups)
    assert result["admitted"] == ["canonical"]
    evidence = deepcopy(evidence)
    evidence[0]["status"] = "ineligible"
    assert resolve_candidates(candidates, evidence, structural_groups=groups)["admitted"] == [
        "variant"
    ]


def test_unresolved_requires_recorded_explicit_decision():
    candidates = [{"input_id": "missing"}]
    assert not resolve_candidates(candidates, [])["freeze_allowed"]
    result = resolve_candidates(candidates, [], accept_unresolved=True)
    assert result["freeze_allowed"]
    assert result["conditional_on_unresolved"] and result["requires_preparation_revision"]
    assert result["admitted"] == []
