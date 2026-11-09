from fractions import Fraction
from itertools import product
import json
from pathlib import Path

import pytest

from benchpress_compare.weights import (
    FAMILIES,
    LEVELS,
    TARGETS,
    WeightError,
    allocate_weights,
    assign_case_weights,
    audit_case_weights,
    read_fraction,
    width_band,
)


def inputs():
    return [
        {"input_id": f"{family}/{band}", "family": family, "band": band, "recipe": "recipe"}
        for family, band in product(FAMILIES, ("S", "M", "L"))
    ]


def cases(items):
    return [
        {
            **item,
            "case_id": f"{item['input_id']}/{target}/{level}",
            "target_class": target,
            "level": level,
            "seeds": [10, 20, 30],
        }
        for item, target, level in product(items, TARGETS, LEVELS)
    ]


def test_exact_full_marginals_and_sensitivity():
    allocation = allocate_weights(inputs())
    assert read_fraction(allocation["total_weight"]) == 1
    assert read_fraction(allocation["marginals"]["band"]["L"]) == Fraction(1, 2)
    assert read_fraction(allocation["marginals"]["sensitivity_band"]["L"]) == Fraction(1, 3)
    audit = audit_case_weights(assign_case_weights(cases(inputs()), allocation))
    assert audit["input_count"] == 24
    for field in ("weight", "sensitivity_weight"):
        assert read_fraction(audit["marginals"][field]["target"]["square"]) == Fraction(1, 3)
        assert read_fraction(audit["marginals"][field]["level"]["3"]) == Fraction(1, 2)
        assert all(
            read_fraction(value) == Fraction(1, 48)
            for value in audit["marginals"][field]["family_target_level"].values()
        )


def test_missing_bands_normalize_only_within_family():
    pool = [item for item in inputs() if not (item["family"] == "F7" and item["band"] == "L")]
    allocation = allocate_weights(pool)
    assert read_fraction(allocation["inputs"]["F7/M"]["input_weight"]) == Fraction(3, 40)
    assert read_fraction(allocation["inputs"]["F7/S"]["input_weight"]) == Fraction(1, 20)
    assert read_fraction(allocation["inputs"]["F7/M"]["sensitivity_input_weight"]) == Fraction(
        1, 16
    )


def test_recipe_count_does_not_become_input_count_weighting():
    pool = inputs()
    pool += [
        {"input_id": f"extra/{i}", "family": "F1", "band": "S", "recipe": "second"}
        for i in range(4)
    ]
    allocation = allocate_weights(pool)
    assert read_fraction(allocation["inputs"]["F1/S"]["input_weight"]) == Fraction(1, 80)
    assert read_fraction(allocation["inputs"]["extra/0"]["input_weight"]) == Fraction(1, 320)


def test_empty_family_stops_full_freeze_and_smoke_preserves_absent_mass():
    pool = [item for item in inputs() if item["family"] != "F8"]
    with pytest.raises(WeightError, match="empty families"):
        allocate_weights(pool)
    allocation = allocate_weights(pool, require_all_families=False)
    assert read_fraction(allocation["total_weight"]) == Fraction(7, 8)
    assert allocation["missing_families"] == ["F8"]


def test_duplicate_aliases_and_verified_structures_rejected():
    pool = inputs()
    pool[0]["aliases"] = [pool[1]["input_id"]]
    with pytest.raises(WeightError, match="duplicate"):
        allocate_weights(pool)
    pool = inputs()
    pool[0]["verified_structural_group"] = pool[1]["verified_structural_group"] = "duplicate"
    with pytest.raises(WeightError, match="structural"):
        allocate_weights(pool)


def test_active_width_must_agree_and_case_coverage_is_exact():
    pool = inputs()
    pool[0]["active_width"] = 50
    with pytest.raises(WeightError, match="band disagrees"):
        allocate_weights(pool)
    with pytest.raises(WeightError, match="six"):
        assign_case_weights(cases(inputs())[:-1])


@pytest.mark.parametrize(
    "width,band", [(8, "S"), (24, "S"), (25, "M"), (64, "M"), (65, "L"), (133, "L")]
)
def test_active_band_boundaries(width, band):
    assert width_band(width) == band


def test_unsupported_width_and_float_weight_rejected():
    for width in (7, 134, True, 25.0):
        with pytest.raises(WeightError):
            width_band(width)
    with pytest.raises(WeightError):
        read_fraction(0.125)


def test_full_catalogue_reproduces_every_conditional_planning_fraction():
    catalogue = json.loads((Path(__file__).parents[1] / "data" / "candidates.json").read_text())
    pool = [item for item in catalogue["candidates"] if item["category"] == "main"]
    assert len(pool) == 138
    allocation = allocate_weights(pool)
    for item in pool:
        assert read_fraction(allocation["inputs"][item["input_id"]]["case_weight"]) == Fraction(
            item["planning_case_weight"]
        )
    assert {
        band: read_fraction(value) for band, value in allocation["marginals"]["band"].items()
    } == {"S": Fraction(7, 40), "M": Fraction(57, 160), "L": Fraction(15, 32)}
    assert read_fraction(allocation["inputs"]["Q/large/vqe_uccsd_n28"]["input_weight"]) == Fraction(
        3, 40
    )
