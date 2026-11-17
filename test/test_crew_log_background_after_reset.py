"""A background call that ends after its slot moved to a new unit grows no older unit.

The entry is written to the slot's newest unit and counts in its usage, so the savepoint beside the newest unit stays valid and a
restart reads nothing from the older unit, whatever its size.

Real store, real emitter, real slot read path; reads are counted per unit with a
wrapper around ``CrewLog.iter_from``.
"""

from __future__ import annotations

import itertools
import json
import os
from typing import Any

import pytest
from crew_log_drain import assert_drained

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import checkpoint as savepoints
from kiro_crew.crew_log import eager as crew_log_eager
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import pending_open
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log.projection_readers import _slot_units_for_fold
from kiro_crew.crew_log.store_paths import crew_log_dir

SLOT = "member-bg-reset"
FOLD = "workstreams"


@pytest.fixture(autouse=True)
def _home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    _floor_monkeypatch.setattr(crew_log_eager, "note_commit", lambda *a, **k: None)
    _floor_monkeypatch.setattr(savepoints, "MIN_ADVANCE_ENTRIES", 2)
    from kiro_crew.crew_log import store as crew_log_store

    ticks = itertools.count(1_700_000_000_000, 1_000)
    _floor_monkeypatch.setattr(
        crew_log_store, "now_ms", lambda: next(ticks)  # flake-ok: a fixed clock pin
    )
    crew_log_emit.reset_caches()
    crew_log_eager.stop_for_tests()
    crew_log.forget_slot_folds()
    yield
    crew_log.forget_slot_folds()
    crew_log_emit.reset_caches()


class _Provider:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id


class _Sessions:
    def __init__(self, unit: str, slot: str = SLOT) -> None:
        self.unit = unit
        self.slot = slot

    def get_provider(self, key: str) -> Any:
        return _Provider(self.unit) if key == self.slot else None


class _Usage:
    credits = 1.5
    input_tokens = 40
    output_tokens = 4
    cache_read_tokens = 0
    cache_creation_tokens = 0


def _open(unit: str, slot: str = SLOT) -> None:
    crew_log_emit.on_session_opened(
        unit, agent="kirocrew", slot=slot, model="m", cwd="/w", owner="default"
    )


def _turns(unit: str, count: int) -> None:
    for n in range(count):
        crew_log_emit.on_turn_started(unit, n + 1, "user")


def _read() -> Any:
    units = _slot_units_for_fold(SLOT, FOLD)
    return crew_log.fold_slot_warm(FOLD, units, slot=SLOT).state


def _unit_files(unit: str) -> "dict[str, int]":
    directory = crew_log_dir(lg.KIND_SESSION, unit)
    return {p.name: p.stat().st_size for p in directory.iterdir() if p.is_file()}


def _restart_reads() -> "dict[str, int]":
    crew_log.forget_slot_folds()
    read: dict[str, int] = {}
    real = CrewLog.iter_from

    def counted(self, *args, **kwargs):
        for entry in real(self, *args, **kwargs):
            read[self.id] = read.get(self.id, 0) + 1
            yield entry

    CrewLog.iter_from = counted  # type: ignore[method-assign]
    try:
        _read()
    finally:
        CrewLog.iter_from = real  # type: ignore[method-assign]
    return read


@pytest.mark.parametrize("filler", [100, 400, 1_600])
def test_a_restart_after_a_background_call_across_a_reset_reads_nothing_from_the_old_unit(
    filler,
):
    """Red on main: the entry grows the old unit, the newest unit's savepoint does not
    describe the slot, and the restart re-reads the old unit. Here it reads zero."""
    from kiro_crew.llm_helpers import _background_crew_log_owner, _record_background_crew_log

    old, new = f"old-{filler}", f"new-{filler}"
    _open(old)
    _turns(old, filler)
    assert_drained()
    sessions = _Sessions(old)
    owner = _background_crew_log_owner(sessions, SLOT, "title")
    assert owner == old
    # The slot resets while the call runs: a new unit opens and the slot reads warm.
    _open(new)
    _turns(new, 1)
    assert_drained()
    sessions.unit = new
    _read()
    before = _unit_files(old)

    _record_background_crew_log(
        owner,
        "title",
        _Usage(),
        model="m",
        provider="acp",
        elapsed_ms=5,
        sessions=sessions,
        crew_log_session_key=SLOT,
    )
    assert_drained()

    assert _unit_files(old) == before, "the old unit's log and cuts did not grow"
    written = [
        e
        for e in CrewLog.open(lg.KIND_SESSION, new).iter_from(1)
        if e.type == "background/completed"
    ]
    assert len(written) == 1

    read = _restart_reads()
    assert read.get(old, 0) == 0, read
    assert read.get(new, 0) <= 3, read


