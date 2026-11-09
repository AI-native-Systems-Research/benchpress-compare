"""Public version-one record validation at artifact boundaries."""

from __future__ import annotations

from enum import StrEnum
from jsonschema import Draft202012Validator

from .errors import IntegrityError

VERSION = 1
PROTOCOLS = {
    "worker": "observation-worker-v1",
    "instrumentation": "sabre-native-call-v1",
    "metric": "native-n2-weighted-d2-v1",
    "validation": "legality-routing-instrument-v1",
    "target": "directed-target-v1",
    "fixture": "bound-logical-ir-v1",
    "identity": "canonical-json-v1",
}
TRANSPILE_CONTRACT = {
    "levels": [2, 3],
    "approximation_degree": 1.0,
    "initial_layout": None,
    "layout_trials": 20,
    "layout_swap_trials": 20,
    "routing_trials": 20,
    "arm_options": {},
}


class RunState(StrEnum):
    NEW = "NEW"
    PREPARING = "PREPARING"
    SCREENING = "SCREENING"
    PILOTING = "PILOTING"
    FROZEN = "FROZEN"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    COMPLETE_WITH_FAILURES = "COMPLETE_WITH_FAILURES"
    PREPARATION_FAILED = "PREPARATION_FAILED"
    NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"
    INTERRUPTED = "INTERRUPTED"
    CANCELLED = "CANCELLED"


class ObservationState(StrEnum):
    PLANNED = "PLANNED"
    SUBMITTING = "SUBMITTING"
    SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    RESULT_READY = "RESULT_READY"
    SUCCEEDED = "SUCCEEDED"
    INFRA_FAILED = "INFRA_FAILED"
    RETRY_READY = "RETRY_READY"
    COMPILER_FAILED = "COMPILER_FAILED"
    INVALID = "INVALID"
    RESOURCE_LIMIT = "RESOURCE_LIMIT"
    HARNESS_FAILED = "HARNESS_FAILED"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"


TERMINAL = {
    "SUCCEEDED",
    "COMPILER_FAILED",
    "INVALID",
    "RESOURCE_LIMIT",
    "HARNESS_FAILED",
    "CANCELLED",
    "INFRA_FAILED",
}
REQUIRED = {
    "SourceSnapshot": ["tree_hash", "files"],
    "BuildManifest": ["build_key", "attestation"],
    "InputFixture": ["input_id", "fixture_hash"],
    "TargetSnapshot": ["target_class", "num_qubits"],
    "EligibilityRecord": ["input_id", "status"],
    "Case": ["case_id", "input_id", "fixture_hash", "target_hash", "level", "seeds"],
    "ObservationSpec": [
        "observation_id",
        "case_id",
        "arm",
        "phase",
        "build_id",
        "seed",
        "runtime_profile",
        "protocols",
    ],
    "Attempt": ["observation_id", "attempt_id", "submission_token", "status"],
    "ObservationResult": ["observation_id", "case_id", "arm", "seed", "status"],
    "RunPlan": ["cases", "observations", "builds", "config", "weights", "pilot", "plan_hash"],
}


def record_schema(kind: str) -> dict:
    if kind not in REQUIRED:
        raise ValueError(f"Unknown record kind: {kind}")
    properties = {
        "schema_version": {"const": VERSION},
        "level": {"enum": [2, 3]},
        "seed": {"type": "integer", "minimum": 0},
        "seeds": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": {"type": "integer", "minimum": 0},
        },
        "arm": {"enum": ["baseline", "evolve"]},
        "phase": {"enum": ["preflight", "pilot", "comparison"]},
    }
    for key in ("N2", "D2"):
        properties[key] = {"type": ["integer", "null"], "minimum": 0}
    for key in ("case_id", "observation_id", "input_id", "build_id"):
        properties[key] = {"type": "string", "minLength": 1}
    properties["plan_hash"] = {
        "type": "string" if kind == "RunPlan" else ["string", "null"],
        "minLength": 1,
    }
    if kind == "ObservationResult":
        properties["status"] = {"enum": sorted(TERMINAL)}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["schema_version", *REQUIRED[kind]],
        "properties": properties,
    }


def validate_record(kind: str, value: dict) -> dict:
    errors = sorted(
        Draft202012Validator(record_schema(kind)).iter_errors(value),
        key=lambda error: str(error.path),
    )
    if errors:
        raise IntegrityError(f"Invalid {kind}: {errors[0].message}")
    return value
