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


def test_streamed_gzip_is_canonical_lossless_and_deterministic(tmp_path):
    import gzip
    from dataclasses import dataclass
    from enum import Enum
    from fractions import Fraction
    from pathlib import Path
    from benchpress_compare.identity import atomic_write_json_gzip, canonical_bytes, file_hash

    class Choice(Enum):
        VALUE = "value"

    @dataclass
    class Record:
        field: str

    value = {
        "unicode": "λ雪", "number": -1.25e-30, "tuple": (True, None, 3),
        "fraction": Fraction(2, 3), "enum": Choice.VALUE, "path": Path("some/path"),
        "dataclass": Record("data"),
        "operations": [{"qubits": [0, 1], "name": "cx", "params": []}] * 10_000,
    }
    first, second = tmp_path / "first.json.gz", tmp_path / "second.json.gz"
    atomic_write_json_gzip(first, value)
    atomic_write_json_gzip(second, dict(reversed(list(value.items()))))
    assert gzip.decompress(first.read_bytes()) == canonical_bytes(value) + b"\n"
    assert file_hash(first) == file_hash(second)
    assert first.stat().st_size < len(canonical_bytes(value)) // 20
    assert first.read_bytes()[4:8] == b"\0\0\0\0"


@pytest.mark.parametrize("invalid", [{"nested": [{1: "bad key"}]}, {"bad": float("nan")}])
def test_failed_streamed_gzip_preserves_destination_and_cleans_temporary_file(tmp_path, invalid):
    from benchpress_compare.identity import atomic_write_json_gzip

    destination = tmp_path / "output.json.gz"
    atomic_write_json_gzip(destination, {"original": True})
    before = destination.read_bytes()
    with pytest.raises((TypeError, ValueError)):
        atomic_write_json_gzip(destination, invalid)
    assert destination.read_bytes() == before
    assert list(tmp_path.iterdir()) == [destination]


def test_compressed_artifact_corruption_is_detected_by_bundle_hash(tmp_path):
    from benchpress_compare.identity import atomic_write_json_gzip
    from benchpress_compare.store import publish_bundle, verify_bundle

    artifact = tmp_path / "output.json.gz"
    atomic_write_json_gzip(artifact, {"operations": [{"name": "cx", "qubits": [0, 1]}]})
    bundle = tmp_path / "bundle"
    publish_bundle(bundle, {"schema_version": 1}, {artifact.name: artifact})
    assert verify_bundle(bundle)
    with (bundle / artifact.name).open("r+b") as stream:
        stream.seek(-1, 2)
        original = stream.read(1)
        stream.seek(-1, 2)
        stream.write(bytes([original[0] ^ 1]))
    with pytest.raises(IntegrityError, match="checksum"):
        verify_bundle(bundle)
