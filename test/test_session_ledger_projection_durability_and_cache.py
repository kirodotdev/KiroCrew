"""Tests for the session ledger's durability answer and the caches a read goes through.

An entry is on disk before ``record`` returns, and ``record_update`` reports ``durable``
from this unit's own growth, so a refused or unlanded append is never called durable,
and an update too large for one entry is refused before the append. The slot map sees a
unit whose header lands late or that replaces another, and a stray directory does not
break its scan. The fold cache answers what a cold fold would: it is bounded in resident
bytes, rebuilt for a new unit, and not stale after an older unit grows or an append
lands during a read. A timestamp no datetime can hold does not crash the fold. The
constants, the autouse home fixture, ``_unit`` and ``_entries`` live in
``session_ledger_projection_helpers.py``.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from dataclasses import replace

import pytest
from session_ledger_projection_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    LATER_SESSION,
    SESSION,
    SLOT,
    _entries,
    _isolated_home,
    _unit,
)

from kiro_crew import crew_log as lg
from kiro_crew import session_ledger as sl
from kiro_crew.crew_log import CrewLog, eager
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log import store


def test_a_refusal_during_the_append_is_not_reported_durable():
    """The refusal count brackets the append, so an INLINE refusal is visible.

    A second gateway owning the unit appends inline rather than queueing, so the
    counter moves during the append; a sample taken afterwards folds that move into
    the baseline and calls a refused write durable.
    """
    _unit(SESSION)
    refused = [0]
    real_emit = crew_log_emit.on_ledger_recorded

    def _refusing(session_id, data):
        refused[0] += 1  # the append is refused as it is made, not queued

    crew_log_emit.on_ledger_recorded = _refusing  # type: ignore[assignment]
    real_dropped = crew_log_emit.dropped_writes
    crew_log_emit.dropped_writes = lambda: refused[0]  # type: ignore[assignment]
    try:
        _state, durable = sl.record_update(SLOT, session_id=SESSION, goal="g")
    finally:
        crew_log_emit.on_ledger_recorded = real_emit  # type: ignore[assignment]
        crew_log_emit.dropped_writes = real_dropped  # type: ignore[assignment]
    assert durable is False


def test_durability_is_answered_from_this_units_own_growth():
    """A process-wide counter cannot say whether THIS append landed.

    An append that never reaches the file leaves the unit's newest seq where it was,
    which is evidence about this write rather than about every session in the process.
    """
    _unit(SESSION)
    real = crew_log_emit.on_ledger_recorded
    crew_log_emit.on_ledger_recorded = lambda session_id, data: None  # never written
    try:
        _state, durable = sl.record_update(SLOT, session_id=SESSION, goal="g")
    finally:
        crew_log_emit.on_ledger_recorded = real  # type: ignore[assignment]
    assert durable is False

    crew_log.forget_slot_folds()
    _state, durable = sl.record_update(SLOT, session_id=SESSION, goal="g2")
    assert durable is True


def test_a_fold_racing_an_append_is_not_cached_with_a_stale_seq():
    """A pre-sampled seq beside a newer checkpoint would make the next read empty.

    The sample is taken before the fold, so an append landing while it runs is in the
    checkpoint but not in the sample. Caching that pair would have the next read
    advance from a seq already folded, which ``advance`` refuses.
    """
    _unit()
    sl.record(SLOT, session_id=SESSION, goal="before the race")
    # ``ledger`` is an EAGER fold, so that record also woke the fold worker. Settle it
    # BEFORE the table is dropped, or its pass lands afterwards and the case has two
    # folders racing on one key -- which is a different race from the one under test.
    # flake-ok: the 10 s cap only names a stuck fold worker; drain returns as soon as every queued wake is folded
    assert eager.drain(timeout=10.0)
    crew_log.forget_slot_folds()

    real_mark = crew_log._unit_mark
    calls = {"n": 0}
    # THIS thread's sample is the one the race is about. ``ledger`` is an EAGER fold, so
    # the append above also wakes the fold worker, and that worker samples marks through
    # the same module attribute -- so a probe counting every call can have its one shot
    # consumed by the worker, land the racing append outside the read under test, and
    # report an empty value as if the read had dropped it.
    reader = threading.get_ident()

    def _sampling_that_lands_an_append(unit_id: str):
        mark = real_mark(unit_id)
        if threading.get_ident() != reader:
            return mark
        calls["n"] += 1
        if calls["n"] == 1:
            # Between the sample and the fold, one more entry lands.
            handle = CrewLog.open(lg.KIND_SESSION, SESSION)
            handle.append(
                sl.LEDGER_ENTRY_TYPE,
                {"slot": SLOT, "next": "landed during the fold"},
                src="gateway",
            )
            del handle
        return mark

    crew_log._unit_mark = _sampling_that_lands_an_append  # type: ignore[assignment]
    try:
        first = sl.read_state(SLOT)
    finally:
        crew_log._unit_mark = real_mark  # type: ignore[assignment]

    # The racing entry is folded in, and the warm cell records the height it REACHED
    # rather than the height that was sampled before it read ...
    assert first["next"] == "landed during the fold"
    held = crew_log._slot_memos[(str(sl.data_home()), SLOT, sl._FOLD_NAME)]
    assert held.reached == crew_log._unit_mark(SESSION).last_seq
    assert held.marks[-1].last_seq == held.reached
    # ... so the next read continues from above that entry and still answers with it,
    # rather than folding it a second time or resuming past the ones before it.
    assert sl.read_state(SLOT)["next"] == "landed during the fold"
    assert sl.read_state(SLOT)["goal"] == "before the race"


# --------------------------------------------------------------------------- #
# the append is durable before it is acknowledged
# --------------------------------------------------------------------------- #


def test_the_entry_is_on_disk_before_record_returns():
    """An acknowledgement a caller acts on has to mean the entry landed."""
    _unit()
    sl.record(SLOT, session_id=SESSION, goal="durable", event="e", event_kind="progress")
    # Read the file directly rather than through the fold, and take no flush of our
    # own: if the append were still queued this would see nothing.
    assert [e for e in _entries() if e.type == sl.LEDGER_ENTRY_TYPE]


# --------------------------------------------------------------------------- #
# the slot cache cannot serve a stale map
# --------------------------------------------------------------------------- #


def test_a_unit_whose_header_lands_after_a_scan_becomes_visible():
    """``create`` publishes the header AFTER its mkdir, and that lands inside the
    directory, so the root's mtime and child count do not move with it. A cache
    keyed on the root alone would hold a map missing this unit until unrelated
    churn happened to invalidate it."""
    directory = store.crew_log_dir(lg.KIND_SESSION, SESSION)
    directory.mkdir(parents=True, exist_ok=True)
    # Scan the window: the store exists, its header does not.
    assert store.session_units_for_slot(SLOT) == ()
    # Publish the header without touching the root.
    _unit()
    assert store.session_units_for_slot(SLOT) == (SESSION,)


def test_a_permanently_unprovable_child_does_not_break_the_scan():
    """A stray directory is re-checked and never proves; the real units still resolve."""
    (store.crew_log_root(lg.KIND_SESSION) / "not-a-unit").mkdir(parents=True, exist_ok=True)
    _unit()
    assert store.session_units_for_slot(SLOT) == (SESSION,)
    assert store.session_units_for_slot(SLOT) == (SESSION,)


# --------------------------------------------------------------------------- #
# the fold is continued, not re-walked
# --------------------------------------------------------------------------- #


def test_the_continued_fold_equals_a_cold_one_at_every_update():
    """The resumed answer and the from-scratch answer are one implementation.

    The record is read on every loop wake, so it is folded from a cached checkpoint
    rather than re-walked from seq 1. That is only safe while the two agree.
    """
    _unit()
    for n in range(6):
        sl.record(
            SLOT,
            session_id=SESSION,
            next_step=f"step {n}",
            event=f"e{n}",
            event_kind="progress",
        )
        warm = sl.read_state(SLOT)
        crew_log.forget_slot_folds()
        assert sl.read_state(SLOT) == warm, f"warm and cold folds disagree after {n + 1} updates"


def test_a_new_unit_for_the_slot_rebuilds_the_fold():
    """A reset gives the slot another session, so the cached unit list is stale."""
    _unit(SESSION)
    sl.record(SLOT, session_id=SESSION, goal="before")
    assert sl.read_state(SLOT)["goal"] == "before"
    _unit(LATER_SESSION)
    sl.record(SLOT, session_id=LATER_SESSION, next_step="after")
    state = sl.read_state(SLOT)
    assert state["goal"] == "before"
    assert state["next"] == "after"


def test_the_fold_cache_is_bounded_by_resident_bytes(monkeypatch):
    """A gateway sees many slots over its life; the warm store cannot grow with them.

    Bounded in BYTES, each cell charged its own fold's measured cost, so the ceiling here
    is six ``ledger`` cells however many slots are read.
    """
    budget = crew_log.slot_fold_cell_bytes("ledger") * 6
    monkeypatch.setenv(crew_log.SLOT_FOLD_CACHE_BYTES_ENV, str(budget))
    _unit()
    for n in range(14):
        sl.read_state(f"chat-{n}")
    held = sum(crew_log.slot_fold_cell_bytes(key[2]) for key in crew_log._slot_memos)
    assert held <= budget
    assert len(crew_log._slot_memos) == 6


def test_growth_in_an_older_unit_is_not_hidden_by_the_cache():
    """An earlier unit is not closed to writes, so its seq is part of the cache key.

    A forced reset tears a session down while a turn is still running and that turn
    keeps appending through the handle it holds. Keying growth on the newest unit
    alone left those entries permanently outside the record: the unit list is
    unchanged and the newest seq is unchanged, so nothing would invalidate it.
    """
    _unit(SESSION)
    sl.record(SLOT, session_id=SESSION, goal="first")
    _unit(LATER_SESSION)
    sl.record(SLOT, session_id=LATER_SESSION, next_step="second")
    assert sl.read_state(SLOT)["next"] == "second"

    # Append to the OLDER unit, exactly as a turn that outlived its reset does.
    handle = CrewLog.open(lg.KIND_SESSION, SESSION)
    handle.append(
        sl.LEDGER_ENTRY_TYPE,
        {"slot": SLOT, "event": "late entry from the retired session", "event_kind": "progress"},
        src="gateway",
    )
    del handle

    events = [e["text"] for e in sl.read_state(SLOT)["events"]]
    assert "late entry from the retired session" in events


def test_a_durable_append_is_reported_as_durable():
    """The route tells a caller whether the update reached disk, rather than implying it."""
    _unit()
    _state, durable = sl.record_update(SLOT, session_id=SESSION, goal="g")
    assert durable is True


def test_the_plain_record_helper_answers_with_the_record_alone():
    """A caller that cannot act on durability keeps the record's ten fields.

    The durability rides beside the record rather than inside it, so the shape every
    reader already expected is unchanged.
    """
    _unit()
    state = sl.record(SLOT, session_id=SESSION, goal="g")
    assert state["goal"] == "g"
    assert "durable" not in state


# --------------------------------------------------------------------------- #
# damaged input cannot take the record down
# --------------------------------------------------------------------------- #


def test_a_timestamp_outside_the_datetime_range_does_not_crash_the_fold():
    """The line stays on disk, so a crash here would be permanent for that slot."""
    _unit()
    handle = CrewLog.open(lg.KIND_SESSION, SESSION)
    handle.append(sl.LEDGER_ENTRY_TYPE, {"slot": SLOT, "goal": "survives"}, src="gateway")
    del handle
    # Rewrite the entry's envelope time to a value no datetime can hold, the way a
    # damaged or planted line would carry it.
    path = store.crew_log_path(lg.KIND_SESSION, SESSION)
    lines = path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[-1])
    entry["time"] = 10**19
    lines[-1] = json.dumps(entry)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    crew_log.forget_slot_folds()

    state = sl.read_state(SLOT)
    assert state["goal"] == "survives"
    assert state["created_at"] == ""
    # And the fold answers through the route's own entry point too, not only here.
    assert crew_log.read_slot_projection(SLOT, "ledger").value["goal"] == "survives"


def test_a_same_count_unit_swap_invalidates_the_slot_map():
    """A purge plus a create inside one mtime tick leaves the count and mtime equal.

    The names are in the fingerprint for that case: without them the replacement
    unit's record stays invisible for as long as the directory sits still.
    """
    _unit(SESSION)
    assert store.session_units_for_slot(SLOT) == (SESSION,)
    root = store.crew_log_root(lg.KIND_SESSION)
    before = root.stat().st_mtime_ns
    # Swap one unit for another and restore the root's mtime, so only the NAMES differ.
    # The emitter caches this unit's open handle, which HOLDS its `.lease`. Windows
    # refuses to unlink an open file, so the test drops the handle it caused to be
    # opened before standing in for a delete. POSIX would allow the unlink; the
    # cleanup is the test's either way.
    crew_log_emit.reset_caches()
    with eager.paused():
        shutil.rmtree(store.crew_log_dir(lg.KIND_SESSION, SESSION))
    _unit(LATER_SESSION)
    os.utime(root, ns=(before, before))
    assert store.session_units_for_slot(SLOT) == (LATER_SESSION,)


def test_an_append_landing_during_a_fold_survives_the_next_read():
    """An entry the pass read but the pre-read sample missed stays in the record.

    The heights a read samples are its READ PLAN, never the cell's claim about itself.
    An append landing during a read is folded in while sitting outside that sample, so a
    cell that remembered the sample would describe itself as one entry short and send
    every later read back to the file for a tail it has already folded. Both halves are
    checked here: the entry is in the answer, and the next read opens no unit.
    """
    _unit(SESSION)
    for goal in ("first", "second", "third"):
        sl.record(SLOT, session_id=SESSION, goal=goal)
    # ``record`` folds to build its own answer, so the cache is already warm at the seq
    # BEFORE the newest entry. Two more appends are needed for a sample to sit between
    # that checkpoint and the truth, and they must not fold on the way in -- which is
    # why they go through the emitter rather than through ``record``.
    for goal in ("fourth", "fifth"):
        crew_log_emit.on_ledger_recorded(SESSION, {"slot": SLOT, "goal": goal})
    # flake-ok: the 5 s cap only names a stuck writer; flush returns as soon as the queue drains
    assert crew_log_emit.flush(timeout=5.0)
    # Every committed entry wakes the eager folder, which folds this unit's slot AND
    # session folds on its own thread through the same module attributes the probes
    # below replace. Settled first, and both probes count only THIS thread: otherwise
    # the folder can take the one short sample, or its own session pass shows up as a
    # read here, and the case measures the folder rather than the read under test.
    # flake-ok: the 10 s cap only names a stuck fold worker; drain returns as soon as every queued wake is folded
    assert eager.drain(timeout=10.0)
    reader = threading.get_ident()

    # Stand in for an append landing DURING the fold: the pre-read sample misses the
    # newest seq, while the stream reads to the end of the file and folds it.
    real = crew_log._unit_mark
    calls = {"n": 0}

    def _sampled_one_short(unit_id: str):
        if threading.get_ident() != reader:
            return real(unit_id)
        calls["n"] += 1
        mark = real(unit_id)
        if calls["n"] == 1:
            # Only the HEIGHT is lowered. A real sample reports the file's identity and
            # byte fingerprint whatever its height, so dropping either here would stand
            # in for a log whose bytes changed rather than one that merely grew.
            return replace(mark, last_seq=mark.last_seq - 1)
        return mark

    crew_log._unit_mark = _sampled_one_short  # type: ignore[assignment]
    try:
        assert sl.read_state(SLOT, SESSION)["goal"] == "fifth"
    finally:
        crew_log._unit_mark = real  # type: ignore[assignment]

    # The read IMMEDIATELY after is where a cell holding the sample shows itself: its
    # marks would disagree with the file and send it back for a tail already folded. One
    # read later is too late to ask -- by then the sample has been replaced by a real one
    # and the cell describes itself again either way.
    reads: list[int] = []
    real_iter = CrewLog.iter_from

    def counted(self, start, **kwargs):
        if threading.get_ident() == reader:
            reads.append(start)
        return real_iter(self, start, **kwargs)

    CrewLog.iter_from = counted  # type: ignore[assignment]
    try:
        assert sl.read_state(SLOT, SESSION)["goal"] == "fifth"
    finally:
        CrewLog.iter_from = real_iter  # type: ignore[assignment]

    assert reads == []
    # And the entry the short sample missed stays in the record.
    assert sl.read_state(SLOT, SESSION)["goal"] == "fifth"


def test_an_update_too_large_for_one_entry_is_refused_before_the_append():
    """A refused append is permanent, so the update must not be reported as taken.

    The writer counts an over-ceiling entry and drops it, which reaches the caller as
    ``durable=False`` -- the very same value a writer that is merely still busy gives,
    and that update IS recorded. So a caller acting on ``durable`` alone cannot tell a
    record that will land from one that can never land, and the one that can never land
    would be reported recorded and then be absent from the slot at the next read.
    """
    _unit()
    # The CLAMPS' own worst case, not an arbitrary big value: each key and value is
    # exactly what ``record_update`` retains, so this is the largest entry the public
    # surface can be asked to write. 32 * (128 + 2000) is over the 64 KiB ceiling, so
    # the ceiling is reachable through clamped input alone.
    oversized = {
        ("k%03d" % i).ljust(sl._MAX_ARTIFACT_KEY, "x"): "v" * sl._MAX_TEXT
        for i in range(sl._MAX_ARTIFACTS)
    }
    assert not crew_log_emit.ledger_entry_fits({"slot": SLOT, "artifacts": oversized})

    with pytest.raises(sl.LedgerEntryTooLarge):
        sl.record_update(
            SLOT,
            session_id=SESSION,
            artifacts=oversized,
            event="too big",
            event_kind="progress",
        )

    # Refused BEFORE the append, so the log holds no entry for it and the record is
    # unchanged -- the refusal is not an entry that half happened.
    assert [e for e in _entries() if e.type == sl.LEDGER_ENTRY_TYPE] == []
    assert sl.read_state(SLOT)["artifacts"] == {}

    # And an ordinary update on the same slot still lands, so the refusal is scoped to
    # the entry that does not fit rather than to the slot.
    sl.record(SLOT, session_id=SESSION, phase="implementing", event="ok", event_kind="phase")
    assert sl.read_state(SLOT)["phase"] == "implementing"
