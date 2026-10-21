"""The takeover note and its retraction on the slot-close teardown.

The tombstone-gated note (a reopen that begins after the close notes nothing),
the per-watch ``TakeoverNote`` handle and its retraction, the basis-relative
accounting a retraction must not disturb, the dismissal a hand-over close keeps
owed until its taker publishes or retracts, and the delivery-rehydrate-versus-
explicit-resume distinction -- all staged on a frozen tombstone clock. Split out
of ``test_slot_close_recreation_race_takeover_ordering.py`` to respect the line
cap; the ordering arbitration itself (claims, bases, the drain's arms) stays
there and the shared fakes live in ``slot_close_recreation_race_helpers.py``.
"""

from __future__ import annotations

import asyncio

import pytest
from chat_test_helpers import _make_state
from slot_close_recreation_race_helpers import _no_nudge_service  # noqa: F401
from slot_close_recreation_race_helpers import (
    NAME,
    _arm_running_turn,
    _close_handed_over_to_a_parked_taker,
    _event_fired,
    _json,
    _make_stale,
    _reached,
    _Req,
)

from kiro_crew.dashboard import chat_handlers as handlers
from kiro_crew.testing.wait import async_wait_until


class _TombstoneClock:
    """A controllable wall clock shared by the close tombstone and the noters.

    ``note_slot_closed`` stamps the tombstone from ``channel_slots.time.time()``;
    a delivery rehydrate reads its start from ``chat_persistence.time.time()``;
    the resume endpoint reads its instant through the facade's ``time``. The
    "resume issued before the close" staging is an ORDER of those two reads, so
    the test assigns both instants explicitly instead of nudging a live-clock
    stamp and hoping the next read lands within the nudge. Only ``time()`` is
    frozen; ``monotonic()`` and the rest delegate to the real module, so lock
    deadlines and waits keep working.
    """

    def __init__(self, real) -> None:
        self._real = real
        self.now = real.time()

    def time(self) -> float:
        return self.now

    def __getattr__(self, name):
        return getattr(self._real, name)


def _freeze_tombstone_clock(monkeypatch) -> _TombstoneClock:
    """Bind the tombstone stamp and every noter's start to one controllable clock."""
    import time as real_time

    from kiro_crew.dashboard import channel_slots, chat_persistence

    clock = _TombstoneClock(real_time)
    for module in (channel_slots, chat_persistence, handlers):
        monkeypatch.setattr(module, "time", clock)
    return clock


@pytest.mark.asyncio
async def test_a_rehydrate_begun_after_the_close_does_not_make_it_yield(tmp_path, monkeypatch):
    """A delivery that begins after the user closed the tab is a reopen, not a takeover.

    The close's tombstone stands from the pop. A rehydrate that begins after it
    is reopening a closed tab: it must not mark the close's claim (the archive
    would lose to a reopen the user issued after closing) and the close must
    not yield its key to it. The close commits -- ``closed`` written, the
    session removed -- and the rehydrate, its read ordered after the close's
    write, finds the closed transcript and is refused. Nothing is resurrected
    and nobody is left owning the key.
    """
    import threading

    from kiro_crew.dashboard import chat_persistence
    from kiro_crew.dashboard.chat_persistence import rehydrate_slot_from_history_async
    from kiro_crew.dashboard.chat_utils import slot_transcript_key

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(NAME)
    for row in ("first question", "first answer", "second question", "second answer"):
        slot.append("user" if "question" in row else "assistant", row)
    slot.drain()
    assert await chat_persistence.save_slot_off_loop(state, slot, force=True)
    slot.messages = list(slot.messages)[:2]
    slot._pending_rewrite = True
    slot._dirty = True
    removed_keys: list[str] = []

    async def _remove(key) -> None:
        removed_keys.append(key)

    state.sessions.remove = _remove  # type: ignore[assignment]
    waiting = threading.Event()
    real_wait = chat_persistence._await_takeover_ordering_settled

    async def _observed_wait(owner, name):
        waiting.set()
        return await real_wait(owner, name)

    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(slot, entered, release)
    clock = _freeze_tombstone_clock(monkeypatch)
    # The close stamps its tombstone at ``t0``.
    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, close, seam="the shielded task-cancel wait")
    # The tab is popped and its tombstone stands; a delivery's rehydrate begins
    # at ``t0 + 1``, inside the teardown's awaits -- after the user's close.
    clock.now += 1.0
    chat_persistence._await_takeover_ordering_settled = _observed_wait
    try:
        rehydrate = asyncio.create_task(rehydrate_slot_from_history_async(state, NAME))
        assert await _event_fired(waiting, 5.0), "the rehydrate never reached its wait"
        release.set()
        resp = await close
        outcome = await rehydrate
    finally:
        chat_persistence._await_takeover_ordering_settled = real_wait

    assert resp.status == 200, resp.text
    assert outcome is None, "a rehydrate begun after the close rebuilt the closed tab"
    assert NAME not in state._slots
    assert removed_keys == [f"dashboard:{NAME}"], "a committed close removes the session"
    meta = state.conversation_log.get_metadata(slot_transcript_key(NAME))
    assert meta.get("closed") is True
    assert state._takeover_watches == {}


@pytest.mark.asyncio
async def test_a_noter_refused_after_noting_does_not_make_the_close_yield(tmp_path, monkeypatch):
    """A takeover that noted and then gave up must not win the arbitration.

    A resume the user issued before closing notes its takeover and waits on the
    teardown watch; the wait expires (the ordering never settles, or the request
    is cancelled) and the resume is refused, publishing nothing. The teardown
    watch moved all the same. The close must not yield the key to a taker that
    gave up: the refused noter retracts its note, the moved watch does not read
    as a takeover, and the archive commits the user's edit -- ``closed`` written,
    the session removed -- rather than handing the key to nobody with a success
    the close never committed.
    """
    import threading

    from kiro_crew.dashboard import chat_persistence
    from kiro_crew.dashboard.chat_utils import slot_transcript_key
    from kiro_crew.history import HistoryLockTimeout

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(NAME)
    for row in ("first question", "first answer", "second question", "second answer"):
        slot.append("user" if "question" in row else "assistant", row)
    slot.drain()
    assert await chat_persistence.save_slot_off_loop(state, slot, force=True)
    slot.messages = list(slot.messages)[:2]
    slot._pending_rewrite = True
    slot._dirty = True
    removed_keys: list[str] = []

    async def _remove(key) -> None:
        removed_keys.append(key)

    state.sessions.remove = _remove  # type: ignore[assignment]

    # The resume's ordering wait expires: the noter is refused after its note,
    # while the close is still parked at its cancel wait.
    waiting = threading.Event()

    async def _expired_wait(owner, name):
        waiting.set()
        return False

    monkeypatch.setattr(chat_persistence, "_await_takeover_ordering_settled", _expired_wait)

    clock = _freeze_tombstone_clock(monkeypatch)
    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(slot, entered, release)
    # The resume predates the close, as in the headline race: the close stamps
    # its tombstone at ``t0 + 1`` and the resume reads its start at ``t0``.
    clock.now += 1.0
    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, close, seam="the shielded task-cancel wait")
    clock.now -= 1.0
    rehydrate = asyncio.create_task(chat_persistence.rehydrate_slot_from_history_async(state, NAME))
    assert await _event_fired(waiting, 5.0), "the resume never reached its wait"
    with pytest.raises(HistoryLockTimeout):
        await rehydrate
    assert NAME not in state._slots
    # The note is retracted; the close's archive now runs against a watch that
    # moved and moved back.
    release.set()
    resp = await close
    assert resp.status == 200, resp.text
    assert removed_keys == [f"dashboard:{NAME}"], "the close must commit, not hand over"
    assert NAME not in state._slots
    on_disk = state.conversation_log.read_messages_chained(slot_transcript_key(NAME))
    assert [m["content"] for m in on_disk] == ["first question", "first answer"]
    assert state.conversation_log.get_metadata(slot_transcript_key(NAME)).get("closed") is True
    assert state._takeover_watches == {}


