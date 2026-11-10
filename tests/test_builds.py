import os
from pathlib import Path
import subprocess

import pytest

from benchpress_compare.builds import release_policy, snapshot_source, verify_snapshot
from benchpress_compare.builds.release import (
    _dependency_constraints,
    _requirements,
    binary_identity,
    resolve_common_dependencies,
    sanitized_environment,
)
from benchpress_compare.errors import IntegrityError, PreparationError


def git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    )


@pytest.fixture
def source(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init")
    git(source, "config", "user.email", "test@example.invalid")
    git(source, "config", "user.name", "Test")
    (source / "module.py").write_text("value = 1\n")
    (source / ".gitignore").write_text("*.ignored\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "initial")
    return source


def test_snapshot_hash_tracks_dirty_untracked_deleted_and_executable(source, tmp_path):
    first = snapshot_source(source, tmp_path / "first")
    (source / "module.py").write_text("value = 2\n")
    dirty = snapshot_source(source, tmp_path / "dirty")
    assert dirty["tree_hash"] != first["tree_hash"]
    assert dirty["git_dirty"] is True
    (source / "added.py").write_text("new = 1\n")
    untracked = snapshot_source(source, tmp_path / "untracked")
    assert untracked["tree_hash"] != dirty["tree_hash"]
    (source / "module.py").unlink()
    deleted = snapshot_source(source, tmp_path / "deleted")
    assert deleted["tree_hash"] != untracked["tree_hash"]
    (source / "added.py").chmod(0o755)
    executable = snapshot_source(source, tmp_path / "executable")
    assert executable["tree_hash"] != deleted["tree_hash"]
    assert (tmp_path / "executable/source/added.py").stat().st_mode & 0o111
    assert first["git_revision"] == executable["git_revision"]


def test_output_exclusions_are_root_specific_and_tracked_files_survive(source, tmp_path):
    (source / "target").mkdir()
    (source / "target/generated").write_text("ignore")
    (source / "tests/target").mkdir(parents=True)
    (source / "tests/target/fixture").write_text("keep")
    (source / "thing.ignored").write_text("ignore")
    (source / "build").mkdir()
    (source / "build/source-file").write_text("tracked source")
    git(source, "add", "build/source-file")
    result = snapshot_source(source, tmp_path / "snapshot")
    paths = {entry["path"] for entry in result["files"]}
    assert "target/generated" not in paths
    assert "thing.ignored" not in paths
    assert "tests/target/fixture" in paths
    assert "build/source-file" in paths
    verify_snapshot(tmp_path / "snapshot")


def test_symlinks_are_preserved_and_escaping_links_rejected(source, tmp_path):
    (source / "link.py").symlink_to("module.py")
    snapshot_source(source, tmp_path / "snapshot")
    assert (tmp_path / "snapshot/source/link.py").is_symlink()
    assert os.readlink(tmp_path / "snapshot/source/link.py") == "module.py"
    (tmp_path / "outside").write_text("outside")
    (source / "escape").symlink_to("../outside")
    with pytest.raises(PreparationError, match="escapes"):
        snapshot_source(source, tmp_path / "unsafe")
    assert not (tmp_path / "unsafe").exists()


def test_snapshot_and_archive_mutation_are_rejected(source, tmp_path):
    snapshot_source(source, tmp_path / "snapshot")
    (tmp_path / "snapshot/source/module.py").write_text("corrupt")
    with pytest.raises(IntegrityError, match="contents changed"):
        verify_snapshot(tmp_path / "snapshot")
    snapshot_source(source, tmp_path / "snapshot2")
    (tmp_path / "snapshot2/source.tar").write_bytes(b"corrupt")
    with pytest.raises(IntegrityError, match="archive changed"):
        verify_snapshot(tmp_path / "snapshot2")


def test_concurrent_source_edit_aborts_snapshot(source, tmp_path, monkeypatch):
    import benchpress_compare.builds.snapshot as module

    real_copy = module.shutil.copyfile

    def mutate(origin, target):
        value = real_copy(origin, target)
        if Path(origin).name == "module.py":
            Path(origin).write_text("changed while copying")
        return value

    monkeypatch.setattr(module.shutil, "copyfile", mutate)
    with pytest.raises(PreparationError, match="changed during snapshot"):
        snapshot_source(source, tmp_path / "snapshot")
    assert not (tmp_path / "snapshot").exists()


def test_materialized_submodule_contents_affect_source_identity(source, tmp_path):
    child = source / "submodule"
    child.mkdir()
    git(child, "init")
    git(child, "config", "user.email", "test@example.invalid")
    git(child, "config", "user.name", "Test")
    (child / "implementation").write_text("one")
    git(child, "add", ".")
    git(child, "commit", "-m", "child")
    git(source, "add", "submodule")
    first = snapshot_source(source, tmp_path / "snapshot")
    assert first["submodules"][0]["path"] == "submodule"
    assert (tmp_path / "snapshot/source/submodule/implementation").read_text() == "one"
    (child / "implementation").write_text("two")
    second = snapshot_source(source, tmp_path / "snapshot2")
    assert first["tree_hash"] != second["tree_hash"]


def test_release_profile_preserves_source_defaults_and_rejects_debug(tmp_path):
    (tmp_path / "Cargo.toml").write_text('[profile.release]\nlto = "fat"\ncodegen-units = 1\n')
    policy = release_policy(tmp_path)
    assert policy["effective_profile"]["lto"] == "fat"
    assert policy["effective_profile"]["codegen-units"] == 1
    assert policy["effective_profile"]["opt-level"] == 3
    with pytest.raises(PreparationError, match="disabled"):
        release_policy(tmp_path, {"CARGO_PROFILE_RELEASE_OPT_LEVEL": "0"})
    with pytest.raises(PreparationError, match="Unsupported"):
        release_policy(tmp_path, {"RUST_DEBUG": "1"})


def test_source_cargo_config_cannot_disable_optimizations(tmp_path):
    (tmp_path / "Cargo.toml").write_text('[profile.release]\nlto = "fat"\n')
    (tmp_path / ".cargo").mkdir()
    (tmp_path / ".cargo/config.toml").write_text('[build]\nrustflags = ["-C", "opt-level=0"]\n')
    with pytest.raises(PreparationError, match="optimization"):
        release_policy(tmp_path)


def test_build_environment_drops_undeclared_flags_and_source_imports(tmp_path, monkeypatch):
    monkeypatch.setenv("RUSTFLAGS", "-C opt-level=0")
    monkeypatch.setenv("CARGO_PROFILE_RELEASE_LTO", "thin")
    monkeypatch.setenv("PYTHONPATH", "/tmp/unverified-qiskit")
    monkeypatch.setenv("QISKIT_BUILD_PROFILE", "debug")
    environment = sanitized_environment(tmp_path)
    assert (
        not {"RUSTFLAGS", "CARGO_PROFILE_RELEASE_LTO", "PYTHONPATH", "QISKIT_BUILD_PROFILE"}
        & environment.keys()
    )
    assert environment["UV_CACHE_DIR"].startswith(str(tmp_path))
    assert environment["PYTHONNOUSERSITE"] == "1"


def test_dynamic_dependency_files_are_in_common_contract(tmp_path):
    (tmp_path / "pyproject.toml").write_text("""
[build-system]
requires = ["setuptools>=77", "setuptools-rust>=1.13"]
build-backend = "setuptools.build_meta"
[project]
dynamic = ["dependencies"]
[tool.setuptools.dynamic.dependencies]
file = ["requirements.txt"]
""")
    (tmp_path / "requirements.txt").write_text("numpy>=2 # comment\nscipy>=1.5\n")
    requirements, _ = _requirements(tmp_path)
    assert "numpy>=2" in requirements
    assert "scipy>=1.5" in requirements


def test_observation_build_identity_includes_actual_wheel_and_native_binaries():
    first = binary_identity(
        "same-input-build-key", "wheel-one", {"qiskit/_accelerate.so": "native-one"}
    )
    assert first == binary_identity(
        "same-input-build-key", "wheel-one", {"qiskit/_accelerate.so": "native-one"}
    )
    assert first != binary_identity(
        "same-input-build-key", "wheel-two", {"qiskit/_accelerate.so": "native-one"}
    )
    assert first != binary_identity(
        "same-input-build-key", "wheel-one", {"qiskit/_accelerate.so": "native-two"}
    )


def test_common_resolution_pins_both_installed_and_isolated_build_toolchains(tmp_path, monkeypatch):
    from benchpress_compare.identity import file_hash

    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["setuptools>=77", "setuptools-rust>=1.13"]\n'
        'build-backend = "setuptools.build_meta"\n'
    )
    observed = {}

    def resolve(command, **kwargs):
        assert "--no-build-isolation" not in command
        installed = Path(command[command.index("--constraints") + 1])
        isolated = Path(command[command.index("--build-constraints") + 1])
        assert installed.read_bytes() == isolated.read_bytes()
        assert "setuptools==80.9.0" in isolated.read_text()
        assert "setuptools-rust==1.13.0" in installed.read_text()
        observed["constraints"] = isolated
        Path(command[command.index("--output-file") + 1]).write_text("setuptools==80.9.0\n")

    monkeypatch.setattr("benchpress_compare.builds.release._run", resolve)
    lock = resolve_common_dependencies([source, source], tmp_path / "dependencies")
    assert lock["build_constraints_hash"] == file_hash(observed["constraints"])
    assert _dependency_constraints(lock) == observed["constraints"].read_bytes()
    observed["constraints"].write_text("setuptools==84.2.1\n")
    with pytest.raises(IntegrityError, match="constraints changed"):
        _dependency_constraints(lock)


def test_old_lock_cannot_silently_select_a_new_isolated_toolchain():
    with pytest.raises(IntegrityError, match="lacks pinned build constraints"):
        _dependency_constraints({"requirements_path": "old-lock.txt"})