def _spend_after_one_call(slot: str, reset: bool) -> "tuple[float, dict[str, Any]]":
    """One billed background call on *slot*, with or without a reset during it.

    Answers the slot's usage total (summed over its units) and its ``workstreams``
    spend row, which is keyed by slot.
    """
    from kiro_crew.llm_helpers import _background_crew_log_owner, _record_background_crew_log

    first, second = f"{slot}-a", f"{slot}-b"
    _open(first, slot)
    _turns(first, 3)
    assert_drained()
    sessions = _Sessions(first, slot)
    owner = _background_crew_log_owner(sessions, slot, "summary")
    if reset:
        _open(second, slot)
        _turns(second, 1)
        assert_drained()
        sessions.unit = second
    _record_background_crew_log(
        owner,
        "summary",
        _Usage(),
        model="m",
        provider="acp",
        elapsed_ms=5,
        sessions=sessions,
        crew_log_session_key=slot,
    )
    assert_drained()
    units = _slot_units_for_fold(slot, FOLD)
    total = sum(crew_log.read_projection(unit, "usage").value["credits"] for unit in units)
    spend = crew_log.fold_slot_warm(FOLD, units, slot=slot).state["spend"]
    assert list(spend) == [slot], spend
    return total, {key: spend[slot][key] for key in ("credits", "reported", "duration_ms")}


def test_a_reset_during_the_call_counts_in_the_successor_unit_and_keeps_the_slot_total():
    """After a reset the call counts once, in the successor unit's ``usage``.

    That is the unit the root session card folds, so the card shows the call there.
    The slot's total is the no-reset total, because ``workstreams`` keys spend by slot.
    """
    plain = _spend_after_one_call("member-bg-card-plain", reset=False)
    _spend_after_one_call("member-bg-card-moved", reset=True)
    successor = crew_log.read_projection("member-bg-card-moved-b", "usage").value
    old = crew_log.read_projection("member-bg-card-moved-a", "usage").value
    assert successor["credits"] == pytest.approx(_Usage.credits)
    assert successor["credits_by_source"]["background"]["reported"] == 1
    assert old["credits"] == pytest.approx(0.0)
    assert plain[0] == pytest.approx(_Usage.credits)


def test_a_reset_during_the_call_charges_the_same_spend_exactly_once():
    """The same call gives the same slot total and the same slot spend row, reset or not."""
    plain = _spend_after_one_call("member-bg-plain", reset=False)
    moved = _spend_after_one_call("member-bg-moved", reset=True)
    assert plain == moved
    assert plain[0] == pytest.approx(_Usage.credits)
    assert plain[1]["credits"] == pytest.approx(_Usage.credits)


def test_a_call_ending_before_the_new_unit_has_a_crew_log_is_held_until_it_opens():
    """Right after a reset the newest unit can have no crew log yet: the entry waits.

    It is written once the unit's first open creates the crew log, with the normal
    header, and the older unit is never written. Spend is counted once.
    """
    from kiro_crew.llm_helpers import _background_crew_log_owner, _record_background_crew_log

    old, new = "held-old", "held-new"
    _open(old)
    _turns(old, 3)
    assert_drained()
    sessions = _Sessions(old)
    owner = _background_crew_log_owner(sessions, SLOT, "title")
    sessions.unit = new  # reset: the slot now names a unit with no crew log
    before = _unit_files(old)
    _record_background_crew_log(
        owner,
        "title",
        _Usage(),
        model="m",
        provider="acp",
        elapsed_ms=5,
        sessions=sessions,
        crew_log_session_key=SLOT,
    )
    assert_drained()
    assert not CrewLog.exists(lg.KIND_SESSION, new), "no crew log is created from here"
    assert _unit_files(old) == before
    _open(new)
    assert_drained()
    entries = list(CrewLog.open(lg.KIND_SESSION, new).iter_from(1))
    types = [e.type for e in entries]
    assert types.index("session/opened") < types.index("background/completed")
    assert [e for e in entries if e.type == "background/completed"]
    assert _unit_files(old) == before, "the older unit never grows"
    units = _slot_units_for_fold(SLOT, FOLD)
    total = sum(crew_log.read_projection(unit, "usage").value["credits"] for unit in units)
    assert total == pytest.approx(_Usage.credits)