def test_a_retraction_before_the_basis_opened_does_not_hide_a_later_takeover(tmp_path) -> None:
    """Takeover accounting is per note and basis-relative, not a net counter.

    An overlapping save keeps the key's watch alive. A resume notes, is refused,
    and retracts; the watch has seen one note and one retraction. A teardown
    then opens its basis on that watch and a REAL takeover notes after it. Both
    consumers -- ``basis_moved`` (the hand-over decision) and the claim
    registration (the commit gate) -- must see that takeover: a net count that
    the earlier retraction already lowered would read it as nothing moved, and
    the teardown's pending truncation would commit under the taker's read.
    """
    from kiro_crew.dashboard.slot_registry import SlotRegistry

    state = _make_state(tmp_path)
    overlapping = SlotRegistry.open_takeover_basis(state, NAME)
    teardown = None
    claim = None
    try:
        note = SlotRegistry.open_same_key_takeover_note(state, NAME, transcript=f"dashboard:{NAME}")
        assert note is not None
        SlotRegistry.retract_same_key_takeover(state, NAME, note)
        assert SlotRegistry.basis_moved(state, overlapping) is False

        teardown = SlotRegistry.open_takeover_basis(state, NAME)
        assert SlotRegistry.basis_moved(state, teardown) is False
        assert SlotRegistry.note_same_key_takeover(state, NAME, transcript=f"dashboard:{NAME}")
        assert SlotRegistry.basis_moved(state, teardown) is True, "the takeover was hidden"
        claim = SlotRegistry.begin_truncation_claim(state, NAME, basis=teardown)
        assert claim is not None and claim.outcome == "overtaken"
    finally:
        if claim is not None:
            SlotRegistry.retire_truncation_claim(state, NAME, claim)
        SlotRegistry.close_takeover_basis(state, teardown)
        SlotRegistry.close_takeover_basis(state, overlapping)
    assert state._takeover_watches == {}


def test_a_pre_basis_note_retracted_after_the_basis_does_not_hide_a_later_takeover(
    tmp_path,
) -> None:
    """The retraction of a note older than the basis must not count against newer notes.

    A resume notes BEFORE the teardown opens its basis and is refused AFTER it;
    a real takeover notes in between. Retracting the older note removes only
    that note: the takeover that postdates the basis still stands, for both
    consumers.
    """
    from kiro_crew.dashboard.slot_registry import SlotRegistry

    state = _make_state(tmp_path)
    overlapping = SlotRegistry.open_takeover_basis(state, NAME)
    teardown = None
    claim = None
    try:
        older = SlotRegistry.open_same_key_takeover_note(
            state, NAME, transcript=f"dashboard:{NAME}"
        )
        assert older is not None
        teardown = SlotRegistry.open_takeover_basis(state, NAME)
        assert SlotRegistry.basis_moved(state, teardown) is False
        assert SlotRegistry.note_same_key_takeover(state, NAME, transcript=f"dashboard:{NAME}")
        SlotRegistry.retract_same_key_takeover(state, NAME, older)
        assert SlotRegistry.basis_moved(state, teardown) is True, "the takeover was hidden"
        claim = SlotRegistry.begin_truncation_claim(state, NAME, basis=teardown)
        assert claim is not None and claim.outcome == "overtaken"
        # The older note's retraction is what the OVERLAPPING basis sees: its
        # own takeover evidence is the newer note, which still stands.
        assert SlotRegistry.basis_moved(state, overlapping) is True
    finally:
        if claim is not None:
            SlotRegistry.retire_truncation_claim(state, NAME, claim)
        SlotRegistry.close_takeover_basis(state, teardown)
        SlotRegistry.close_takeover_basis(state, overlapping)
    assert state._takeover_watches == {}


def test_a_stale_retraction_across_watch_recreation_does_not_withdraw_a_newer_note(
    tmp_path,
) -> None:
    """A note handle is bound to the watch that issued it, not to a sequence number.

    Reopen A notes on the key's first watch; that watch retires with its last
    holder. A fresh save opens a second watch, and reopen B notes on it. A is
    then refused and retracts: its handle names the retired watch, so the
    retraction withdraws nothing from the live one -- B's note still stands,
    and a basis opened before B sees the takeover in both consumers. A
    retraction keyed on the bare sequence would remove B's note (both are the
    first note of their watch) and let the pending truncation commit under
    B's read.
    """
    from kiro_crew.dashboard.slot_registry import SlotRegistry

    state = _make_state(tmp_path)
    first_holder = SlotRegistry.open_takeover_basis(state, NAME)
    note_a = SlotRegistry.open_same_key_takeover_note(state, NAME, transcript=f"dashboard:{NAME}")
    assert note_a is not None
    SlotRegistry.close_takeover_basis(state, first_holder)
    assert state._takeover_watches == {}, "the first watch must retire with its last holder"

    second_holder = SlotRegistry.open_takeover_basis(state, NAME)
    claim = None
    try:
        note_b = SlotRegistry.open_same_key_takeover_note(
            state, NAME, transcript=f"dashboard:{NAME}"
        )
        assert note_b is not None
        assert SlotRegistry.basis_moved(state, second_holder) is True
        # A is refused now and withdraws ITS note -- on a watch that is gone.
        SlotRegistry.retract_same_key_takeover(state, NAME, note_a)
        assert SlotRegistry.basis_moved(state, second_holder) is True, "B's note was withdrawn"
        claim = SlotRegistry.begin_truncation_claim(state, NAME, basis=second_holder)
        assert claim is not None and claim.outcome == "overtaken"
        # B's own retraction is the one that withdraws B's note.
        SlotRegistry.retract_same_key_takeover(state, NAME, note_b)
        assert SlotRegistry.basis_moved(state, second_holder) is False
    finally:
        if claim is not None:
            SlotRegistry.retire_truncation_claim(state, NAME, claim)
        SlotRegistry.close_takeover_basis(state, second_holder)
    assert state._takeover_watches == {}


