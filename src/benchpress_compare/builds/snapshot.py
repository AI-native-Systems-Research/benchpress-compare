"""Content snapshots, independent of Git cleanliness or branch identity."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
from pathlib import Path

from ..errors import IntegrityError, PreparationError
from ..identity import atomic_write_json, canonical_hash, file_hash, fsync_directory

# Only root build locations are excluded for untracked files. A source directory
# called e.g. tests/target is meaningful and must not disappear from snapshots.
ROOT_OUTPUTS = {
    ".git",
    ".venv",
    "venv",
    "build",
    "dist",
    "target",
    ".tox",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
}


def _git(source: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(source), *args], capture_output=True, check=False)


def _ignored_output(relative: Path) -> bool:
    return (
        relative.parts[0] in ROOT_OUTPUTS
        or "__pycache__" in relative.parts
        or relative.suffix == ".pyc"
    )


def _source_paths(source: Path) -> tuple[list[Path], list[dict]]:
    probe = _git(source, "rev-parse", "--show-toplevel")
    git_root = Path(os.fsdecode(probe.stdout).strip()).resolve() if probe.returncode == 0 else None
    paths: set[Path] = set()
    submodules = []
    if git_root == source:
        tracked = _git(source, "ls-files", "--cached", "-z")
        untracked = _git(source, "ls-files", "--others", "--exclude-standard", "-z")
        if tracked.returncode or untracked.returncode:
            raise PreparationError(f"Cannot enumerate source Git contents: {source}")
        paths.update(Path(os.fsdecode(name)) for name in tracked.stdout.split(b"\0") if name)
        paths.update(
            Path(os.fsdecode(name))
            for name in untracked.stdout.split(b"\0")
            if name and not _ignored_output(Path(os.fsdecode(name)))
        )
        for relative in sorted(paths.copy()):
            path = source / relative
            if path.is_dir() and not path.is_symlink():
                paths.remove(relative)
                subpaths, nested = _source_paths(path)
                if not subpaths:
                    raise PreparationError(f"Submodule is empty or uninitialized: {relative}")
                paths.update(relative / child for child in subpaths)
                revision = _git(path, "rev-parse", "HEAD")
                if revision.returncode:
                    raise PreparationError(f"Submodule is not materialized: {relative}")
                submodules.append(
                    {"path": relative.as_posix(), "revision": os.fsdecode(revision.stdout).strip()}
                )
                submodules.extend(
                    {**record, "path": (relative / record["path"]).as_posix()} for record in nested
                )
    else:
        for directory, directories, files in os.walk(source, followlinks=False):
            root = Path(directory)
            for name in directories[:]:
                path = root / name
                relative = path.relative_to(source)
                if _ignored_output(relative) or name == ".git":
                    directories.remove(name)
                elif path.is_symlink():
                    paths.add(relative)
                    directories.remove(name)
            paths.update(
                (root / name).relative_to(source)
                for name in files
                if not _ignored_output((root / name).relative_to(source))
            )
    return sorted(paths), sorted(submodules, key=lambda record: record["path"])


def tree_manifest(source: Path | str, *, copied: bool = False) -> tuple[list[dict], list[dict]]:
    source = Path(source).resolve()
    paths, submodules = _source_paths(source)
    if copied:
        # The frozen copy has no Git index; unlike untracked live files, every
        # copied path is part of the source identity, including tracked outputs.
        paths = sorted(
            path.relative_to(source)
            for path in source.rglob("*")
            if path.is_symlink() or path.is_file()
        )
        submodules = []
    records = []
    for relative in paths:
        path = source / relative
        try:
            info = path.lstat()
        except FileNotFoundError:
            # Deleted tracked paths contribute through their absence.
            continue
        if stat.S_ISLNK(info.st_mode):
            link = os.readlink(path)
            try:
                target = path.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise PreparationError(f"Broken or cyclic source link: {relative}") from exc
            if not target.is_relative_to(source):
                raise PreparationError(f"Source link escapes tree: {relative}")
            records.append(
                {
                    "path": relative.as_posix(),
                    "type": "symlink",
                    "mode": stat.S_IMODE(info.st_mode),
                    "target": link,
                }
            )
        elif stat.S_ISREG(info.st_mode):
            records.append(
                {
                    "path": relative.as_posix(),
                    "type": "file",
                    "mode": stat.S_IMODE(info.st_mode),
                    "sha256": file_hash(path),
                    "size": info.st_size,
                }
            )
        else:
            raise PreparationError(f"Unsupported source entry: {relative}")
    return records, submodules


def verify_snapshot(directory: Path | str) -> dict:
    directory = Path(directory)
    try:
        manifest = json.loads((directory / "snapshot.json").read_text())
    except (OSError, ValueError) as exc:
        raise IntegrityError(f"Invalid source snapshot: {directory}") from exc
    if manifest.get("schema_version") != 1:
        raise IntegrityError("Unsupported source snapshot schema")
    files, _ = tree_manifest(directory / "source", copied=True)
    if files != manifest.get("files") or canonical_hash(files) != manifest.get("tree_hash"):
        raise IntegrityError(f"Source snapshot contents changed: {directory}")
    if file_hash(directory / "source.tar") != manifest.get("archive_hash"):
        raise IntegrityError(f"Source archive changed: {directory}")
    return manifest


def snapshot_source(source: Path | str, destination: Path | str) -> dict:
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if (
        not source.is_dir()
        or destination.is_relative_to(source)
        or source.is_relative_to(destination)
    ):
        raise PreparationError("Source must be a directory separate from its snapshot destination")
    before, submodules = tree_manifest(source)
    if not before:
        raise PreparationError(f"Source tree is empty: {source}")
    tree_hash = canonical_hash(before)
    if destination.exists():
        previous = verify_snapshot(destination)
        if previous["tree_hash"] != tree_hash:
            raise IntegrityError(
                f"Snapshot destination already contains different sources: {destination}"
            )
        return previous
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=destination.parent))
    try:
        tree = staging / "source"
        tree.mkdir()
        for entry in before:
            source_file, target = source / entry["path"], tree / entry["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            if entry["type"] == "symlink":
                # Normalize absolute in-tree symlinks into portable relative
                # paths. Absolute symlinks require a different representation.
                if os.path.isabs(entry["target"]):
                    raise PreparationError(
                        f"Absolute source symlink is unsupported; use a relative link: {entry['path']}"
                    )
                target.symlink_to(entry["target"])
            else:
                shutil.copyfile(source_file, target)
                target.chmod(entry["mode"])
                with target.open("rb") as stream:
                    os.fsync(stream.fileno())
        copied, _ = tree_manifest(tree, copied=True)
        after, after_submodules = tree_manifest(source)
        if before != after or submodules != after_submodules or copied != before:
            raise PreparationError("Source changed during snapshot; retry with a stable tree")
        archive = staging / "source.tar"
        with tarfile.open(archive, "w", format=tarfile.PAX_FORMAT) as output:
            for entry in before:
                path = tree / entry["path"]
                info = output.gettarinfo(str(path), arcname=entry["path"])
                info.uid = info.gid = info.mtime = 0
                info.uname = info.gname = ""
                if info.isfile():
                    with path.open("rb") as content:
                        output.addfile(info, content)
                else:
                    output.addfile(info)
        with archive.open("rb") as stream:
            os.fsync(stream.fileno())
        revision = _git(source, "rev-parse", "HEAD")
        status = _git(source, "status", "--porcelain", "--untracked-files=all")
        manifest = {
            "schema_version": 1,
            "tree_hash": tree_hash,
            "files": before,
            "source_location": str(source),
            "archive_hash": file_hash(archive),
            "git_revision": os.fsdecode(revision.stdout).strip()
            if revision.returncode == 0
            else None,
            "git_dirty": bool(status.stdout) if status.returncode == 0 else None,
            "submodules": submodules,
            "exclusion_policy": {
                "version": 1,
                "untracked_root_outputs": sorted(ROOT_OUTPUTS),
                "generated_names": ["__pycache__", "*.pyc"],
            },
        }
        atomic_write_json(staging / "snapshot.json", manifest)
        for directory, _, _ in os.walk(staging, topdown=False):
            fsync_directory(Path(directory))
        # Source publications use a unique destination selected by the owner.
        # Shared callers hold their per-key store lease around this operation.
        os.rename(staging, destination)
        fsync_directory(destination.parent)
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging)
