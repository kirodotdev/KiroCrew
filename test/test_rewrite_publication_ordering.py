"""Ordering of truncating history rewrites against same-key slot takeover.

The defect these pin: a truncating rewrite (rewind / edit-resend / regenerate /
switch-variant) decides its window on one slot object and commits on a worker
thread, while a same-name close-and-recreate pops ``state._slots[name]`` and
republishes through ``SlotRegistry.put_slot`` on the event loop — with no lock
between the two layers. The save's recreate-won map recheck runs at the locked
commit boundary, but it can only see replacements already PUBLISHED: a takeover
that begins after the recheck is invisible to any map re-read, and because the
save holds the per-session lock across its whole read-modify-write, the
replacement's own transcript read is simply blocked until the save releases —
after which it reads the truncated file and adopts a window missing the rows
the truncation dropped. Shrinking the recheck-to-write gap changes nothing:
the harm is the read landing after the replace, not the publication landing
before the recheck.

The contract under test is the truncation claim (``TruncationClaim``,
``slot_registry.py``): every truncating save registers a claim on its map key
for the whole write, every same-key takeover marks it (``put_slot`` and the
start of a resume's transcript read), and one mutex-guarded transition at the
save's commit gate decides the winner. A takeover that begins first makes the
save refuse with nothing written, so the takeover's read — serialized behind
the per-session lock — finds the transcript the truncation never touched. A
save that commits first leaves the claim ``committed``, and a later takeover
knowingly resumes the post-rewrite transcript. The interleaved schedules
therefore collapse onto the two sequential histories, which is what the race
tests below assert from both sides.

The interleaving is staged the way the sibling close-race suite stages its
teardown windows: a seam only the save's own progress reaches — here
``_frozen_prefix_and_foreign_appends``, which runs INSIDE the per-session
lock, after the recreate-won recheck and before the commit gate — parks the
worker on a ``threading.Event`` so the takeover can be
minted at an instant the map recheck has no way to observe. The
park is bounded on the test side so a save that never reaches the seam names
itself instead of running into the suite timeout.
"""

from __future__ import annotations

import asyncio
import threading

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_persistence
from kiro_crew.dashboard.chat_persistence import (
    rehydrate_slot_from_history_async,
    save_slot_off_loop,
)
from kiro_crew.dashboard.chat_utils import slot_history_key, slot_transcript_key
from kiro_crew.dashboard.slot_registry import SlotRegistry, TruncationClaim

NAME = "chat-1-1785"

#: Bounded waits for cross-thread coordination. Orders of magnitude more than
#: the single lock hop each needs, far under the suite's 120s ceiling, so a
#: miss fails the owning test with a named assertion rather than parking the
#: whole run (and on Windows, killing the xdist worker).
_WAIT = 5.0


def _state_with_saved_transcript(tmp_path):
    """A state whose slot has a four-row transcript already durable on disk."""
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(NAME)
    slot.append("user", "first question")
    slot.append("assistant", "first answer")
    slot.append("user", "second question")
    slot.append("assistant", "second answer")
    slot.drain()
    return state, slot


async def _persist_baseline(state, slot) -> None:
    assert await save_slot_off_loop(state, slot, force=True)
    on_disk = state.conversation_log.read_messages_chained(slot_transcript_key(NAME))
    assert len(on_disk) == 4


def _park_save_inside_lock(monkeypatch):
    """Park the truncating save between the map recheck and the commit gate.

    ``_frozen_prefix_and_foreign_appends`` runs inside ``_locked``, after the
    recreate-won recheck and before the commit gate — the exact stretch no map
    re-read can cover. The wrapper signals ``entered``, holds the worker on
    ``release``, then delegates to the real function so the payload assembly
    under test is unchanged. The wait is bounded so a test that never releases
    (its own assertion failed first) frees the worker instead of leaking a
    parked thread into the next test.
    """
    entered = threading.Event()
    release = threading.Event()
    real = chat_persistence._frozen_prefix_and_foreign_appends

    def _parked(*args, **kwargs):
        entered.set()
        release.wait(_WAIT)
        return real(*args, **kwargs)

    monkeypatch.setattr(chat_persistence, "_frozen_prefix_and_foreign_appends", _parked)

    # A refusal must leave no trace: the dropped-line archive runs only on the
    # winning side of the commit gate, because an archive written before a
    # refusal would record still-live rows as dropped. Recording the calls is
    # what lets the race tests assert that.
    archived: list = []
    real_archive = chat_persistence._archive_dropped_lines

    def _recording_archive(*args, **kwargs):
        archived.append(args)
        return real_archive(*args, **kwargs)

    monkeypatch.setattr(chat_persistence, "_archive_dropped_lines", _recording_archive)
    return entered, release, archived