def test_a_handed_over_dismissal_settles_on_publish_or_on_the_last_retraction(tmp_path) -> None:
    """The registry rule behind the owed dismissal.

    A close hands its basis to the dismissal while a taker stands: the basis
    stays open (non-truncating, so readers are not held), and the dismissal
    settles when the taker's publication becomes final at its success exit
    (nothing to write) or when the last standing taker retracts (the record
    comes back to be written). With nobody standing the record comes back at
    once.
    """
    from kiro_crew.dashboard.slot_registry import SlotRegistry

    state = _make_state(tmp_path)
    # Deferred: a taker stands.
    basis = SlotRegistry.open_takeover_basis(state, NAME)
    note = SlotRegistry.open_same_key_takeover_note(state, NAME, transcript=f"dashboard:{NAME}")
    assert note is not None and SlotRegistry.basis_moved(state, basis) is True
    assert (
        SlotRegistry.hand_basis_to_dismissal(
            state, basis, transcript=f"dashboard:{NAME}", closed_at=1.0
        )
        is None
    )
    assert basis.deferred is True and basis.truncating is False
    assert SlotRegistry.takeover_ordering_pending(state, NAME) is False, "readers are not held"
    SlotRegistry.close_takeover_basis(state, basis)  # the close's own finally: a no-op
    assert NAME in state._takeover_watches, "the watch outlives the close while a taker stands"
    # A second taker notes and retracts: the first still stands, nothing is owed yet.
    second = SlotRegistry.open_same_key_takeover_note(state, NAME, transcript=f"dashboard:{NAME}")
    assert SlotRegistry.retract_same_key_takeover(state, NAME, second) is None
    # The last taker retracts: the dismissal comes back to be written, the watch retires.
    owed = SlotRegistry.retract_same_key_takeover(state, NAME, note)
    assert owed is not None and owed.closed_at == 1.0 and owed.transcript == f"dashboard:{NAME}"
    # Returned for writing but still OWED: the watch holds it until the write is
    # confirmed, so a failed or overtaken write is retried rather than lost.
    assert state._takeover_watches[NAME].dismissal is owed
    assert SlotRegistry.owed_dismissal(state, NAME) is owed
    assert SlotRegistry.dismissal_still_owed(state, owed) is True
    SlotRegistry.settle_written(state, owed)
    assert state._takeover_watches == {}
    SlotRegistry.settle_written(state, owed)  # idempotent after the watch is gone

    # Settled by the noter's publication instead: the insert itself defers
    # (the noter can still refuse and pop), the noter's success exit settles --
    # nothing to write, watch retired.
    basis = SlotRegistry.open_takeover_basis(state, NAME)
    note = SlotRegistry.open_same_key_takeover_note(state, NAME, transcript=f"dashboard:{NAME}")
    assert SlotRegistry.hand_basis_to_dismissal(state, basis, transcript="t", closed_at=2.0) is None
    SlotRegistry.put_slot(state, NAME, state.get_or_create_slot("other"))
    assert state._takeover_watches[NAME].dismissal is not None, "the insert is not final"
    SlotRegistry.settle_dismissal_on_publish(state, NAME)
    assert state._takeover_watches == {}
    assert SlotRegistry.retract_same_key_takeover(state, NAME, note) is None
    state._slots.pop(NAME, None)

    # Nobody standing: the record comes back at once for the caller to write --
    # REGISTERED on the watch all the same, the holder kept, so the write-time
    # guard admits it and only the confirmed write releases it.
    basis = SlotRegistry.open_takeover_basis(state, NAME)
    owed = SlotRegistry.hand_basis_to_dismissal(state, basis, transcript="t", closed_at=3.0)
    assert owed is not None and owed.closed_at == 3.0
    assert basis.deferred is True and basis.truncating is False
    assert state._takeover_watches[NAME].dismissal is owed
    assert SlotRegistry.dismissal_still_owed(state, owed) is True
    SlotRegistry.close_takeover_basis(state, basis)  # the close's own finally: a no-op
    assert NAME in state._takeover_watches, "an unwritten immediate dismissal stays owed"
    SlotRegistry.settle_written(state, owed)
    assert state._takeover_watches == {}
    SlotRegistry.settle_written(state, owed)  # idempotent after the immediate path
    # Only publication notes standing (a noter that published, then popped):
    # the same immediate path, the same registration.
    basis = SlotRegistry.open_takeover_basis(state, NAME)
    SlotRegistry.put_slot(state, NAME, state.get_or_create_slot("gone-again"))
    state._slots.pop(NAME)
    owed = SlotRegistry.hand_basis_to_dismissal(state, basis, transcript="t", closed_at=3.5)
    assert owed is not None and state._takeover_watches[NAME].dismissal is owed
    assert SlotRegistry.dismissal_still_owed(state, owed) is True
    # ...and a late publication racing the queued write is still refused by the guard.
    late = state.get_or_create_slot(NAME)
    assert SlotRegistry.dismissal_still_owed(state, owed) is False
    SlotRegistry.settle_dismissal_on_publish(state, NAME)
    assert state._takeover_watches == {}
    assert state._slots.pop(NAME) is late

    # A publication landing while the write is queued: the write-time guard
    # answers False and the publication's settlement stands.
    basis = SlotRegistry.open_takeover_basis(state, NAME)
    note = SlotRegistry.open_same_key_takeover_note(state, NAME, transcript=f"dashboard:{NAME}")
    assert SlotRegistry.hand_basis_to_dismissal(state, basis, transcript="t", closed_at=4.0) is None
    owed = SlotRegistry.retract_same_key_takeover(state, NAME, note)
    assert owed is not None
    newcomer = state.get_or_create_slot(NAME)
    assert SlotRegistry.dismissal_still_owed(state, owed) is False, "a live key must not be stamped"
    SlotRegistry.settle_dismissal_on_publish(state, NAME)
    assert state._takeover_watches == {}
    assert newcomer is state._slots.pop(NAME)


def test_a_retracted_note_no_longer_moves_the_basis(tmp_path) -> None:
    """The registry rule behind the two races above."""
    from kiro_crew.dashboard.slot_registry import (
        NO_TAKEOVER_NOTE,
        SlotRegistry,
        TakeoverNote,
    )

    state = _make_state(tmp_path)
    basis = SlotRegistry.open_takeover_basis(state, NAME)
    try:
        assert SlotRegistry.basis_moved(state, basis) is False
        first = SlotRegistry.open_same_key_takeover_note(
            state, NAME, transcript=f"dashboard:{NAME}"
        )
        assert first is not None
        assert SlotRegistry.basis_moved(state, basis) is True
        # The noter is refused after noting and withdraws its note.
        SlotRegistry.retract_same_key_takeover(state, NAME, first)
        assert SlotRegistry.basis_moved(state, basis) is False
        # Two notes, one retraction: the other taker still stands.
        second = SlotRegistry.open_same_key_takeover_note(
            state, NAME, transcript=f"dashboard:{NAME}"
        )
        third = SlotRegistry.open_same_key_takeover_note(
            state, NAME, transcript=f"dashboard:{NAME}"
        )
        assert second is not None and third is not None
        SlotRegistry.retract_same_key_takeover(state, NAME, second)
        assert SlotRegistry.basis_moved(state, basis) is True
        # Retracting a note twice, one that never stood, the no-watch handle,
        # or a handle minted by another watch withdraws nothing else.
        live_watch = state._takeover_watches[NAME]
        for stale in (
            first,
            second,
            NO_TAKEOVER_NOTE,
            TakeoverNote(live_watch.id, 999),
            TakeoverNote(live_watch.id + 1000, third.seq),
        ):
            SlotRegistry.retract_same_key_takeover(state, NAME, stale)
        assert SlotRegistry.basis_moved(state, basis) is True
        SlotRegistry.retract_same_key_takeover(state, NAME, third)
        assert state._takeover_watches[NAME].standing == {}
        assert SlotRegistry.basis_moved(state, basis) is False
    finally:
        SlotRegistry.close_takeover_basis(state, basis)
    assert state._takeover_watches == {}


@pytest.mark.asyncio
async def test_a_resume_that_waited_out_a_committing_close_does_not_publish_over_its_marker(
    tmp_path, monkeypatch
):
    """The close metadata a resume acts on is the one it read AFTER its ordering wait.

    A resume that begins after the user's close is a reopen: it notes no takeover,
    and the close's archive commits ``closed=True`` -- possibly while the resume is
    parked in its ordering wait behind that very save. The reopen decisions
    (``defer_clear``, the reserved publish, the ``closed_at`` the restore puts
    back) must come from a read taken after the wait: derived from the pre-wait
    snapshot they read the tab as open, skip the reopen write, and publish a slot
    whose durable line still carries the close marker, so the tab disappears at
    the next restart. Either outcome below is consistent: the resume refuses (the
    close landed after it began, so its compare-and-clear declines) or it
    publishes with the marker cleared. Publishing over the marker is the bug.
    """
    import threading

    from kiro_crew.dashboard.chat_utils import slot_transcript_key

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(NAME)
    for row in ("first question", "first answer", "second question", "second answer"):
        slot.append("user" if "question" in row else "assistant", row)
    slot.drain()
    assert await handlers.save_slot_off_loop(state, slot, force=True)

    # The resume reaches its ordering wait and is held there until the test has
    # let the close commit its archive.
    waiting = threading.Event()
    proceed = threading.Event()
    real_wait = handlers._await_takeover_ordering_settled

    async def _held_wait(owner, name):
        waiting.set()
        # Held on the loop, never on a pool thread: the close this waits out
        # runs its archive on the default executor.
        await async_wait_until(proceed.is_set, timeout=5.0)
        return await real_wait(owner, name)

    monkeypatch.setattr(handlers, "_await_takeover_ordering_settled", _held_wait)

    clock = _freeze_tombstone_clock(monkeypatch)
    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(slot, entered, release)
    # The close stamps its tombstone at ``t0`` and parks at its cancel wait with
    # the key popped and its teardown watch open.
    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, close, seam="the shielded task-cancel wait")
    # The user's resume begins at ``t0 + 1``, after the close: a reopen, which
    # notes nothing and reads the still-open metadata, then waits on the ordering.
    clock.now += 1.0
    resume = asyncio.create_task(
        handlers.resume_slot_from_history(state, name=NAME, history_key=slot_transcript_key(NAME))
    )
    assert await _event_fired(waiting, 5.0), "the resume never reached its wait"
    assert not resume.done()
    # The close commits its archive while the resume waits: ``closed`` lands.
    release.set()
    resp = await close
    assert resp.status == 200, resp.text
    key = slot_transcript_key(NAME)
    assert state.conversation_log.get_metadata(key).get("closed") is True
    proceed.set()

    outcome = await resume
    on_disk = state.conversation_log.get_metadata(key)
    if outcome.refusal is None:
        assert outcome.slot is not None and state._slots.get(NAME) is outcome.slot
        assert "closed" not in on_disk, (
            "the resume published a tab whose durable line still says closed: it "
            "vanishes at the next restart"
        )
    else:
        assert outcome.refusal.code in {"resume_conflict", "reopen_failed"}, outcome.refusal
        assert NAME not in state._slots
        assert on_disk.get("closed") is True, "a refused reopen must leave the close marker"
    assert state._takeover_watches == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_shape", ["refused", "cancelled"])
