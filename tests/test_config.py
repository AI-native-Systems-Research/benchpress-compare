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
