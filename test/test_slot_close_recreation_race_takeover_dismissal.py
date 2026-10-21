"""The dismissal a hand-over close leaves owed, and the incarnation it belongs to.

A close whose archive lost to a takeover hands its key over with its ``closed``
marker unwritten, and the marker belongs to the CONVERSATION the user closed,
not to the name-derived key: a session can be permanently deleted and the same
key created again while the dismissal is owed. These tests pin the incarnation
witness the owed marker carries and what it does on a mismatch. Split out of
``test_slot_close_recreation_race_takeover_retraction.py`` to respect the line
cap; the stage helpers live there and the shared fakes in
``slot_close_recreation_race_helpers.py``.
"""

from __future__ import annotations

import asyncio

import pytest
from chat_test_helpers import _make_state
from slot_close_recreation_race_helpers import (
    NAME,
    _close_handed_over_to_a_parked_taker,
    _event_fired,
)

from kiro_crew.dashboard import chat_handlers as handlers
from kiro_crew.dashboard import chat_persistence
from kiro_crew.dashboard.chat_utils import slot_transcript_key
from kiro_crew.dashboard.slot_registry import SlotRegistry


async def _recreate_the_key_under_a_new_incarnation(state, key: str) -> str:
    """Delete the transcript permanently and create a fresh conversation under the same key.

    Returns the new line's ``created_at``, which differs from the old one.
    """
    log = state.conversation_log
    old = str(log.get_metadata(key).get("created_at") or "")
    assert old, "the staged close must have observed an identified transcript"
    assert log.delete_session(key)
    log.append(key, "user", "a brand-new conversation under the same name")
    new = str(log.get_metadata(key).get("created_at") or "")
    assert new and new != old, (old, new)
    return new


@pytest.mark.asyncio
async def test_an_owed_marker_is_not_written_onto_a_new_incarnation_of_the_key(
    tmp_path, monkeypatch
):
    """The dismissal belongs to the conversation the user closed, not to its key.

    The close defers its dismissal; the transcript is permanently deleted and the
    same name-derived key is created again, a different conversation; the pending
    reopen is refused and its exit writes what the close left owed. Written by key
    alone, the OLD dismissal lands on the NEW conversation: a brand-new tab marked
    closed, with no recovery. The marker carries the closed transcript's
    ``created_at`` and is dropped, settled as satisfied, when the line it would
    stamp is another incarnation.
    """
    state = _make_state(tmp_path)
    key, rehydrate, read_release, _unpark = await _close_handed_over_to_a_parked_taker(
        state, monkeypatch, close="single"
    )
    owed = state._takeover_watches[NAME].dismissal
    assert owed is not None and owed.created_at, "the dismissal must carry its incarnation"
    new_created_at = await _recreate_the_key_under_a_new_incarnation(state, key)
    assert new_created_at != owed.created_at
    # The pending reopen is refused (the identity it read is gone) and retracts.
    rehydrate.cancel()
    read_release.set()
    with pytest.raises(asyncio.CancelledError):
        await rehydrate
    meta = state.conversation_log.get_metadata(key)
    assert "closed" not in meta, "the old dismissal was stamped onto a brand-new conversation"
    assert meta.get("created_at") == new_created_at
    assert state._takeover_watches == {}, "a dismissal with nothing left to dismiss is settled"
    rows = [m["content"] for m in state.conversation_log.read_messages_chained(key)]
    assert rows == ["a brand-new conversation under the same name"]


@pytest.mark.asyncio
async def test_an_owed_marker_lands_on_the_same_incarnation(tmp_path, monkeypatch):
    """The happy path keeps writing: same conversation, same ``created_at``, marker lands."""
    state = _make_state(tmp_path)
    key, rehydrate, read_release, _unpark = await _close_handed_over_to_a_parked_taker(
        state, monkeypatch, close="single"
    )
    owed = state._takeover_watches[NAME].dismissal
    before = str(state.conversation_log.get_metadata(key).get("created_at") or "")
    assert owed is not None and owed.created_at == before
    rehydrate.cancel()
    read_release.set()
    with pytest.raises(asyncio.CancelledError):
        await rehydrate
    meta = state.conversation_log.get_metadata(key)
    assert meta.get("closed") is True
    assert meta.get("created_at") == before
    assert state._takeover_watches == {}