def _call_across_a_reset_with_no_new_log(old: str, new: str) -> "dict[str, int]":
    """A billed call whose slot moved to *new*, which has no crew log yet."""
    from kiro_crew.llm_helpers import _background_crew_log_owner, _record_background_crew_log

    _open(old)
    _turns(old, 3)
    assert_drained()
    sessions = _Sessions(old)
    owner = _background_crew_log_owner(sessions, SLOT, "title")
    sessions.unit = new
    before = _unit_files(old)
    _record_background_crew_log(
        owner,
        "title",
        _Usage(),
        model="m",
        provider="acp",
        elapsed_ms=5,
        sessions=sessions,
        crew_log_session_key=SLOT,
    )
    assert_drained()
    assert not CrewLog.exists(lg.KIND_SESSION, new)
    return before


def _held_once(new: str, old: str) -> None:
    entries = list(CrewLog.open(lg.KIND_SESSION, new).iter_from(1))
    types = [e.type for e in entries]
    assert types.count("background/completed") == 1, types
    assert types.index("session/opened") < types.index("background/completed")
    assert [e for e in entries if e.type == "background/completed"]
    assert not pending_open.path_for(new).exists(), "the pending file is gone"


def test_a_held_entry_survives_a_restart_before_the_unit_opens():
    """The entry is on disk, owner-only, so a new process still writes it on open."""
    import stat

    old, new = "restart-old", "restart-new"
    before = _call_across_a_reset_with_no_new_log(old, new)
    pending = pending_open.path_for(new)
    if os.name != "nt":  # Windows reports 0o666 for every file
        assert stat.S_IMODE(pending.stat().st_mode) == 0o600
    crew_log_emit.reset_caches()  # what a restart loses: every in-memory structure
    _open(new)
    assert_drained()
    _held_once(new, old)
    assert _unit_files(old) == before


def test_a_held_entry_is_written_when_the_first_open_entry_fails_once(_floor_monkeypatch):
    """The header lands, ``session/opened`` fails once, the retry still writes the entry."""
    old, new = "retry-old", "retry-new"
    before = _call_across_a_reset_with_no_new_log(old, new)
    real = CrewLog.append
    left = {"n": 1}

    def _fail_once(self, entry_type, *args, **kwargs):
        if entry_type == "session/opened" and left["n"]:
            left["n"] -= 1
            raise OSError("input/output error")
        return real(self, entry_type, *args, **kwargs)

    _floor_monkeypatch.setattr(CrewLog, "append", _fail_once)
    _open(new)
    assert_drained()
    assert left["n"] == 0, "the injected failure never fired"
    _held_once(new, old)
    assert _unit_files(old) == before


def test_the_existence_check_never_runs_on_the_event_loop(_floor_monkeypatch):
    """The caller only queues; the writer thread decides whether the log exists."""
    import asyncio

    on_loop: list[bool] = []
    real = CrewLog.exists

    def _watched(cls, *args, **kwargs):
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return real(*args, **kwargs)

    async def _call() -> None:
        crew_log_emit.on_background_completed(
            "loop-new", kind="title", credits=1.0, newer_unit=True
        )

    _floor_monkeypatch.setattr(CrewLog, "exists", classmethod(_watched))
    asyncio.run(_call())
    assert_drained()
    assert on_loop and not any(on_loop), on_loop


def test_a_drain_cut_short_after_an_append_counts_the_spend_once(_floor_monkeypatch):
    """A crash between a replayed append and the unlink: the next drain skips it."""
    old, new = "cut-old", "cut-new"
    _call_across_a_reset_with_no_new_log(old, new)
    real_unlink = type(pending_open.path_for(new)).unlink
    crashed = {"n": 0}

    def _crash_once(self, *args, **kwargs):
        if self.suffix == ".jsonl" and not crashed["n"]:
            crashed["n"] = 1
            raise OSError("killed before the unlink")
        return real_unlink(self, *args, **kwargs)

    _floor_monkeypatch.setattr(type(pending_open.path_for(new)), "unlink", _crash_once)
    _open(new)
    assert_drained()
    assert crashed["n"] == 1, "the injected crash never fired"
    assert pending_open.path_for(new).exists(), "the cut-short drain left its file"
    crew_log_emit.reset_caches()
    _open(new)  # the next opening append drains again
    assert_drained()
    _held_once(new, old)
    assert crew_log.read_projection(new, "usage").value["credits"] == pytest.approx(_Usage.credits)


