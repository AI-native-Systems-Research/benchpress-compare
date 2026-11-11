"""Canonical identities and crash-safe, same-filesystem JSON publication."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import asdict, is_dataclass
from enum import Enum
from fractions import Fraction
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path
import tempfile

from .errors import IntegrityError


def _normalize(value):
    if is_dataclass(value) and not isinstance(value, type):
        return _normalize(asdict(value))
    if isinstance(value, Enum):
        return _normalize(value.value)
    if isinstance(value, Fraction):
        return {"numerator": value.numerator, "denominator": value.denominator}
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Nonfinite values are not valid artifacts")
        return value
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("Canonical mapping keys must be strings")
        return {key: _normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    raise TypeError(f"Unsupported canonical value: {type(value).__name__}")


def canonical_bytes(value) -> bytes:
    return json.dumps(
        _normalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_hash(value) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_hash(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fsync_directory(path: str | Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        # Some platforms/filesystems cannot fsync directories. Other failures matter.
        import errno

        if exc.errno not in {errno.EINVAL, errno.ENOTSUP, errno.EBADF}:
            raise


def atomic_write_json(path: str | Path, value, *, fence=None) -> None:
    """Stage durable JSON privately, then publish under an optional fence.

    ``fence`` is a context-manager factory, such as ``lease.fenced``. It is
    entered only after the temporary file has been serialized and synced, so
    independent writes can stage concurrently while ownership still protects
    the replacement and its directory sync.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = canonical_bytes(value) + b"\n"
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        with fence() if fence is not None else nullcontext():
            os.replace(name, path)
            fsync_directory(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def atomic_write_json_gzip(path: str | Path, value) -> None:
    """Stream canonical JSON into a deterministic, atomically published gzip.

    Unlike canonical_bytes(), this avoids both a normalized copy of a large
    circuit and its complete serialized byte string. Gzip headers contain no
    output filename or timestamp, so equal artifacts have equal byte hashes.
    """
    def check_keys(item):
        if isinstance(item, dict):
            if not all(isinstance(key, str) for key in item):
                raise TypeError("Canonical mapping keys must be strings")
            for child in item.values():
                check_keys(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                check_keys(child)

    # JSONEncoder otherwise silently stringifies non-string mapping keys. Its
    # default handler normalizes only exceptional values, not the full tree.
    check_keys(value)
    encoder = json.JSONEncoder(
        default=_normalize, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            with gzip.GzipFile(
                filename="", mode="wb", fileobj=stream, compresslevel=6, mtime=0,
            ) as compressed:
                with io.TextIOWrapper(compressed, encoding="utf-8", newline="\n") as text:
                    text.writelines(encoder.iterencode(value))
                    text.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        fsync_directory(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def read_json(path: str | Path):
    def invalid_constant(value):
        raise IntegrityError(f"Nonfinite JSON value: {value}")

    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream, parse_constant=invalid_constant)


def case_identity(fixture_hash: str, target_hash: str, level: int, contract_hash: str) -> str:
    return canonical_hash(
        {
            "kind": "case",
            "schema_version": 1,
            "fixture_hash": fixture_hash,
            "target_hash": target_hash,
            "level": level,
            "contract_hash": contract_hash,
        }
    )


def observation_identity(spec: dict) -> str:
    # Arm, attempt, cohort, labels, and total seed count deliberately do not enter.
    keys = ("case_id", "build_id", "seed", "phase", "runtime_profile", "protocols")
    return canonical_hash(
        {"kind": "observation", "schema_version": 1, **{key: spec[key] for key in keys}}
    )


def harness_identity() -> str:
    """Hash coordinator source independently of paths or wheel timestamps."""
    package = Path(__file__).resolve().parent
    sources = {
        path.relative_to(package).as_posix(): file_hash(path)
        for path in sorted(package.rglob("*.py"))
        if "__pycache__" not in path.parts
    }
    from .corpus import load_catalogue
    from .profiles import load_profiles

    return canonical_hash(
        {
            "schema_version": 1,
            "sources": sources,
            "catalogue": canonical_hash(load_catalogue()),
            "profiles": canonical_hash(load_profiles()),
        }
    )
