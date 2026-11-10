from pathlib import Path

import pytest

from benchpress_compare.config import RunConfig, SiteConfig
from benchpress_compare.errors import CompareError


def sources(tmp_path):
    for name in ("baseline", "evolve"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "pyproject.toml").write_text("[project]\nname='qiskit'\n")
    return {key: str(tmp_path / key) for key in ("baseline", "evolve", "store", "results_root")}


def test_paths_and_defaults(tmp_path):
    cfg = RunConfig(**sources(tmp_path)).validate()
    assert cfg.seeds == 3
    assert cfg.profile == "full" and cfg.design == "benchpress-eight-family-v1"
    assert cfg.wall_seconds == 3600
    assert Path(cfg.store).is_absolute()
    cfg.store = str(Path(cfg.baseline) / "cache")
    with pytest.raises(CompareError, match="source"):
        cfg.validate()


def test_store_results_overlap(tmp_path):
    cfg = RunConfig(**sources(tmp_path))
    cfg.results_root = str(Path(cfg.store) / "run")
    with pytest.raises(CompareError, match="overlap"):
        cfg.validate()


def test_site_has_no_implicit_queue(tmp_path):
    path = tmp_path / "site.toml"
    path.write_text("[lsf]\nmax_jobs=4\n")
    with pytest.raises(CompareError):
        SiteConfig.read(path)
    path.write_text('[lsf]\nqueue="test"\nmemory_unit="MB"\n')
    assert SiteConfig.read(path).queue == "test"


@pytest.mark.parametrize("profile,wall", [("evolve", 900), ("small", 60)])
def test_subset_defaults_and_explicit_watchdog(tmp_path, profile, wall):
    from benchpress_compare.identity import canonical_hash
    from benchpress_compare.profiles import load_profile

    paths = sources(tmp_path)
    cfg = RunConfig(**paths, profile=profile).validate()
    assert cfg.wall_seconds == wall
    assert cfg.design == f"benchpress-{profile}-v1"
    assert cfg.complete_families_required
    assert cfg.profile_manifest_hash == canonical_hash(load_profile(profile))
    assert RunConfig(**paths, profile=profile, wall_seconds=123).validate().wall_seconds == 123
    with pytest.raises(CompareError, match="manifest changed"):
        RunConfig(**paths, profile=profile, profile_manifest_hash="stale").validate()


@pytest.mark.parametrize("options", [{"smoke": ["sample"]}, {"allow_missing_families": True}])
def test_subset_cannot_disable_eight_family_requirement(tmp_path, options):
    with pytest.raises(CompareError, match="Subset profiles cannot"):
        RunConfig(**sources(tmp_path), profile="small", **options).validate()


def test_unknown_profile_is_rejected(tmp_path):
    with pytest.raises(CompareError, match="profile must"):
        RunConfig(**sources(tmp_path), profile="unknown").validate()