def test_a_failed_partial_write_is_cut_back_and_the_retry_holds_one_record(_floor_monkeypatch):
    """ENOSPC after part of a line: the bytes are cut off, the retry's line is whole."""
    real_write = os.write
    failures = {"left": 1}

    def _partial(fd, data):
        if failures["left"] and data.startswith(b'{"type"'):
            failures["left"] -= 1
            real_write(fd, data[:10])
            raise OSError(28, "No space left on device")
        return real_write(fd, data)

    _floor_monkeypatch.setattr(os, "write", _partial)
    record = {"type": "background/completed", "data": {"kind": "title"}, "src": "gateway"}
    with pytest.raises(OSError):
        pending_open._keep("torn-unit", record)
    assert pending_open._keep("torn-unit", record)
    lines = pending_open.path_for("torn-unit").read_bytes().split(b"\n")
    assert lines[-1] == b"" and len(lines) == 2, lines
    assert json.loads(lines[0]) == record, "the retry's line is whole"


def test_past_the_bound_a_held_entry_is_dropped_with_one_warning(caplog, _floor_monkeypatch):
    _floor_monkeypatch.setattr(pending_open, "PER_UNIT_LIMIT", 2)
    _floor_monkeypatch.setattr(pending_open, "_warned", set())
    record = {"type": "background/completed", "data": {"kind": "title"}, "src": "gateway"}
    kept = [pending_open._keep("full-unit", record) for _ in range(4)]
    assert kept == [True, True, False, False]
    assert pending_open.path_for("full-unit").read_bytes().count(b"\n") == 2
    assert len([r for r in caplog.records if "full-unit" in r.getMessage()]) == 1


def test_the_retention_sweep_removes_pending_files_of_retired_and_stale_units():
    record = {"type": "background/completed", "data": {"kind": "title"}, "src": "gateway"}
    for unit in ("retired-unit", "stale-unit", "fresh-unit"):
        assert pending_open._keep(unit, record)
    stale = pending_open.path_for("stale-unit")
    os.utime(stale, (1_000_000, 1_000_000))
    pending_open.sweep(["retired-unit"], cutoff_ms=2_000_000_000)
    assert not pending_open.path_for("retired-unit").exists()
    assert not stale.exists()
    assert pending_open.path_for("fresh-unit").exists()


@pytest.mark.parametrize("failure", ["enospc", "short"])
def test_a_failed_marker_write_leaves_no_marker_and_the_next_drain_writes_once(
    failure, _floor_monkeypatch
):
    """A marker write that fails leaves no marker, so it cannot block a later drain."""
    old, new = f"marker-{failure}-old", f"marker-{failure}-new"
    before = _call_across_a_reset_with_no_new_log(old, new)
    real_write = os.write
    left = {"n": 1}

    def _failing(fd, data):
        if left["n"] and b'"from_seq"' in bytes(data):
            left["n"] -= 1
            if failure == "enospc":
                raise OSError(28, "No space left on device")
            return 0
        return real_write(fd, data)

    _floor_monkeypatch.setattr(os, "write", _failing)
    _open(new)
    assert_drained()
    assert left["n"] == 0, "the injected failure never fired"
    marker = pending_open.path_for(new).with_suffix(".draining")
    assert not marker.exists(), "a failed marker write left a marker"
    assert pending_open.path_for(new).exists(), "the held entry is still waiting"
    crew_log_emit.reset_caches()
    _open(new)
    assert_drained()
    _held_once(new, old)
    assert _unit_files(old) == before
    assert crew_log.read_projection(new, "usage").value["credits"] == pytest.approx(_Usage.credits)


def _hold_one_and_open_with_a_failed_drain(new: str, _floor_monkeypatch, failures: int) -> dict:
    """Hold one billed entry for *new*, then open *new* while its replay fails."""
    crew_log_emit.on_background_completed(new, kind="title", credits=1.0, newer_unit=True)
    assert_drained()
    assert pending_open.path_for(new).exists()
    real = CrewLog.append
    left = {"n": failures}

    def _fail(self, entry_type, *args, **kwargs):
        if entry_type == "background/completed" and left["n"]:
            left["n"] -= 1
            raise OSError("input/output error")
        return real(self, entry_type, *args, **kwargs)

    _floor_monkeypatch.setattr(CrewLog, "append", _fail)
    _open(new)
    assert_drained()
    assert pending_open.path_for(new).exists(), "the failed replay left its file"
    return left