def _dispatch_truncating_save(state, slot) -> asyncio.Task:
    """Start a guarded truncating save, shaped like the regenerate call site."""
    truncated = list(slot.messages)[:2]
    return asyncio.create_task(
        save_slot_off_loop(
            state,
            slot,
            truncated,
            expected_history_key=slot_history_key(slot),
            expected_slot_name=NAME,
        )
    )


async def _save_reached_park(entered: threading.Event, save: asyncio.Task) -> None:
    """Wait for the save to reach the in-lock seam, named on a miss."""
    if not await asyncio.to_thread(entered.wait, _WAIT):
        save.cancel()
        raise AssertionError(
            "the truncating save never reached its in-lock park seam: it refused or "
            "blocked before the in-lock stretch, so the takeover could not be staged "
            "inside the window under test"
        )


def _sole_claim(state) -> TruncationClaim:
    claims = [claim for held in state._truncation_claims.values() for claim in held]
    assert len(claims) == 1, claims
    return claims[0]


@pytest.mark.asyncio
async def test_resume_beginning_mid_write_makes_the_truncating_save_refuse(tmp_path, monkeypatch):
    """A resume that begins while the write holds the lock reads the full transcript.

    This is the schedule the recreate-won map recheck cannot cover: the save is
    already past it (parked at the archive seam) when the close pops the key and
    the resume begins. Without the claim the resume's read blocks behind the
    per-session lock, the save replaces the file, and the replacement adopts the
    truncated window. With it, the resume's takeover note marks the claim before
    the read is dispatched, the commit gate loses, and nothing is written.
    """
    state, slot = _state_with_saved_transcript(tmp_path)
    await _persist_baseline(state, slot)
    entered, release, archived = _park_save_inside_lock(monkeypatch)

    save = _dispatch_truncating_save(state, slot)
    await _save_reached_park(entered, save)
    # The map recheck already passed (the seam sits after it), so from here on
    # the claim is the only guard that can still see the takeover.
    state._slots.pop(NAME)

    resume = asyncio.create_task(rehydrate_slot_from_history_async(state, NAME))
    # One scheduling of the coroutine runs everything up to its first await —
    # including the takeover note — so after this hop the claim is provably
    # marked while the save still holds the lock.
    await asyncio.sleep(0)
    assert _sole_claim(state).outcome == "overtaken"
    release.set()

    assert await save is False
    replacement = await resume
    assert replacement is not None
    assert [m["content"] for m in replacement.messages] == [
        "first question",
        "first answer",
        "second question",
        "second answer",
    ]
    on_disk = state.conversation_log.read_messages_chained(slot_transcript_key(NAME))
    assert len(on_disk) == 4
    # A refused rewrite leaves no trace: no archive sidecar recorded rows as
    # dropped that the transcript still holds.
    assert archived == []
    # The claim scope retires its registration on every exit, refusal included,
    # and the dispatcher closes its watch the same way.
    assert state._truncation_claims == {}
    assert state._takeover_watches == {}


@pytest.mark.asyncio
async def test_publication_mid_write_makes_the_truncating_save_refuse(tmp_path, monkeypatch):
    """``put_slot`` itself marks the claim, for takeovers that publish without a read."""
    state, slot = _state_with_saved_transcript(tmp_path)
    await _persist_baseline(state, slot)
    entered, release, archived = _park_save_inside_lock(monkeypatch)

    save = _dispatch_truncating_save(state, slot)
    await _save_reached_park(entered, save)
    state._slots.pop(NAME)

    replacement = state.get_or_create_slot(NAME)
    assert _sole_claim(state).outcome == "overtaken"
    release.set()

    assert await save is False
    assert state._slots[NAME] is replacement
    on_disk = state.conversation_log.read_messages_chained(slot_transcript_key(NAME))
    assert len(on_disk) == 4
    assert archived == []
    assert state._truncation_claims == {}


@pytest.mark.asyncio
async def test_truncating_save_commits_when_no_takeover_interleaves(tmp_path, monkeypatch):
    """The claim never refuses a race-free rewrite, and leaves no residue behind."""
    state, slot = _state_with_saved_transcript(tmp_path)
    await _persist_baseline(state, slot)

    assert await save_slot_off_loop(
        state,
        slot,
        list(slot.messages)[:2],
        expected_history_key=slot_history_key(slot),
        expected_slot_name=NAME,
    )
    on_disk = state.conversation_log.read_messages_chained(slot_transcript_key(NAME))
    assert [m["content"] for m in on_disk] == ["first question", "first answer"]
    assert state._truncation_claims == {}