async def test_a_taker_refused_after_registering_its_slot_still_leaves_the_tab_dismissed(
    tmp_path, monkeypatch, exit_shape
):
    """A publication is not final until the noter's success exit: a refusal after it still writes ``closed``.

    Both noters insert their slot before their last refusal point -- a rehydrate
    publishes through ``get_or_create_slot`` and then loads its window, popping the
    slot when that fails. Settling the owed dismissal at the insert would release
    the watch the later retraction needs, so a taker that registers and then
    refuses (or is cancelled) leaves the key empty and the transcript open: the
    same resurrected-tab outcome as a taker that never registered, on a narrower
    window. The dismissal stays owed through the insert while the note stands,
    the wrapper settles it only at its success exit, and the refusal flows
    through the retraction, which writes the marker.
    """
    import threading

    from kiro_crew.dashboard import chat_persistence
    from kiro_crew.dashboard.chat_utils import slot_transcript_key

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(NAME)
    for row in ("first question", "first answer", "second question", "second answer"):
        slot.append("user" if "question" in row else "assistant", row)
    slot.drain()
    assert await handlers.save_slot_off_loop(state, slot, force=True)
    key = slot_transcript_key(NAME)
    slot.messages = list(slot.messages)[:2]
    slot._pending_rewrite = True
    slot._dirty = True

    waiting = threading.Event()
    real_wait = chat_persistence._await_takeover_ordering_settled

    async def _observed_wait(owner, name):
        waiting.set()
        return await real_wait(owner, name)

    monkeypatch.setattr(chat_persistence, "_await_takeover_ordering_settled", _observed_wait)
    # The taker's window load -- AFTER ``get_or_create_slot`` published its slot --
    # is where it gives up.
    registered = threading.Event()

    def _failing_load(st, published, *args, **kwargs):
        registered.set()
        assert st._slots.get(NAME) is published, "the taker had not registered its slot yet"
        if exit_shape == "refused":
            raise OSError("the window could not be loaded")
        raise asyncio.CancelledError()

    monkeypatch.setattr(chat_persistence, "_load_rehydrated_window", _failing_load)

    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(slot, entered, release)
    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, close, seam="the shielded task-cancel wait")
    rehydrate = asyncio.create_task(
        chat_persistence.rehydrate_slot_from_history_async(state, NAME, adopt_closed=True)
    )
    assert await _event_fired(waiting, 10.0), "the rehydrate never reached its wait"
    release.set()
    resp = await close
    assert resp.status == 200, resp.text
    assert "closed" not in state.conversation_log.get_metadata(key)
    # The taker registers its slot, then gives up.
    if exit_shape == "refused":
        with pytest.raises(OSError):
            await rehydrate
    else:
        with pytest.raises(asyncio.CancelledError):
            await rehydrate
    assert registered.is_set()
    assert NAME not in state._slots, "a refused taker must not leave its slot on the key"
    assert state.conversation_log.get_metadata(key).get("closed") is True, (
        "the taker registered and then gave up after the close answered 200, and nothing "
        "recorded the dismissal: the restore brings the tab back"
    )
    assert state._takeover_watches == {}


async def _close_whose_taker_gives_up_during_its_drain(state, monkeypatch, *, close):
    """Stage: the taker retracts WHILE the close's hand-over drain runs, so the arm finds nobody standing.

    The archive lost to an ``adopt_closed`` rehydrate's note; the drain is parked
    inside its append, the rehydrate -- still in its ordering wait behind this
    teardown -- is cancelled, and its retraction runs before the arm reaches its
    dismissal hand-off. With nobody standing the arm takes the immediate path and
    writes the marker itself. Returns ``(key, close_task, drain_release)``: the
    drain is still parked when this returns.
    """
    import threading

    from kiro_crew.dashboard import chat_persistence
    from kiro_crew.dashboard.chat_utils import slot_transcript_key

    slot = state.get_or_create_slot(NAME)
    for row in ("first question", "first answer", "second question", "second answer"):
        slot.append("user" if "question" in row else "assistant", row)
    slot.drain()
    assert await handlers.save_slot_off_loop(state, slot, force=True)
    key = slot_transcript_key(NAME)
    slot.messages = list(slot.messages)[:2]
    slot._pending_rewrite = True
    slot._dirty = True
    read_parked = threading.Event()
    read_release = threading.Event()
    real_prefetch = chat_persistence._prefetch_rehydrate_inputs

    def _parked_prefetch(*args, **kwargs):
        read_parked.set()
        if not read_release.wait(10.0):
            raise AssertionError("the parked rehydrate read was never released by the test")
        return real_prefetch(*args, **kwargs)

    monkeypatch.setattr(chat_persistence, "_prefetch_rehydrate_inputs", _parked_prefetch)
    waiting = threading.Event()
    real_wait = chat_persistence._await_takeover_ordering_settled

    async def _observed_wait(owner, name):
        waiting.set()
        return await real_wait(owner, name)

    monkeypatch.setattr(chat_persistence, "_await_takeover_ordering_settled", _observed_wait)
    # The drain parks inside its append-safe arm, after the archive's refusal.
    drain_parked = threading.Event()
    drain_release = threading.Event()
    real_append = handlers.append_window_rows

    def _parked_append(*args, **kwargs):
        drain_parked.set()
        assert drain_release.wait(10.0), "the parked drain was never released by the test"
        return real_append(*args, **kwargs)

    monkeypatch.setattr(handlers, "append_window_rows", _parked_append)
    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(slot, entered, release)
    if close == "cleanup":
        _make_stale(state)
        closing = asyncio.create_task(handlers.api_chat_slots_cleanup(_Req(state, NAME)))
    else:
        closing = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, closing, seam="the shielded task-cancel wait")
    rehydrate = asyncio.create_task(
        chat_persistence.rehydrate_slot_from_history_async(state, NAME, adopt_closed=True)
    )
    assert await _event_fired(waiting, 10.0), "the rehydrate never reached its wait"
    release.set()
    assert await _event_fired(drain_parked, 10.0), "the close never reached its drain"
    # The taker gives up while the drain runs -- still parked in its ordering
    # wait behind this very teardown -- so its retraction lands before the arm's
    # dismissal hand-off and the arm finds nobody standing.
    rehydrate.cancel()
    read_release.set()
    with pytest.raises(asyncio.CancelledError):
        await rehydrate
    assert not read_parked.is_set(), "the taker must give up before it ever reads"
    assert not closing.done()
    assert "closed" not in state.conversation_log.get_metadata(key)
    return key, closing, drain_release


