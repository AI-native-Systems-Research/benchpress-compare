import math
from copy import deepcopy
from fractions import Fraction

import pytest

from benchpress_compare.aggregate import AggregationError, aggregate, normalize_query
from benchpress_compare.weights import fraction_record, read_fraction


def case(key="c", input_id="i", weight=Fraction(1), **kwargs):
    return {"case_id": key, "input_id": input_id, "seeds": [1, 2, 3], "weight": fraction_record(weight),
            "sensitivity_weight": fraction_record(weight), "family": "F1", "band": "S", "recipe": "r",
            "target_class": "square", "target_id": "square9", "level": 2, **kwargs}


def observed(cases, baseline=(10, 10, 10), evolve=(10, 10, 10), *, d2_baseline=None, d2_evolve=None):
    output = []
    for item in cases:
        for arm, n2, d2 in (("baseline", baseline, d2_baseline or baseline), ("evolve", evolve, d2_evolve or evolve)):
            for seed, value, depth in zip(item["seeds"], n2, d2):
                output.append({"observation_id": f"{item['case_id']}/{arm}/{seed}", "case_id": item["case_id"], "arm": arm,
                               "seed": seed, "status": "SUCCEEDED", "N2": value, "D2": depth})
    return output


def test_ratio_of_means_not_mean_of_ratios():
    cases = [case()]
    report = aggregate(cases, observed(cases, (1, 10, 100), (2, 10, 50)))
    assert read_fraction(report["cases"][0]["metrics"]["N2"]["ratio"]) == Fraction(62, 111)
    assert report["metrics"]["N2"]["score"] == pytest.approx(62 / 111)
    assert report["metrics"]["N2"]["score"] != pytest.approx((2 + 1 + .5) / 3)


def test_weighted_geometric_not_arithmetic_and_circuit_once():
    cases = [case("a", "i", Fraction(1, 4)), case("b", "j", Fraction(3, 4))]
    observations = observed(cases[:1], (10, 10, 10), (20, 20, 20)) + observed(cases[1:], (10, 10, 10), (5, 5, 5))
    result = aggregate(cases, observations)
    assert result["metrics"]["N2"]["score"] == pytest.approx(2 ** -.5)
    assert result["metrics"]["N2"]["circuit_counts"] == {"better": 1, "same": 0, "worse": 1, "unclassifiable": 0}


def test_crossed_metric_directions_independent():
    cases = [case("a"), case("b")]
    observations = observed(cases, (10, 10, 10), (9, 9, 9), d2_baseline=(10, 10, 10), d2_evolve=(11, 11, 11))
    result = aggregate(cases, observations)
    assert result["expected_circuits"] == 1
    assert result["metrics"]["N2"]["circuit_counts"]["better"] == 1
    assert result["metrics"]["D2"]["circuit_counts"]["worse"] == 1


def test_missing_seed_blocks_only_affected_slice_without_reweighting():
    cases = [case("a", "first", Fraction(1, 4), family="F1"), case("b", "second", Fraction(3, 4), family="F2")]
    observations = observed(cases)[:-1]
    result = aggregate(cases, observations)
    assert result["metrics"]["N2"]["score"] is None
    assert read_fraction(result["metrics"]["N2"]["available_weight"]) == Fraction(1, 4)
    assert result["metrics"]["N2"]["circuit_counts"]["unclassifiable"] == 1
    assert aggregate(cases, observations, query={"family": "F1"})["metrics"]["N2"]["score"] == 1


@pytest.mark.parametrize("b,e,reason,direction", [(0, 0, "both_zero", "same"), (0, 5, "baseline_zero", "worse"), (5, 0, "evolve_zero", "better")])
def test_zero_ratios_never_finite_score_or_circuit_win(b, e, reason, direction):
    cases = [case()]
    result = aggregate(cases, observed(cases, (b,) * 3, (e,) * 3))
    metric = result["metrics"]["N2"]
    assert metric["score"] is None
    assert metric["circuit_counts"] == {"better": 0, "same": 0, "worse": 0, "unclassifiable": 1}
    assert metric["reasons"] == {reason: 1}
    assert result["cases"][0]["metrics"]["N2"]["absolute_direction"] == direction


