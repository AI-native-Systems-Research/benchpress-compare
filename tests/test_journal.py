import json

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
    with pytest.raises(IntegrityError):
        journal.append("ANOTHER")


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