@pytest.mark.asyncio
async def test_an_owed_marker_without_an_identity_is_never_written(tmp_path, monkeypatch):
    """A closed slot that recorded no transcript identity owes a marker nobody can place.

    Nothing can tell the conversation the user closed from one created since
    under the same key, so the marker is not written -- and not retried forever
    against a line it could never match: the dismissal settles as unwritable.
    """
    state = _make_state(tmp_path)
    key, rehydrate, read_release, _unpark = await _close_handed_over_to_a_parked_taker(
        state, monkeypatch, close="single"
    )
    owed = state._takeover_watches[NAME].dismissal
    assert owed is not None
    # A legacy observation: the slot saw its file but recorded no created_at.
    object.__setattr__(owed, "created_at", "")
    rehydrate.cancel()
    read_release.set()
    with pytest.raises(asyncio.CancelledError):
        await rehydrate
    assert "closed" not in state.conversation_log.get_metadata(key)
    assert state._takeover_watches == {}, "an unidentifiable dismissal settles rather than retries"


@pytest.mark.asyncio
async def test_a_retried_owed_marker_also_checks_the_incarnation(tmp_path, monkeypatch):
    """The retry path (an earlier write failed) drops a marker whose incarnation is gone."""
    state = _make_state(tmp_path)
    key, rehydrate, read_release, unpark = await _close_handed_over_to_a_parked_taker(
        state, monkeypatch, close="single"
    )
    log = state.conversation_log
    real_update = type(log).update_metadata_if
    attempts = {"n": 0}

    def _refusing_once(self, k, fields, *args, **kwargs):
        if "closed" in fields:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise OSError("disk full")
        return real_update(self, k, fields, *args, **kwargs)

    monkeypatch.setattr(type(log), "update_metadata_if", _refusing_once)
    rehydrate.cancel()
    read_release.set()
    with pytest.raises(asyncio.CancelledError):
        await rehydrate
    assert attempts["n"] == 1 and NAME in state._takeover_watches
    # Between the failed write and its retry the key is recreated.
    new_created_at = await _recreate_the_key_under_a_new_incarnation(state, key)
    unpark()

    def _failing_load(*args, **kwargs):
        raise OSError("the window could not be loaded")

    monkeypatch.setattr(chat_persistence, "_load_rehydrated_window", _failing_load)
    with pytest.raises(OSError):
        await chat_persistence.rehydrate_slot_from_history_async(state, NAME, adopt_closed=True)
    assert attempts["n"] == 2
    meta = log.get_metadata(slot_transcript_key(NAME))
    assert "closed" not in meta and meta.get("created_at") == new_created_at
    assert state._takeover_watches == {}


@pytest.mark.asyncio
async def test_a_resume_answered_by_another_keys_live_slot_does_not_settle_this_keys_dismissal(
    tmp_path, monkeypatch
):
    """A dedup to a slot under another key publishes nothing here; this key's dismissal stays owed.

    Key A has an owed dismissal (its close handed the key to a taker whose read
    is parked); B is live. ``POST /slots/A/resume {"key": "dashboard:B"}`` is
    answered by B itself (``_live_slot_for_resume`` dedups by session identity
    across slots) -- no refusal, but nothing published under A. Reading that as
    "A published" settled A's dismissal and released its holder, so A's taker
    later retracted into nothing and A came back at the next restart. The
    publication test is occupancy: the slot answered with must now hold THIS key.
    """
    state = _make_state(tmp_path)
    key_a, rehydrate, read_release, _unpark = await _close_handed_over_to_a_parked_taker(
        state, monkeypatch, close="single"
    )
    other = "chat-1-9999"
    b = state.get_or_create_slot(other)
    b.append("user", "B is live")
    b.drain()
    assert await chat_persistence.save_slot_off_loop(state, b, force=True)
    owed = state._takeover_watches[NAME].dismissal
    assert owed is not None
    # Resume A by B's session key: answered by B, live, no refusal.
    outcome = await handlers.resume_slot_from_history(
        state, name=NAME, history_key=slot_transcript_key(other)
    )
    assert outcome.refusal is None and outcome.slot is b and outcome.already_live
    assert NAME not in state._slots, "nothing was published under A"
    assert (
        state._takeover_watches[NAME].dismissal is owed
    ), "a dedup to another key's slot settled A's owed dismissal"
    # A's taker gives up: A's marker lands; B is untouched and open.
    rehydrate.cancel()
    read_release.set()
    with pytest.raises(asyncio.CancelledError):
        await rehydrate
    assert state.conversation_log.get_metadata(key_a).get("closed") is True
    assert "closed" not in state.conversation_log.get_metadata(slot_transcript_key(other))
    assert state._slots.get(other) is b
    assert state._takeover_watches == {}