def _background_entries(unit: str) -> "list[Any]":
    entries = list(CrewLog.open(lg.KIND_SESSION, unit).iter_from(1))
    return [e for e in entries if e.type == "background/completed"]


def test_a_failed_replay_then_an_identical_call_counts_both(_floor_monkeypatch):
    """The second call waits for the held one; identical payloads are both counted."""
    new = "twice-new"
    left = _hold_one_and_open_with_a_failed_drain(new, _floor_monkeypatch, failures=2)
    crew_log_emit.on_background_completed(new, kind="title", credits=1.0, newer_unit=True)
    assert_drained()
    assert left["n"] == 0, "the injected failures never fired"
    crew_log_emit.reset_caches()
    _open(new)  # a later drain must not take the second call for the held one
    assert_drained()
    assert len(_background_entries(new)) == 2
    assert not pending_open.path_for(new).exists()
    assert crew_log.read_projection(new, "usage").value["credits"] == pytest.approx(2.0)


def test_a_call_with_no_reset_lands_after_a_leftover_held_entry(_floor_monkeypatch):
    """A no-reset call on a unit with a held entry replays it first, then appends."""
    new = "leftover-new"
    _hold_one_and_open_with_a_failed_drain(new, _floor_monkeypatch, failures=1)
    log = CrewLog.open(lg.KIND_SESSION, new)
    start = log.last_seq
    crew_log_emit.on_background_completed(new, kind="title", credits=1.0)
    assert_drained()
    landed = _background_entries(new)
    assert len(landed) == 2 and all(e.seq > start for e in landed)
    assert not pending_open.path_for(new).exists()
    crew_log_emit.reset_caches()
    _open(new)
    assert_drained()
    assert crew_log.read_projection(new, "usage").value["credits"] == pytest.approx(2.0)


def test_a_resumed_drain_cut_short_again_counts_the_spend_once(_floor_monkeypatch):
    """Failed replay, a turn, then a replay that crashes before the unlink: counted once."""
    new = "resumed-new"
    _hold_one_and_open_with_a_failed_drain(new, _floor_monkeypatch, failures=1)
    _turns(new, 1)  # turn/started lands right after the first marker
    assert_drained()
    real_unlink = type(pending_open.path_for(new)).unlink
    crashed = {"n": 0}

    def _crash_once(self, *args, **kwargs):
        if self.suffix == ".jsonl" and not crashed["n"]:
            crashed["n"] = 1
            raise OSError("killed before the unlink")
        return real_unlink(self, *args, **kwargs)

    _floor_monkeypatch.setattr(type(pending_open.path_for(new)), "unlink", _crash_once)
    crew_log_emit.reset_caches()
    _open(new)  # this drain appends the held charge, then crashes
    assert_drained()
    assert crashed["n"] == 1, "the injected crash never fired"
    assert len(_background_entries(new)) == 1
    crew_log_emit.reset_caches()
    _open(new)  # the next drain must see the charge it already wrote
    assert_drained()
    assert len(_background_entries(new)) == 1
    assert not pending_open.path_for(new).exists()
    assert crew_log.read_projection(new, "usage").value["credits"] == pytest.approx(1.0)


def test_the_directory_is_synced_after_the_marker_rename_and_before_the_replay(
    _floor_monkeypatch,
):
    """A power loss after the rename must not lose the marker's name."""
    from kiro_crew import atomic_write as aw

    new = "dirsync-new"
    crew_log_emit.on_background_completed(new, kind="title", credits=1.0, newer_unit=True)
    assert_drained()
    events: list[str] = []
    real_write, real_sync, real_append = aw.atomic_write, aw.fsync_dir, CrewLog.append

    def _write(path, *args, **kwargs):
        result = real_write(path, *args, **kwargs)
        events.append(f"write:{os.path.basename(str(path))}")
        return result

    def _sync(path, *args, **kwargs):
        events.append(f"sync:{os.path.basename(str(path))}")
        return real_sync(path, *args, **kwargs)

    def _append(self, entry_type, *args, **kwargs):
        if entry_type == "background/completed":
            events.append("append")
        return real_append(self, entry_type, *args, **kwargs)

    _floor_monkeypatch.setattr(aw, "atomic_write", _write)
    _floor_monkeypatch.setattr(aw, "fsync_dir", _sync)
    _floor_monkeypatch.setattr(CrewLog, "append", _append)
    _open(new)
    assert_drained()
    marker = pending_open.path_for(new).with_suffix(".draining").name
    folder = f"sync:{pending_open.path_for(new).parent.name}"
    at = events.index(f"write:{marker}")
    assert events[at + 1] == folder, events
    assert events.index("append") > at + 1, events
    assert events[-1] == folder, "the cleanup is synced too"


