"""Run a frozen LSF shard sequentially using fresh, bounded child processes.

Each child owns its own immutable attempt receipt. A shard failure never erases
earlier children and never authorizes a whole-shard retry.
"""

from __future__ import annotations

from pathlib import Path
import signal
import sys
import time

from benchpress_compare.errors import IntegrityError
from benchpress_compare.executors.local import LocalExecutor
from benchpress_compare.identity import atomic_write_json, file_hash, read_json


def run_shard(path):
    path = Path(path).resolve()
    manifest = read_json(path)
    if manifest.get("schema_version") != 1:
        raise IntegrityError("Unsupported shard manifest")
    stopped = False
    def request_stop(_signum, _frame):
        nonlocal stopped
        stopped = True
    old_handlers = {signum: signal.signal(signum, request_stop) for signum in (signal.SIGTERM, signal.SIGINT)}
    executor = LocalExecutor()
    outcomes, harness_failed = [], False
    active = None
    try:
        for entry in manifest["entries"]:
            spec_path = Path(entry["spec_path"])
            if file_hash(spec_path) != entry["spec_hash"]:
                raise IntegrityError("Shard child specification changed")
            spec = read_json(spec_path)
            if spec["submission_token"] != manifest["submission_token"] or spec["plan_hash"] != manifest["plan_hash"]:
                raise IntegrityError("Shard child identity mismatch")
            output = Path(spec["output_dir"])
            if (output / "supervisor.json").exists():
                raise IntegrityError("A shard attempt was already supervised; reconcile it rather than replaying")
            supervisor = {"schema_version": 1,
                **{key: spec[key] for key in ("attempt_id", "observation_id", "submission_token")},
                "resources": {"elapsed_seconds": 0, "peak_rss_bytes": 0}}
            if stopped or harness_failed:
                supervisor.update(status="CANCELLED" if stopped else "HARNESS_FAILED",
                                  reason="shard_cancelled" if stopped else "previous_worker_harness_failure")
            else:
                active = executor.submit(entry["command"], output, spec["runtime_profile"],
                                         name="bpc-child-" + spec["attempt_id"], token=spec["attempt_id"])
                while True:
                    if stopped:
                        active = executor.cancel(active)
                    active = executor.poll(active)
                    # Persist descendant/process identities for recovery and
                    # retain measured resource telemetry per observation.
                    atomic_write_json(output / "job.json", active)
                    if active["status"] in {"DONE", "EXIT", "RESOURCE_LIMIT", "INFRA_FAILED", "CANCELLED", "NEEDS_RECONCILIATION"}:
                        break
                    time.sleep(min(.1, spec["runtime_profile"].get("sample_interval_seconds", .1)))
                supervisor.update(status=active["status"], return_code=active.get("return_code"),
                                  reason=active.get("reason"), resources={
                    "elapsed_seconds": active.get("elapsed_seconds", 0),
                    "peak_rss_bytes": active.get("peak_rss_bytes", 0)})
                if active["status"] == "NEEDS_RECONCILIATION":
                    supervisor["status"] = "HARNESS_FAILED"
                    harness_failed = True
                receipt_path = output / "receipt.json"
                if receipt_path.exists():
                    try:
                        receipt = read_json(receipt_path)
                        harness_failed |= isinstance(receipt, dict) and receipt.get("status") == "HARNESS_FAILED"
                    except (ValueError, IntegrityError):
                        pass  # The coordinator records malformed output as INVALID.
                active = None
            atomic_write_json(output / "supervisor.json", supervisor)
            outcomes.append(supervisor)
        atomic_write_json(path.parent / "shard-receipt.json", {
            "schema_version": 1, "submission_token": manifest["submission_token"],
            "plan_hash": manifest["plan_hash"], "status": "CANCELLED" if stopped else "HARNESS_FAILED" if harness_failed else "DONE",
            "children": outcomes,
        })
        return 3 if stopped or harness_failed else 0
    finally:
        if active is not None:
            executor.cancel(active)
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    return run_shard(arguments[0])


if __name__ == "__main__":
    raise SystemExit(main())
