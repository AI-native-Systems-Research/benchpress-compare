import pytest

from benchpress_compare.seeds import SEED_RANGES, derive_seeds, seed_contract


def test_streams_disjoint_reproducible_and_prefix_stable():
    streams = {phase: derive_seeds("case/α", phase, 100) for phase in SEED_RANGES}
    assert len(set(seed for seeds in streams.values() for seed in seeds)) == 400
    for phase, seeds in streams.items():
        assert seeds[:3] == derive_seeds("case/α", phase, 3)
        lower, upper = SEED_RANGES[phase]
        assert all(lower <= seed < upper for seed in seeds)
        assert seeds != derive_seeds("case/α", phase, 100, 42)
    assert seed_contract()["master_seed"] == 20261109


def test_generation_identity_is_source_qualified_recipe():
    assert derive_seeds("Q/large/qft@1", "generation", 3) != derive_seeds("Q/large/qft@2", "generation", 3)


@pytest.mark.parametrize("args", [("", "comparison", 1), ("case", "unknown", 1), ("case", "comparison", -1), ("case", "comparison", True), ("case", "comparison", 1 << 30)])
def test_invalid_streams(args):
    with pytest.raises(ValueError):
        derive_seeds(*args)


def test_collision_rejection_preserves_prefix(monkeypatch):
    monkeypatch.setitem(SEED_RANGES, "pilot", (10, 14))
    seeds = derive_seeds("tiny-range", "pilot", 4)
    assert set(seeds) == set(range(10, 14))
    assert seeds[:3] == derive_seeds("tiny-range", "pilot", 3)
