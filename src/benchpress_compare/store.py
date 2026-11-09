"""Verified immutable bundles and fenced filesystem leases.

The filesystem must support exclusive creation, atomic rename and POSIX advisory
locks across clients. Deployments must exercise these operations on their actual
shared filesystem before treating the store as durable.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator, Mapping

from .identity import atomic_write_json, canonical_hash, file_hash
from .errors import IntegrityError, ReconciliationError


class LeaseBusy(ReconciliationError):
    """Another owner holds an unexpired lease."""


class LeaseLost(ReconciliationError):
    """The owner's generation is no longer authorized to publish."""


def fsync_directory(path: Path) -> bool:
    """Return whether directory fsync is supported by this filesystem."""
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return True
    except OSError as exc:
        import errno

        if exc.errno not in {errno.EINVAL, errno.ENOTSUP, errno.EBADF}:
            raise
        return False


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise IntegrityError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise IntegrityError(f"Unsupported record schema: {path}")
    return value


@contextlib.contextmanager
def _guard(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    # This inode is permanent: deleting it would let two generations lock
    # different inodes under the same pathname.
    with path.open("a+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


class Lease:
    def __init__(self, path: Path | str, *, ttl: float = 120, heartbeat: bool = True):
        if ttl <= 0:
            raise ValueError("Lease TTL must be positive")
        self.path = Path(path)
        self.ttl = ttl
        self.heartbeat_enabled = heartbeat
        self.token = uuid.uuid4().hex
        self.generation: int | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._failure: BaseException | None = None

    @property
    def guard_path(self) -> Path:
        return self.path.with_name(self.path.name + ".guard")

    def acquire(self) -> "Lease":
        with _guard(self.guard_path):
            if self.path.exists():
                previous = _read_json(self.path)
                if previous["expires_at"] > time.time():
                    raise LeaseBusy(f"Lease held by {previous['owner']}: {self.path}")
            generation_path = self.path.with_name(self.path.name + ".generation")
            previous_generation = (
                _read_json(generation_path)["generation"] if generation_path.exists() else 0
            )
            self.generation = previous_generation + 1
            atomic_write_json(generation_path, {"schema_version": 1, "generation": self.generation})
            record = self._record()
            # Replacement of an expired owner is serialized by the permanent
            # guard. The first owner also uses O_EXCL, making creation explicit.
            if self.path.exists():
                atomic_write_json(self.path, record)
            else:
                descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "w") as stream:
                    json.dump(record, stream, sort_keys=True)
                    stream.flush()
                    os.fsync(stream.fileno())
            fsync_directory(self.path.parent)
        if self.heartbeat_enabled:
            self._thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
            self._thread.start()
        return self

    def _record(self) -> dict:
        return {
            "schema_version": 1,
            "token": self.token,
            "generation": self.generation,
            "owner": {"host": socket.gethostname(), "pid": os.getpid()},
            "expires_at": time.time() + self.ttl,
        }

    def _assert_locked(self) -> None:
        if self._failure is not None:
            raise LeaseLost(f"Lease heartbeat failed: {self._failure}")
        if self.generation is None or not self.path.exists():
            raise LeaseLost(f"Lease is not held: {self.path}")
        current = _read_json(self.path)
        if (current.get("token"), current.get("generation")) != (self.token, self.generation):
            raise LeaseLost(f"Lease was superseded: {self.path}")
        if current["expires_at"] <= time.time():
            raise LeaseLost(f"Lease expired: {self.path}")

    @contextlib.contextmanager
    def fenced(self) -> Iterator[None]:
        """Hold the takeover guard across a central-state publication."""
        with _guard(self.guard_path):
            self._assert_locked()
            yield

    def assert_owned(self) -> None:
        with self.fenced():
            pass

    def heartbeat(self) -> None:
        with self.fenced():
            atomic_write_json(self.path, self._record())

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.ttl / 3):
            try:
                self.heartbeat()
            except BaseException as exc:
                self._failure = exc
                return

    def release(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1, self.ttl / 3))
        with _guard(self.guard_path):
            if self.path.exists():
                current = _read_json(self.path)
                if (
                    current.get("token") == self.token
                    and current.get("generation") == self.generation
                ):
                    self.path.unlink()
                    fsync_directory(self.path.parent)
        self.generation = None

    def __enter__(self) -> "Lease":
        return self.acquire()

    def __exit__(self, *_: Any) -> None:
        self.release()


def _relative_name(value: str) -> Path:
    relative = Path(value)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {".", ".."} for part in relative.parts)
    ):
        raise ValueError(f"Unsafe artifact path: {value!r}")
    return relative