def test_an_entry_over_the_line_ceiling_is_dropped_and_the_next_one_lands(
    caplog, _floor_monkeypatch
):
    """A 70,000-character model id is dropped once, counted, and blocks nothing."""
    _floor_monkeypatch.setattr(pending_open, "_warned", set())
    new = "huge-new"
    dropped = crew_log_emit.dropped_writes()
    crew_log_emit.on_background_completed(
        new, kind="title", model="m" * 70_000, credits=5.0, newer_unit=True
    )
    assert_drained()
    crew_log_emit.on_background_completed(new, kind="title", credits=1.0, newer_unit=True)
    assert_drained()
    assert crew_log_emit.dropped_writes() == dropped + 1
    mine = [r for r in caplog.records if r.name == pending_open.__name__]
    assert len([r for r in mine if new in r.getMessage()]) == 1
    _open(new)
    assert_drained()
    assert len(_background_entries(new)) == 1
    assert crew_log.read_projection(new, "usage").value["credits"] == pytest.approx(1.0)


def test_an_oversized_line_left_by_an_older_build_does_not_block_the_drain():
    new = "old-huge-new"
    path = pending_open.path_for(new)
    path.parent.mkdir(parents=True, exist_ok=True)
    big = {"type": "background/completed", "data": {"kind": "t", "model": "m" * 70_000}}
    good = {"type": "background/completed", "data": {"kind": "t", "credits": 1.0}}
    path.write_bytes(
        b"".join(json.dumps({**r, "src": "gateway"}).encode() + b"\n" for r in (big, good))
    )
    _open(new)
    assert_drained()
    assert len(_background_entries(new)) == 1
    assert not path.exists()


def test_a_failed_directory_sync_is_cut_back_so_the_retry_holds_one(_floor_monkeypatch):
    """fsync_dir raises EIO once after the record is written: the retry keeps one."""
    import errno

    from kiro_crew import atomic_write as aw

    new = "eio-new"
    real_sync = aw.fsync_dir
    left = {"n": 1}

    def _eio_once(path, *args, **kwargs):
        if left["n"] and os.path.basename(str(path)) == pending_open.path_for(new).parent.name:
            left["n"] -= 1
            raise OSError(errno.EIO, "input/output error")
        return real_sync(path, *args, **kwargs)

    _floor_monkeypatch.setattr(aw, "fsync_dir", _eio_once)
    crew_log_emit.on_background_completed(new, kind="title", credits=1.0, newer_unit=True)
    assert_drained()
    assert left["n"] == 0, "the injected failure never fired"
    assert pending_open.path_for(new).read_bytes().count(b"\n") == 1
    _open(new)
    assert_drained()
    assert len(_background_entries(new)) == 1


def test_a_close_error_after_a_good_write_does_not_retry(_floor_monkeypatch):
    """os.close raises once the record is fsynced: kept once, no retry."""
    new = "close-new"
    real_write, real_close = os.write, os.close
    written: set[int] = set()
    left = {"n": 1}

    def _write(fd, data):
        if bytes(data).startswith(b'{"type"'):
            written.add(fd)
        return real_write(fd, data)

    def _close(fd):
        real_close(fd)
        if left["n"] and fd in written:
            left["n"] -= 1
            raise OSError(5, "input/output error")

    _floor_monkeypatch.setattr(os, "write", _write)
    _floor_monkeypatch.setattr(os, "close", _close)
    crew_log_emit.on_background_completed(new, kind="title", credits=1.0, newer_unit=True)
    assert_drained()
    assert left["n"] == 0, "the injected failure never fired"
    assert pending_open.path_for(new).read_bytes().count(b"\n") == 1
    _open(new)
    assert_drained()
    assert len(_background_entries(new)) == 1
