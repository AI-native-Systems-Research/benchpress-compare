"""Self-contained archives of verified result artifacts and shared references."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
import tarfile
import tempfile

from .builds import verify_build, verify_snapshot
from .errors import CompareError, IntegrityError
from .identity import atomic_write_json, file_hash, fsync_directory, read_json
from .orchestrator import verify_plan
from .store import Lease, Store, verify_bundle


def _reference(path, store, references):
    path = Path(path).resolve()
    if not path.is_relative_to(store.root):
        raise IntegrityError(f"Baseline reference escapes its shared store: {path}")
    references[str(path)] = "shared-store/" + path.relative_to(store.root).as_posix()


def _entries(root: Path, excluded: set[Path]) -> list[Path]:
    entries = [root]
    for directory, directories, filenames in os.walk(root, followlinks=False):
        parent = Path(directory)
        for name in directories[:]:
            path = parent / name
            if path in excluded:
                directories.remove(name)
            else:
                entries.append(path)
                if path.is_symlink():
                    directories.remove(name)
        entries.extend(parent / name for name in filenames if parent / name not in excluded)
    return sorted(entries)


def _archive_name(path: Path, mappings: dict[Path, str]) -> str:
    for root in sorted(mappings, key=lambda value: len(value.parts), reverse=True):
        if path.is_relative_to(root):
            suffix = path.relative_to(root).as_posix()
            return mappings[root] if suffix == "." else mappings[root] + "/" + suffix
    raise IntegrityError(f"Archive symlink refers to an external file: {path}")


class _HashedReader:
    def __init__(self, stream):
        self.stream = stream
        self.digest = hashlib.sha256()

    def read(self, size=-1):
        data = self.stream.read(size)
        self.digest.update(data)
        return data


def _add_verified_tree(archive, root, mappings, excluded):
    inventory = []
    for path in _entries(root, excluded):
        info = path.lstat()
        expected = file_hash(path) if stat.S_ISREG(info.st_mode) else None
        inventory.append((path, info.st_size, info.st_mtime_ns, info.st_mode, expected))
    for path, size, mtime, mode, expected in inventory:
        archive_name = _archive_name(path, mappings)
        member = archive.gettarinfo(str(path), arcname=archive_name)
        if member.issym():
            target = path.resolve(strict=True)
            if any(target == excluded_path or target.is_relative_to(excluded_path) for excluded_path in excluded):
                raise IntegrityError(f"Archive link depends on an excluded environment: {path}")
            target_name = _archive_name(target, mappings)
            member.linkname = os.path.relpath(target_name, str(PurePosixPath(archive_name).parent))
            archive.addfile(member)
        elif member.isfile():
            with path.open("rb") as content:
                reader = _HashedReader(content)
                archive.addfile(member, reader)
                if reader.digest.hexdigest() != expected:
                    raise IntegrityError(f"Artifact changed during export: {path}")
        elif member.isdir():
            archive.addfile(member)
        elif member.islnk():
            # tarfile generates these only for a regular inode already added
            # under another archive name; preserve uv's hardlinked artifacts.
            if member.linkname not in archive.getnames():
                raise IntegrityError(f"Archive hardlink has no included target: {path}")
            archive.addfile(member)
        else:
            raise IntegrityError(f"Unsupported archive artifact: {path}")
        after = path.lstat()
        if (after.st_size, after.st_mtime_ns, after.st_mode) != (size, mtime, mode):
            raise IntegrityError(f"Artifact changed during export: {path}")
    if _entries(root, excluded) != [item[0] for item in inventory]:
        raise IntegrityError(f"Artifact inventory changed during export: {root}")


def export_run(root, output):
    root, output = Path(root).resolve(), Path(output).expanduser().resolve()
    if not (root / "request.json").is_file():
        raise CompareError("Export requires an existing comparison request")
    request = read_json(root / "request.json")
    store = Store(request["config"]["store"])
    if output.is_relative_to(root) or output.is_relative_to(store.root):
        raise CompareError("Export destination must be outside the results and shared store")
    if output.exists():
        raise CompareError("Export destination already exists; choose a new archive path")
    output.parent.mkdir(parents=True, exist_ok=True)
    # A read-only snapshot must not race central journal/plan publication. No
    # running controller is stopped: an active owner's lease makes export fail.
    with Lease(root / "state/controller.lease"):
        references, excluded = {}, {root / "state/controller.lease"}
        if (root / "plan.json").exists():
            verify_plan(root, check_harness=False)
        source_refs = root / "snapshots/references.json"
        if source_refs.exists():
            for arm, location in read_json(source_refs)["paths"].items():
                source = Path(location)
                verify_snapshot(source)
                if arm == "baseline":
                    _reference(source, store, references)
                elif not source.resolve().is_relative_to(root):
                    raise IntegrityError("Evolved snapshot lies outside the comparison")
        for manifest_path in [root / "builds/baseline.json", root / "builds/evolve/manifest.json"]:
            if manifest_path.exists():
                record = read_json(manifest_path)
                build = Path(record["manifest_path"]).parent.resolve()
                verify_build(build)
                excluded.add(build / "env")
                if manifest_path.name == "baseline.json":
                    _reference(build, store, references)
                elif not build.is_relative_to(root):
                    raise IntegrityError("Evolved build lies outside the comparison")
        # Every accepted result is checked, including preparation and pilot data.
        for parent in [root / "observations", *(root / phase / "observations" for phase in ("tasks", "preflight", "pilot"))]:
            for directory in sorted(parent.glob("*")):
                if not directory.is_dir() or directory.name.startswith("."):
                    continue
                bundle = verify_bundle(directory)
                if bundle is None:
                    raise IntegrityError(f"Accepted observation bundle is incomplete: {directory}")
                record = bundle["record"]
                if record.get("cache_key"):
                    key = record["cache_key"]
                    if store.get("baseline/observations", key) is None:
                        raise IntegrityError(f"Referenced cache entry is incomplete: {key}")
                    cached = store.path("baseline/observations", key)
                    if record.get("cache_manifest_hash") != file_hash(cached / "manifest.json"):
                        raise IntegrityError(f"Referenced cache manifest changed: {key}")
                    _reference(cached, store, references)
        mappings = {root: "results", **{Path(path): name for path, name in references.items()}}
        descriptor, temporary = tempfile.mkstemp(prefix=".benchpress-export-", dir=output.parent)
        os.close(descriptor)
        try:
            with tempfile.TemporaryDirectory(dir=output.parent) as meta:
                metadata_path = Path(meta) / "export.json"
                atomic_write_json(metadata_path, {
                    "schema_version": 1, "source_results": str(root), "references": references,
                    "excluded_environments": sorted(str(path) for path in excluded if path.name == "env"),
                    "environment_relocation": "recreate from included wheels and dependency lock; virtualenvs contain nonrelocatable interpreter links",
                })
                with tarfile.open(temporary, "w", dereference=False) as archive:
                    for source in mappings:
                        _add_verified_tree(archive, source, mappings, excluded)
                    archive.add(metadata_path, arcname="export.json")
            with open(temporary, "rb") as stream:
                os.fsync(stream.fileno())
            # Exclusive publication avoids replacing an archive another client
            # created while this potentially long export was being assembled.
            os.link(temporary, output)
            fsync_directory(output.parent)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return {"schema_version": 1, "path": str(output), "sha256": file_hash(output),
            "store_references": len(references)}
