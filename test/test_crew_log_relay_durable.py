"""The board charge relay survives a crash: a committed charge whose copy never landed
is replayed on start, exactly once, to the board the charge was written under -- and
only when that can be proven, so a rebound worker's charges are refused, not misrouted.

Each test runs the real crew-log writer and the real relay on a temp data home with a
real binding file. A crash between a charge's own commit and its board copy is modelled
by dropping the copy the live relay would have handed the writer (``hand_over`` as a
no-op) while the charge itself commits durably; ``replay_on_start`` is then the restart.
"""

from __future__ import annotations

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import eager as crew_log_eager
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log import work_relay
from kiro_crew.crew_log.emit_runtime import _write as emit_write
from kiro_crew.testing.wait import default_timeout
from kiro_crew.work_ledger_writes import _write_binding

LEAD = "chat-durable-lead"
OTHER = "chat-durable-other"
WORKER = "chat-durable-worker"
LEAD_UNIT = "u-durable-lead"
OTHER_UNIT = "u-durable-other"
WORKER_UNIT = "u-durable-worker"
WORKER_UNIT_2 = "u-durable-worker-2"


@pytest.fixture(autouse=True)
def _live_crew_log(tmp_path):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        patch.setenv("KIROCREW_CREW_LOG", "1")
        crew_log_emit.reset_caches()
        crew_log_eager.stop_for_tests()
        crew_log.forget_slot_folds()
        work_relay.forget_for_tests()
        yield patch
        crew_log_emit.flush()
        crew_log_eager.stop_for_tests()
        crew_log.forget_slot_folds()
        work_relay.forget_for_tests()
        crew_log_emit.reset_caches()


def _unit(unit: str, slot: str) -> None:
    CrewLog.create(lg.KIND_SESSION, unit, owner="owner", agent="lead", slot=slot)


def _noted_unit(unit: str, slot: str) -> None:
    from kiro_crew.crew_log import writer_groups

    _unit(unit, slot)
    writer_groups.note_unit_slot(unit, slot)


def _settle() -> None:
    assert crew_log_emit.flush(timeout=default_timeout())
    assert crew_log_eager.drain(timeout=default_timeout())


def _boards() -> None:
    for slot, unit in ((LEAD, LEAD_UNIT), (OTHER, OTHER_UNIT)):
        _unit(unit, slot)
        data = {"slot": slot, "actor": "conductor", "by": slot, "action": "goal", "goal": "g"}
        assert crew_log_emit.on_work_recorded(unit, {**data, "round": 1}, timeout=default_timeout())
    _settle()


def _emit_closer(turn: int, credits: float, unit: str = WORKER_UNIT) -> None:
    emit_write(
        unit,
        "turn/completed",
        {
            "turn": turn,
            "stop_reason": "end_turn",
            "depth": 0,
            "duration_ms": 10,
            "model": "opus",
            "provider": "kiro",
            "credits": credits,
            "tokens": {"input": 5, "output": 1, "cache_read": 0, "cache_write": 0},
        },
    )


def _charges(unit: str = LEAD_UNIT) -> "list[float]":
    log = CrewLog.open(lg.KIND_SESSION, unit)
    return [e.data["credits"] for e in log.iter_from(1) if e.type == "work/charged"]


def _closers(unit: str = LEAD_UNIT) -> "list[str]":
    log = CrewLog.open(lg.KIND_SESSION, unit)
    return [e.data.get("closer") for e in log.iter_from(1) if e.type == "work/charged"]


class _Crash:
    """Drop the copy the live relay hands the writer, so a committed charge has no board
    copy -- a crash between the commit and the copy. The charge itself still commits.

    ``restart()`` restores the live hand-over (and ONLY that), the way a fresh process
    would have it, so the surrounding env patches stay in place.
    """

    def __init__(self) -> None:
        self._real = work_relay.hand_over
        work_relay.hand_over = lambda copies: None  # type: ignore[assignment]

    def restart(self) -> None:
        work_relay.hand_over = self._real  # type: ignore[assignment]
        # A real restart loses the in-memory per-(origin, board) relay guard, so the
        # durable cursor -- not that guard -- is what makes replay exactly once.
        work_relay._RECENT.clear()


def test_committed_charge_replays_when_binding_unchanged(_live_crew_log):
    """A charge committed under a binding never rebound, whose copy a crash lost, lands
    exactly one board copy on restart."""
    _boards()
    _noted_unit(WORKER_UNIT, WORKER)
    _write_binding(WORKER, LEAD, "it_0000000a")
    crash = _Crash()
    _emit_closer(1, 2.5)
    _settle()
    assert _charges(LEAD_UNIT) == [], "the crash should have lost the live copy"
    # Restart: the live relay works again, and replay re-sends the uncopied charge.
    crash.restart()
    assert work_relay.replay_on_start() == 1
    _settle()
    assert _charges(LEAD_UNIT) == [2.5]