def test_empty_slice_and_supplemental_do_not_claim_neutral_score():
    cases = [case()]
    result = aggregate(cases, observed(cases), query={"band": "L"})
    assert result["expected_cases"] == result["expected_circuits"] == 0
    assert result["metrics"]["N2"]["reasons"] == {"empty_slice": 1}
    cases = [case(weight=0)]
    result = aggregate(cases, observed(cases))
    assert len(result["supplemental_cases"]) == 1
    assert result["expected_circuits"] == 0


def test_union_within_dimension_intersection_between_dimensions():
    cases = [case("a", family="F1", band="S"), case("b", family="F2", band="M"), case("c", family="F3", band="L")]
    result = aggregate(cases, observed(cases), query={"family": ["F1", "F2"], "band": ["S", "M"]})
    assert result["expected_cases"] == 2
    assert normalize_query({"level": ["L3", "3", 2], "target": ["square"]}) == {"level": [2, 3], "target_class": ["square"]}


def test_exact_equality_and_tiny_change_despite_display_rounding():
    cases = [case("a"), case("b")]
    observations = observed(cases[:1], (3, 3, 3), (7, 7, 7)) + observed(cases[1:], (7, 7, 7), (3, 3, 3))
    result = aggregate(cases, observations)
    assert result["metrics"]["N2"]["circuit_counts"]["same"] == 1
    big = 10 ** 18
    observations = observed(cases, (big,) * 3, (big + 1,) * 3)
    result = aggregate(cases, observations)
    assert result["metrics"]["N2"]["circuit_counts"]["worse"] == 1
    assert result["metrics"]["N2"]["score"] == 1.0  # display precision cannot determine classification


def test_overall_is_geometric_mean_of_equal_mass_levels():
    cases = [case("l2", "i", Fraction(1, 2), level=2), case("l3", "i", Fraction(1, 2), level=3)]
    observations = observed(cases[:1], (10,) * 3, (20,) * 3) + observed(cases[1:], (10,) * 3, (5,) * 3)
    score = aggregate(cases, observations)["metrics"]["N2"]["score"]
    per_level = [aggregate(cases, observations, query={"level": level})["metrics"]["N2"]["score"] for level in (2, 3)]
    assert score == pytest.approx(math.sqrt(math.prod(per_level)))


def test_uniform_scaling_returns_multiplier():
    cases = [case(str(i), str(i), Fraction(i + 1, 6)) for i in range(3)]
    result = aggregate(cases, observed(cases, (10, 30, 20), (30, 90, 60)))
    assert result["metrics"]["N2"]["score"] == pytest.approx(3)


def test_metric_specific_unavailable_preserves_other_metric():
    cases = [case()]
    observations = observed(cases)
    observations[0]["D2"] = None
    observations[0]["metric_reasons"] = {"D2": "unsupported_loop"}
    result = aggregate(cases, observations)
    assert result["metrics"]["N2"]["available"]
    assert not result["metrics"]["D2"]["available"]


@pytest.mark.parametrize("invalid", [-1, float("nan"), float("inf"), True, 1.2])
def test_invalid_metric_is_integrity_error(invalid):
    cases = [case()]
    observations = observed(cases)
    observations[0]["N2"] = invalid
    result = aggregate(cases, observations)
    assert result["metrics"]["N2"]["integrity_errors"] == 1
    assert result["metrics"]["N2"]["score"] is None


def test_conflicting_successful_receipts_are_not_cherry_picked():
    cases = [case()]
    observations = observed(cases)
    conflict = deepcopy(observations[0])
    conflict["N2"] = 9
    with pytest.raises(AggregationError, match="conflicting"):
        aggregate(cases, observations + [conflict])


def test_preflight_data_cannot_become_comparison_measurements():
    cases = [case()]
    observations = [{**item, "phase": "preflight"} for item in observed(cases)]
    result = aggregate(cases, observations)
    assert result["received_observations"] == 0
    assert result["metrics"]["N2"]["score"] is None
