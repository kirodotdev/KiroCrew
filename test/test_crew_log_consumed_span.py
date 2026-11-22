"""A savepoint is never written over records that changed after the pass folded them.

The write's (size, mtime) fingerprint is the cheap first check. It cannot see a
same-size rewrite that lands inside one mtime tick, so these tests put the old mtime
back after each edit: only the digest of the records the pass framed can refuse it.
"""

from __future__ import annotations

import os
from pathlib import Path

from test_crew_log_session_savepoint_growth import (  # noqa: F401
    PAD,
    _append,
    _fold,
    _isolated_home,
    _unit,
    _witness_seq,
    crew_log_store,
    lg,
)

from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log.store import ConsumedSpan, CrewLog, _iter_entries_located


def _same_tick_rewrite(path: Path, old: bytes, new: bytes) -> None:
    before = os.stat(path)
    data = path.read_bytes()
    assert data.count(old) == 1 and len(old) == len(new)
    with open(path, "r+b") as handle:
        handle.seek(data.index(old))
        handle.write(new)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))


def _fold_then(monkeypatch, unit: str, after_walk) -> None:
    """One ``fold_session`` pass that runs *after_walk* once the walk has read everything."""
    real = CrewLog.iter_from

    def walked(self, *args, **kwargs):
        yield from real(self, *args, **kwargs)
        after_walk()

    with monkeypatch.context() as patched:
        patched.setattr(CrewLog, "iter_from", walked)
        crew_log.fold_session(unit)


def _rewrite_during_pass(monkeypatch, unit: str, record: int) -> "tuple[int | None, int | None]":
    """(witness seq before, after) for a pass whose folded *record* is rewritten in place."""
    _unit(unit, 300)
    _fold(unit)
    valid = _witness_seq(unit)
    _append(unit, 300, first=10**7)
    path = crew_log_store.segment_paths(lg.KIND_SESSION, unit)[-1]
    old = f"e{record} {PAD}".encode()
    new = f"e{record} {'y' * len(PAD)}".encode()
    _fold_then(monkeypatch, unit, lambda: _same_tick_rewrite(path, old, new))
    return valid, _witness_seq(unit)


def test_a_same_tick_rewrite_of_the_first_folded_record_writes_no_savepoint(monkeypatch):
    valid, after = _rewrite_during_pass(monkeypatch, "u-first", 10**7)
    assert valid is not None and after == valid


def test_a_same_tick_rewrite_of_the_last_folded_record_writes_no_savepoint(monkeypatch):
    valid, after = _rewrite_during_pass(monkeypatch, "u-last", 10**7 + 299)
    assert valid is not None and after == valid


def test_a_pass_over_a_unit_that_only_grew_still_writes_its_savepoint():
    _unit("u-grew", 300)
    _fold("u-grew")
    valid = _witness_seq("u-grew")
    _append("u-grew", 300, first=10**7)
    _fold("u-grew", during=lambda: _append("u-grew", 1, first=10**8))
    after = _witness_seq("u-grew")
    assert valid is not None and after is not None and after > valid


def test_the_span_rereads_only_the_records_it_framed(tmp_path):
    path = tmp_path / "log.jsonl"
    lines = [b'{"a": 1}\n', b'{"b": 2}\n', b'{"c": 3}\n']
    path.write_bytes(b"".join(lines))
    span = ConsumedSpan()
    start = len(lines[0])
    list(_iter_entries_located(path, start + len(lines[1]), start=start, span=span))
    assert span.consumed_bytes == len(lines[1])
    # Bytes outside the framed range never refuse.
    path.write_bytes(b'{"z": 9}\n' + lines[1] + b'{"q": 0}\n')
    assert span.holds(tmp_path, "u-span")
    # A byte inside it does.
    path.write_bytes(lines[0] + b'{"b": 3}\n' + lines[2])
    assert not span.holds(tmp_path, "u-span")


def test_a_spoiled_span_refuses_however_the_bytes_read(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_bytes(b'{"a": 1}\n{"b": 2}\n')
    span = ConsumedSpan()
    list(_iter_entries_located(path, start=0, span=span))
    assert span.holds(tmp_path, "u-span")
    span.spoil()
    assert not span.holds(tmp_path, "u-span")


def test_a_refused_savepoint_leaves_no_bundle_a_later_pass_could_persist(monkeypatch):
    from kiro_crew.crew_log import projection_session_memo as memo

    _unit("u-memo", 300)
    _fold("u-memo")
    valid = _witness_seq("u-memo")
    _append("u-memo", 300, first=10**7)
    path = crew_log_store.segment_paths(lg.KIND_SESSION, "u-memo")[-1]
    old = f"e{10**7} {PAD}".encode()
    new = f"e{10**7} {'y' * len(PAD)}".encode()
    real = CrewLog.iter_from

    def walked(self, *args, **kwargs):
        yield from real(self, *args, **kwargs)
        _same_tick_rewrite(path, old, new)

    with monkeypatch.context() as patched:
        patched.setattr(CrewLog, "iter_from", walked)
        served = crew_log.fold_session_warm("u-memo")
    assert served.bundle.origin is None and served.bundle.cuts is None
    assert not any(key[1] == "u-memo" for key in memo._session_memos)
    assert _witness_seq("u-memo") == valid


def test_a_span_check_that_cannot_place_its_path_releases_the_lease(monkeypatch):
    from kiro_crew.crew_log import lease
    from kiro_crew.crew_log.errors import CODE_BAD_ROOT, CrewLogError

    _unit("u-lease", 300)
    _fold("u-lease")
    valid = _witness_seq("u-lease")
    _append("u-lease", 300, first=10**7)
    held_before = len(lease._held)
    calls: "list[str]" = []

    def moved(self, directory, unit_id):
        calls.append(unit_id)
        raise CrewLogError("the crew log root moved", code=CODE_BAD_ROOT)

    monkeypatch.setattr(ConsumedSpan, "holds", moved)
    _fold("u-lease")
    assert calls == ["u-lease"], "the span check ran under the lease"
    assert len(lease._held) == held_before
    assert _witness_seq("u-lease") == valid
