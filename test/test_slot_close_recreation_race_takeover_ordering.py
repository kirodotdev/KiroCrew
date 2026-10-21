"""Rewrite-vs-takeover ordering on the slot-close teardown.

The arbitration between a close's truncating archive and a same-key takeover: the
teardown watch and its basis, the tombstone-gated takeover note and its per-watch
retraction (``TakeoverNote``), the hand-over drain's append-safe arms, and the
base reproduction of the torn history, staged on a frozen tombstone clock. Split
out of ``test_slot_close_recreation_race.py`` to respect the line cap; the takeover
note and its retraction have their own part (``…_takeover_retraction.py``), the
close-vs-recreate guards themselves stay in the original file, and the shared
fakes live in ``slot_close_recreation_race_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest
from chat_test_helpers import _make_state
from slot_close_recreation_race_helpers import _no_nudge_service  # noqa: F401
from slot_close_recreation_race_helpers import (
    NAME,
    _arm_running_turn,
    _json,
    _make_stale,
    _reached,
    _Req,
)

from kiro_crew.dashboard import chat_handlers as handlers
from kiro_crew.testing.wait import async_wait_until


async def _event_fired(event: threading.Event, bound: float) -> bool:
    """Whether *event* fires within *bound*, polled from the loop.

    Parking ``event.wait`` on the default executor would hold a pool thread the
    code under test may itself need to make the event fire, so the wait would
    starve on a one-thread pool; polling ``is_set`` holds none.
    """
    try:
        await async_wait_until(event.is_set, timeout=bound, describe=lambda: "")
    except AssertionError:
        return event.is_set()
    return True


def _state_with_slot(tmp_path, name: str = NAME):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(name)
    slot.append("user", "watch the PR")
    slot.append("assistant", "watching")
    slot.drain()
    return state


async def _linked_slot_with_a_tail(state, linked: str, name: str = NAME):
    """A slot whose conversation lives on *linked*, with one row past its last save."""
    slot = state.get_or_create_slot(name, linked_session_key=linked)
    slot.append("user", "PERSISTED-1")
    slot.drain()
    assert await handlers.save_slot_off_loop(state, slot, best_effort=False)
    assert slot._disk_window_len == 1, "the seed save did not commit the first window"
    slot.append("assistant", "TAIL-2")
    slot.drain()
    return slot


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
async def test_close_archive_commits_over_a_delivery_that_begins_mid_teardown(
    tmp_path, monkeypatch
):
    """A delivery's rehydrate beginning inside the teardown is a reopen, not a takeover.

    The close pops the key and records its tombstone, then awaits (task cancel,
    executor hops) before its archive save runs. A rehydrate that begins in
    that gap begins AFTER the user's close: it notes no takeover (the tombstone
    gates the note), so the archive's rewrite-shaped write (``_pending_rewrite``)
    commits the user's edit; the rehydrate's read, ordered after the close's
    write by the ordering wait, finds the closed transcript and is refused as
    any reopen of a closed tab is. Nothing is resurrected and the key is left
    to nobody live -- the sequential history "close, then reopen".
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
    # A rewind that never committed: the live window is truncated and the
    # archive save will take the rewrite path.
    slot.messages = list(slot.messages)[:2]
    slot._pending_rewrite = True
    slot._dirty = True

    # Park the resume's transcript read so its publication stays pending while
    # the close runs its archive: its takeover note has fired, its replacement
    # is not yet in the map — the exact state no map read can see.
    read_parked = threading.Event()
    read_release = threading.Event()
    real_prefetch = chat_persistence._prefetch_rehydrate_inputs

    def _parked_prefetch(*args, **kwargs):
        read_parked.set()
        if not read_release.wait(5.0):
            raise AssertionError("the parked resume read was never released by the test")
        return real_prefetch(*args, **kwargs)

    monkeypatch.setattr(chat_persistence, "_prefetch_rehydrate_inputs", _parked_prefetch)

    # Before it can read, the resume waits for the teardown's watch to retire:
    # its takeover note bumped that watch, and the watch closes only when the
    # close completes. The wait is observed directly, so the close is released
    # only once the resume is provably held by it.
    waiting = threading.Event()
    real_wait = chat_persistence._await_takeover_ordering_settled

    async def _observed_wait(owner, name):
        waiting.set()
        return await real_wait(owner, name)

    monkeypatch.setattr(chat_persistence, "_await_takeover_ordering_settled", _observed_wait)

    clock = _freeze_tombstone_clock(monkeypatch)
    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(slot, entered, release)
    # The close stamps its tombstone at ``t0``.
    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, close, seam="the shielded task-cancel wait")
    # The key is already popped; the rehydrate begins inside the teardown
    # window, at ``t0 + 1``: after the user's close.
    clock.now += 1.0
    resume = asyncio.create_task(rehydrate_slot_from_history_async(state, NAME))
    # Held in the ordering wait with no note of its own: now the turn's cancel
    # completes and the close runs its archive against exactly that state.
    assert await _event_fired(waiting, 5.0), "the resume never reached its wait"
    assert not resume.done()
    release.set()

    resp = await close
    # The archive commits: the user's rewind lands, closed, and the session is
    # torn down.
    assert resp.status == 200

    # The watch retired with the close, so the rehydrate's read proceeds to the
    # parked prefetch; it finds the closed transcript and is refused.
    assert await _event_fired(read_parked, 5.0), "the rehydrate never reached its read"
    read_release.set()
    assert await resume is None, "a rehydrate begun after the close rebuilt the closed tab"
    assert NAME not in state._slots
    on_disk = state.conversation_log.read_messages_chained(slot_transcript_key(NAME))
    assert [m["content"] for m in on_disk] == ["first question", "first answer"]
    meta = state.conversation_log.get_metadata(slot_transcript_key(NAME))
    assert meta.get("closed") is True
    assert state._takeover_watches == {}