@pytest.mark.asyncio
async def test_takeover_after_commit_reads_the_rewritten_transcript(tmp_path, monkeypatch):
    """A takeover with no claim in flight resumes the post-rewrite transcript.

    The other half of the ordering contract: once the rewrite is durable, a
    later close-and-resume is the sequential history "rewrite, then reopen",
    and the takeover note against an empty claim table must change nothing.
    """
    state, slot = _state_with_saved_transcript(tmp_path)
    await _persist_baseline(state, slot)
    assert await save_slot_off_loop(
        state,
        slot,
        list(slot.messages)[:2],
        expected_history_key=slot_history_key(slot),
        expected_slot_name=NAME,
    )

    state._slots.pop(NAME)
    replacement = await rehydrate_slot_from_history_async(state, NAME)
    assert replacement is not None
    assert [m["content"] for m in replacement.messages] == ["first question", "first answer"]


@pytest.mark.asyncio
async def test_takeover_during_dispatch_makes_an_unguarded_truncating_save_refuse(
    tmp_path, monkeypatch
):
    """A takeover landing before the claim exists is observed through the generation.

    A pending-rewrite retry dispatched by the periodic flush carries no
    ``expected_slot_name``, so the map recheck never runs for it — and a
    takeover firing between its dispatch and the worker's claim registration
    has no claim to mark. The dispatch-time takeover generation is the guard:
    the registration compares it and a moved value leaves the claim born
    overtaken, so the commit gate refuses. Staged by parking the worker at the
    registration itself, after the basis was read on the loop, and running a
    full resume in the gap.
    """
    state, slot = _state_with_saved_transcript(tmp_path)
    await _persist_baseline(state, slot)

    entered = threading.Event()
    release = threading.Event()
    real_begin = SlotRegistry.begin_truncation_claim

    def _parked_begin(owner, name, *, basis=None):
        entered.set()
        release.wait(_WAIT)
        return real_begin(owner, name, basis=basis)

    monkeypatch.setattr(SlotRegistry, "begin_truncation_claim", staticmethod(_parked_begin))

    # No ``expected_slot_name``: the flush-shaped dispatch, guarded only by the
    # claim. The basis is read inside ``save_slot_off_loop`` before the hop.
    save = asyncio.create_task(save_slot_off_loop(state, slot, list(slot.messages)[:2]))
    if not await asyncio.to_thread(entered.wait, _WAIT):
        save.cancel()
        raise AssertionError(
            "the truncating save never reached its claim registration, so the takeover "
            "could not be staged inside the dispatch window under test"
        )

    state._slots.pop(NAME)
    replacement = await rehydrate_slot_from_history_async(state, NAME)
    release.set()

    assert await save is False
    assert replacement is not None
    assert len(replacement.messages) == 4
    on_disk = state.conversation_log.read_messages_chained(slot_transcript_key(NAME))
    assert len(on_disk) == 4
    assert state._truncation_claims == {}


@pytest.mark.asyncio
async def test_cancelled_dispatch_keeps_the_watch_open_for_its_running_worker(
    tmp_path, monkeypatch
):
    """A cancelled await does not retire the watch under a worker still saving.

    The executor cannot be interrupted: a client disconnect cancels the awaiting
    task while the worker runs on, and a watch retired at that moment would let
    a takeover in the still-open dispatch gap go unobserved. Ownership of the
    close therefore rides with the worker — the loop side releases the holder
    only when the worker provably never ran — so the takeover staged below is
    observed and the orphaned write still refuses.
    """
    state, slot = _state_with_saved_transcript(tmp_path)
    await _persist_baseline(state, slot)

    entered = threading.Event()
    release = threading.Event()
    real_begin = SlotRegistry.begin_truncation_claim

    def _parked_begin(owner, name, *, basis=None):
        entered.set()
        release.wait(_WAIT)
        return real_begin(owner, name, basis=basis)

    monkeypatch.setattr(SlotRegistry, "begin_truncation_claim", staticmethod(_parked_begin))

    save = asyncio.create_task(save_slot_off_loop(state, slot, list(slot.messages)[:2]))
    if not await asyncio.to_thread(entered.wait, _WAIT):
        save.cancel()
        raise AssertionError("the truncating save never reached its claim registration")

    save.cancel()
    with pytest.raises(asyncio.CancelledError):
        await save
    # The worker still runs under its watch: the cancelled await released
    # nothing, so the takeover below has something to move.
    assert NAME in state._takeover_watches

    state._slots.pop(NAME)
    replacement = state.get_or_create_slot(NAME)
    release.set()

    # The worker settles on its own schedule; the watch drains when it closes
    # its basis, and the orphaned truncation never reaches the file.
    for _ in range(int(_WAIT * 100)):
        if state._takeover_watches == {}:
            break
        await asyncio.sleep(0.01)
    assert state._takeover_watches == {}
    on_disk = state.conversation_log.read_messages_chained(slot_transcript_key(NAME))
    assert len(on_disk) == 4
    assert state._slots[NAME] is replacement
    assert state._truncation_claims == {}


