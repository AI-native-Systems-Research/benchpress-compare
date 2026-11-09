"""Append-only hash-chained run events and replaceable checkpoints."""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path
from typing import Any

from .identity import atomic_write_json, canonical_hash, fsync_directory
from .store import IntegrityError, Lease, _guard


class Journal:
    def __init__(self, path: Path | str, *, lease: Lease | None = None):
        self.path = Path(path)
        self.lease = lease

    def _read(self) -> tuple[list[dict], int]:
        if not self.path.exists():
            return [], 0
        data = self.path.read_bytes()
        events = []
        committed_bytes = 0
        previous = None
        for line in data.splitlines(keepends=True):
            # A final non-newline-terminated append was not committed. Even
            # valid JSON at that boundary may have been interrupted before fsync.
            if not line.endswith(b"\n"):
                break
            try:
                event = json.loads(line)
                claimed = event["event_hash"]
                payload = {key: value for key, value in event.items() if key != "event_hash"}
                if (
                    event["schema_version"] != 1
                    or event["sequence"] != len(events)
                    or event["previous"] != previous
                ):
                    raise ValueError("event version, sequence, or predecessor mismatch")
                if claimed != canonical_hash(payload):
                    raise ValueError("event hash mismatch")
            except (KeyError, TypeError, ValueError) as exc:
                raise IntegrityError(
                    f"Corrupt committed journal record {len(events)} in {self.path}: {exc}"
                ) from exc
            events.append(event)
            previous = claimed
            committed_bytes += len(line)
        return events, committed_bytes

    def replay(self) -> list[dict]:
        return self._read()[0]

    def append(self, event_type: str, data: dict | None = None, **fields: Any) -> dict:
        if not event_type:
            raise ValueError("Event type is required")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        ownership = self.lease.fenced() if self.lease else contextlib.nullcontext()
        with ownership, _guard(self.path.with_name(self.path.name + ".guard")):
            events, committed_bytes = self._read()
            event = {
                "schema_version": 1,
                "sequence": len(events),
                "previous": events[-1]["event_hash"] if events else None,
                "event_type": event_type,
                "timestamp": time.time(),
                "generation": self.lease.generation if self.lease else None,
                "data": dict(data or {}, **fields),
            }
            event["event_hash"] = canonical_hash(event)
            with self.path.open("a+b") as stream:
                stream.truncate(committed_bytes)
                stream.seek(0, os.SEEK_END)
                stream.write(
                    (
                        json.dumps(event, sort_keys=True, separators=(",", ":"), allow_nan=False)
                        + "\n"
                    ).encode()
                )
                stream.flush()
                os.fsync(stream.fileno())
            fsync_directory(self.path.parent)
            return event

    def checkpoint(self, path: Path | str, state: dict) -> None:
        ownership = self.lease.fenced() if self.lease else contextlib.nullcontext()
        with ownership:
            events = self.replay()
            atomic_write_json(
                Path(path),
                {
                    "schema_version": 1,
                    "last_event": events[-1]["event_hash"] if events else None,
                    "event_count": len(events),
                    "state": state,
                },
            )