def _billed_by(unit: str = LEAD_UNIT) -> "list[str]":
    log = CrewLog.open(lg.KIND_SESSION, unit)
    return [e.data.get("by") for e in log.iter_from(1) if e.type == "work/charged"]


def test_replayed_charge_is_billed_to_the_worker_slot_not_the_unit_id(_live_crew_log):
    """The replayed copy names the worker's slot key as ``by`` (what the board bills),
    not the worker's unit id, which differ here (chat-... vs u-...)."""
    assert WORKER != WORKER_UNIT
    _boards()
    _noted_unit(WORKER_UNIT, WORKER)
    _write_binding(WORKER, LEAD, "it_0000000a")
    crash = _Crash()
    _emit_closer(1, 2.5)
    _settle()
    crash.restart()
    assert work_relay.replay_on_start() == 1
    _settle()
    assert _charges(LEAD_UNIT) == [2.5]
    assert _billed_by(LEAD_UNIT) == [WORKER]


def test_no_duplicate_when_crash_lands_between_board_append_and_cursor_write(_live_crew_log):
    """The copy landed but the cursor write was lost: a second restart re-sends it and
    the reader counts the charge once by its closer, never twice."""
    _boards()
    _noted_unit(WORKER_UNIT, WORKER)
    _write_binding(WORKER, LEAD, "it_0000000a")
    crash = _Crash()
    _emit_closer(1, 3.0)
    _settle()
    crash.restart()
    # First restart: the copy lands, so the board has it once.
    assert work_relay.replay_on_start() == 1
    _settle()
    assert _charges(LEAD_UNIT) == [3.0]
    # A crash after the copy but before the cursor advanced replays the same closer; the
    # reader dedupes it, so the board still shows one charge.
    work_relay._RECENT.clear()  # a second restart loses the in-memory per-origin guard
    work_relay.replay_on_start()
    _settle()
    # A duplicate physical copy may land (the cursor write was lost), but both copies
    # carry ONE closer, so the board's fold bills the charge exactly once
    # (projection_workstreams._workstreams_charged dedupes by closer).
    assert len(set(_closers(LEAD_UNIT))) == 1
    assert all(c == 3.0 for c in _charges(LEAD_UNIT))


def test_replay_skips_a_charge_committed_before_a_rebind(_live_crew_log):
    """Bound to the lead, a charge commits and its copy is lost; then a rebind to another
    board. Replay refuses the charge -- PR1 cannot prove which board it was written
    under once the worker was rebound -- rather than risk the wrong board."""
    _boards()
    _noted_unit(WORKER_UNIT, WORKER)
    _write_binding(WORKER, LEAD, "it_0000000a")
    crash = _Crash()
    _emit_closer(1, 1.0)
    _settle()
    crash.restart()
    _write_binding(WORKER, OTHER, "it_0000000b")  # rebind
    assert work_relay.replay_on_start() == 0
    _settle()
    assert _charges(LEAD_UNIT) == []
    assert _charges(OTHER_UNIT) == []


def test_replay_skips_an_aba_rebind(_live_crew_log):
    """A->B->A: the live binding board (A) equals the oldest boundary's board, but a
    charge written under the first A must not be assumed to belong to the live A. More
    than one boundary record means PR1 refuses the whole above-boundary window."""
    _boards()
    _noted_unit(WORKER_UNIT, WORKER)
    _write_binding(WORKER, LEAD, "it_0000000a")  # A
    crash = _Crash()
    _emit_closer(1, 1.0)
    _settle()
    crash.restart()
    _write_binding(WORKER, OTHER, "it_0000000b")  # B
    _write_binding(WORKER, LEAD, "it_0000000c")  # A again, new item
    assert work_relay.replay_on_start() == 0
    _settle()
    assert _charges(LEAD_UNIT) == []
    assert _charges(OTHER_UNIT) == []


def test_replay_skips_a_charge_before_any_boundary(_live_crew_log):
    """A charge committed before any bind recorded a boundary (a crash before the first
    bind's sidecar write) is below the boundary seq, so replay never re-sends it."""
    _boards()
    _noted_unit(WORKER_UNIT, WORKER)
    crash = _Crash()
    _emit_closer(1, 1.0)  # committed while unbound: no boundary covers it
    _settle()
    crash.restart()
    _write_binding(WORKER, LEAD, "it_0000000a")  # boundary_seq is now at or past seq 1
    assert work_relay.replay_on_start() == 0
    _settle()
    assert _charges(LEAD_UNIT) == []