def test_flush_pass_skips_a_slot_whose_key_changed_owner_mid_pass(tmp_path):
    """The periodic flush never saves a snapshot entry the map has stopped backing.

    The pass snapshots the slot table once and then pays real disk time per
    slot, so a same-key takeover can land between the snapshot and a later
    slot's turn — after the basis for that slot would be read, where the
    generation alone cannot help. The pass re-reads the live occupant at each
    slot's turn and skips a stale entry: the close flow that popped it owns
    its tail, and its window must not land on the replacement's transcript.
    """
    state = _make_state(tmp_path)
    decoy = state.get_or_create_slot("chat-2-1786")
    decoy.append("user", "decoy row")
    decoy.drain()
    victim = state.get_or_create_slot(NAME)
    victim.append("user", "victim row")
    victim.drain()

    flushed: list[str] = []
    real_flush = type(state).flush_slot_now

    def _swapping_flush(slot, takeover_basis=None):
        flushed.append(slot.key)
        if slot is decoy:
            # The takeover lands while the pass is busy with an earlier slot:
            # the victim's snapshot entry is stale from here on.
            state._slots.pop(NAME)
            state.get_or_create_slot(NAME)
        return real_flush(state, slot, takeover_basis=takeover_basis)

    state.flush_slot_now = _swapping_flush
    state._flush_dirty_slots()

    assert flushed == [decoy.key]
    on_disk = state.conversation_log.read_messages_chained(slot_transcript_key(NAME))
    assert on_disk == []


def test_takeover_note_marks_every_undecided_claim_and_only_those(tmp_path):
    """Two claims can be live for one key; a takeover loses both, once decided stays."""
    state = _make_state(tmp_path)
    first = SlotRegistry.begin_truncation_claim(state, NAME)
    second = SlotRegistry.begin_truncation_claim(state, NAME)
    assert SlotRegistry.commit_truncation_claim(state, first)

    SlotRegistry.note_same_key_takeover(state, NAME)
    # A committed decision is final in both directions: the takeover cannot
    # revoke it, and re-asking answers the same way.
    assert first.outcome == "committed"
    assert SlotRegistry.commit_truncation_claim(state, first)
    assert second.outcome == "overtaken"
    assert not SlotRegistry.commit_truncation_claim(state, second)

    SlotRegistry.retire_truncation_claim(state, NAME, first)
    assert state._truncation_claims == {NAME: [second]}
    SlotRegistry.retire_truncation_claim(state, NAME, second)
    assert state._truncation_claims == {}
    # Retiring an already-retired claim is harmless, mirroring construction marks.
    SlotRegistry.retire_truncation_claim(state, NAME, second)


def test_a_stale_basis_leaves_the_claim_born_overtaken(tmp_path):
    """The open watch bridges takeovers that fire before any claim exists."""
    state = _make_state(tmp_path)
    basis = SlotRegistry.open_takeover_basis(state, NAME)
    assert basis is not None
    SlotRegistry.note_same_key_takeover(state, NAME)

    stale = SlotRegistry.begin_truncation_claim(state, NAME, basis=basis)
    assert stale is not None
    assert stale.outcome == "overtaken"
    assert not SlotRegistry.commit_truncation_claim(state, stale)
    SlotRegistry.retire_truncation_claim(state, NAME, stale)
    SlotRegistry.close_takeover_basis(state, basis)

    # A basis opened after the takeover is current again: the next save is a
    # new dispatch that saw the post-takeover world, and it must not be
    # refused for history it never raced.
    fresh_basis = SlotRegistry.open_takeover_basis(state, NAME)
    fresh = SlotRegistry.begin_truncation_claim(state, NAME, basis=fresh_basis)
    assert fresh is not None
    assert fresh.outcome == ""
    assert SlotRegistry.commit_truncation_claim(state, fresh)
    SlotRegistry.retire_truncation_claim(state, NAME, fresh)
    SlotRegistry.close_takeover_basis(state, fresh_basis)
    assert state._truncation_claims == {}
    # The last holder out retires the key's watch: the table is bounded by
    # in-flight saves, not by the keys the process has published.
    assert state._takeover_watches == {}


