from unittest.mock import Mock
import threading

import pytest

from benchpress_compare.config import RunConfig
from benchpress_compare.identity import atomic_write_json, read_json
from benchpress_compare.orchestrator import ExecutionEngine, read_results
from benchpress_compare.pilot import runtime_profile
from benchpress_compare.store import Lease


@pytest.mark.parametrize("cancel_after", [1, 4])
@pytest.mark.parametrize("sharded", [False, True])
def test_cancellation_during_preparation_never_submits_the_batch(
    tmp_path, monkeypatch, cancel_after, sharded
):
    site = tmp_path / "site.toml"
    site.write_text('[lsf]\nqueue="fake"\nsubmit_workers=4\nshard_max_observations=2\n')
    root = tmp_path / "run"
    config = RunConfig(
        "/unused/baseline",
        "/unused/evolve",
        str(tmp_path / "store"),
        str(root),
        executor="lsf",
        lsf_config=str(site),
    )
    specs = [
        {
            "observation_id": str(index),
            "task": "freeze-fixture" if sharded else "build",
            "phase": "comparison",
            "runtime_profile": runtime_profile(memory_gb=0.125, wall_seconds=5),
        }
        for index in range(10)
    ]
    executor = Mock()
    executor.submit.side_effect = AssertionError("Submitted after cancellation")
    cancel_path = root / "state/cancel-request.json"
    with Lease(root / "state/controller.lease") as lease:
        engine = ExecutionEngine(root, config, lease, executor=executor)
        method = "_prepare_bundle" if sharded else "_prepare_launch"
        prepare = getattr(engine, method)
        prepared_count = 0
        preparation_lock = threading.Lock()

        def prepare_then_cancel(*args):
            nonlocal prepared_count
            prepared = prepare(*args)
            with preparation_lock:
                prepared_count += 1
                if prepared_count == cancel_after:
                    atomic_write_json(cancel_path, {"schema_version": 1})
            return prepared

        monkeypatch.setattr(engine, method, prepare_then_cancel)
        results = engine.run(specs)
        assert len(results) == len(specs)
        assert {result["status"] for result in results} == {"CANCELLED"}
        attempts = list((root / "attempts").glob("*/*/attempt.json"))
        # All four reserved jobs can prepare concurrently, but none may submit
        # once any preparation observes the cancellation request.
        assert len(attempts) == 4 * (2 if sharded else 1)
        assert {read_json(path)["status"] for path in attempts} == {"CANCELLED"}
        assert len(read_results(root)) == len(specs)
        # A fresh controller sees terminal attempts/results, never nonexistent
        # scheduler jobs requiring reconciliation.
        cancel_path.unlink()
        resumed = ExecutionEngine(root, config, lease, executor=executor)
        assert resumed.run(specs) == results
    executor.submit.assert_not_called()
    executor.poll_many.assert_not_called()
    executor.cancel.assert_not_called()