def test_replay_skips_the_first_boundary_over_a_pre_existing_binding(_live_crew_log):
    """A worker bound before this shipped has a binding file and no sidecar. Its first
    bind after the upgrade records one boundary, but a charge in flight under the OLD
    binding can land above it, so that first boundary is unproven and replay refuses
    it -- even though there is only one boundary record."""
    import os

    from kiro_crew import work_ledger

    _boards()
    _noted_unit(WORKER_UNIT, WORKER)
    # Bind A, then erase the sidecar: the pre-upgrade state is a binding with no record.
    _write_binding(WORKER, LEAD, "it_0000000a")
    os.remove(work_ledger.relay_boundary_path(WORKER))
    # The first bind after the upgrade records boundary #1 at the current tail (0). A
    # charge that read the OLD binding then commits ABOVE that boundary_seq -- the real
    # race: the bind captured the tail before the in-flight charge appended.
    _write_binding(WORKER, LEAD, "it_0000000b")
    crash = _Crash()
    _emit_closer(1, 1.0)
    _settle()
    crash.restart()
    assert work_relay.replay_on_start() == 0
    _settle()
    assert _charges(LEAD_UNIT) == []


def test_replay_covers_only_the_bound_unit(_live_crew_log):
    """PR1's boundary names one worker unit. A charge in a later worker unit is not in
    that boundary's unit, so PR1 does not replay it (stated limit; PR-later work)."""
    _boards()
    _noted_unit(WORKER_UNIT, WORKER)
    _write_binding(WORKER, LEAD, "it_0000000a")
    # A second worker unit for the same slot, created after the bind.
    _noted_unit(WORKER_UNIT_2, WORKER)
    crash = _Crash()
    _emit_closer(1, 4.0, unit=WORKER_UNIT_2)
    _settle()
    crash.restart()
    assert work_relay.replay_on_start() == 0
    _settle()
    assert _charges(LEAD_UNIT) == []


class _DropSeqs:
    """Drop only the board copies for specific source seqs, so a crash can lose one
    charge's copy while a later, live charge's copy lands normally."""

    def __init__(self, seqs: "set[int]") -> None:
        self._seqs = seqs
        self._real = work_relay.hand_over
        work_relay.hand_over = self._filtered  # type: ignore[assignment]

    def _filtered(self, copies: "list") -> None:
        kept = []
        for copy in copies:
            closer = str(copy.payload.get("closer") or "")
            _, _, seq_text = closer.rpartition(":")
            if seq_text.isdigit() and int(seq_text) in self._seqs:
                continue  # crash: this copy never lands
            kept.append(copy)
        if kept:
            self._real(kept)

    def restart(self) -> None:
        work_relay.hand_over = self._real  # type: ignore[assignment]
        work_relay._RECENT.clear()


def test_a_live_copy_does_not_advance_the_cursor_past_an_uncopied_charge(_live_crew_log):
    """F1: charge 1's copy is lost to a crash; a resumed worker's charge 2 copy lands
    live and advances the cursor only as pending, not past charge 1. Replay still
    re-sends charge 1, so the board gets both. Without the contiguous-frontier rule a
    live copy of a higher seq would move the cursor past charge 1 and replay would skip
    it forever."""
    _boards()
    _noted_unit(WORKER_UNIT, WORKER)
    _write_binding(WORKER, LEAD, "it_0000000a")
    drop = _DropSeqs({1})  # charge 1's copy is lost; charge 2's lands
    _emit_closer(1, 1.0)
    _settle()
    _emit_closer(2, 2.0)
    _settle()
    # Only charge 2 made it to the board so far; the cursor must not have skipped 1.
    assert _charges(LEAD_UNIT) == [2.0]
    drop.restart()
    assert work_relay.replay_on_start() >= 1
    _settle()
    # Replay re-sends charge 1 (lost) and may re-send charge 2 (a duplicate physical
    # copy, billed once by closer). Both closers are present: a live copy of a higher
    # seq leaves the replay frontier below charge 1, so replay still covers charge 1.
    closers = _closers(LEAD_UNIT)
    assert f"{WORKER_UNIT}:1" in closers
    assert f"{WORKER_UNIT}:2" in closers


