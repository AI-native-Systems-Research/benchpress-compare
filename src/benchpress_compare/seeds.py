"""Versioned, reproducible and phase-separated seed streams.

The seed payload is UTF-8 JSON of the ordered array
``[algorithm, master_seed, phase, identity, index, rejection_counter]``, encoded
without spaces. SHA-256 is interpreted as an unsigned big-endian integer. Each
phase owns a disjoint quarter of the 31-bit nonnegative integer range. Repeated
values are rejected in index order, so extending a stream preserves its prefix.
"""

from __future__ import annotations

import hashlib
import json

SEED_ALGORITHM = "sha256-phase-ranges-v1"
DEFAULT_MASTER_SEED = 20261109
_SIZE = 1 << 29
SEED_RANGES = {
    "generation": (0, _SIZE),
    "preflight": (_SIZE, 2 * _SIZE),
    "pilot": (2 * _SIZE, 3 * _SIZE),
    "comparison": (3 * _SIZE, 4 * _SIZE),
}


def derive_seeds(
    identity: str,
    phase: str,
    count: int,
    master_seed: int = DEFAULT_MASTER_SEED,
) -> list[int]:
    """Return a stream prefix; bounds are exclusive at the upper endpoint."""
    if not isinstance(identity, str) or not identity:
        raise ValueError("seed identity must be a nonempty string")
    if type(master_seed) is not int or master_seed < 0:
        raise ValueError("master_seed must be a nonnegative integer")
    if phase not in SEED_RANGES:
        raise ValueError(f"unknown seed phase: {phase}")
    if type(count) is not int or count < 0:
        raise ValueError("seed count must be a nonnegative integer")
    lower, upper = SEED_RANGES[phase]
    if count > upper - lower:
        raise ValueError("seed count exceeds phase capacity")
    seen: set[int] = set()
    seeds = []
    for index in range(count):
        rejection = 0
        while True:
            payload = json.dumps(
                [SEED_ALGORITHM, master_seed, phase, identity, index, rejection],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            seed = lower + int.from_bytes(hashlib.sha256(payload).digest(), "big") % (upper - lower)
            if seed not in seen:
                seeds.append(seed)
                seen.add(seed)
                break
            rejection += 1
    return seeds


def seed_contract(master_seed: int = DEFAULT_MASTER_SEED) -> dict:
    # Validation is shared with actual derivation, including when no work exists.
    derive_seeds("contract", "generation", 0, master_seed)
    return {
        "schema_version": 1,
        "algorithm": SEED_ALGORITHM,
        "master_seed": master_seed,
        "ranges": {phase: {"lower": bounds[0], "upper_exclusive": bounds[1]} for phase, bounds in SEED_RANGES.items()},
    }
