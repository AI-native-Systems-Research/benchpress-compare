import pytest

from benchpress_compare.identity import canonical_hash, observation_identity
from benchpress_compare.schema import validate_record
from benchpress_compare.errors import IntegrityError


def test_types_and_mapping_order():
    assert canonical_hash({"a": 1, "b": 2}) == canonical_hash({"b": 2, "a": 1})
    assert canonical_hash(1) != canonical_hash("1")
    with pytest.raises(ValueError):
        canonical_hash(float("nan"))


def test_observation_reuse_is_independent_of_cohort_and_attempt():
    spec = dict(
        case_id="c",
        build_id="b",
        phase="comparison",
        seed=3,
        runtime_profile={"native_threads": 1},
        protocols={"metric": "v1"},
    )
    key = observation_identity(spec)
    assert observation_identity(dict(spec, arm="evolve", attempt_id="retry", weight=0)) == key
    assert observation_identity(dict(spec, phase="pilot")) != key
    assert observation_identity(dict(spec, runtime_profile={"native_threads": 2})) != key


def test_incompatible_record_rejected():
    with pytest.raises(IntegrityError):
        validate_record("ObservationResult", {"schema_version": 2})