def test_takeovers_of_unwatched_keys_leave_no_record(tmp_path):
    """The watch table stays empty for publications with no save in flight."""
    state = _make_state(tmp_path)
    state.get_or_create_slot(NAME)
    state.get_or_create_slot("chat-2-1786")
    state._slots.pop(NAME)
    state.get_or_create_slot(NAME)
    SlotRegistry.note_same_key_takeover(state, "chat-3-1787")
    assert state._takeover_watches == {}


def test_claim_helpers_tolerate_owners_without_a_claim_table():
    """Partial test doubles get the pre-contract shape, never an AttributeError.

    Owners are built attribute by attribute across the suite (the same reason
    ``_slots_under_construction`` is read through getattr), so every helper
    no-ops — and the commit gate fails OPEN, because an unguarded save is the
    established behavior for a state that carries no claim table.
    """
    owner = object()
    assert SlotRegistry.begin_truncation_claim(owner, NAME) is None
    assert SlotRegistry.open_takeover_basis(owner, NAME) is None
    SlotRegistry.close_takeover_basis(owner, None)
    SlotRegistry.note_same_key_takeover(owner, NAME)
    SlotRegistry.retire_truncation_claim(owner, NAME, None)
    assert SlotRegistry.commit_truncation_claim(owner, TruncationClaim())


@pytest.mark.asyncio
async def test_shutdown_saves_the_occupant_that_won_a_refused_key(tmp_path, monkeypatch):
    """A shutdown save refused by a takeover revisits and saves the winner.

    Shutdown has no later pass to lean on — the process exits after it — so a
    refusal must not leave the key's CURRENT occupant unsaved: its window is
    what the next startup restores.
    """
    state, slot = _state_with_saved_transcript(tmp_path)
    successor = object.__new__(type(slot))

    saved: list = []

    def _refusing_first(_state, target, **kwargs):
        saved.append(target)
        if target is slot:
            # The takeover lands during the save: the original's write loses
            # its key and the map now holds the winner.
            state._slots[NAME] = successor
            return False
        return True

    monkeypatch.setattr(chat_persistence, "_save_slot_to_history", _refusing_first)
    chat_persistence.save_all_slots_to_history(state)

    assert saved == [slot, successor]
    assert state._takeover_watches == {}


def _stage_pending_rewrite_with_tail(slot) -> None:
    """A rewind that never committed, then more conversation on top of it.

    The window shape the hand-over hazard needs: the truncation makes the
    save rewrite-shaped (``_pending_rewrite``), and the appended tail is real
    conversation the transcript does not hold yet — rows whose only remaining
    writer is the save under test.
    """
    slot.messages = list(slot.messages)[:2]
    slot._pending_rewrite = True
    slot.append("user", "TAIL-after-failed-rewrite")
    slot.drain()
    slot._dirty = True