@pytest.mark.asyncio
async def test_a_resume_that_dedups_to_this_keys_own_live_slot_counts_as_published(
    tmp_path, monkeypatch
):
    """Same key, already live: the occupant IS the slot answered with, so the dismissal settles."""
    state = _make_state(tmp_path)
    key, rehydrate, read_release, _unpark = await _close_handed_over_to_a_parked_taker(
        state, monkeypatch, close="single"
    )
    # A plain create takes the key while the dismissal is owed (a non-noting
    # publication while a resume note stands defers the settlement to a noter's
    # exit); the user then resumes A, which dedups to that live slot.
    live = state.get_or_create_slot(NAME)
    assert state._takeover_watches[NAME].dismissal is not None
    outcome = await handlers.resume_slot_from_history(
        state, name=NAME, history_key=slot_transcript_key(NAME)
    )
    assert outcome.refusal is None and outcome.slot is live and outcome.already_live
    watch = state._takeover_watches.get(NAME)
    assert watch is None or watch.dismissal is None, "A's own live slot is a publication"
    rehydrate.cancel()
    read_release.set()
    with pytest.raises(asyncio.CancelledError):
        await rehydrate
    assert "closed" not in state.conversation_log.get_metadata(key)
    assert state._slots.get(NAME) is live
    assert state._takeover_watches == {}


@pytest.mark.asyncio
async def test_a_plain_create_between_the_markers_guard_and_its_rename_is_unstamped(
    tmp_path, monkeypatch
):
    """A publication winning the key after the guard passed does not leave its line stamped closed.

    The guard decides under the transcript lock, but a same-name create takes no
    lock: ``put_slot`` can land between the guard's "still owed" and the rename,
    settle the dismissal, and the rename then stamps ``closed`` onto the line of
    a live tab -- hidden at the next restart. After the rename, still under the
    lock, the writer reads the ownership again and un-stamps a marker a
    publication has won against; the outcome is ``superseded`` and the tab stays
    open. The resume side has its own ordering (``begin_resume_read``); this is
    the publish side.
    """
    import threading

    from kiro_crew.dashboard.slot_persistence import takeover_ordering

    state = _make_state(tmp_path)
    key, rehydrate, read_release, _unpark = await _close_handed_over_to_a_parked_taker(
        state, monkeypatch, close="single"
    )
    log = state.conversation_log
    guard_passed = threading.Event()
    rename_release = threading.Event()
    real_update_locked = type(log)._update_metadata_locked

    def _parked_update(self, k, fields):
        if "closed" in fields and fields.get("closed") is True:
            guard_passed.set()
            assert rename_release.wait(10.0), "the parked marker write was never released"
        return real_update_locked(self, k, fields)

    monkeypatch.setattr(type(log), "_update_metadata_locked", _parked_update)
    outcomes: list = []
    real_record = takeover_ordering.record_deferred_dismissal

    def _recording(st, dismissal):
        outcome = real_record(st, dismissal)
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(takeover_ordering, "record_deferred_dismissal", _recording)
    rehydrate.cancel()
    read_release.set()
    exit_task = asyncio.ensure_future(rehydrate)
    assert await _event_fired(guard_passed, 10.0), "the owed write never passed its guard"
    # A plain same-name create lands in the guard-to-rename window: no note, no
    # read registration, ``put_slot`` settles the dismissal.
    newcomer = state.get_or_create_slot(NAME)
    rename_release.set()
    with pytest.raises(asyncio.CancelledError):
        await exit_task
    assert outcomes == ["superseded"], outcomes
    assert state._slots.get(NAME) is newcomer
    assert "closed" not in log.get_metadata(
        key
    ), "the owed marker was stamped onto the line of a tab a create had just published"
    assert state._takeover_watches == {}


