from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from benchpress_compare.errors import IntegrityError
from benchpress_compare.journal import Journal
from benchpress_compare.store import Lease, LeaseLost


def test_replay_and_torn_tail_recovery(tmp_path):
    journal = Journal(tmp_path / "events.jsonl")
    first = journal.append("START", {"run_id": "a"})
    with journal.path.open("ab") as stream:
        stream.write(b'{"unfinished":')
    assert journal.replay() == [first]
    second = journal.append("COMPLETE", status="succeeded")
    assert second["previous"] == first["event_hash"]
    assert second["sequence"] == 1
    assert len(journal.replay()) == 2
    journal.checkpoint(tmp_path / "checkpoint.json", {"run": "COMPLETE"})
    checkpoint = json.loads((tmp_path / "checkpoint.json").read_text())
    assert checkpoint["event_count"] == 2
    assert checkpoint["last_event"] == second["event_hash"]


def test_committed_corruption_is_rejected(tmp_path):
    journal = Journal(tmp_path / "events.jsonl")
    journal.append("START", value=1)
    journal.append("NEXT", value=2)
    journal.path.write_text(journal.path.read_text().replace('"value":1', '"value":9'))
    with pytest.raises(IntegrityError, match="committed journal"):
        journal.replay()
    # The appending instance trusts its cached tail while the file signature is
    # unchanged; a same-size in-place edit is caught by replay and by any new
    # instance, which is what a restarted controller is.
    with pytest.raises(IntegrityError):
        Journal(journal.path).append("ANOTHER")


def test_append_verifies_the_chain_once_per_instance(tmp_path, monkeypatch):
    journal = Journal(tmp_path / "events.jsonl")
    calls = []
    read = Journal._read
    monkeypatch.setattr(Journal, "_read", lambda self: calls.append(1) or read(self))
    for index in range(200):
        journal.append("EVENT", index=index)
    assert len(calls) == 1
    monkeypatch.undo()
    events = journal.replay()
    assert [event["sequence"] for event in events] == list(range(200))
    assert all(b["previous"] == a["event_hash"] for a, b in zip(events, events[1:]))


def test_cached_tail_is_invalidated_by_another_writer(tmp_path):
    first = Journal(tmp_path / "events.jsonl")
    second = Journal(tmp_path / "events.jsonl")
    first.append("A")
    second.append("B")
    third = first.append("C")
    events = first.replay()
    assert [event["event_type"] for event in events] == ["A", "B", "C"]
    assert third["sequence"] == 2
    assert third["previous"] == events[1]["event_hash"]


def test_cached_tail_is_invalidated_by_a_torn_tail(tmp_path):
    journal = Journal(tmp_path / "events.jsonl")
    first = journal.append("START")
    with journal.path.open("ab") as stream:
        stream.write(b'{"unfinished":')
    second = journal.append("NEXT")
    assert second["sequence"] == 1
    assert second["previous"] == first["event_hash"]
    assert journal.replay() == [first, second]


def test_valid_but_unterminated_tail_is_not_committed(tmp_path):
    journal = Journal(tmp_path / "events.jsonl")
    first = journal.append("START")
    journal.path.write_bytes(journal.path.read_bytes().rstrip(b"\n"))
    assert journal.replay() == []
    replacement = journal.append("RESTART")
    assert replacement["sequence"] == 0
    assert replacement["previous"] is None
    assert replacement != first


def test_only_lease_owner_can_write(tmp_path):
    with Lease(tmp_path / "owner", heartbeat=False) as lease:
        journal = Journal(tmp_path / "events.jsonl", lease=lease)
        journal.append("START")
    with pytest.raises(LeaseLost):
        journal.append("STALE")
    assert len(journal.replay()) == 1


def test_append_many_keeps_individual_records_with_one_durable_sync(tmp_path, monkeypatch):
    import benchpress_compare.journal as module

    journal = Journal(tmp_path / "events.jsonl")
    syncs = []
    fsync = module.os.fsync

    def sync(descriptor):
        syncs.append(descriptor)
        return fsync(descriptor)

    monkeypatch.setattr(module.os, "fsync", sync)
    events = journal.append_many(("EVENT", {"index": index}) for index in range(8))
    # One fsync for the journal contents and one for its containing directory,
    # independent of the number of event records in the group.
    assert len(syncs) == 2
    assert journal.replay() == events
    assert [event["sequence"] for event in events] == list(range(8))
    assert [event["data"]["index"] for event in events] == list(range(8))
    assert len(journal.path.read_bytes().splitlines()) == 8
    assert journal.append("AFTER", index=8)["previous"] == events[-1]["event_hash"]


def test_append_many_recovers_the_complete_prefix_of_a_torn_group(tmp_path):
    journal = Journal(tmp_path / "events.jsonl")
    original = journal.append_many([("FIRST", {}), ("SECOND", {}), ("TORN", {})])
    journal.path.write_bytes(journal.path.read_bytes()[:-10])
    assert journal.replay() == original[:2]
    recovered = journal.append_many([("REPLACEMENT", {}), ("LAST", {})])
    assert recovered[0]["sequence"] == 2
    assert recovered[0]["previous"] == original[1]["event_hash"]
    assert journal.replay() == original[:2] + recovered


@pytest.mark.parametrize(
    "invalid,error",
    [
        (("", {}), ValueError),
        (("BAD", {"value": float("nan")}), ValueError),
        (("BAD", {"value": object()}), TypeError),
    ],
)
def test_append_many_validation_failure_never_writes_a_partial_group(tmp_path, invalid, error):
    journal = Journal(tmp_path / "events.jsonl")
    first = journal.append("FIRST")
    before = journal.path.read_bytes()
    with pytest.raises(error):
        journal.append_many([("VALID", {}), invalid])
    assert journal.path.read_bytes() == before
    next_event = journal.append("AFTER")
    assert next_event["sequence"] == 1
    assert next_event["previous"] == first["event_hash"]


def test_concurrent_groups_and_single_appends_keep_one_hash_chain(tmp_path):
    path = tmp_path / "events.jsonl"
    shared = Journal(path)
    start = threading.Barrier(6)

    def write(writer):
        # Exercise both a shared instance and independent tail caches.
        journal = shared if writer % 2 else Journal(path)
        start.wait(timeout=10)
        for group in range(4):
            journal.append_many(
                ("GROUP", {"writer": writer, "group": group, "index": index}) for index in range(3)
            )
            journal.append("SINGLE", writer=writer, group=group)

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(write, range(6)))
    events = Journal(path).replay()
    assert len(events) == 6 * 4 * 4
    for writer in range(6):
        for group in range(4):
            batch = [
                event
                for event in events
                if event["event_type"] == "GROUP"
                and event["data"]["writer"] == writer
                and event["data"]["group"] == group
            ]
            assert [event["data"]["index"] for event in batch] == [0, 1, 2]
            assert [event["sequence"] for event in batch] == list(
                range(batch[0]["sequence"], batch[0]["sequence"] + 3)
            )


def test_append_many_is_fenced_and_empty_groups_are_noops(tmp_path):
    path = tmp_path / "events.jsonl"
    with Lease(tmp_path / "owner", heartbeat=False) as lease:
        journal = Journal(path, lease=lease)
        assert journal.append_many([]) == []
        assert not path.exists()
        events = journal.append_many([("FIRST", {}), ("SECOND", {})])
        assert {event["generation"] for event in events} == {lease.generation}
    before = path.read_bytes()
    with pytest.raises(LeaseLost):
        journal.append_many([("STALE", {}), ("ALSO_STALE", {})])
    assert path.read_bytes() == before