def test_a_cursor_advance_does_not_clobber_a_concurrent_boundary(_live_crew_log):
    """F2: advance_cursor and note_boundary share one per-worker lock across load..store,
    so a cursor advance that began before a boundary landed cannot store its stale
    snapshot over that boundary. The race is driven deterministically: the advance pauses
    on another thread right after it loads the sidecar (patched _load), the boundary note
    then runs on the main thread, and because the lock serializes them the note blocks
    until the advance's store completes -- so the boundary is not lost. Without the
    shared per-worker lock both run unlocked and the advance's stale store erases the
    boundary."""
    import threading

    from kiro_crew.crew_log import work_relay_cursor
    from kiro_crew.testing.wait import wait_until

    _boards()
    _noted_unit(WORKER_UNIT, WORKER)
    # No binding yet: the sidecar is absent, the exact first-bind window of F2.
    real_load = work_relay_cursor._load
    loaded = threading.Event()
    release = threading.Event()
    note_entered = threading.Event()

    def _load_then_pause(worker_slot_key):
        state = real_load(worker_slot_key)
        if worker_slot_key == WORKER and not loaded.is_set():
            loaded.set()
            release.wait(default_timeout())  # hold the advance inside its locked section
        return state

    def _advance_on_another_thread():
        work_relay_cursor.advance_cursor(WORKER, WORKER_UNIT, LEAD, 5)

    def _bind_on_another_thread():
        note_entered.set()  # about to contend for the per-worker lock
        _write_binding(WORKER, LEAD, "it_0000000a")

    work_relay_cursor._load = _load_then_pause  # type: ignore[assignment]
    advancer = threading.Thread(target=_advance_on_another_thread)
    binder = threading.Thread(target=_bind_on_another_thread)
    try:
        advancer.start()
        assert loaded.wait(default_timeout()), "the advance never loaded the sidecar"
        # The advance now holds the per-worker lock (paused mid-section). Start the bind;
        # its note_boundary must block on that lock, not clobber. Wait until the bind
        # thread is contending for the worker lock (the lock is held by the advance).
        binder.start()
        assert note_entered.wait(default_timeout())
        # Wait until the bind has written its binding file -- it then enters note_boundary
        # and contends for the worker lock the advance holds. No fixed sleep.
        from kiro_crew import work_ledger

        wait_until(lambda: work_ledger.binding_path(WORKER).exists(), timeout=default_timeout())
        assert work_relay_cursor._locks[WORKER].locked()
        # While the advance holds the lock, the bind's note cannot have landed a boundary.
        assert work_relay_cursor._load(WORKER)["boundaries"] == []
        # Release the advance; the bind's note then runs and lands its boundary.
        release.set()
        advancer.join(default_timeout())
        binder.join(default_timeout())
    finally:
        work_relay_cursor._load = real_load  # type: ignore[assignment]
        release.set()
    # The boundary survives alongside the advance's cursor write.
    state = work_relay_cursor._load(WORKER)
    assert len(state["boundaries"]) == 1, state["boundaries"]
    assert state["boundaries"][0]["board"] == LEAD


def test_a_charge_in_the_bind_window_is_refused_and_one_after_it_replays(_live_crew_log):
    """Contract pin (passes on this commit; it locks the ruled F3 behaviour, it does not
    change it): a charge that commits between the binding save and the boundary's tail
    read falls at or below boundary_seq, so replay (which resumes above boundary_seq)
    never copies it -- a crash there loses that one copy, by design, no worse than before
    this change. A charge committed after the boundary replays as usual."""
    from kiro_crew.crew_log import work_relay_cursor

    _boards()
    _noted_unit(WORKER_UNIT, WORKER)
    # Commit charge 1 at the moment the boundary reads the tail, so it lands inside the
    # boundary (seq 1 <= boundary_seq) -- the concurrent-with-bind window.
    real = work_relay_cursor._newest_unit_and_tail
    fired = {"done": False}

    def _tail_with_window_commit(worker_slot_key):
        if worker_slot_key == WORKER and not fired["done"]:
            fired["done"] = True
            _emit_closer(1, 1.0)
            _settle()
        return real(worker_slot_key)

    crash = _Crash()
    work_relay_cursor._newest_unit_and_tail = _tail_with_window_commit  # type: ignore[assignment]
    try:
        _write_binding(WORKER, LEAD, "it_0000000a")
    finally:
        work_relay_cursor._newest_unit_and_tail = real  # type: ignore[assignment]
    assert fired["done"], "the in-window commit was not exercised"
    _emit_closer(2, 2.0)  # after the boundary -> replays
    _settle()
    crash.restart()
    work_relay.replay_on_start()
    _settle()
    closers = _closers(LEAD_UNIT)
    assert f"{WORKER_UNIT}:2" in closers, "the post-window charge must replay"
    assert f"{WORKER_UNIT}:1" not in closers, "the in-window charge must not replay"