@pytest.mark.asyncio
@pytest.mark.parametrize("close", ["single", "cleanup"])
async def test_a_taker_that_gives_up_during_the_drain_leaves_the_close_to_write_its_own_marker(
    tmp_path, monkeypatch, close
):
    """With nobody standing when the arm runs, the close writes the marker itself -- and it lands.

    The immediate path shares the deferred path's protocol: the record the arm
    writes is registered on the watch first, so its own write-time guard admits
    it. A record handed back unregistered is refused by that guard, the close
    answers 200 with no marker, and the restore brings the tab back.
    """
    state = _make_state(tmp_path)
    key, closing, drain_release = await _close_whose_taker_gives_up_during_its_drain(
        state, monkeypatch, close=close
    )
    drain_release.set()
    resp = await closing
    assert resp.status == 200, resp.text
    assert state.conversation_log.get_metadata(key).get("closed") is True, (
        "nobody stood when the close ran its dismissal, it wrote the marker itself, and the "
        "marker is not on disk: the close's own guard refused an unregistered record"
    )
    assert NAME not in state._slots
    assert state._takeover_watches == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("close", ["single", "cleanup"])
async def test_a_plain_create_between_the_hand_off_and_the_queued_write_is_a_committed_close(
    tmp_path, monkeypatch, close
):
    """A publication that settles the dismissal while the close's write is queued is not a failed close.

    Nobody stands when the arm hands off, so it writes the marker itself; before
    the write takes the lock a plain create of the same name publishes a new tab
    and ``put_slot`` settles the dismissal (no resume note stands). The write is
    rightly skipped -- the tab is live -- and that is a committed close: 200, no
    marker on the live transcript, the new tab untouched. Reading the skipped
    write as a failure reports ``history_save_failed`` (or files the key under
    ``failed``) for a close in which nothing is owed.
    """
    import threading

    from kiro_crew.dashboard.slot_persistence import takeover_ordering

    state = _make_state(tmp_path)
    key, closing, drain_release = await _close_whose_taker_gives_up_during_its_drain(
        state, monkeypatch, close=close
    )
    # Park the arm's own write before it takes the lock.
    write_parked = threading.Event()
    write_release = threading.Event()
    real_record = takeover_ordering.record_deferred_dismissal

    def _parked_record(st, dismissal):
        write_parked.set()
        assert write_release.wait(10.0), "the close's write was never released by the test"
        return real_record(st, dismissal)

    monkeypatch.setattr(handlers, "record_deferred_dismissal", _parked_record)
    drain_release.set()
    assert await _event_fired(write_parked, 10.0), "the close never reached its own write"
    # The user creates a tab of the same name while the write is queued.
    newcomer = state.get_or_create_slot(NAME)
    write_release.set()
    resp = await closing
    assert resp.status == 200, resp.text
    if close == "cleanup":
        body = _json(resp)
        assert NAME not in body.get("failed", []), body
    assert state._slots.get(NAME) is newcomer, "the new tab must stay live"
    assert "closed" not in state.conversation_log.get_metadata(
        key
    ), "the owed marker was stamped onto the transcript of a tab the user just created"
    assert state._takeover_watches == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("close", ["single", "cleanup"])
async def test_a_close_whose_own_marker_write_fails_does_not_report_success(
    tmp_path, monkeypatch, close
):
    """A marker the close writes itself and cannot land is a close that did not commit.

    Nobody stands to retry the owed write, so the close must not answer 200 and
    leave the restore to contradict it: the single-tab close reports
    ``history_save_failed``, the bulk cleanup files the key under ``failed``. The
    dismissal stays owed on its watch, so a later taker's exit on the key still
    writes it.
    """
    from kiro_crew.dashboard import chat_persistence

    state = _make_state(tmp_path)
    key, closing, drain_release = await _close_whose_taker_gives_up_during_its_drain(
        state, monkeypatch, close=close
    )
    log = state.conversation_log
    real_update = type(log).update_metadata_if
    attempts = {"n": 0}

    def _refusing_once(self, key, fields, *args, **kwargs):
        if "closed" not in fields:
            return real_update(self, key, fields, *args, **kwargs)
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise OSError("disk full")
        return real_update(self, key, fields, *args, **kwargs)

    monkeypatch.setattr(type(log), "update_metadata_if", _refusing_once)
    drain_release.set()
    resp = await closing
    assert attempts["n"] == 1
    if close == "single":
        assert resp.status == 500, "a close whose marker did not land must not report success"
        assert _json(resp)["code"] == "history_save_failed"
    else:
        body = _json(resp)
        assert NAME in body.get("failed", []), body
        assert NAME not in body.get("keys", []), body
    assert "closed" not in log.get_metadata(key)
    assert NAME in state._takeover_watches, "the unwritten dismissal must stay owed"
    # The tail the drain landed is on disk regardless.
    rows = [m["content"] for m in log.read_messages_chained(key)]
    assert rows[:2] == ["first question", "first answer"]

    # A later taker's exit on the key writes the owed marker.
    def _failing_load(*args, **kwargs):
        raise OSError("the window could not be loaded")

    monkeypatch.setattr(chat_persistence, "_load_rehydrated_window", _failing_load)
    with pytest.raises(OSError):
        await chat_persistence.rehydrate_slot_from_history_async(state, NAME, adopt_closed=True)
    assert attempts["n"] == 2
    assert log.get_metadata(key).get("closed") is True
    assert NAME not in state._slots
    assert state._takeover_watches == {}


@pytest.mark.asyncio
async def test_cleanup_keeps_its_dismissal_owed_when_the_taker_gives_up_later(
    tmp_path, monkeypatch
):
    """The bulk cleanup's takeover arm owes its marker exactly like the single-tab close.

    Cleanup sweeps an idle tab whose archive loses to a member reopen's note and
    answers with the key handed over; the reopen then gives up. The arm that
    retired its watch on return left the dismissal to nobody -- no slot, no
    ``closed`` -- and the swept tab came back at the next restart.
    """
    state = _make_state(tmp_path)
    key, rehydrate, read_release, _unpark = await _close_handed_over_to_a_parked_taker(
        state, monkeypatch, close="cleanup"
    )
    rehydrate.cancel()
    read_release.set()
    with pytest.raises(asyncio.CancelledError):
        await rehydrate
    assert state.conversation_log.get_metadata(key).get("closed") is True, (
        "cleanup swept the tab and answered, the taker gave up, and nothing recorded the "
        "dismissal: the restore brings the swept tab back"
    )
    assert NAME not in state._slots
    assert state._takeover_watches == {}


@pytest.mark.asyncio
async def test_an_owed_marker_is_not_written_over_a_reopen_that_published_meanwhile(
    tmp_path, monkeypatch
):
    """A publication landing while the owed write is queued keeps its tab open.

    The last taker retracts and its exit queues the marker write; before the
    write takes the transcript lock, the user reopens the conversation and the
    reopen publishes. Stamping the transcript closed then would hide the tab the
    user just opened at the next restart. The write-time guard sees the key held
    and skips; the publication's own settlement retires the watch.
    """
    import threading

    from kiro_crew.dashboard.slot_persistence import takeover_ordering

    state = _make_state(tmp_path)
    key, rehydrate, read_release, _unpark = await _close_handed_over_to_a_parked_taker(
        state, monkeypatch, close="single"
    )
    # Park the owed write before it takes the lock, so a reopen can land first.
    write_parked = threading.Event()
    write_release = threading.Event()
    real_record = takeover_ordering.record_deferred_dismissal

    def _parked_record(st, dismissal):
        write_parked.set()
        assert write_release.wait(10.0), "the owed write was never released by the test"
        return real_record(st, dismissal)

    monkeypatch.setattr(takeover_ordering, "record_deferred_dismissal", _parked_record)
    rehydrate.cancel()
    read_release.set()
    exit_task = asyncio.ensure_future(rehydrate)
    assert await _event_fired(write_parked, 10.0), "the taker's exit never queued the write"
    # The user reopens while the write is queued: a non-noting publication.
    reopened = state.get_or_create_slot(NAME)
    write_release.set()
    with pytest.raises(asyncio.CancelledError):
        await exit_task
    assert state._slots.get(NAME) is reopened
    assert "closed" not in state.conversation_log.get_metadata(
        key
    ), "the owed marker was stamped onto the transcript of a tab the user just reopened"
    assert state._takeover_watches == {}


