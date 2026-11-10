"""Validated, persistable scientific and site configuration."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
import os
from pathlib import Path
import tomllib

from .errors import CompareError
from .identity import canonical_hash
from .profiles import load_profile


def positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise ValueError("must be a positive integer")
    return number


def overlapping(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _writable(path: Path):
    probe = path
    while not probe.exists():
        probe = probe.parent
    if not probe.is_dir() or not os.access(probe, os.W_OK | os.X_OK):
        raise CompareError(f"Destination is not writable: {path}")


@dataclass
class RunConfig:
    baseline: str
    evolve: str
    store: str
    results_root: str
    corpus: str | None = None
    executor: str = "local"
    seeds: int = 3
    master_seed: int = 20261109
    workers: str = "auto"
    native_threads: int = 1
    cpu_budget: float | None = None
    memory_gb: float | None = None
    wall_seconds: float | None = None
    worker_memory_gb: float = 8
    build_cpus: int = 4
    infrastructure_retries: int = 1
    lsf_config: str | None = None
    smoke: list[str] = field(default_factory=list)
    allow_missing_families: bool = False
    design: str = "benchpress-eight-family-v1"
    schema_version: int = 1
    profile: str = "full"
    profile_manifest_hash: str | None = None

    def __post_init__(self):
        if self.wall_seconds is None:
            self.wall_seconds = load_profile(self.profile)["default_wall_seconds"]

    def validate(self):
        manifest = load_profile(self.profile)
        manifest_hash = canonical_hash(manifest)
        if self.profile_manifest_hash not in (None, manifest_hash):
            raise CompareError("Profile manifest changed after the request was frozen")
        self.profile_manifest_hash = manifest_hash
        if self.profile != "full" and (self.smoke or self.allow_missing_families):
            raise CompareError("Subset profiles cannot use --smoke or --allow-missing-families")
        self.design = manifest["design"]
        for key in ("baseline", "evolve", "store", "results_root"):
            setattr(self, key, str(Path(getattr(self, key)).expanduser().resolve()))
        sources = [Path(self.baseline), Path(self.evolve)]
        for source in sources:
            if not source.is_dir() or not os.access(source, os.R_OK | os.X_OK):
                raise CompareError(f"Source directory is not readable: {source}")
            if not (source / "pyproject.toml").is_file():
                raise CompareError(f"Qiskit source needs pyproject.toml: {source}")
        destinations = [Path(self.store), Path(self.results_root)]
        if overlapping(*destinations):
            raise CompareError("Store and results-root must not overlap")
        for destination in destinations:
            if any(overlapping(source, destination) for source in sources):
                raise CompareError("Destinations must be outside and not contain source trees")
            _writable(destination)
        for key in ("seeds", "native_threads", "build_cpus"):
            if not isinstance(getattr(self, key), int) or getattr(self, key) <= 0:
                raise CompareError(f"{key} must be a positive integer")
        if self.seeds > (1 << 29):
            raise CompareError("seeds exceeds the disjoint comparison seed range")
        if type(self.master_seed) is not int or self.master_seed < 0:
            raise CompareError("master_seed must be a nonnegative integer")
        if self.infrastructure_retries < 0:
            raise CompareError("infrastructure_retries must be nonnegative")
        for key in ("cpu_budget", "memory_gb", "wall_seconds", "worker_memory_gb"):
            value = getattr(self, key)
            if value is not None and (not math.isfinite(value) or value <= 0):
                raise CompareError(f"{key} must be finite and positive")
        if self.workers != "auto":
            try:
                positive(self.workers)
            except ValueError as exc:
                raise CompareError("workers must be auto or a positive integer") from exc
        if self.executor not in {"local", "lsf"}:
            raise CompareError("executor must be local or lsf")
        if self.executor == "lsf" and not self.lsf_config:
            raise CompareError("LSF requires an explicit --lsf-config")
        if self.corpus:
            self.corpus = str(Path(self.corpus).expanduser().resolve())
            if not Path(self.corpus).is_dir():
                raise CompareError(f"Corpus directory not found: {self.corpus}")
        if self.lsf_config:
            self.lsf_config = str(Path(self.lsf_config).expanduser().resolve())
            SiteConfig.read(self.lsf_config)
        if self.smoke:
            self.design = "benchpress-smoke-v1"
        elif self.allow_missing_families:
            # A reduced design is an explicitly named configuration, never the
            # standard eight-family score with silently redistributed weight.
            self.design = "benchpress-reduced-families-v1"
        return self

    @property
    def complete_families_required(self) -> bool:
        return not (self.smoke or self.allow_missing_families)

    def to_dict(self):
        return asdict(self)


@dataclass
class SiteConfig:
    queue: str
    project: str | None = None
    reservation: str | None = None
    host_select: str | None = None
    max_jobs: int = 16
    max_cpus: int = 16
    max_memory_gb: float = 128
    poll_seconds: float = 10
    visibility_grace_seconds: float = 300
    job_wall_seconds: int = 86400
    # Fixture and comparison shards hold at most this many sequential
    # observations, also bounded by the job wall limit. Six is one case: three
    # seeds on both arms. Every observation of a big circuit
    # (planner.BIG_CIRCUITS) always gets a job of its own.
    shard_max_observations: int = 6
    memory_unit: str = "MB"
    memory_limit_unit: str | None = None
    memory_reservation_unit: str | None = None
    memory_per_slot: bool = False
    controller_cpus: int = 1
    controller_memory_gb: float = 4
    build_cpus: int = 4
    build_memory_gb: float = 16
    bsub: str = "bsub"
    bjobs: str = "bjobs"
    bhist: str = "bhist"
    bkill: str = "bkill"
    machine_readable: bool = True
    controller_python: str | None = None

    @classmethod
    def read(cls, path):
        try:
            with Path(path).open("rb") as stream:
                raw = tomllib.load(stream)
            cfg = cls(**raw.get("lsf", raw))
        except (OSError, TypeError, tomllib.TOMLDecodeError) as exc:
            raise CompareError(f"Invalid LSF config: {exc}") from exc
        if not cfg.queue or cfg.memory_unit not in {"KB", "MB", "GB"}:
            raise CompareError("LSF queue and explicit KB/MB/GB memory units are required")
        for unit in (cfg.memory_limit_unit, cfg.memory_reservation_unit):
            if unit is not None and unit not in {"KB", "MB", "GB"}:
                raise CompareError("LSF memory limit/reservation units must be KB, MB, or GB")
        for key, value in asdict(cfg).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if not math.isfinite(value) or value <= 0:
                    raise CompareError(f"LSF {key} must be finite and positive")
        return cfg