@pytest.mark.asyncio
async def test_refused_handover_save_lands_the_tail_ahead_of_the_takeover_read(
    tmp_path, monkeypatch
):
    """A takeover read queued behind a refused hand-over save hydrates WITH the tail.

    The ordering hazard: a ``_pending_rewrite`` hand-over save is refused at
    the commit gate while the takeover's transcript read waits on the same
    per-session lock. An append deferred to the loop-side drain arm runs after
    that read acquires the lock, so the replacement publishes a window without
    the tail rows — and its own later truncating rewrite rebuilds the file
    without collecting foreign appends, dropping them for good. The commit
    gate's append-safe fallback closes it: the refused save lands the tail
    id-deduped BEFORE releasing the lock, so the queued read finds a transcript
    that already carries the rows and the replacement's window includes them.
    """
    state, slot = _state_with_saved_transcript(tmp_path)
    await _persist_baseline(state, slot)
    _stage_pending_rewrite_with_tail(slot)
    entered, release, archived = _park_save_inside_lock(monkeypatch)

    # Shaped like the hand-over drain's dispatch: no explicit snapshot, the
    # rewrite shape comes from ``_pending_rewrite`` alone.
    save = asyncio.create_task(
        save_slot_off_loop(
            state,
            slot,
            closed=False,
            best_effort=False,
            expected_history_key=slot_history_key(slot),
            rows_only=True,
        )
    )
    await _save_reached_park(entered, save)
    state._slots.pop(NAME)

    resume = asyncio.create_task(rehydrate_slot_from_history_async(state, NAME))
    # One scheduling runs the resume up to its first await — the takeover note
    # marks the claim, and the read is dispatched to queue behind the lock the
    # parked save still holds.
    await asyncio.sleep(0)
    assert _sole_claim(state).outcome == "overtaken"
    release.set()

    # The truncation still loses: nothing rebuilt, no archive residue.
    assert await save is False
    assert archived == []

    # The core of the contract: the read that was queued behind the refused
    # save observes the tail rows the fallback appended under its lock, so
    # the replacement's OWN WINDOW carries them — a later truncating rewrite
    # by the replacement keeps them as window rows.
    replacement = await resume
    assert replacement is not None
    contents = [m["content"] for m in replacement.messages]
    assert "TAIL-after-failed-rewrite" in contents, (
        "the replacement hydrated a window without the tail rows the refused "
        "save owed: the append-safe fallback did not run ahead of the read"
    )
    for expected in ("first question", "first answer", "second question", "second answer"):
        assert expected in contents, "the lost truncation must not drop on-disk rows"
    on_disk = [
        m["content"]
        for m in state.conversation_log.read_messages_chained(slot_transcript_key(NAME))
    ]
    assert "TAIL-after-failed-rewrite" in on_disk
    assert len(on_disk) == 5
    assert state._truncation_claims == {}


@pytest.mark.asyncio
async def test_drain_reports_a_mid_save_takeover_refusal_as_the_append_it_becomes(
    tmp_path, monkeypatch
):
    """A drain whose save loses to a MID-SAVE takeover verifies the rows, not the loss.

    The basis check at the drain's dispatch can only see takeovers that already
    moved the watch; one that fires while the rows-only save is in flight
    refuses at the commit gate instead. That refusal is not a loss — the gate's
    fallback landed the rows under its lock — so the drain must route it to the
    append-safe arm (idempotent, verifying) rather than report the close's tail
    as unrecoverable.
    """
    from kiro_crew.dashboard import chat_handlers as handlers

    state, slot = _state_with_saved_transcript(tmp_path)
    await _persist_baseline(state, slot)
    _stage_pending_rewrite_with_tail(slot)
    entered, release, archived = _park_save_inside_lock(monkeypatch)

    basis = SlotRegistry.open_takeover_basis(state, NAME)
    try:
        drain = asyncio.create_task(
            handlers._persist_handover_tail(state, NAME, slot, takeover_basis=basis)
        )
        await _save_reached_park(entered, drain)
        # The takeover publishes while the drain's save holds the lock: the
        # claim is marked, the watch moves, and the dispatch-time basis check
        # is already behind us.
        state._slots.pop(NAME)
        replacement = state.get_or_create_slot(NAME)
        assert _sole_claim(state).outcome == "overtaken"
        release.set()

        drained = await drain
    finally:
        SlotRegistry.close_takeover_basis(state, basis)

    assert drained.rows_committed is True, (
        "a takeover-refused drain save whose rows the commit gate landed "
        "append-safely must not be reported as a lost tail"
    )
    assert state._slots[NAME] is replacement
    assert archived == []
    on_disk = [
        m["content"]
        for m in state.conversation_log.read_messages_chained(slot_transcript_key(NAME))
    ]
    assert "TAIL-after-failed-rewrite" in on_disk
    assert len(on_disk) == 5
    assert state._truncation_claims == {}


