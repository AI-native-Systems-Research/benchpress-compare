"""Isolated preparation tasks using the same durable executor protocol."""

from __future__ import annotations

from pathlib import Path
import os
import sys
import time
import traceback

from .identity import atomic_write_json, read_json


def main(argv=None):
    spec = read_json((argv or sys.argv[1:])[0])
    start = time.monotonic()
    receipt = {
        key: spec[key]
        for key in (
            "observation_id",
            "case_id",
            "phase",
            "arm",
            "seed",
            "build_id",
            "attempt_id",
            "plan_hash",
            "lease_generation",
            "run_id",
            "submission_token",
        )
        if key in spec
    }
    receipt.update(schema_version=1, status="SUCCEEDED")
    try:
        from .builds import build_release, resolve_common_dependencies
        from .store import Lease, LeaseBusy

        if spec["task"] == "resolve-dependencies":
            receipt["result"] = resolve_common_dependencies(
                spec["snapshots"],
                spec["destination"],
                python=spec["python"],
                extra_requirements=spec["extra_requirements"],
            )
        elif spec["task"] == "build":
            if spec.get("rustup_home"):
                os.environ["RUSTUP_HOME"] = spec["rustup_home"]
            destination = Path(spec["destination"])
            lease = Lease(destination.with_suffix(".lease"), ttl=180)
            deadline = time.monotonic() + spec["runtime_profile"]["wall_seconds"]
            while True:
                try:
                    lease.acquire()
                    break
                except LeaseBusy:
                    if time.monotonic() >= deadline:
                        receipt.update(status="INFRA_FAILED", reason="build_lease_contention")
                        break
                    time.sleep(min(0.5, max(0, deadline - time.monotonic())))
            if lease.generation is not None:
                try:
                    lease.assert_owned()
                    # The orchestrator reconciles prior attempts before replacement.
                    # Retain interrupted build evidence; installed envs always keep
                    # their original final path when eventually published READY.
                    if (
                        destination.exists()
                        and any(destination.iterdir())
                        and not (destination / "READY").exists()
                    ):
                        archived = destination.with_name(
                            destination.name + ".incomplete-" + spec["attempt_id"]
                        )
                        with lease.fenced():
                            os.rename(destination, archived)
                    receipt["result"] = build_release(
                        spec["snapshot"],
                        destination,
                        python=spec["python"],
                        dependency_lock=spec["dependency_lock"],
                        harness_wheel=spec["harness_wheel"],
                        lease=lease,
                        build_jobs=spec["runtime_profile"].get("native_threads", 1),
                        timeout=max(1, deadline - time.monotonic()),
                    )
                finally:
                    lease.release()
        else:
            raise ValueError(f"Unknown preparation task: {spec['task']}")
    except Exception as exc:
        receipt.update(
            status="HARNESS_FAILED",
            reason=f"{type(exc).__name__}: {exc}",
            traceback=traceback.format_exc(),
        )
    import resource

    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    receipt["resources"] = {
        "elapsed_seconds": time.monotonic() - start,
        "peak_rss_bytes": int(rss if sys.platform == "darwin" else rss * 1024),
    }
    atomic_write_json(Path(spec["output_dir"]) / "receipt.json", receipt)
    return 0 if receipt["status"] == "SUCCEEDED" else 3


if __name__ == "__main__":
    raise SystemExit(main())
