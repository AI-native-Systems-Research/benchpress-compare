from fractions import Fraction
from pathlib import Path
import re

import pytest

from benchpress_compare.corpus import load_catalogue
from benchpress_compare.errors import CompareError
from benchpress_compare.planner import BIG_CIRCUITS
from benchpress_compare.profiles import load_profile, select_candidates
from benchpress_compare.weights import (
    WeightError, allocate_weights, assign_case_weights, audit_case_weights, read_fraction,
)


@pytest.mark.parametrize("name,count", [("evolve", 44), ("small", 22)])
def test_subset_manifest_and_every_weight_match_the_specification(name, count):
    manifest = load_profile(name)
    specification = Path(__file__).parents[1] / manifest["specification"]["path"]
    expected = {}
    for line in specification.read_text().splitlines():
        match = re.match(r"^\| `([QFHS]/[^`]+)` \|", line)
        if match:
            expected[match[1]] = Fraction(line.split("|")[5].strip())
    selected = select_candidates(manifest, load_catalogue()["candidates"])
    assert len(selected) == count
    assert set(manifest["input_ids"]) == set(expected) == {row["input_id"] for row in selected}
    allocation = allocate_weights(selected, targets=manifest["targets"], levels=manifest["levels"])
    assert {
        key: read_fraction(entry["case_weight"]) for key, entry in allocation["inputs"].items()
    } == expected
    assert all(read_fraction(value) == Fraction(1, 8)
               for value in allocation["marginals"]["family"].values())
    bands = {key: read_fraction(value) for key, value in allocation["marginals"]["band"].items()}
    assert bands == (
        {"S": Fraction(7, 40), "M": Fraction(57, 160), "L": Fraction(15, 32)}
        if name == "evolve" else {"S": Fraction(3, 8), "M": Fraction(5, 8)}
    )


def test_small_is_contained_in_evolve_and_excludes_big_circuits():
    small, evolve = (set(load_profile(name)["input_ids"]) for name in ("small", "evolve"))
    assert small < evolve
    assert not small & BIG_CIRCUITS.keys()
    assert len(evolve & BIG_CIRCUITS.keys()) == 3


def test_selection_fails_for_unknown_or_non_main_inputs():
    manifest = load_profile("small")
    candidates = select_candidates(manifest, load_catalogue()["candidates"])
    with pytest.raises(CompareError, match="Unknown profile"):
        select_candidates(manifest, candidates[:-1])
    candidates[0] = dict(candidates[0], category="additional")
    with pytest.raises(CompareError, match="main-table"):
        select_candidates(manifest, candidates)


def test_subset_weight_audit_requires_exactly_the_declared_case():
    selected = select_candidates(load_profile("small"), load_catalogue()["candidates"])
    cases = [dict(row, case_id=row["input_id"], target_class="heavy-hex", level=2, seeds=[1, 2, 3])
             for row in selected]
    weighted = assign_case_weights(cases, targets=["heavy-hex"], levels=[2])
    audit = audit_case_weights(weighted, targets=["heavy-hex"], levels=[2])
    assert audit["case_count"] == audit["input_count"] == 22
    assert read_fraction(audit["marginals"]["weight"]["target"]["heavy-hex"]) == 1
    for changed in (
        weighted[:-1],
        weighted + [dict(weighted[0], case_id="duplicate-case")],
        [dict(weighted[0], target_class="square"), *weighted[1:]],
        [dict(weighted[0], level=3), *weighted[1:]],
    ):
        with pytest.raises(WeightError):
            audit_case_weights(changed, targets=["heavy-hex"], levels=[2])