@pytest.mark.asyncio
async def test_a_failed_handover_to_an_unpublished_takeover_restores_the_original(
    tmp_path, monkeypatch
):
    """A drain that cannot write hands the tab back rather than dropping its only copy.

    The teardown watch has moved -- a replacement published on the key mid-close
    and then left it (a recreate the user immediately closed again) -- so the
    archive loses, and the hand-over drain is the popped original's last writer.
    When that drain fails too (the disk is full), the key is free again and the
    original can come back exactly as it does when the archive save raises. The
    close reports the failure and the tab keeps its unsaved tail dirty for the
    flush to retry, rather than being dropped with its only copy.
    """
    import threading

    from kiro_crew.dashboard import chat_persistence
    from kiro_crew.dashboard.chat_utils import slot_transcript_key
    from kiro_crew.history import ConversationLog

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(NAME)
    for row in ("first question", "first answer", "second question", "second answer"):
        slot.append("user" if "question" in row else "assistant", row)
    slot.drain()
    assert await chat_persistence.save_slot_off_loop(state, slot, force=True)
    slot.messages = list(slot.messages)[:2]
    slot._pending_rewrite = True
    slot.append("user", "TAIL-after-failed-rewrite")
    slot.drain()
    slot._dirty = True

    # Every append-safe write fails: the commit gate's fallback inside the
    # refused archive, and the drain's own arm after it.
    def _disk_full(self, *args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(ConversationLog, "append_if_absent", _disk_full)

    read_parked = threading.Event()
    read_release = threading.Event()
    real_prefetch = chat_persistence._prefetch_rehydrate_inputs

    def _parked_prefetch(*args, **kwargs):
        read_parked.set()
        if not read_release.wait(5.0):
            raise AssertionError("the parked resume read was never released by the test")
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
    # A same-name recreate publishes on the popped key inside the teardown and
    # is itself closed again before the archive decides: the watch has moved
    # and the archive's claim is marked, yet the key is free when the close
    # reads its refused archive -- the shape in which a close that dropped the
    # original would lose its only copy of the tail.

    recreate = state.get_or_create_slot(NAME)
    assert recreate is not slot
    state._slots.pop(NAME)
    release.set()

    resp = await close
    assert resp.status == 500
    assert json.loads(resp.text)["code"] == "history_save_failed"
    # The original is back under its key with the tail it still owes.
    assert state._slots.get(NAME) is slot, "the popped original was dropped with its tail"
    assert slot._dirty is True
    assert [m["content"] for m in slot.messages][-1] == "TAIL-after-failed-rewrite"
    assert slot.is_closing is False, "the restored tab must admit edits again"
    # Nothing reached the transcript: the refused archive left it untouched
    # and no append landed.
    on_disk = state.conversation_log.read_messages_chained(slot_transcript_key(NAME))
    assert len(on_disk) == 4

    assert state._takeover_watches == {}


@pytest.mark.asyncio
async def test_cleanup_restores_the_original_when_its_handover_to_a_takeover_fails(
    tmp_path, monkeypatch
):
    """The sweep's takeover arm restores a popped original whose drain did not land.

    Same shape as the single-tab close: the teardown watch moved and the key is
    still free, the drain fails, and the original -- the only holder of its
    unsaved tail -- comes back under its key with its close over, reported in
    ``failed`` and never in ``archived``.
    """
    from kiro_crew.dashboard import chat_persistence
    from kiro_crew.dashboard.slot_registry import SlotRegistry
    from kiro_crew.history import ConversationLog

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(NAME)
    slot.append("user", "first question")
    slot.append("assistant", "first answer")
    slot.drain()
    assert await chat_persistence.save_slot_off_loop(state, slot, force=True)
    slot.messages = list(slot.messages)[:1]
    slot._pending_rewrite = True
    slot.append("user", "TAIL-after-failed-rewrite")
    slot.drain()
    slot._dirty = True
    _make_stale(state)

    def _disk_full(self, *args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(ConversationLog, "append_if_absent", _disk_full)
    # The takeover note lands inside the sweep's teardown window: the watch
    # opened for ``NAME`` moves while the key is popped and still free.
    real_open = SlotRegistry.open_takeover_basis

    def _open_then_takeover(owner, name):
        basis = real_open(owner, name)
        if name == NAME and basis is not None:
            SlotRegistry.note_same_key_takeover(owner, name, transcript=f"dashboard:{name}")
        return basis

    monkeypatch.setattr(SlotRegistry, "open_takeover_basis", staticmethod(_open_then_takeover))

    resp = await handlers.api_chat_slots_cleanup(_Req(state, NAME))
    body = json.loads(resp.text)
    assert NAME in body["failed"], body
    assert NAME not in body["keys"], body
    assert state._slots.get(NAME) is slot, "the popped original was dropped with its tail"
    assert slot._dirty is True
    assert slot.is_closing is False
    assert [m["content"] for m in slot.messages][-1] == "TAIL-after-failed-rewrite"


@pytest.mark.asyncio
async def test_a_reopen_racing_an_edit_never_hydrates_the_truncation_with_the_removed_tail_behind_it(
    tmp_path, monkeypatch
):
    """The torn history: a reopen's window from before the edit, over a transcript the edit truncated.

    The base reproduction. A tab owes a truncating rewrite (an edit whose inline
    save failed) and is closed: its archive carries the truncation. The user
    reopens the conversation while that archive is still queued for its lock.
    The reopen reads the transcript FIRST -- all four rows, the edit not yet
    on disk -- and is suspended before it publishes; the archive then lands the
    truncation and ``closed``. Without the ordering, the reopen publishes its
    four-row window over a two-row, closed transcript: the tab shows turns the
    edit removed, the next turn's save re-serializes them onto disk (the edit is
    silently undone), and the marker it never saw makes the tab vanish at the
    next restart. Neither consistent history -- "the edit applied, then a
    reopen reads the two rows" or "the reopen read first, so the edit is refused
    and the four rows stand" -- shows the user any of that. Both are accepted
    here; the torn one is not.
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
    # The edit: the window is truncated to two rows and the rewrite is owed.
    slot.messages = list(slot.messages)[:2]
    slot._pending_rewrite = True
    slot._dirty = True

    # The archive worker parks at its entry, before it takes the lock, so the
    # reopen can read the untruncated transcript ahead of it.
    save_entered = threading.Event()
    save_release = threading.Event()
    real_save = chat_persistence._save_slot_to_history

    def _parked_save(*args, **kwargs):
        save_entered.set()
        assert save_release.wait(10.0), "the archive was never released by the test"
        return real_save(*args, **kwargs)

    monkeypatch.setattr(chat_persistence, "_save_slot_to_history", _parked_save)
    # The reopen parks after its transcript read and before it publishes, at the
    # off-loop agent-name restore every reopen performs.
    read_done = threading.Event()
    publish_release = threading.Event()
    real_restored_agent = handlers._restored_agent_name

    def _parked_restore(*args, **kwargs):
        read_done.set()
        assert publish_release.wait(10.0), "the reopen was never released by the test"
        return real_restored_agent(*args, **kwargs)

    monkeypatch.setattr(handlers, "_restored_agent_name", _parked_restore)
    # An implementation that orders the reopen's read behind the pending archive
    # parks the reopen in a wait instead of letting it read; observe that wait
    # where it exists so the staging releases the archive either way.
    held = threading.Event()
    real_wait = getattr(handlers, "_await_takeover_ordering_settled", None)
    if real_wait is not None:

        async def _observed_wait(owner, name):
            held.set()
            return await real_wait(owner, name)

        monkeypatch.setattr(handlers, "_await_takeover_ordering_settled", _observed_wait)

    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(slot, entered, release)
    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, close, seam="the shielded task-cancel wait")
    release.set()
    assert await _event_fired(save_entered, 10.0), "the archive never reached its worker"
    # The user reopens the conversation now, after the close: the key is free,
    # the archive is queued for its lock, the transcript still has four rows.
    reopen = asyncio.create_task(
        handlers.resume_slot_from_history(state, name=NAME, history_key=key)
    )
    await async_wait_until(
        lambda: read_done.is_set() or held.is_set(),
        timeout=10.0,
        describe=lambda: "the reopen neither read the transcript nor waited for the archive",
    )
    # The archive lands now: after the reopen's read (an unordered reopen), or
    # before it (one that waits for the pending truncation).
    save_release.set()
    resp = await close
    publish_release.set()
    outcome = await reopen

    on_disk = [m["content"] for m in state.conversation_log.read_messages_chained(key)]
    meta = state.conversation_log.get_metadata(key)
    live = state._slots.get(NAME)
    shown = [m["content"] for m in live.messages] if live is not None else None
    if outcome.refusal is None:
        # The reopen won: it must have read the transcript the edit never
        # touched, and nothing may be closed underneath it.
        assert (
            outcome.slot is live and shown == on_disk
        ), f"torn: the tab shows {shown} over a transcript holding {on_disk}"
        assert "closed" not in meta, "a live reopened tab with a closed marker vanishes at restart"
    else:
        # The edit won: the two-row transcript is closed and nothing is live.
        assert live is None, f"a refused reopen left a tab live showing {shown}"
        assert on_disk == ["first question", "first answer"]
        assert meta.get("closed") is True
    assert resp.status in (200, 500), resp.text


@pytest.mark.asyncio
async def test_close_reports_failure_for_a_retryable_archive_refusal(tmp_path, monkeypatch):
    """A refusal that is neither a takeover nor a deletion is a failed close.

    The archive save can refuse for retryable reasons — the stale-queue guard
    refuses when another writer committed a newer queued-prompt value while
    this save held an older snapshot — and the popped slot holds the only copy
    of its unsaved tail. Reading that ``False`` as delete-won would discard
    acknowledged rows while answering 200; the close must instead verify the
    deletion positively, and otherwise restore the tab and report the failure
    so the periodic flush retries the write.
    """
    state = _state_with_slot(tmp_path)
    slot = state._slots[NAME]
    # The session exists on disk, so the delete verification answers False.
    await handlers.save_slot_off_loop(state, slot, force=True)

    async def _refused(*_a, **_kw) -> bool:
        return False

    monkeypatch.setattr(handlers, "save_slot_off_loop", _refused)

    resp = await handlers.api_chat_slot_delete(_Req(state, NAME))

    assert resp.status == 500
    assert json.loads(resp.text)["code"] == "history_save_failed"
    assert state._slots.get(NAME) is slot, "the tab must come back for the flush to retry"


@pytest.mark.asyncio
async def test_deletion_is_only_confirmed_on_definitive_evidence(tmp_path):
    """An unverifiable probe is not a deletion for the destructive close arm.

    ``session_was_deleted`` fails CLOSED (True on an unreadable line) because
    its callers refuse a COPY on True — the protective direction. The close and
    cleanup arms DISCARD a popped slot's tail on True, so they consult
    ``session_deletion_confirmed``, which answers True only for a missing file
    or a readable identity contradiction and routes everything unverifiable to
    the retry/restore handling instead.
    """
    from kiro_crew.dashboard.chat_persistence import save_slot_off_loop
    from kiro_crew.dashboard.chat_utils import slot_history_key
    from kiro_crew.dashboard.slot_persistence.write_guards import (
        session_deletion_confirmed,
        session_was_deleted,
    )

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(NAME)
    slot.append("user", "hello")
    slot.drain()
    assert await save_slot_off_loop(state, slot, force=True)

    # File present, identity matching: no deletion by either probe.
    assert session_deletion_confirmed(state, slot) is False

    # An unreadable metadata line: the protective probe refuses the copy, the
    # destructive probe declines to call it a deletion.
    real_status = state.conversation_log.get_metadata_status
    state.conversation_log.get_metadata_status = lambda _key: ({}, False)
    try:
        assert session_was_deleted(state, slot) is True
        assert session_deletion_confirmed(state, slot) is False
    finally:
        state.conversation_log.get_metadata_status = real_status

    # A readable-but-empty pair with the file STILL PRESENT stays ambiguous:
    # the transcript demonstrably exists, so the retryable False.
    real_status = state.conversation_log.get_metadata_status
    state.conversation_log.get_metadata_status = lambda _key: ({}, True)
    try:
        assert session_deletion_confirmed(state, slot) is False
    finally:
        state.conversation_log.get_metadata_status = real_status

    # A delete landing BETWEEN the stat and the metadata read leaves the same
    # readable-but-empty pair; the probe re-stats and the now-missing file is
    # the definitive witness. Answering False here restores a ghost slot over
    # a deleted session — turns accepted, every save refused, lost on restart.
    path = state.conversation_log._path(slot_history_key(slot))

    def _delete_then_empty(_key):
        path.unlink(missing_ok=True)
        return {}, True

    state.conversation_log.get_metadata_status = _delete_then_empty
    try:
        assert session_deletion_confirmed(state, slot) is True
    finally:
        state.conversation_log.get_metadata_status = real_status

    # A missing file is the definitive witness for both.
    assert session_deletion_confirmed(state, slot) is True


@pytest.mark.asyncio
async def test_both_delete_witnesses_map_the_one_evidence_verdict(tmp_path, monkeypatch):
    """One evidence helper, two mappings: the protective and the destructive bool.

    ``session_was_deleted`` (fork/transfer refuse a COPY on True) treats what it
    cannot verify as deleted; ``session_deletion_confirmed`` (the teardowns
    DISCARD a popped tail on True) treats it as not confirmed. Every verdict the
    shared helper can return is pinned against both, so a later change to the
    evidence (a new identity field, say) reaches both probes at once.
    """
    from kiro_crew.dashboard.chat_persistence import save_slot_off_loop
    from kiro_crew.dashboard.chat_utils import slot_history_key
    from kiro_crew.dashboard.slot_persistence import write_guards

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(NAME)
    slot.append("user", "hello")
    slot.drain()
    assert await save_slot_off_loop(state, slot, force=True)
    log = state.conversation_log
    key = slot_history_key(slot)

    def verdicts():
        return (
            write_guards._session_delete_evidence(state, slot),
            write_guards.session_was_deleted(state, slot),
            write_guards.session_deletion_confirmed(state, slot),
        )

    # present: a committed save observed the file, whose line carries its identity.
    assert verdicts() == ("present", False, False)
    # unobserved: a slot that never saw its file has nothing a delete can contradict.
    fresh = state.get_or_create_slot("never-saved")
    assert write_guards._session_delete_evidence(state, fresh) == "unobserved"
    assert write_guards.session_was_deleted(state, fresh) is False
    assert write_guards.session_deletion_confirmed(state, fresh) is False
    # unverifiable: an unreadable line, a failing read, a stat error other than missing.
    real_status = log.get_metadata_status
    monkeypatch.setattr(log, "get_metadata_status", lambda _key: ({}, False))
    assert verdicts() == ("unverifiable", True, False)

    def _raise(_key):
        raise OSError("transient")

    monkeypatch.setattr(log, "get_metadata_status", _raise)
    assert verdicts() == ("unverifiable", True, False)
    monkeypatch.setattr(log, "get_metadata_status", real_status)
    real_path = log._path

    class _Unstattable:
        def stat(self):
            raise PermissionError("stat refused")

    monkeypatch.setattr(log, "_path", lambda _key: _Unstattable())
    assert verdicts() == ("unverifiable", True, False)
    monkeypatch.setattr(log, "_path", real_path)
    # deleted: a readable line whose identity contradicts the recorded one.
    monkeypatch.setattr(
        log, "get_metadata_status", lambda _key: ({"created_at": "2001-01-01T00:00:00Z"}, True)
    )
    assert verdicts() == ("deleted", True, True)
    monkeypatch.setattr(log, "get_metadata_status", real_status)
    # deleted: the file vanished between the stat and the read (the empty, readable pair).
    path = real_path(key)

    def _delete_then_empty(_key):
        path.unlink()
        return ({}, True)

    monkeypatch.setattr(log, "get_metadata_status", _delete_then_empty)
    assert verdicts() == ("deleted", True, True)
    monkeypatch.setattr(log, "get_metadata_status", real_status)
    # deleted: the file is simply gone.
    assert verdicts() == ("deleted", True, True)


def test_teardown_drain_basis_follows_the_transcript_not_the_key(tmp_path):
    """The teardown watch reaches a drain only where its key names the drain's file.

    An unbound original writes the key's own transcript — what a same-key
    takeover resumes — so its drain keeps the watch. A linked original writes
    elsewhere: an unbound replacement on the key touches nothing that drain
    writes, and a moved watch handed to it would refuse a safe write and leave
    the popped slot's rows unreachable. A published replacement that provably
    shares the linked transcript is the one linked case that keeps it.
    """
    from kiro_crew.dashboard.slot_registry import TakeoverBasis

    state = _state_with_slot(tmp_path)
    slot = state._slots.pop(NAME)
    basis = TakeoverBasis(NAME, 0)

    # Unbound original: the key's transcript is the drain's file.
    assert handlers._teardown_drain_basis(state, NAME, slot, basis) is basis

    # Linked original, key free or divergently replaced: the watch stays home.
    slot.linked_session_key = "slack:C123:456.789"
    assert handlers._teardown_drain_basis(state, NAME, slot, basis) is None
    divergent = state.get_or_create_slot(NAME)
    assert divergent.linked_session_key == ""
    assert handlers._teardown_drain_basis(state, NAME, slot, basis) is None

    # A published replacement sharing the linked transcript keeps the ordering.
    divergent.linked_session_key = "slack:C123:456.789"
    assert handlers._teardown_drain_basis(state, NAME, slot, basis) is basis

    # An UNPUBLISHED resume that declared the linked transcript as its read
    # target keeps it too: the map cannot see a resume whose read is still
    # pending, but its takeover note named the file this drain writes.
    from kiro_crew.dashboard.chat_utils import slot_history_key
    from kiro_crew.dashboard.slot_registry import SlotRegistry

    state._slots.pop(NAME)
    open_basis = SlotRegistry.open_takeover_basis(state, NAME)
    try:
        SlotRegistry.note_same_key_takeover(state, NAME, transcript=slot_history_key(slot))
        assert handlers._teardown_drain_basis(state, NAME, slot, open_basis) is open_basis
    finally:
        SlotRegistry.close_takeover_basis(state, open_basis)


@pytest.mark.asyncio
async def test_handover_drain_saves_the_tail_a_lost_truncation_rode_with(tmp_path):
    """Rows appended after a failed inline rewrite survive a takeover hand-over.

    A ``_pending_rewrite`` retry riding the hand-over drain makes the whole
    write rewrite-shaped, so a takeover that moved the teardown watch would
    refuse it — and the window's NEW rows would die with the popped object for
    an arbitration only the truncation deserved to lose. The drain disarms the
    lost truncation instead: the save runs default-shaped, the foreign-append
    merge carries the on-disk rows the abandoned truncation would have
    dropped, and the appended tail lands.
    """
    from kiro_crew.dashboard.chat_persistence import save_slot_off_loop
    from kiro_crew.dashboard.chat_utils import slot_transcript_key
    from kiro_crew.dashboard.slot_registry import SlotRegistry

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(NAME)
    for i in range(4):
        slot.append("user" if i % 2 == 0 else "assistant", f"ROW-{i}")
    slot.drain()
    assert await save_slot_off_loop(state, slot, force=True)

    # A rewind that never committed, then more conversation on top of it.
    slot.messages = list(slot.messages)[:2]
    slot._pending_rewrite = True
    slot.append("user", "TAIL-after-failed-rewrite")
    slot.drain()
    slot._dirty = True

    # The teardown's watch, moved by a takeover before the drain runs.
    basis = SlotRegistry.open_takeover_basis(state, NAME)
    state._slots.pop(NAME)
    state.get_or_create_slot(NAME)
    try:
        drained = await handlers._persist_handover_tail(state, NAME, slot, takeover_basis=basis)
    finally:
        SlotRegistry.close_takeover_basis(state, basis)

    assert drained.rows_committed is True
    contents = [
        m["content"]
        for m in state.conversation_log.read_messages_chained(slot_transcript_key(NAME))
    ]
    assert "TAIL-after-failed-rewrite" in contents, "the appended tail must land"
    for i in range(4):
        assert f"ROW-{i}" in contents, "the lost truncation must not drop on-disk rows"


@pytest.mark.asyncio
async def test_handover_append_creates_the_header_under_the_restricted_mode(tmp_path):
    """A private original with no committed header hands its tail over under its own mode.

    The append-safe drain arm writes through ``ConversationLog.append``, which
    mints a header with no ``memory_mode`` when the file is absent — a line
    every learning reader takes as persistent. The rows of an incognito slot
    that never committed a header (its first save was the rewrite that failed)
    would then land under exactly that line, and a restart's consolidation
    would learn from them. The drain settles the header first, so the file is
    born restricted and the derivation seam withholds it.
    """
    from kiro_crew.dashboard.chat_utils import slot_transcript_key
    from kiro_crew.dashboard.slot_registry import SlotRegistry
    from kiro_crew.history import TranscriptWithheld, transcript_withholds_derivation

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(NAME, memory_mode="incognito")
    slot.append("user", "off the record")
    slot.append("assistant", "kept off the record")
    slot.drain()
    # The rewrite that never committed, then more conversation; no header yet.
    slot.messages = list(slot.messages)[:1]
    slot._pending_rewrite = True
    slot.append("user", "TAIL-after-failed-rewrite")
    slot.drain()
    slot._dirty = True
    history_key = slot_transcript_key(NAME)
    assert not state.conversation_log._path(history_key).exists()

    basis = SlotRegistry.open_takeover_basis(state, NAME)
    state._slots.pop(NAME)
    state.get_or_create_slot(NAME)
    try:
        drained = await handlers._persist_handover_tail(state, NAME, slot, takeover_basis=basis)
    finally:
        SlotRegistry.close_takeover_basis(state, basis)

    assert drained.rows_committed is True
    meta = state.conversation_log.get_metadata(history_key)
    assert meta.get("memory_mode") == "incognito", meta
    assert not meta.get("memory_store")
    assert transcript_withholds_derivation(state.conversation_log, history_key) is True
    with pytest.raises(TranscriptWithheld):
        state.conversation_log.derive_messages(history_key)
    # The rows did land; only learning from them is withheld.
    contents = [m["content"] for m in state.conversation_log.read_messages_chained(history_key)]
    assert "TAIL-after-failed-rewrite" in contents


@pytest.mark.asyncio
async def test_handover_append_tightens_a_persistent_replacement_line(tmp_path):
    """Private rows appended onto a persistent replacement's line ratchet that line.

    The replacement published its own line at birth — persistent, naming a
    store, carrying a durable execution record and a title. The original's
    hand-over append must not file incognito rows under it as it stands: the
    mode tightens to the stricter value, the store a restricted line must not
    name is cleared, the carried execution record follows the line, and the
    replacement's own fields (its title) stay exactly as it wrote them — the
    same outcome the rows-only save produces for the same race.
    """
    from kiro_crew.dashboard.chat_utils import slot_transcript_key
    from kiro_crew.dashboard.slot_registry import SlotRegistry
    from kiro_crew.execution_context import EXECUTION_CONTEXT_KEY, ExecutionContext, MemoryStoreRef
    from kiro_crew.history import transcript_withholds_derivation

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(NAME, memory_mode="incognito")
    slot.append("user", "off the record")
    slot.drain()
    slot.messages = []
    slot._pending_rewrite = True
    slot.append("user", "TAIL-after-failed-rewrite")
    slot.drain()
    slot._dirty = True
    history_key = slot_transcript_key(NAME)

    basis = SlotRegistry.open_takeover_basis(state, NAME)
    state._slots.pop(NAME)
    state.get_or_create_slot(NAME)
    # The replacement's line, as its own first save would have written it.
    record = ExecutionContext(None, MemoryStoreRef("default"), "template", "").to_record()
    assert state.conversation_log.update_metadata_if(
        history_key,
        {
            "memory_mode": "persistent",
            "memory_store": "default",
            "title": "Replacement title",
            EXECUTION_CONTEXT_KEY: record,
        },
        lambda _meta: True,
    )
    try:
        drained = await handlers._persist_handover_tail(state, NAME, slot, takeover_basis=basis)
    finally:
        SlotRegistry.close_takeover_basis(state, basis)

    assert drained.rows_committed is True
    meta = state.conversation_log.get_metadata(history_key)
    assert meta.get("memory_mode") == "incognito", meta
    assert not meta.get("memory_store"), "a restricted line names no store"
    assert meta[EXECUTION_CONTEXT_KEY]["memory_mode"] == "incognito"
    assert meta[EXECUTION_CONTEXT_KEY]["store"] == record["store"], "identity untouched"
    assert meta.get("title") == "Replacement title", "the replacement's own fields stay"
    assert transcript_withholds_derivation(state.conversation_log, history_key) is True
    contents = [m["content"] for m in state.conversation_log.read_messages_chained(history_key)]
    assert "TAIL-after-failed-rewrite" in contents


@pytest.mark.asyncio
async def test_handover_append_refuses_when_the_header_cannot_be_confirmed(tmp_path, monkeypatch):
    """One deferred header merge during an incognito hand-over appends nothing.

    The tightening merge defers on a header read it cannot complete, without
    running its guard; a drain that appended anyway would land private rows under a
    header whose contract it never saw -- for a slot that never committed a
    header, a mode-less one ``append`` mints on the spot. The drain fails
    closed instead: no row is written, the error arm reports the rows, the
    close reports the hand-over failed, and the slot still holds its tail as
    owed.
    """
    from kiro_crew.dashboard.chat_utils import slot_transcript_key
    from kiro_crew.dashboard.slot_registry import SlotRegistry
    from kiro_crew.history import ConversationLog

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(NAME, memory_mode="incognito")
    slot.append("user", "off the record")
    slot.drain()
    slot.messages = []
    slot._pending_rewrite = True
    slot.append("user", "TAIL-after-failed-rewrite")
    slot.drain()
    slot._dirty = True
    history_key = slot_transcript_key(NAME)
    assert not state.conversation_log._path(history_key).exists()

    # The tightening merge meets a header it cannot read: ``update_metadata_if``
    # defers on a transient read failure -- it answers ``False`` WITHOUT running
    # its guard -- so the staging answers exactly that for the one merge the
    # drain issues, and every other read of the file stays real. (The slot's
    # retained-mode read ahead of the merge goes through the execution reader,
    # which refuses an unreadable record on its own; the seam under test is the
    # merge's deferral, which that reader cannot cover.)
    real_merge = ConversationLog.update_metadata_if
    deferrals = {"left": 1}

    def _defers_once(self, key, fields, guard, **kwargs):
        if key == history_key and deferrals["left"]:
            deferrals["left"] -= 1
            return False
        return real_merge(self, key, fields, guard, **kwargs)

    monkeypatch.setattr(ConversationLog, "update_metadata_if", _defers_once)

    basis = SlotRegistry.open_takeover_basis(state, NAME)
    state._slots.pop(NAME)
    state.get_or_create_slot(NAME)
    try:
        drained = await handlers._persist_handover_tail(state, NAME, slot, takeover_basis=basis)
    finally:
        SlotRegistry.close_takeover_basis(state, basis)

    assert deferrals["left"] == 0, "the refusal must come from the deferred merge, not before it"
    assert drained.rows_committed is False, "an unconfirmed header is a refused hand-over"
    assert not state.conversation_log._path(history_key).exists(), "nothing may be appended"
    assert slot._dirty is True, "the tail stays owed on the slot"
    assert slot.messages[-1]["content"] == "TAIL-after-failed-rewrite"


@pytest.mark.asyncio
async def test_a_cancelled_handover_keeps_its_append_and_its_watch(tmp_path, monkeypatch):
    """Cancelling the teardown mid-append neither drops the rows nor retires the watch early.

    The append runs on an executor thread, which a cancellation cannot stop; a
    bare ``to_thread`` await would resolve the caller's unwind -- and its
    ``finally`` close of the teardown watch -- while the thread is still
    pre-write under that watch, and nothing would observe the rows landing.
    The drain shields the future and hands the watch to its settlement, so
    the acknowledged tail keeps its writer and the watch retires only once the
    append has settled.
    """
    import threading

    from kiro_crew.dashboard.chat_persistence import save_slot_off_loop
    from kiro_crew.dashboard.chat_utils import slot_transcript_key
    from kiro_crew.dashboard.slot_registry import SlotRegistry
    from kiro_crew.testing.wait import async_wait_until

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(NAME)
    for i in range(4):
        slot.append("user" if i % 2 == 0 else "assistant", f"ROW-{i}")
    slot.drain()
    assert await save_slot_off_loop(state, slot, force=True)
    slot.messages = list(slot.messages)[:2]
    slot._pending_rewrite = True
    slot.append("user", "TAIL-after-failed-rewrite")
    slot.drain()
    slot._dirty = True
    history_key = slot_transcript_key(NAME)

    # Park the append thread at its first step, inside the append lock.
    from kiro_crew.dashboard.slot_persistence import metadata_line

    entered = threading.Event()
    release = threading.Event()
    real_tighten = metadata_line.tighten_line_for_appends

    def _parked_tighten(*args, **kwargs):
        entered.set()
        if not release.wait(5.0):
            raise AssertionError("the parked append was never released by the test")
        return real_tighten(*args, **kwargs)

    monkeypatch.setattr(metadata_line, "tighten_line_for_appends", _parked_tighten)

    basis = SlotRegistry.open_takeover_basis(state, NAME)
    state._slots.pop(NAME)
    state.get_or_create_slot(NAME)
    drain = asyncio.create_task(
        handlers._persist_handover_tail(state, NAME, slot, takeover_basis=basis)
    )
    assert await _event_fired(entered, 5.0), "the drain never reached its append"

    drain.cancel()
    with pytest.raises(asyncio.CancelledError):
        await drain
    # The caller's unwind closes its basis now -- as the teardown's ``finally``
    # does -- and that close must be a no-op: the watch was handed to the
    # running append.
    assert basis.deferred is True, "the watch was not transferred to the running append"
    SlotRegistry.close_takeover_basis(state, basis)
    assert NAME in state._takeover_watches, "the watch retired under a running worker"

    release.set()
    await async_wait_until(
        lambda: NAME not in state._takeover_watches,
        timeout=5.0,
        describe=lambda: f"watches={state._takeover_watches}",
    )
    contents = [m["content"] for m in state.conversation_log.read_messages_chained(history_key)]
    assert "TAIL-after-failed-rewrite" in contents, "the cancelled drain lost its rows"
    for i in range(4):
        assert f"ROW-{i}" in contents


def _rotation_staged_handover(state, monkeypatch):
    """A slot whose window reaches past the live file's rotation cut.

    The byte budget is shrunk so a mid-sized window overflows it, and the keep
    count so the live file retains only the newest few rows: a full save lands
    the window, a single append rotates everything older into an archive, and
    the window still holds every row -- the shape in which an absence check
    against the live file alone re-appends the archived rows.
    """
    monkeypatch.setattr("kiro_crew.history._SESSION_MAX_BYTES", 1200)
    monkeypatch.setattr("kiro_crew.history._SESSION_KEEP_LINES", 3)
    slot = state.get_or_create_slot(NAME)
    for i in range(12):
        slot.append("user" if i % 2 == 0 else "assistant", f"ROW-{i:02d} " + "x" * 40)
    slot.drain()
    return slot


def _identities(rows):
    return [(m.get("role"), (m.get("meta") or {}).get("mid"), m.get("content")) for m in rows]


@pytest.mark.asyncio
async def test_handover_append_does_not_duplicate_rows_rotated_into_archives(tmp_path, monkeypatch):
    """A window replayed append-safely lands only the rows the durable history lacks.

    ``append_if_absent`` judges absence against the live file, and rotation
    keeps only the newest rows there: every older window row would read as
    absent and be appended again -- one identity, two rows, every index above
    them shifted, and nothing to clean it up. The drain asks for the archives
    to count, so the replay lands exactly the tail the transcript does not
    hold, wherever the rest of the window now lives.
    """
    from kiro_crew.dashboard.chat_persistence import save_slot_off_loop
    from kiro_crew.dashboard.chat_utils import slot_transcript_key
    from kiro_crew.dashboard.slot_registry import SlotRegistry

    state = _make_state(tmp_path)
    slot = _rotation_staged_handover(state, monkeypatch)
    history_key = slot_transcript_key(NAME)
    assert await save_slot_off_loop(state, slot, force=True)
    # One foreign append pushes the file over budget: rotation archives the
    # head and keeps the newest rows live. The window still holds all of them.
    state.conversation_log.append(history_key, "assistant", "FOREIGN " + "y" * 40)
    live = state.conversation_log.read_messages_chained(history_key)
    rotated = state.conversation_log.read_rotated_messages_chained(history_key)
    assert rotated, "the staging must have rotated rows into an archive"
    assert len(live) < 12, "the live file must hold fewer rows than the window"
    before = _identities(rotated + live)
    assert len(before) == len(set(before))

    # The hand-over shape: a rewrite that never committed, then a new tail.
    slot._pending_rewrite = True
    slot.append("user", "TAIL-after-failed-rewrite")
    slot.drain()
    slot._dirty = True

    basis = SlotRegistry.open_takeover_basis(state, NAME)
    state._slots.pop(NAME)
    state.get_or_create_slot(NAME)
    try:
        drained = await handlers._persist_handover_tail(state, NAME, slot, takeover_basis=basis)
    finally:
        SlotRegistry.close_takeover_basis(state, basis)
    assert drained.rows_committed is True

    rotated = state.conversation_log.read_rotated_messages_chained(history_key)
    live = state.conversation_log.read_messages_chained(history_key)
    after = _identities(rotated + live)
    assert len(after) == len(set(after)), "a window row was appended a second time"
    assert after[: len(before)] == before, "existing indices moved"
    assert after[len(before) :] == [
        ("user", after[-1][1], "TAIL-after-failed-rewrite")
    ], "only the owed tail may land"


@pytest.mark.asyncio
async def test_handover_append_drops_notes_authorized_for_another_session(tmp_path, monkeypatch):
    """A note row stamped for session A never rides the hand-over append into B.

    Every full save filters its window through ``drop_notes_authorized_elsewhere``
    (and audits the drop as ``note_save_drop``), so such a row is not on disk --
    and an append-safe replay that skipped the filter would read it as absent
    and land it in the session the slot was rebound to. The drain prepares its
    rows the way the full save does, so B's transcript carries the slot's own
    tail and nothing stamped for A, and the drop is audited exactly as the full
    save audits it.
    """
    from types import SimpleNamespace

    from kiro_crew.dashboard import chat_persistence
    from kiro_crew.dashboard.chat_utils import effective_session_key, slot_history_key
    from kiro_crew.dashboard.slot_registry import SlotRegistry

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(NAME)
    # The slot runs on session B (a channel binding); its transcript is B's.
    slot.linked_session_key = "slack:9999.1234"
    session_b_key = slot_history_key(slot)
    slot.append("user", "first question")
    slot.append("assistant", "first answer")
    slot.drain()
    assert await chat_persistence.save_slot_off_loop(state, slot, force=True)
    # A note row stamped for session A -- the slot's own key, the session it
    # was bound to when the note was written -- sits in the window the drain
    # is about to replay, beside the tail that is genuinely owed.
    session_a = f"dashboard:{NAME}"
    assert effective_session_key(slot) != session_a
    slot.messages = list(slot.messages)[:1]
    slot._pending_rewrite = True
    slot.append("inject", "A-only note", "msg msg-note", meta={"noteSession": session_a})
    slot.append("user", "TAIL-after-failed-rewrite")
    slot.drain()
    slot._dirty = True

    audited: list[dict] = []
    monkeypatch.setattr(
        chat_persistence,
        "sel",
        lambda: SimpleNamespace(log_api_access=lambda **kw: audited.append(kw)),
    )

    basis = SlotRegistry.open_takeover_basis(state, NAME)
    state._slots.pop(NAME)
    state.get_or_create_slot(NAME)
    try:
        drained = await handlers._persist_handover_tail(state, NAME, slot, takeover_basis=basis)
    finally:
        SlotRegistry.close_takeover_basis(state, basis)
    assert drained.rows_committed is True

    contents = [m["content"] for m in state.conversation_log.read_messages_chained(session_b_key)]
    assert "A-only note" not in contents, "a note authorized for A landed in B's transcript"
    assert "TAIL-after-failed-rewrite" in contents
    drops = [row for row in audited if row.get("operation") == "note_save_drop"]
    assert len(drops) == 1 and drops[0]["outcome"] == "denied", audited
    assert f"slot={NAME} dropped=1" in drops[0]["resources"]


def test_a_declared_transcript_matches_every_spelling_of_its_file(tmp_path) -> None:
    """The drain basis follows the file a takeover declared, however it was spelled.

    A Slack transcript is addressed as ``slack:<ts>`` or ``slack_<ts>`` and both
    resolve to one file. A resume that declared one spelling must be seen by a
    teardown that writes the other, or the teardown's rewrite-shaped drain
    would keep no watch and commit its truncation over the transcript the
    resume is about to read. Declared targets are recorded and matched by
    stem, the identity ``replacement_shares_transcript`` already compares by.
    """
    from kiro_crew.dashboard.slot_registry import SlotRegistry

    state = _make_state(tmp_path)
    linked = state.get_or_create_slot(NAME)
    linked.linked_session_key = "slack:123.456"
    assert handlers.slot_history_key(linked) == "slack:123.456"

    state._slots.pop(NAME)
    basis = SlotRegistry.open_takeover_basis(state, NAME)
    try:
        # An unpublished resume declares the alias spelling of the same file.
        assert SlotRegistry.note_same_key_takeover(state, NAME, transcript="slack_123.456")
        assert SlotRegistry.takeover_noted_transcript(state, basis, "slack:123.456") is True
        assert SlotRegistry.takeover_noted_transcript(state, basis, "slack_123.456") is True
        assert SlotRegistry.takeover_noted_transcript(state, basis, "slack:999.000") is False
        # The teardown keeps its watch for the linked original's drain.
        assert handlers._teardown_drain_basis(state, NAME, linked, basis) is basis
    finally:
        SlotRegistry.close_takeover_basis(state, basis)

    # The key's own transcript, spelled either way, keeps the watch too.
    unbound = state.get_or_create_slot(NAME)
    basis = SlotRegistry.open_takeover_basis(state, NAME)
    try:
        assert handlers._teardown_drain_basis(state, NAME, unbound, basis) is basis
    finally:
        SlotRegistry.close_takeover_basis(state, basis)


@pytest.mark.asyncio
async def test_a_divergent_publication_does_not_overtake_a_linked_tabs_rewrite_archive(
    tmp_path, monkeypatch
) -> None:
    """A same-name publication that resumes another transcript leaves the close's archive alone.

    A linked tab (its transcript ``cron:job7``) owes a rewrite and is closed; an
    unbound same-name slot is published while the close awaits, taking the KEY
    but a different FILE (``dashboard:<name>``). The publication's takeover note
    declares the file it writes, and the archive's claim records the file it
    writes, so the two never meet: the archive is neither marked overtaken nor
    born so, it COMMITS the rewrite with ``closed``, and the close reports
    success. Marking the claim by key alone refused that archive at its commit
    gate, and -- the drain basis being discriminated away for the divergent
    file -- the close then failed ``history_save_failed`` without writing
    ``closed``, so the channel reconcile could bring back the dismissed tab.
    """
    state = _make_state(tmp_path)
    linked = "cron:job7"
    original = await _linked_slot_with_a_tail(state, linked)
    # The tail rides a rewrite: the archive is the rewrite-shaped write a
    # same-transcript takeover would have to lose to.
    original._pending_rewrite = True
    original._dirty = True

    # Park the archive INSIDE its lock, between its claim registration and its
    # commit gate -- the stretch in which a publication's note finds a claim
    # to mark.
    from kiro_crew.dashboard.slot_persistence import metadata_line

    in_lock = threading.Event()
    go_on = threading.Event()
    real_line = metadata_line.build_full_line

    def _parked_line(*args, **kwargs):
        in_lock.set()
        assert go_on.wait(5.0), "the parked archive was never released by the test"
        return real_line(*args, **kwargs)

    monkeypatch.setattr(metadata_line, "build_full_line", _parked_line)
    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(original, entered, release)
    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, close, seam="the shielded task-cancel wait")
    release.set()
    assert await _event_fired(in_lock, 5.0), "the archive never reached its in-lock seam"
    # The archive holds its claim. An unbound same-name slot is published into
    # the popped key: its note declares ``dashboard:<name>``, not the linked
    # file the archive writes.
    replacement = state.get_or_create_slot(NAME)
    assert replacement is not original
    assert not replacement.linked_session_key, "the replacement must be unbound for this case"
    go_on.set()
    resp = await close
    assert resp.status == 200, resp.text
    meta = state.conversation_log.get_metadata(linked)
    assert meta.get("closed") is True, "the archive must commit and write closed"
    assert [m.get("content", "") for m in state.conversation_log.read_messages(linked)][
        -1
    ] == "TAIL-2", "the rewrite's window must be on disk"
    assert state._slots.get(NAME) is replacement, "the replacement was clobbered by the close"
    assert state._takeover_watches == {}


@pytest.mark.asyncio
async def test_divergent_takeover_does_not_relabel_an_unrelated_archive_refusal(
    tmp_path, monkeypatch
) -> None:
    """A non-takeover refusal beside a divergent takeover stays a failed close.

    Two independent events coincide: the archive save refuses for its own
    retryable reason (the stale-queue shape — the watch relevant to this
    slot's transcript never moved), and an unbound recreate takes the key,
    moving the RAW teardown watch. Testing the raw basis at the refusal
    branch relabels that refusal a settled takeover and reports the close as
    a success whose ``closed`` flag never landed — the dismissal silently
    lost. The branch tests the DISCRIMINATED basis (``None`` for a divergent
    takeover that never touched this slot's routed transcript), so the
    refusal routes to the retry/failure arm and the close reports honestly.
    """
    state = _make_state(tmp_path)
    linked = "cron:job7"
    original = await _linked_slot_with_a_tail(state, linked)

    real_save = handlers.save_slot_off_loop

    async def _refusing_archive(st, slot, *a, **kw) -> bool:
        if kw.get("closed"):
            # A refusal that is NOT a takeover of this slot's transcript:
            # nothing written, the watch this write answers to untouched.
            return False
        return await real_save(st, slot, *a, **kw)

    monkeypatch.setattr(handlers, "save_slot_off_loop", _refusing_archive)

    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(original, entered, release)

    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, close, seam="the shielded task-cancel wait")
    # The divergent takeover: an unbound recreate over the linked original.
    # Publication moves the RAW teardown watch, while the original's routed
    # transcript — the file the refused archive answers to — is untouched.
    replacement = state.get_or_create_slot(NAME)
    assert replacement is not original
    assert not replacement.linked_session_key, "the replacement must be unbound for this case"
    release.set()
    resp = await close

    assert resp.status == 500, (
        "an unrelated archive refusal coinciding with a divergent takeover was "
        "relabeled a settled takeover: the close reported success while the "
        "dismissal never landed"
    )
    assert _json(resp)["code"] == "history_save_failed"
    # The dismissal did NOT land, and the close said so — nothing claims it did.
    meta = state.conversation_log.get_metadata(linked)
    assert not meta.get("closed"), "a refused archive must not leave a closed flag"
    # The hand-over arm still preserved the original's rows on its own transcript.
    assert [m.get("content", "") for m in state.conversation_log.read_messages(linked)] == [
        "PERSISTED-1",
        "TAIL-2",
    ], "the failed close dropped the original's tail"
    assert state._slots.get(NAME) is replacement, "the replacement was clobbered by the close"


@pytest.mark.asyncio
async def test_a_delete_won_refusal_beside_a_takeover_does_not_resurrect_the_transcript(
    tmp_path, monkeypatch
) -> None:
    """A permanent delete outranks a coinciding takeover at the archive refusal.

    Two events land while the close's archive is in flight: a same-key
    takeover moves the teardown watch, and the session is permanently
    deleted. Testing the takeover branch first routes the refusal into the
    hand-over drain, whose append-safe arm writes through
    ``append_if_absent`` with no deletion guard — ``append`` recreates the
    missing file with a fresh metadata line, silently un-deleting what the
    user permanently removed. The deletion probe runs first: definitive
    delete evidence takes the archive-skipped exit, nothing is written, and
    the deleted transcript stays deleted.
    """
    from kiro_crew.dashboard.chat_utils import slot_history_key

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(NAME)
    for i in range(4):
        slot.append("user" if i % 2 == 0 else "assistant", f"ROW-{i}")
    slot.drain()
    assert await handlers.save_slot_off_loop(state, slot, force=True)
    # The hand-over shape whose drain takes the unguarded append-safe arm.
    slot.messages = list(slot.messages)[:2]
    slot._pending_rewrite = True
    slot.append("user", "TAIL-after-failed-rewrite")
    slot.drain()
    slot._dirty = True

    path = state.conversation_log._path(slot_history_key(slot))
    assert path.exists()
    real_save = handlers.save_slot_off_loop

    async def _delete_and_takeover_then_refuse(st, target, *a, **kw) -> bool:
        if kw.get("closed"):
            # Both events land while the archive holds dispatch: the delete
            # disposes of the transcript, and a same-key recreate publishes —
            # its takeover note moves the open teardown watch.
            st.conversation_log.delete_session(slot_history_key(target))
            st._slots.pop(NAME, None)
            st.get_or_create_slot(NAME)
            return False
        return await real_save(st, target, *a, **kw)

    monkeypatch.setattr(handlers, "save_slot_off_loop", _delete_and_takeover_then_refuse)

    resp = await handlers.api_chat_slot_delete(_Req(state, NAME))

    assert resp.status == 200, "a delete-won archive refusal is a completed close"
    assert not path.exists(), (
        "the close resurrected a permanently deleted transcript: the takeover "
        "drain's append recreated the file the delete removed"
    )


@pytest.mark.asyncio
async def test_handover_append_arm_does_not_resurrect_a_deleted_transcript(tmp_path) -> None:
    """The drain's append-safe arm carries its own delete witness, in-lock.

    The archive path's deletion probe runs before the drain dispatches, but
    two drain routes carry no probe at all (the shared-transcript pre-save
    exit among them), and the append-safe arm writes outside
    ``save_slot_off_loop`` — bypassing the save's delete-won guard — where
    ``append`` recreates a missing transcript with a fresh metadata line. A
    permanent delete landing before that arm's append must win: the witness
    inside ``atomic_appends`` sees the missing file against the slot's
    recorded identity, writes nothing, and reports the hand-over complete
    (the delete disposed of the rows; nothing is owed).
    """
    from kiro_crew.dashboard.chat_utils import slot_history_key
    from kiro_crew.dashboard.slot_registry import SlotRegistry

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(NAME)
    for i in range(4):
        slot.append("user" if i % 2 == 0 else "assistant", f"ROW-{i}")
    slot.drain()
    assert await handlers.save_slot_off_loop(state, slot, force=True)
    # The hand-over shape whose drain takes the append-safe arm.
    slot.messages = list(slot.messages)[:2]
    slot._pending_rewrite = True
    slot.append("user", "TAIL-after-failed-rewrite")
    slot.drain()
    slot._dirty = True

    history_key = slot_history_key(slot)
    path = state.conversation_log._path(history_key)

    # The probe-less route: a takeover moved the watch, and the permanent
    # delete landed after any caller-side probe could have run.
    basis = SlotRegistry.open_takeover_basis(state, NAME)
    state._slots.pop(NAME)
    state.get_or_create_slot(NAME)
    state.conversation_log.delete_session(history_key)
    assert not path.exists()
    try:
        drained = await handlers._persist_handover_tail(state, NAME, slot, takeover_basis=basis)
    finally:
        SlotRegistry.close_takeover_basis(state, basis)

    assert not path.exists(), (
        "the append-safe arm resurrected a permanently deleted transcript: "
        "its append recreated the file the delete removed"
    )
    assert drained.rows_committed is True, (
        "a delete-won hand-over owes nothing and must not be reported as a "
        "lost tail: the delete disposed of the transcript, rows included"
    )