def verify_bundle(path: Path | str) -> dict | None:
    """Ignore incomplete bundles; raise on corruption of a published bundle."""
    path = Path(path)
    if not (path / "COMPLETE").is_file():
        return None
    marker = _read_json(path / "COMPLETE")
    if marker.get("manifest_hash") != file_hash(path / "manifest.json"):
        raise IntegrityError(f"Bundle manifest checksum mismatch: {path}")
    manifest = _read_json(path / "manifest.json")
    for name, expected in manifest.get("artifacts", {}).items():
        artifact = path / _relative_name(name)
        if (
            artifact.is_symlink()
            or not artifact.is_file()
            or not artifact.resolve().is_relative_to(path.resolve())
        ):
            raise IntegrityError(f"Missing or escaping artifact: {artifact}")
        if file_hash(artifact) != expected:
            raise IntegrityError(f"Artifact checksum mismatch: {artifact}")
    return manifest


def publish_bundle(
    destination: Path | str,
    record: Mapping[str, Any],
    artifacts: Mapping[str, Path | bytes] | None = None,
    *,
    lease: Lease | None = None,
) -> dict:
    """Publish a complete immutable bundle on the destination filesystem."""
    destination = Path(destination)
    if record.get("schema_version") != 1:
        raise ValueError("Bundle record needs schema_version=1")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".publish-", dir=destination.parent))
    try:
        checksums = {}
        for name, content in sorted((artifacts or {}).items()):
            relative = _relative_name(name)
            if name in {"manifest.json", "COMPLETE"}:
                raise ValueError(f"Reserved artifact name: {name}")
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                target.write_bytes(content)
            else:
                source = Path(content)
                if not source.is_file():
                    raise ValueError(f"Artifact is not a regular file: {source}")
                shutil.copyfile(source, target)
            with target.open("rb") as stream:
                os.fsync(stream.fileno())
            checksums[name] = file_hash(target)
        manifest = {"schema_version": 1, "record": dict(record), "artifacts": checksums}
        atomic_write_json(staging / "manifest.json", manifest)
        atomic_write_json(
            staging / "COMPLETE",
            {"schema_version": 1, "manifest_hash": file_hash(staging / "manifest.json")},
        )
        for directory, _, _ in os.walk(staging, topdown=False):
            fsync_directory(Path(directory))
        publication_guard = (
            lease.fenced()
            if lease
            else _guard(destination.parent / ("." + destination.name + ".publish.guard"))
        )
        with publication_guard:
            if destination.exists():
                existing = verify_bundle(destination)
                if existing == manifest:
                    return existing
                if existing is not None:
                    raise IntegrityError(f"Conflicting immutable publication: {destination}")
                raise IntegrityError(
                    f"Incomplete destination needs explicit reconciliation: {destination}"
                )
            os.rename(staging, destination)
            fsync_directory(destination.parent)
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging)


class Store:
    """Shared baseline store. Evolved artifacts have no publication namespace."""

    CATEGORIES = frozenset(
        {
            "sources",
            "baseline/wheels",
            "baseline/envs",
            "baseline/observations",
            "baseline/eligibility",
        }
    )

    def __init__(self, root: Path | str):
        self.root = Path(root).resolve() / "benchpress-compare" / "v1"

    def path(self, category: str, key: str) -> Path:
        if category not in self.CATEGORIES:
            raise ValueError(f"Unknown baseline store category: {category}")
        if not key or any(character not in "0123456789abcdef" for character in key):
            raise ValueError("Store keys must be lowercase hexadecimal content identities")
        return self.root / category / key

    def lease(self, category: str, key: str, *, ttl: float = 120) -> Lease:
        self.path(category, key)
        return Lease(self.root / "locks" / (canonical_hash([category, key]) + ".json"), ttl=ttl)

    def get(self, category: str, key: str) -> dict | None:
        result = verify_bundle(self.path(category, key))
        if result is not None and result["record"].get("key") != key:
            raise IntegrityError("Store entry key disagrees with its location")
        return result

    def publish(
        self,
        category: str,
        key: str,
        record: Mapping[str, Any],
        artifacts: Mapping[str, Path | bytes] | None = None,
    ) -> dict:
        if category == "baseline/observations":
            if record.get("arm") != "baseline" or str(record.get("status", "")).lower() not in {
                "succeeded",
                "success",
            }:
                raise ValueError("Only successful baseline observations are reusable")
            if record.get("validation_status") != "verified":
                raise ValueError("Only verified observations are reusable")
        payload = dict(record, schema_version=1, key=key)
        with self.lease(category, key) as lease:
            return publish_bundle(self.path(category, key), payload, artifacts, lease=lease)