@pytest.mark.asyncio
async def test_resume_waits_for_a_save_still_in_dispatch_under_an_open_watch(tmp_path, monkeypatch):
    """A resume cannot outrun a takeover-losing save whose claim does not exist yet.

    The dispatch gap: a teardown opens its watch, submits the save, and the
    takeover note bumps the watch before the worker registers its claim — so a
    claims-only wait sees nothing pending, the resume reads, and the born-
    overtaken claim's refused save appends the tail behind the hydrated
    window. The barrier therefore waits on the WATCH through retirement:
    staged by parking the worker at the claim registration itself, with the
    resume dispatched mid-park.
    """
    from kiro_crew.dashboard import chat_handlers as handlers

    state, slot = _state_with_saved_transcript(tmp_path)
    await _persist_baseline(state, slot)
    _stage_pending_rewrite_with_tail(slot)

    entered = threading.Event()
    release = threading.Event()
    real_begin = SlotRegistry.begin_truncation_claim

    def _parked_begin(owner, name, **kwargs):
        entered.set()
        release.wait(_WAIT)
        return real_begin(owner, name, **kwargs)

    monkeypatch.setattr(SlotRegistry, "begin_truncation_claim", staticmethod(_parked_begin))

    basis = SlotRegistry.open_takeover_basis(state, NAME)
    try:
        drain = asyncio.create_task(
            handlers._persist_handover_tail(state, NAME, slot, takeover_basis=basis)
        )
        if not await asyncio.to_thread(entered.wait, _WAIT):
            drain.cancel()
            raise AssertionError("the drain save never reached its claim registration")
        # The save is parked BEFORE its claim exists. The takeover lands now:
        # the note bumps the open watch (nothing else records it), and the
        # resume's read is dispatched while the claim table is still empty.
        state._slots.pop(NAME)
        resume = asyncio.create_task(rehydrate_slot_from_history_async(state, NAME))
        await asyncio.sleep(0.1)
        assert not resume.done(), (
            "the resume read proceeded while a takeover-losing save was still in "
            "dispatch under an open watch: the barrier must wait for the watch, "
            "not only for registered claims"
        )
        release.set()
        drained = await drain
        # The dispatcher's close, exactly where the teardown call sites put
        # it: once the save's outcome is in hand. The watch retiring is what
        # frees the waiting resume — which is the assertion: the read was
        # held through the whole dispatch-to-retirement span.
        SlotRegistry.close_takeover_basis(state, basis)
        replacement = await resume
    finally:
        release.set()
        SlotRegistry.close_takeover_basis(state, basis)

    assert drained.rows_committed is True
    assert replacement is not None
    contents = [m["content"] for m in replacement.messages]
    assert "TAIL-after-failed-rewrite" in contents, (
        "the replacement hydrated a window without the tail the born-overtaken " "save appended"
    )
    assert state._truncation_claims == {}


@pytest.mark.asyncio
async def test_cancelled_caller_leaves_its_watch_open_for_the_running_worker(tmp_path, monkeypatch):
    """Cancellation after submission transfers the caller's basis to the worker.

    The caller's unwind reaches its ``finally`` close immediately, while the
    shielded worker may still be pre-registration under the watch — retiring
    it there leaves a takeover in that gap unrecorded, and the orphaned
    truncation commits over the takeover's transcript. Ownership transfers
    instead: the caller's close is a no-op and the watch retires only when
    the worker settles.
    """
    state, slot = _state_with_saved_transcript(tmp_path)
    await _persist_baseline(state, slot)
    _stage_pending_rewrite_with_tail(slot)

    entered = threading.Event()
    release = threading.Event()
    real_save = chat_persistence._save_slot_to_history

    def _parked_save(*args, **kwargs):
        entered.set()
        release.wait(_WAIT)
        return real_save(*args, **kwargs)

    monkeypatch.setattr(chat_persistence, "_save_slot_to_history", _parked_save)

    basis = SlotRegistry.open_takeover_basis(state, NAME)

    async def _caller() -> bool:
        try:
            return await save_slot_off_loop(
                state,
                slot,
                closed=False,
                best_effort=False,
                expected_history_key=slot_history_key(slot),
                rows_only=True,
                takeover_basis=basis,
            )
        finally:
            # The dispatcher's own unwind close, exactly as the teardown
            # call sites are shaped.
            SlotRegistry.close_takeover_basis(state, basis)

    caller = asyncio.create_task(_caller())
    assert await asyncio.to_thread(entered.wait, _WAIT), "the worker never started"
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller

    # The caller is gone, its finally ran — and the watch MUST still be open,
    # because the worker is still pre-completion under it. A takeover arriving
    # now is recorded through this watch.
    assert NAME in state._takeover_watches, (
        "the cancelled caller retired its watch while the shielded worker was "
        "still running: a takeover in this window goes unrecorded"
    )
    state._slots.pop(NAME)
    state.get_or_create_slot(NAME)

    release.set()
    # The worker settles, its future's done-callback releases the transferred
    # basis, and the watch retires. Bounded poll on the loop.
    for _ in range(int(_WAIT / 0.01)):
        if NAME not in state._takeover_watches:
            break
        await asyncio.sleep(0.01)
    assert (
        NAME not in state._takeover_watches
    ), "the transferred basis was never released after the worker settled"
    # The takeover was recorded: the save refused rather than committing an
    # orphaned truncation over the replacement's transcript.
    on_disk = [
        m["content"]
        for m in state.conversation_log.read_messages_chained(slot_transcript_key(NAME))
    ]
    for expected in ("first question", "first answer", "second question", "second answer"):
        assert expected in on_disk, "the orphaned truncation must not have committed"