@pytest.mark.asyncio
async def test_an_owed_marker_with_no_publication_in_the_window_is_written(tmp_path, monkeypatch):
    """The post-commit re-check leaves an unopposed write alone: ``written``, marker on disk."""
    from kiro_crew.dashboard.slot_persistence import takeover_ordering

    state = _make_state(tmp_path)
    key, rehydrate, read_release, _unpark = await _close_handed_over_to_a_parked_taker(
        state, monkeypatch, close="single"
    )
    outcomes: list = []
    real_record = takeover_ordering.record_deferred_dismissal

    def _recording(st, dismissal):
        outcome = real_record(st, dismissal)
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(takeover_ordering, "record_deferred_dismissal", _recording)
    rehydrate.cancel()
    read_release.set()
    with pytest.raises(asyncio.CancelledError):
        await rehydrate
    assert outcomes == ["written"], outcomes
    assert state.conversation_log.get_metadata(key).get("closed") is True
    assert state._takeover_watches == {}


async def _close_superseded_in_the_rename_window(state, monkeypatch):
    """Stage: the owed marker's write is superseded by a same-name create in its guard→rename window.

    Returns ``(key, finish, newcomer)``: ``finish`` releases the parked rename
    and awaits the taker's exit (which runs the write and then the un-stamp).
    """
    import threading

    key, rehydrate, read_release, _unpark = await _close_handed_over_to_a_parked_taker(
        state, monkeypatch, close="single"
    )
    log = state.conversation_log
    guard_passed = threading.Event()
    rename_release = threading.Event()
    real_update_locked = type(log)._update_metadata_locked

    def _parked_update(self, k, fields):
        if fields.get("closed") is True:
            guard_passed.set()
            assert rename_release.wait(10.0), "the parked marker write was never released"
        return real_update_locked(self, k, fields)

    monkeypatch.setattr(type(log), "_update_metadata_locked", _parked_update)
    rehydrate.cancel()
    read_release.set()
    exit_task = asyncio.ensure_future(rehydrate)
    assert await _event_fired(guard_passed, 10.0), "the owed write never passed its guard"
    newcomer = state.get_or_create_slot(NAME)

    async def finish() -> None:
        rename_release.set()
        with pytest.raises(asyncio.CancelledError):
            await exit_task

    return key, finish, newcomer


@pytest.mark.asyncio
async def test_an_unstamp_that_fails_once_stays_owed_and_the_next_exit_clears_it(
    tmp_path, monkeypatch
):
    """A superseded marker whose clear fails is retried by the next exit on the key, never left on.

    The dismissal is already settled on the registry when the stamp must come
    off, so a failed clear cannot ride the owed-dismissal retry; it is parked as
    an owed un-stamp on the key's watch, and the next exit on the key (here a
    refused rehydrate) clears it. Reporting the failure and moving on would
    leave the created tab's line stamped closed: hidden at the next restart.
    """
    from kiro_crew.dashboard.slot_persistence import takeover_ordering

    state = _make_state(tmp_path)
    outcomes: list = []
    real_record = takeover_ordering.record_deferred_dismissal

    def _recording(st, dismissal):
        outcome = real_record(st, dismissal)
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(takeover_ordering, "record_deferred_dismissal", _recording)
    log = state.conversation_log
    real_clear = type(log).clear_closed
    attempts = {"n": 0}

    # Every clear of OUR stamp fails while ``broken`` holds: the first attempt
    # (right after the superseded write) and the same exit's own immediate
    # retry both fail, so the clear must survive the exit as owed.
    broken = {"on": True}

    def _raises_while_broken(self, k, **kwargs):
        if "only_if_closed_before" in kwargs:
            attempts["n"] += 1
            if broken["on"]:
                raise OSError("disk full")
        return real_clear(self, k, **kwargs)

    monkeypatch.setattr(type(log), "clear_closed", _raises_while_broken)
    key, finish, newcomer = await _close_superseded_in_the_rename_window(state, monkeypatch)
    await finish()
    assert outcomes == ["superseded"], "a settled dismissal is never a failed close"
    assert attempts["n"] >= 1
    assert log.get_metadata(key).get("closed") is True, "the clear failed; stamp still on"
    assert SlotRegistry.owed_unstamp(state, NAME) is not None, "the clear must stay owed"
    assert state._slots.get(NAME) is newcomer
    failed_attempts = attempts["n"]
    # The disk recovers; the next exit on the key retries the owed clear.
    broken["on"] = False
    state._slots.pop(NAME)

    def _failing_load(*args, **kwargs):
        raise OSError("the window could not be loaded")

    monkeypatch.setattr(chat_persistence, "_load_rehydrated_window", _failing_load)
    with pytest.raises(OSError):
        await chat_persistence.rehydrate_slot_from_history_async(state, NAME, adopt_closed=True)
    assert attempts["n"] == failed_attempts + 1
    assert "closed" not in log.get_metadata(key), "the retried clear must take the stamp off"
    assert SlotRegistry.owed_unstamp(state, NAME) is None
    assert state._takeover_watches == {}


