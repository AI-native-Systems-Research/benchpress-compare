from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import os
from pathlib import Path
import stat
import threading

import pytest

from benchpress_compare.identity import atomic_write_json, read_json
from benchpress_compare.store import Lease, LeaseLost


def test_takeover_after_staging_cannot_overwrite_destination(tmp_path):
    destination = tmp_path / "attempt.json"
    atomic_write_json(destination, {"original": True})
    original = destination.read_bytes()
    first = Lease(tmp_path / "lease", heartbeat=False).acquire()
    second = None

    def supersede_before_publication():
        nonlocal second
        staged = list(tmp_path.glob(".attempt.json.*"))
        assert len(staged) == 1
        assert read_json(staged[0]) == {"stale": True}
        expired = read_json(first.path)
        atomic_write_json(first.path, {**expired, "expires_at": 0})
        second = Lease(first.path, heartbeat=False).acquire()
        return first.fenced()

    try:
        with pytest.raises(LeaseLost, match="superseded"):
            atomic_write_json(destination, {"stale": True}, fence=supersede_before_publication)
        assert destination.read_bytes() == original
        assert not list(tmp_path.glob(".attempt.json.*"))
        second.assert_owned()
    finally:
        first.release()
        if second is not None:
            second.release()


def test_fenced_writes_stage_concurrently_and_publish_serially(tmp_path, monkeypatch):
    import benchpress_compare.identity as identity

    staged = threading.Barrier(4)
    lock = threading.Lock()
    publishing = 0
    publication_count = 0
    replace_count = 0
    directory_sync_count = 0
    original_fsync = os.fsync
    original_replace = os.replace
    original_directory_sync = identity.fsync_directory

    def sync_staging(fd):
        original_fsync(fd)
        if stat.S_ISREG(os.fstat(fd).st_mode):
            # All writers must finish their private file fsync before any can
            # publish. Holding the shared fence during staging would deadlock.
            staged.wait(timeout=10)

    def replace(source, destination):
        nonlocal replace_count
        with lock:
            assert publishing == 1
            replace_count += 1
        original_replace(source, destination)

    def sync_directory(path):
        nonlocal directory_sync_count
        with lock:
            assert publishing == 1
            directory_sync_count += 1
        original_directory_sync(path)

    with Lease(tmp_path / "lease", heartbeat=False) as lease:

        @contextmanager
        def fence():
            nonlocal publishing, publication_count
            with lease.fenced():
                with lock:
                    publishing += 1
                    assert publishing == 1
                    publication_count += 1
                try:
                    yield
                finally:
                    with lock:
                        publishing -= 1

        with monkeypatch.context() as patch:
            patch.setattr(identity.os, "fsync", sync_staging)
            patch.setattr(identity.os, "replace", replace)
            patch.setattr(identity, "fsync_directory", sync_directory)
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [
                    pool.submit(
                        atomic_write_json, tmp_path / f"attempt-{i}.json", {"index": i}, fence=fence
                    )
                    for i in range(4)
                ]
                for future in futures:
                    future.result(timeout=15)
        assert publication_count == replace_count == directory_sync_count == 4
        assert publishing == 0
        for i in range(4):
            assert read_json(tmp_path / f"attempt-{i}.json") == {"index": i}
        assert not list(Path(tmp_path).glob(".attempt-*.json.*"))
