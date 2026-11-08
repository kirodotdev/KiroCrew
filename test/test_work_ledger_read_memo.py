"""Conductor work ledger store: a non-strict read of an unchanged file skips the open.

The Crew board re-reads every item and event log on each poll. A non-strict read keeps
the file's bytes with the stat it read them under and serves them again while the stat
is unchanged; every write replaces the file, so the next read sees it at once. Strict
reads, the writers' own, always go to disk.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import work_ledger_helpers
from work_ledger_helpers import CONDUCTOR, WORKER, _new_item

from kiro_crew import work_ledger as wl
from kiro_crew import work_ledger_store as store

# The shared fixtures, bound here by name so pytest registers them for this module.
_isolated_home = work_ledger_helpers._isolated_home


@pytest.fixture
def disk_reads(monkeypatch):
    """Every path a record or event-log read actually opened, in order."""
    # Records come through ``atomic_write.read_bytes_with_retry``, which reads with
    # ``Path.read_bytes``; strict event logs use ``Path.read_text``.
    opened: list[str] = []
    real_read_bytes = Path.read_bytes
    real_read_text = Path.read_text

    def read_bytes(self, *args, **kwargs):
        if self.suffix in (".json", ".jsonl"):
            opened.append(str(self))
        return real_read_bytes(self, *args, **kwargs)

    def read_text(self, *args, **kwargs):
        if self.suffix in (".json", ".jsonl"):
            opened.append(str(self))
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(Path, "read_text", read_text)
    return opened


def _bound_item() -> str:
    item_id = _new_item(title="first title")
    wl.apply_conductor_action(CONDUCTOR, "bind", item_id=item_id, worker_session_key=WORKER)
    return item_id


def test_a_write_after_a_memo_hit_is_read_back_at_once(disk_reads):
    item_id = _bound_item()
    item_file = str(wl.item_path(CONDUCTOR, item_id))
    events_file = str(wl.item_events_path(CONDUCTOR, item_id))
    before = wl.read_work_item(CONDUCTOR, item_id)
    before_events = wl.read_events(CONDUCTOR, item_id)

    disk_reads.clear()
    assert wl.read_work_item(CONDUCTOR, item_id).summary == before.summary
    assert len(wl.read_events(CONDUCTOR, item_id)) == len(before_events)
    assert (
        item_file not in disk_reads and events_file not in disk_reads
    ), "unchanged files were re-opened"

    wl.apply_worker_report(CONDUCTOR, item_id, status="progress", summary="half way", artifacts={})
    assert wl.read_work_item(CONDUCTOR, item_id).summary == "half way"
    assert len(wl.read_events(CONDUCTOR, item_id)) == len(before_events) + 1


def test_a_caller_mutating_a_returned_item_does_not_change_the_next_read(disk_reads):
    item_id = _bound_item()
    item_file = str(wl.item_path(CONDUCTOR, item_id))
    first = wl.read_work_item(CONDUCTOR, item_id)
    first.title = "changed by a caller"
    first.artifacts["planted"] = "x"
    events = wl.read_events(CONDUCTOR, item_id)
    events.clear()

    disk_reads.clear()
    again = wl.read_work_item(CONDUCTOR, item_id)
    assert item_file not in disk_reads, "the second read was not served from the memo"
    assert again.title == "first title"
    assert "planted" not in again.artifacts
    assert wl.read_events(CONDUCTOR, item_id) != []


def test_strict_reads_bypass_the_memo(disk_reads):
    item_id = _bound_item()
    path = wl.item_path(CONDUCTOR, item_id)
    events_path = wl.item_events_path(CONDUCTOR, item_id)
    wl.read_work_item(CONDUCTOR, item_id)
    wl.read_events(CONDUCTOR, item_id)

    # An in-place edit with the same size, inode and mtime: the one change the stamp
    # cannot see. A non-strict read serves the kept bytes; a strict read goes to disk.
    stat = path.stat()
    raw = path.read_bytes()
    edited = raw.replace(b"first title", b"FIRST TITLE")
    assert len(edited) == len(raw)
    with open(path, "r+b") as handle:
        handle.write(edited)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    disk_reads.clear()
    assert wl.read_work_item(CONDUCTOR, item_id).title == "first title"
    assert str(path) not in disk_reads
    assert wl.read_work_item(CONDUCTOR, item_id, strict=True).title == "FIRST TITLE"
    assert str(path) in disk_reads

    disk_reads.clear()
    store._read_events_unlocked(events_path, strict=True)
    assert str(events_path) in disk_reads


def test_a_file_with_no_file_id_is_never_kept(tmp_path):
    path = tmp_path / "record.json"
    path.write_bytes(b"{}")
    reads: list[Path] = []

    def read(p: Path) -> bytes:
        reads.append(p)
        return p.read_bytes()

    no_id = SimpleNamespace(st_ino=0, st_mtime_ns=1, st_size=2)
    assert store._memo_read_bytes(path, no_id, read) == b"{}"
    assert store._memo_read_bytes(path, no_id, read) == b"{}"
    assert len(reads) == 2
    assert str(path) not in store._READ_MEMO