@pytest.mark.asyncio
async def test_an_owed_marker_whose_write_fails_stays_owed_and_lands_on_the_next_exit(
    tmp_path, monkeypatch
):
    """A failed marker write is retried by the next exit on the key, never lost.

    The last taker's exit tries the write and the store refuses; the dismissal
    stays owed on the watch (which therefore stays alive). A later reopen that
    notes the key and gives up finds it still owed and writes it.
    """
    from kiro_crew.dashboard import chat_persistence

    state = _make_state(tmp_path)
    key, rehydrate, read_release, unpark = await _close_handed_over_to_a_parked_taker(
        state, monkeypatch, close="single"
    )
    log = state.conversation_log
    real_update = type(log).update_metadata_if
    attempts = {"n": 0}

    def _refusing_once(self, *args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise OSError("disk full")
        return real_update(self, *args, **kwargs)

    monkeypatch.setattr(type(log), "update_metadata_if", _refusing_once)
    rehydrate.cancel()
    read_release.set()
    with pytest.raises(asyncio.CancelledError):
        await rehydrate
    assert attempts["n"] == 1
    assert "closed" not in log.get_metadata(key)
    assert NAME in state._takeover_watches, "a failed write must leave the dismissal owed"
    assert state._takeover_watches[NAME].dismissal is not None
    # The next taker on the key notes, gives up, and its exit retries the write.
    unpark()

    def _failing_load(*args, **kwargs):
        raise OSError("the window could not be loaded")

    monkeypatch.setattr(chat_persistence, "_load_rehydrated_window", _failing_load)
    with pytest.raises(OSError):
        await chat_persistence.rehydrate_slot_from_history_async(state, NAME, adopt_closed=True)
    assert attempts["n"] == 2
    assert log.get_metadata(key).get("closed") is True, "the retried write must land"
    assert NAME not in state._slots
    assert state._takeover_watches == {}


@pytest.mark.asyncio
async def test_an_owed_marker_yields_to_a_user_resume_that_read_the_header_before_it(
    tmp_path, monkeypatch
):
    """A deferred marker never commits between a resume's header snapshot and its publish.

    The deferred holder is non-truncating on purpose -- readers are never held
    behind a dismissal -- so a user's quick reopen after the close records no
    note and waits on nothing; it reads the header OPEN. If the owed marker then
    landed before the resume published, the resume's ``defer_clear`` would stay
    False, its publication would settle the dismissal, and nothing would remove
    the marker from the live tab's line: the tab vanishes at the next restart.
    The resume registers its read before the snapshot; the marker write yields
    to it, and the resume's publish settles the dismissal with nothing on disk.
    """
    import threading

    from kiro_crew.dashboard import chat_persistence
    from kiro_crew.dashboard.chat_utils import slot_transcript_key

    state = _make_state(tmp_path)
    key, rehydrate, read_release, _unpark = await _close_handed_over_to_a_parked_taker(
        state, monkeypatch, close="single"
    )
    # The user reopens now: the resume parks after its header snapshot, before
    # its publish (the off-loop agent-name restore).
    snapshot_taken = threading.Event()
    publish_release = threading.Event()
    real_restored_agent = handlers._restored_agent_name

    def _parked_restore(*args, **kwargs):
        snapshot_taken.set()
        assert publish_release.wait(10.0), "the resume was never released by the test"
        return real_restored_agent(*args, **kwargs)

    monkeypatch.setattr(handlers, "_restored_agent_name", _parked_restore)
    resume = asyncio.create_task(
        handlers.resume_slot_from_history(state, name=NAME, history_key=slot_transcript_key(NAME))
    )
    assert await _event_fired(snapshot_taken, 10.0), "the resume never read the header"
    assert "closed" not in state.conversation_log.get_metadata(key)
    # The taker backs out now: the dismissal comes due and its write is attempted
    # while the resume is in flight.
    rehydrate.cancel()
    read_release.set()
    with pytest.raises(asyncio.CancelledError):
        await rehydrate
    assert "closed" not in state.conversation_log.get_metadata(
        key
    ), "the owed marker committed between the resume's header snapshot and its publish"
    assert NAME in state._takeover_watches, "the dismissal stays owed while the resume is in flight"
    publish_release.set()
    outcome = await resume
    assert outcome.refusal is None, outcome
    assert state._slots.get(NAME) is outcome.slot
    assert "closed" not in state.conversation_log.get_metadata(
        key
    ), "the live tab's line carries the close marker: it vanishes at the next restart"
    assert state._takeover_watches == {}, "the resume's publish settles the dismissal"
    assert [m["content"] for m in outcome.slot.messages][:2] == ["first question", "first answer"]
    assert chat_persistence is not None


@pytest.mark.asyncio
async def test_an_owed_marker_lands_when_the_resume_it_yielded_to_refuses(tmp_path, monkeypatch):
    """A marker that yielded to an in-flight resume is written by that resume's refusal exit."""
    import threading

    from kiro_crew.dashboard.chat_utils import slot_transcript_key

    state = _make_state(tmp_path)
    key, rehydrate, read_release, _unpark = await _close_handed_over_to_a_parked_taker(
        state, monkeypatch, close="single"
    )
    snapshot_taken = threading.Event()
    publish_release = threading.Event()

    def _refusing_restore(*args, **kwargs):
        snapshot_taken.set()
        assert publish_release.wait(10.0), "the resume was never released by the test"
        raise OSError("the agent spec could not be read")

    monkeypatch.setattr(handlers, "_restored_agent_name", _refusing_restore)
    resume = asyncio.create_task(
        handlers.resume_slot_from_history(state, name=NAME, history_key=slot_transcript_key(NAME))
    )
    assert await _event_fired(snapshot_taken, 10.0), "the resume never read the header"
    rehydrate.cancel()
    read_release.set()
    with pytest.raises(asyncio.CancelledError):
        await rehydrate
    assert "closed" not in state.conversation_log.get_metadata(key), "the marker must yield"
    publish_release.set()
    with pytest.raises(OSError):
        await resume
    assert NAME not in state._slots
    assert (
        state.conversation_log.get_metadata(key).get("closed") is True
    ), "the resume the marker yielded to refused, and nothing wrote the dismissal it owed"
    assert state._takeover_watches == {}


@pytest.mark.asyncio
async def test_two_resumes_in_flight_and_a_cancelled_one_still_settle_the_owed_marker(
    tmp_path, monkeypatch
):
    """Two reads in flight: the marker yields to both; the second's exit decides.

    The first resume is cancelled while registered (its mark is cleared, the
    marker still yields to the second); the second refuses, and its exit writes
    the marker. A dismissal is never lost to a cancelled reader, and a reader
    that outlives a cancelled sibling still orders the write.
    """
    import threading

    from kiro_crew.dashboard.chat_utils import slot_transcript_key

    state = _make_state(tmp_path)
    key, rehydrate, read_release, _unpark = await _close_handed_over_to_a_parked_taker(
        state, monkeypatch, close="single"
    )
    snapshots = threading.Semaphore(0)
    publish_release = threading.Event()

    def _parked_restore(*args, **kwargs):
        snapshots.release()
        assert publish_release.wait(10.0), "the resumes were never released by the test"
        raise OSError("the agent spec could not be read")

    monkeypatch.setattr(handlers, "_restored_agent_name", _parked_restore)
    first = asyncio.create_task(
        handlers.resume_slot_from_history(state, name=NAME, history_key=slot_transcript_key(NAME))
    )
    second = asyncio.create_task(
        handlers.resume_slot_from_history(state, name=NAME, history_key=slot_transcript_key(NAME))
    )
    for _ in range(2):
        assert await _event_fired_sem(snapshots, 10.0), "a resume never read the header"
    rehydrate.cancel()
    read_release.set()
    with pytest.raises(asyncio.CancelledError):
        await rehydrate
    assert "closed" not in state.conversation_log.get_metadata(key)
    first.cancel()
    publish_release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    with pytest.raises(OSError):
        await second
    assert state.conversation_log.get_metadata(key).get("closed") is True
    assert NAME not in state._slots
    assert state._takeover_watches == {}


async def _event_fired_sem(sem, bound: float) -> bool:
    """Whether *sem* can be acquired within *bound*, polled from the loop."""
    try:
        await async_wait_until(lambda: sem.acquire(blocking=False), timeout=bound)
    except AssertionError:
        return False
    return True


@pytest.mark.asyncio
async def test_a_resume_admitted_between_the_markers_guard_and_its_rename_reopens_the_tab(
    tmp_path, monkeypatch
):
    """The owed marker's guard and the resume's header read are ordered by the transcript lock.

    The guard runs under the transcript lock; registering a read is a registry
    transition and a plain ``get_metadata`` is a cached file read, so a resume
    could register and snapshot an open header between the guard's "no read in
    flight" and the rename. The resume's close-metadata snapshot is therefore
    taken UNDER the same lock once the key carries a watch: a marker that
    landed before it is seen, the resume reopens against it (``defer_clear``)
    and clears it before publishing, and the live tab's line ends up open.
    """
    import threading

    from kiro_crew.dashboard.chat_utils import slot_transcript_key

    state = _make_state(tmp_path)
    key, rehydrate, read_release, _unpark = await _close_handed_over_to_a_parked_taker(
        state, monkeypatch, close="single"
    )
    log = state.conversation_log
    # Park the owed write AFTER its guard passed, still under the lock, right
    # before the rename.
    guard_passed = threading.Event()
    rename_release = threading.Event()
    real_update_locked = type(log)._update_metadata_locked

    def _parked_update(self, k, fields):
        if "closed" in fields:
            guard_passed.set()
            assert rename_release.wait(10.0), "the parked marker write was never released"
        return real_update_locked(self, k, fields)

    monkeypatch.setattr(type(log), "_update_metadata_locked", _parked_update)
    # The taker backs out: its exit queues the owed write, which passes its
    # guard (no read in flight yet) and parks before the rename.
    rehydrate.cancel()
    read_release.set()
    exit_task = asyncio.ensure_future(rehydrate)
    assert await _event_fired(guard_passed, 10.0), "the owed write never reached its rename"
    # The user resumes NOW, in the guard-to-rename window.
    resume = asyncio.create_task(
        handlers.resume_slot_from_history(state, name=NAME, history_key=slot_transcript_key(NAME))
    )
    await asyncio.sleep(0)
    assert not resume.done()
    rename_release.set()
    with pytest.raises(asyncio.CancelledError):
        await exit_task
    outcome = await resume
    assert outcome.refusal is None, outcome
    assert state._slots.get(NAME) is outcome.slot
    assert "closed" not in log.get_metadata(key), (
        "the marker landed between the resume's header snapshot and its publish and stayed "
        "on the live tab's line: it vanishes at the next restart"
    )
    assert state._takeover_watches == {}


@pytest.mark.asyncio
async def test_a_skipped_marker_write_whose_line_reads_open_stays_owed(tmp_path, monkeypatch):
    """Readability is not a commit: an open line after a skipped write keeps the dismissal owed.

    ``update_metadata_if`` answers False without writing when its own metadata
    read fails transiently; the line then reads back OPEN. Taking a readable
    line as "written" would clear the dismissal over a missing marker. The
    outcome is ``failed``, the dismissal stays owed, and the next exit writes it.
    """
    from kiro_crew.dashboard import chat_persistence
    from kiro_crew.dashboard.chat_utils import slot_transcript_key

    state = _make_state(tmp_path)
    key, rehydrate, read_release, unpark = await _close_handed_over_to_a_parked_taker(
        state, monkeypatch, close="single"
    )
    log = state.conversation_log
    real_update_if = type(log).update_metadata_if
    attempts = {"n": 0}

    def _skips_once(self, k, fields, *args, **kwargs):
        if "closed" in fields:
            attempts["n"] += 1
            if attempts["n"] == 1:
                # A transient read failure inside the guarded update: False,
                # nothing written, the line untouched.
                return False
        return real_update_if(self, k, fields, *args, **kwargs)

    monkeypatch.setattr(type(log), "update_metadata_if", _skips_once)
    rehydrate.cancel()
    read_release.set()
    with pytest.raises(asyncio.CancelledError):
        await rehydrate
    assert attempts["n"] == 1
    assert "closed" not in log.get_metadata(key)
    assert log.get_metadata_status(key)[1] is True, "the line reads back, open"
    assert NAME in state._takeover_watches, "a readable open line must not settle the dismissal"
    assert state._takeover_watches[NAME].dismissal is not None
    # The next exit on the key writes it.
    unpark()

    def _failing_load(*args, **kwargs):
        raise OSError("the window could not be loaded")

    monkeypatch.setattr(chat_persistence, "_load_rehydrated_window", _failing_load)
    with pytest.raises(OSError):
        await chat_persistence.rehydrate_slot_from_history_async(state, NAME, adopt_closed=True)
    assert attempts["n"] == 2
    assert log.get_metadata(slot_transcript_key(NAME)).get("closed") is True
    assert state._takeover_watches == {}


@pytest.mark.asyncio
async def test_a_publication_that_noted_nothing_settles_the_dismissal_at_once(tmp_path):
    """A non-noter's publication is final at the insert: the owed marker must not close its tab.

    A plain create on a key whose close handed it over to a taker that has since
    retracted (no note standing) is not a takeover and runs no wrapper exit; its
    ``put_slot`` is the only publication there is, so the dismissal settles there
    and the tab it publishes is never closed under. Settling twice is harmless.
    """
    from kiro_crew.dashboard.slot_registry import SlotRegistry

    state = _make_state(tmp_path)
    basis = SlotRegistry.open_takeover_basis(state, NAME)
    note = SlotRegistry.open_same_key_takeover_note(state, NAME, transcript=f"dashboard:{NAME}")
    assert note is not None
    assert SlotRegistry.hand_basis_to_dismissal(state, basis, transcript="t", closed_at=1.0) is None
    # A noter's publication while the note stands does NOT settle: its exit does.
    taker = state.get_or_create_slot(NAME)
    assert state._takeover_watches[NAME].dismissal is not None
    SlotRegistry.settle_dismissal_on_publish(state, NAME)
    assert state._takeover_watches == {}
    SlotRegistry.settle_dismissal_on_publish(state, NAME)  # idempotent
    assert SlotRegistry.retract_same_key_takeover(state, NAME, note) is None
    state._slots.pop(NAME)
    assert taker is not None

    # No note standing: a plain publication settles at the insert.
    basis = SlotRegistry.open_takeover_basis(state, NAME)
    assert (
        SlotRegistry.hand_basis_to_dismissal(state, basis, transcript="t", closed_at=2.0)
        is not None
    )
    SlotRegistry.close_takeover_basis(state, basis)
    basis = SlotRegistry.open_takeover_basis(state, NAME)
    second = SlotRegistry.open_same_key_takeover_note(state, NAME, transcript=f"dashboard:{NAME}")
    assert SlotRegistry.hand_basis_to_dismissal(state, basis, transcript="t", closed_at=3.0) is None
    owed = SlotRegistry.retract_same_key_takeover(state, NAME, second)
    assert owed is not None, "the last retraction returns the record to write"
    SlotRegistry.settle_written(state, owed)
    assert state._takeover_watches == {}


@pytest.mark.asyncio
async def test_a_taker_that_gives_up_after_the_close_returned_still_leaves_the_tab_dismissed(
    tmp_path, monkeypatch
):
    """A hand-over close owes its ``closed`` marker until the taker publishes or retracts.

    The archive is refused because an ``adopt_closed`` rehydrate (one that reads
    closed transcripts on purpose, so the tombstone gate does not stop it) noted
    the key; the close drains the tail, hands the key over and answers 200 with
    the marker unwritten. The rehydrate is then cancelled, after the close has
    returned. A close whose watch retired with its return would make that
    retraction a no-op: no slot on the key, no marker on the transcript, and the
    recent-session restore (which reads ``closed``, ``chat_persistence``'s
    ``_prefetch_recent_session``) brings back the tab the user dismissed. The
    dismissal is kept owed instead: the teardown's watch outlives the close,
    and the last taker's retraction records the marker.
    """
    import threading

    from kiro_crew.dashboard import chat_persistence
    from kiro_crew.dashboard.chat_utils import slot_transcript_key

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(NAME)
    for row in ("first question", "first answer", "second question", "second answer"):
        slot.append("user" if "question" in row else "assistant", row)
    slot.drain()
    assert await handlers.save_slot_off_loop(state, slot, force=True)
    key = slot_transcript_key(NAME)
    slot.messages = list(slot.messages)[:2]
    slot._pending_rewrite = True
    slot._dirty = True

    # The rehydrate's transcript read parks so it is still unpublished when the
    # close returns.
    read_parked = threading.Event()
    read_release = threading.Event()
    real_prefetch = chat_persistence._prefetch_rehydrate_inputs

    def _parked_prefetch(*args, **kwargs):
        read_parked.set()
        if not read_release.wait(10.0):
            raise AssertionError("the parked rehydrate read was never released by the test")
        return real_prefetch(*args, **kwargs)

    monkeypatch.setattr(chat_persistence, "_prefetch_rehydrate_inputs", _parked_prefetch)
    waiting = threading.Event()
    real_wait = chat_persistence._await_takeover_ordering_settled

    async def _observed_wait(owner, name):
        waiting.set()
        return await real_wait(owner, name)

    monkeypatch.setattr(chat_persistence, "_await_takeover_ordering_settled", _observed_wait)

    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(slot, entered, release)
    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, close, seam="the shielded task-cancel wait")
    # The key is popped and its tombstone stands; an app-owned worker's
    # rehydrate begins now and notes the key regardless of the tombstone.
    rehydrate = asyncio.create_task(
        chat_persistence.rehydrate_slot_from_history_async(state, NAME, adopt_closed=True)
    )
    assert await _event_fired(waiting, 10.0), "the rehydrate never reached its wait"
    release.set()
    resp = await close
    # The archive lost to the note; the close handed the key over and reported
    # the hand-over, with ``closed`` unwritten and the taker still reading.
    assert resp.status == 200, resp.text
    assert NAME not in state._slots
    assert "closed" not in state.conversation_log.get_metadata(key)
    assert await _event_fired(read_parked, 10.0), "the rehydrate never reached its read"
    # The taker gives up after the close has returned.
    rehydrate.cancel()
    read_release.set()
    with pytest.raises(asyncio.CancelledError):
        await rehydrate
    on_disk = state.conversation_log.get_metadata(key)
    assert on_disk.get("closed") is True, (
        "the user closed the tab and the close answered 200, but the taker that made it "
        "yield gave up and nothing recorded the dismissal: the restore brings the tab back"
    )
    assert NAME not in state._slots
    assert state._takeover_watches == {}, "the hand-over's watch must retire with the dismissal"
    # The drained tail is on disk under the dismissed transcript.
    rows = [m["content"] for m in state.conversation_log.read_messages_chained(key)]
    assert rows[:2] == ["first question", "first answer"]