@pytest.mark.asyncio
async def test_an_unstamp_never_removes_a_newer_close_of_the_created_tab(tmp_path, monkeypatch):
    """The un-stamp is a compare-and-clear on OUR ``closed_at``; a later close stays."""
    from kiro_crew.dashboard.slot_persistence import takeover_ordering
    from kiro_crew.dashboard.slot_registry import OwedUnstamp

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(NAME)
    slot.append("user", "hello")
    slot.drain()
    assert await handlers.save_slot_off_loop(state, slot, force=True)
    key = slot_transcript_key(NAME)
    log = state.conversation_log
    ours = 1_700_000_000.0
    later = ours + 5.0
    # The created tab was legitimately closed AFTER our stamp would have landed.
    log.update_metadata(key, {"closed": True, "closed_at": later})
    assert takeover_ordering.unstamp_superseded_marker(state, OwedUnstamp(NAME, key, ours)) is True
    meta = log.get_metadata(key)
    assert meta.get("closed") is True and meta.get("closed_at") == later, "a newer close must stay"
    assert state._takeover_watches == {}
    # Our own stamp, by contrast, comes off -- and a second un-stamp is a no-op.
    log.update_metadata(key, {"closed": True, "closed_at": ours})
    assert takeover_ordering.unstamp_superseded_marker(state, OwedUnstamp(NAME, key, ours)) is True
    assert "closed" not in log.get_metadata(key)
    assert takeover_ordering.unstamp_superseded_marker(state, OwedUnstamp(NAME, key, ours)) is True
    assert state._takeover_watches == {}


def test_the_after_commit_hook_does_no_io() -> None:
    """``update_metadata_if`` documents its hook as I/O-free; the recheck only records a verdict."""
    import ast
    import inspect

    from kiro_crew.dashboard.slot_persistence import takeover_ordering

    source = inspect.getsource(takeover_ordering.record_deferred_dismissal)
    tree = ast.parse(source)
    hooks = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_recheck_after_commit"
    ]
    assert len(hooks) == 1
    calls = {ast.unparse(node.func) for node in ast.walk(hooks[0]) if isinstance(node, ast.Call)}
    assert calls == {"SlotRegistry.dismissal_still_owed"}, calls


def test_the_dismissal_record_carries_the_closed_incarnation(tmp_path) -> None:
    """``hand_basis_to_dismissal`` stores the witness its callers pass; the default is empty."""
    state = _make_state(tmp_path)
    basis = SlotRegistry.open_takeover_basis(state, NAME)
    owed = SlotRegistry.hand_basis_to_dismissal(
        state, basis, transcript="t", closed_at=1.0, created_at="2026-10-19T00:00:00+00:00"
    )
    assert owed is not None and owed.created_at == "2026-10-19T00:00:00+00:00"
    SlotRegistry.settle_written(state, owed)
    basis = SlotRegistry.open_takeover_basis(state, NAME)
    owed = SlotRegistry.hand_basis_to_dismissal(state, basis, transcript="t", closed_at=2.0)
    assert owed is not None and owed.created_at == ""
    SlotRegistry.settle_written(state, owed)
    assert state._takeover_watches == {}