@pytest.mark.asyncio
async def test_append_safe_fallback_persists_the_complete_row(tmp_path, monkeypatch):
    """The refused save's fallback appends the row the save would have written.

    A (role, content) reconstruction drops the row's ``ts``, provenance,
    ``variants`` and ``meta`` from its only durable copy. The fallback rides
    the fully built entries instead, so the appended tail is byte-shaped like
    the committed payload would have been.
    """
    state, slot = _state_with_saved_transcript(tmp_path)
    await _persist_baseline(state, slot)
    slot.messages = list(slot.messages)[:2]
    slot._pending_rewrite = True
    slot.append("assistant", "TAIL-primary")
    tail = slot.messages[-1]
    tail["variants"] = [{"content": "TAIL-primary"}, {"content": "TAIL-alternate"}]
    tail["variant_idx"] = 0
    tail_ts = tail.get("ts")
    slot.drain()
    slot._dirty = True

    entered, release, archived = _park_save_inside_lock(monkeypatch)
    save = asyncio.create_task(
        save_slot_off_loop(
            state,
            slot,
            closed=False,
            best_effort=False,
            expected_history_key=slot_history_key(slot),
            rows_only=True,
        )
    )
    await _save_reached_park(entered, save)
    state._slots.pop(NAME)
    resume = asyncio.create_task(rehydrate_slot_from_history_async(state, NAME))
    await asyncio.sleep(0)
    assert _sole_claim(state).outcome == "overtaken"
    release.set()

    assert await save is False
    replacement = await resume
    assert replacement is not None

    on_disk = state.conversation_log.read_messages_chained(slot_transcript_key(NAME))
    appended = [m for m in on_disk if m.get("content") == "TAIL-primary"]
    assert len(appended) == 1
    row = appended[0]
    assert [v.get("content") for v in row.get("variants", [])] == [
        "TAIL-primary",
        "TAIL-alternate",
    ], "the appended row must carry the variants the save would have written"
    assert row.get("variant_idx") == 0
    if tail_ts:
        assert row.get("ts") == tail_ts, "the appended row must keep its own ts"
    # The hydrated replacement carries the full row too, variants included.
    hydrated = [m for m in replacement.messages if m.get("content") == "TAIL-primary"]
    assert hydrated and hydrated[0].get(
        "variants"
    ), "the replacement's window must hydrate the appended row with its variants"


@pytest.mark.asyncio
async def test_resume_refuses_rather_than_reading_unsettled_history_on_timeout(
    tmp_path, monkeypatch
):
    """An expired ordering wait fails CLOSED: retryable error, not a stale hydrate.

    A wait that expires with the ordering still unsettled must not read anyway:
    the hydrated window would be missing the tail a refused save appends behind
    it, and a later truncating rewrite by the hydrated replacement deletes that
    tail silently. The resume raises the retryable lock-contention error the
    read layer already speaks, and succeeds normally once the ordering settles.
    """
    state, slot = _state_with_saved_transcript(tmp_path)
    await _persist_baseline(state, slot)
    # Collapse the wait deadline to "already expired" so the refusal is
    # observed without holding the suite for the real contention budget.
    monkeypatch.setattr(chat_persistence, "_FLOCK_ACQUIRE_TIMEOUT_S", -2.0)

    # A truncating save "in dispatch" that never settles within the deadline:
    # its open watch is exactly the unsettled ordering the barrier reports.
    basis = SlotRegistry.open_takeover_basis(state, NAME)
    try:
        state._slots.pop(NAME)
        with pytest.raises(chat_persistence.HistoryLockTimeout):
            await rehydrate_slot_from_history_async(state, NAME)
        assert NAME not in state._slots, "a refused resume must publish nothing"
    finally:
        SlotRegistry.close_takeover_basis(state, basis)

    # Settled: the same resume now hydrates normally.
    replacement = await rehydrate_slot_from_history_async(state, NAME)
    assert replacement is not None
    assert [m["content"] for m in replacement.messages] == [
        "first question",
        "first answer",
        "second question",
        "second answer",
    ]