@pytest.mark.asyncio
async def test_close_archive_yields_to_a_user_resume_begun_before_the_close(tmp_path, monkeypatch):
    """A resume the user issued BEFORE closing is the takeover the archive must lose to.

    The explicit resume endpoint deliberately reopens closed transcripts (it
    clears a ``closed`` flag the user's own close wrote while the resume was in
    flight), so a resume that began before the close and is still reading when
    the close runs its archive is a replacement whose publication is pending:
    its takeover note stands (it predates the tombstone), the archive's
    rewrite-shaped write is born overtaken and refuses, the hand-over drain
    lands the window append-safely, and the resume reads the transcript the
    truncation never touched -- the sequential history "reopen, then the
    rewrite is refused", the ordering a replacement is owed.
    """
    import threading

    from kiro_crew.dashboard import chat_persistence
    from kiro_crew.dashboard.chat_utils import slot_transcript_key

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(NAME)
    for row in ("first question", "first answer", "second question", "second answer"):
        slot.append("user" if "question" in row else "assistant", row)
    slot.drain()
    assert await chat_persistence.save_slot_off_loop(state, slot, force=True)
    slot.messages = list(slot.messages)[:2]
    slot._pending_rewrite = True
    slot._dirty = True

    # The resume's transcript read parks so its publication stays pending while
    # the close runs its archive.
    read_parked = threading.Event()
    read_release = threading.Event()
    log = state.conversation_log
    real_read = type(log).read_messages_chained

    def _parked_read(self, key):
        if key == slot_transcript_key(NAME):
            read_parked.set()
            if not read_release.wait(5.0):
                raise AssertionError("the parked resume read was never released by the test")
        return real_read(self, key)

    monkeypatch.setattr(type(log), "read_messages_chained", _parked_read)
    waiting = threading.Event()
    real_wait = handlers._await_takeover_ordering_settled

    async def _observed_wait(owner, name):
        waiting.set()
        return await real_wait(owner, name)

    monkeypatch.setattr(handlers, "_await_takeover_ordering_settled", _observed_wait)

    clock = _freeze_tombstone_clock(monkeypatch)
    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(slot, entered, release)
    # Stage the resume as one the user issued BEFORE closing, as two explicit
    # instants on one frozen clock: the close stamps its tombstone at ``t0 + 1``
    # and the resume reads its start at ``t0`` (two live reads can land in one
    # timestamp tick, ~15.6 ms on Windows, and a live stamp nudged ahead only
    # orders them while the next read lands inside the nudge;
    # testing-conventions.md § Determinism).
    clock.now += 1.0
    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, close, seam="the shielded task-cancel wait")
    clock.now -= 1.0
    resume = asyncio.create_task(
        handlers.resume_slot_from_history(state, name=NAME, history_key=slot_transcript_key(NAME))
    )
    # The note has fired and the resume is held in the ordering wait, its
    # publication pending: now the turn's cancel completes and the close runs
    # its archive against exactly that state.
    assert await _event_fired(waiting, 5.0), "the resume never reached its wait"
    assert not resume.done()
    release.set()

    resp = await close
    # The archive's truncation lost the arbitration, and the hand-over drain
    # then writes the popped slot's window APPEND-SAFELY (truncation disarmed,
    # on-disk rows carried through the foreign merge) -- so the close completes
    # as a hand-over rather than failing, with nothing truncated underneath
    # the resume.
    assert resp.status == 200, resp.text

    # The watch retired with the close, so the resume's read now proceeds to
    # the parked read; release it and read the transcript the refused archive
    # left untouched.
    read_release.set()
    outcome = await resume
    assert outcome.refusal is None, outcome
    assert read_parked.is_set(), "the resume never read the transcript"
    assert outcome.slot is not None and outcome.slot is not slot
    assert [m["content"] for m in outcome.slot.messages] == [
        "first question",
        "first answer",
        "second question",
        "second answer",
    ]
    on_disk = state.conversation_log.read_messages_chained(slot_transcript_key(NAME))
    assert len(on_disk) == 4, "the refused archive must leave the transcript untouched"
    assert state._slots.get(NAME) is outcome.slot
    # The publication settled the dismissal the hand-over left owed: the key is
    # live, so no ``closed`` marker may land under the reopened tab.
    assert "closed" not in state.conversation_log.get_metadata(slot_transcript_key(NAME))
    assert state._takeover_watches == {}
