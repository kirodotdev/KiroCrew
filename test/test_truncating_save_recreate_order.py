"""Ordering between a truncating history save and the retraction of its slot.

The defect these pin: a truncating save (regenerate, switch-variant,
edit-resend) commits on a worker thread while holding the transcript lock, and
re-reads the slot map inside that lock to confirm it still owns the key. The map
is event-loop state, so a retraction landing between that re-read and the
write's rename is invisible to it: the truncated snapshot commits, and a
same-name replacement resuming the same transcript inherits the truncation. The
replacement's own saves then carry the truncated window forward, so the loss is
durable, unlike a stale ``closed`` flag, which the next full save drops and a
resume clears.

Neither ``created_at`` nor ``tab_id`` can answer the commit-time question
("is this slot still the incarnation that owns this transcript?"), because a
same-transcript recreate preserves both. Nor can any predicate read from the
file: the retraction writes nothing to the transcript before the racing save
commits, so there is nothing on disk to compare against.

So the ordering is inverted instead. The retraction waits for the write, rather
than the write trying to observe the retraction: the close fences the slot
synchronously, then waits for the write's executor future before it retracts the
key. The wait is on the FUTURE and not on ``_metadata_persist_inflight``,
because that counter is released in the awaiting coroutine's ``finally`` and so
reads clear the moment a handler is cancelled, with its worker thread still on
its way to the rename. The wait is bounded, and a breach REFUSES the close: the
tab stays open and can be closed again, where retracting the name early is
unrecoverable.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_fork
from kiro_crew.dashboard import chat_handlers as handlers
from kiro_crew.dashboard import chat_persistence, chat_regenerate, chat_rewind
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.dashboard_persistence import DashboardPersistenceCoordinator

NAME = "chat-1-9905"


def _state_with_slot(tmp_path, name: str = NAME):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(name)
    slot.append("user", "first question")
    slot.append("assistant", "first answer")
    slot.append("user", "second question")
    slot.append("assistant", "second answer")
    slot.drain()
    return state


def _register_pending_write(slot) -> asyncio.Future:
    """Publish one never-resolved guarded write, the way the saver publishes its own.

    ``save_slot_off_loop`` adds the executor future to this set and discards it
    from a done callback. A future created here and left pending stands in for a
    worker thread that has not reached its rename.
    """
    pending: asyncio.Future = asyncio.get_running_loop().create_future()
    writes = slot._guarded_history_writes
    writes.add(pending)
    pending.add_done_callback(writes.discard)
    return pending


async def _settle(turns: int = 6) -> None:
    """Give the loop enough turns for a close task to run to its first block."""
    for _ in range(turns):
        await asyncio.sleep(0)


async def _await_flag(flag, timeout: float = 10.0) -> bool:
    """Wait for a threading flag from the loop without occupying an executor slot.

    ``asyncio.to_thread`` would take a worker from the same default executor the
    save under test is holding, so it is deliberately not used here.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while not flag.is_set():
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.01)
    return True


@pytest.mark.asyncio
async def test_close_waits_for_a_guarded_history_write(tmp_path) -> None:
    """The key stays published until the guarded write leaves its commit window.

    Red without the wait: the close reaches its retraction while the write is
    still running, which is exactly the interleaving that lands a truncated
    snapshot on whatever adopts the key next.
    """
    state = _state_with_slot(tmp_path)
    slot = state.get_slot(NAME)
    assert slot is not None
    pending = _register_pending_write(slot)

    closing = asyncio.ensure_future(handlers.close_slot(state, slot, NAME))
    try:
        await _settle()
        assert closing.done() is False
        assert (
            state.get_slot(NAME) is slot
        ), "the close retracted the key while a guarded write was still running"

        pending.set_result(True)
        await asyncio.wait_for(closing, timeout=10)
    finally:
        if not closing.done():
            closing.cancel()
    assert state.get_slot(NAME) is None


@pytest.mark.asyncio
async def test_a_cancelled_saver_leaves_its_write_visible_to_the_close(
    tmp_path, monkeypatch
) -> None:
    """A cancelled handler must not hide a worker thread that is still writing.

    This is the whole reason the wait reads futures rather than the counter. The
    counter is released in the saver's own ``finally``, so cancelling the saver
    drops it to zero while the worker runs on; the future stays pending until the
    thread returns. A close that trusted the counter here would retract the key
    mid-write.
    """
    state = _state_with_slot(tmp_path)
    slot = state.get_slot(NAME)
    assert slot is not None

    started = threading.Event()
    release = threading.Event()

    def _blocking_save(*args, **kwargs):
        started.set()
        release.wait(10)
        return True

    monkeypatch.setattr(chat_persistence, "_save_slot_to_history", _blocking_save)

    saving = asyncio.ensure_future(
        chat_persistence.save_slot_off_loop(
            state, slot, expected_history_key=chat_persistence.slot_history_key(slot)
        )
    )
    try:
        assert await _await_flag(started), "the save never reached its worker thread"
        saving.cancel()
        with pytest.raises(asyncio.CancelledError):
            await saving

        assert (
            getattr(slot, "_metadata_persist_inflight", 0) == 0
        ), "the counter is expected to read clear here; that is why it cannot be trusted"
        assert any(
            not write.done() for write in slot._guarded_history_writes
        ), "the cancelled saver hid a worker thread that is still writing"
    finally:
        release.set()

    # And the write becomes invisible again only once the worker has returned.
    for _ in range(1000):
        if not slot._guarded_history_writes:
            break
        await asyncio.sleep(0.01)
    assert not slot._guarded_history_writes


@pytest.mark.asyncio
async def test_close_is_refused_when_a_write_will_not_finish(tmp_path, monkeypatch) -> None:
    """A write that outlasts the ceiling refuses the close instead of proceeding.

    Proceeding would retract the name with a worker short of its rename, and
    nothing retries or self-corrects on that path. Refusing is recoverable: the
    tab stays open, the fence is released, and the person can close it again.
    """
    monkeypatch.setattr(handlers, "_GUARDED_WRITE_WAIT_SECS", 0.05)
    state = _state_with_slot(tmp_path)
    slot = state.get_slot(NAME)
    assert slot is not None
    pending = _register_pending_write(slot)

    with pytest.raises(handlers.SlotCloseError) as raised:
        await asyncio.wait_for(handlers.close_slot(state, slot, NAME), timeout=10)

    assert raised.value.code == "history_write_running"
    assert raised.value.status == 500
    assert state.get_slot(NAME) is slot, "a refused close must leave the tab open"
    assert slot.is_closing is False, "a refused close must release its admission fence"
    pending.cancel()


def test_drain_reports_promptly_when_no_guarded_write_is_registered(tmp_path) -> None:
    """The common case costs no suspension: nothing registered, nothing awaited."""
    state = _state_with_slot(tmp_path)
    slot = state.get_slot(NAME)
    assert slot is not None
    assert slot._guarded_history_writes == set()

    coro = handlers._await_guarded_history_write(slot, NAME)
    with pytest.raises(StopIteration) as raised:
        # A coroutine that never awaits completes on its first send, which is the
        # property under test: an idle slot pays nothing for the wait.
        coro.send(None)
    assert raised.value.value is True


# Every case ``_destructive_history_busy`` must answer, with its verdict. The
# teardown arm is the one this change adds; the others are enumerated so a future
# edit cannot quietly drop one. Driven through a stand-in because the real slot
# derives ``turn_running`` and ``running`` from its task and stage boundary and
# exposes them read-only. The guard reads three predicates and nothing else, and
# the real slot's own teardown arm is covered by the two fence tests below.
class _Predicates:
    """The three predicates ``_destructive_history_busy`` reads, and no more."""

    def __init__(self, *, turn_running: bool, running: bool, is_closing: bool) -> None:
        self.turn_running = turn_running
        self.running = running
        self.is_closing = is_closing


@pytest.mark.parametrize(
    ("turn_running", "running", "closing", "expected_code"),
    [
        (False, False, False, None),
        (True, True, False, "slot_running"),
        (False, True, False, "slot_busy"),
        (False, False, True, "slot_closing"),
        # Precedence: a running turn outranks a teardown, so an operator reading
        # the refusal sees the condition that started first.
        (True, True, True, "slot_running"),
        (False, True, True, "slot_busy"),
    ],
)
def test_destructive_history_refusal_covers_every_owner(
    turn_running, running, closing, expected_code
) -> None:
    slot = _Predicates(turn_running=turn_running, running=running, is_closing=closing)

    refusal = chat_regenerate._destructive_history_busy(slot)
    if expected_code is None:
        assert refusal is None
        return
    assert refusal is not None
    assert refusal.status == 409
    assert expected_code.encode() in refusal.body


def test_an_aborted_close_readmits_history_mutation(tmp_path) -> None:
    """The fence is scoped to a teardown that happens, not to the attempt.

    A close can fail and leave the slot live (a write that will not finish, a
    nudge that will not retire, a pre-pop re-check that refuses). Releasing the
    fence on that path is what keeps a failed close from wedging regenerate on a
    tab the user still has.
    """
    state = _state_with_slot(tmp_path)
    slot = state.get_slot(NAME)
    assert slot is not None

    slot.begin_close()
    assert chat_regenerate._destructive_history_busy(slot) is not None

    slot.cancel_close()
    assert chat_regenerate._destructive_history_busy(slot) is None


def test_only_the_shared_close_path_waits_for_the_guarded_write(tmp_path) -> None:
    """Residual, pinned at today's answer: which retractions wait, and which do not.

    ``close_slot`` is the only retraction that WAITS, because it is the only one
    obliged to finish: the person asked for it, through the tab dismissal, the
    slot-delete endpoint or session control.

    The bulk stale-slot sweep pops each slot itself and is fenced too, but it
    defers instead of waiting -- a pending guarded write is proof the tab is not
    idle, whatever its last recorded activity says, and waiting would hold the
    sweep open exactly while the tab is being edited. Its own pin covers that.

    These neither wait nor fence:

    * the history-materialise path publishes a slot rebuilt from disk rather
      than retracting a live one;
    * the history-delete handler pops the live slot itself, after unlinking the
      transcript. A guarded write racing that pop cannot resurrect what the
      unlink removed: ``delete_session`` unlinks inside the same transcript
      ``_locked`` region the save takes, and the delete-won guard refuses a save
      whose file is gone when the slot has observed that file before. The write
      commits nothing, so there is nothing for a replacement to inherit.

    This asserts the current arrangement so widening it is a deliberate edit, not
    a silent drift.
    """
    del tmp_path
    source = Path(handlers.__file__).read_text(encoding="utf-8")
    call = "await _await_guarded_history_write("
    assert source.count(call) == 1, "the wait's call sites changed"

    lines = source.splitlines()
    enclosing = []
    for index, line in enumerate(lines):
        if call not in line:
            continue
        enclosing.append(
            next(
                lines[i].split("(")[0].strip()
                for i in range(index, -1, -1)
                if lines[i].startswith("async def ") or lines[i].startswith("def ")
            )
        )

    assert enclosing == ["async def _close_slot"], enclosing


@pytest.mark.asyncio
async def test_a_fenced_slot_refuses_a_guarded_dispatch(tmp_path, monkeypatch) -> None:
    """The third leg: a handler that passed its own check before the fence went up.

    ``_destructive_history_busy`` is read once, early, and the awaits that follow
    it are where a close can fence the slot. Such a handler reaches the dispatch
    seam after the close has finished waiting, so there is nothing left for the
    retraction to order against: it would pop the name with a worker thread on
    its way to the rename. Re-reading the fence at the seam is what makes the
    pair decidable, and refusing writes nothing.
    """
    state = _state_with_slot(tmp_path)
    slot = state.get_slot(NAME)
    assert slot is not None
    key = slot_history_key(slot)

    dispatched: list[str] = []

    def _never(*args, **kwargs):
        dispatched.append("called")
        return True

    monkeypatch.setattr(chat_persistence, "_save_slot_to_history", _never)

    slot.begin_close()
    saved = await chat_persistence.save_slot_off_loop(
        state,
        slot,
        list(slot.messages),
        best_effort=False,
        expected_history_key=key,
    )

    assert saved is False, "a guarded write must be refused while the slot is fenced"
    assert dispatched == [], "the worker must never be dispatched"
    assert not slot._guarded_history_writes, "a refused write must register nothing"


@pytest.mark.asyncio
async def test_a_fenced_slot_still_runs_the_closes_own_archival_save(tmp_path, monkeypatch) -> None:
    """The refusal is scoped to guarded writes, so the close can still archive.

    The close fences the slot and then saves the full window itself. That save
    carries no authorized transcript key, which is what tells it apart from a
    truncating one: refusing it would drop the very transcript the close is
    preserving.
    """
    state = _state_with_slot(tmp_path)
    slot = state.get_slot(NAME)
    assert slot is not None

    dispatched: list[str] = []

    def _record(*args, **kwargs):
        dispatched.append("called")
        return True

    monkeypatch.setattr(chat_persistence, "_save_slot_to_history", _record)

    slot.begin_close()
    saved = await chat_persistence.save_slot_off_loop(
        state, slot, closed=True, closed_at=1.0, best_effort=False
    )

    assert saved is True
    assert dispatched == ["called"], "the close's own archival save must still run"


def test_the_fence_read_and_the_registration_share_one_synchronous_block() -> None:
    """What makes the pair decidable: no suspension between the read and the register.

    The refusal above and the registration it guards must run without an
    intervening ``await``. With one, a third interleaving appears in which the
    fence goes up after the read and before the write becomes visible, which is
    the window this whole ordering exists to remove.
    """
    source = Path(chat_persistence.__file__).read_text(encoding="utf-8")
    lines = source.splitlines()
    start = next(i for i, line in enumerate(lines) if 'getattr(slot, "is_closing", False)' in line)
    end = next(i for i in range(start, len(lines)) if "_register_guarded_write(save)" in lines[i])
    awaits = [
        line.strip()
        for line in lines[start:end]
        if not line.strip().startswith("#") and "await " in line
    ]
    # The one await in between belongs to the unguarded early return, which a
    # guarded write never reaches. Any other await is a real suspension point.
    assert awaits == [
        "return await save"
    ], "an await between the fence read and the registration reopens the window"


def test_the_registration_helper_publishes_the_write_then_clears_it() -> None:
    """The registry is what makes a write waitable, so both halves must hold.

    Publishing without clearing leaves a resolved future in the set forever and
    every later close waits for it; clearing without publishing means the close
    sees an empty registry and pops the name while the thread writes.
    """

    class _Slot:
        key = "chat-1-9905"

    slot = _Slot()
    loop = asyncio.new_event_loop()
    try:
        future: asyncio.Future[bool] = loop.create_future()
        chat_persistence.register_guarded_history_write(slot, future)
        assert future in slot._guarded_history_writes, "the write must be visible to a close"

        future.set_result(True)
        loop.run_until_complete(asyncio.sleep(0))
        assert not slot._guarded_history_writes, "a finished write must clear itself"
    finally:
        loop.close()


def test_rewinds_own_truncating_save_registers_for_the_close_wait() -> None:
    """Rewind is the one truncating save that does not go through the helper.

    It dispatches ``_save_slot_to_history`` with an authorized transcript key
    straight onto a thread, so unless it registers itself the close's wait sees
    an empty registry, pops the name, and the rewrite commits onto the
    transcript the replacement adopted. The registration must also come before
    the await on the save, or the close can pop during that await.
    """
    source = Path(chat_rewind.__file__).read_text(encoding="utf-8")
    lines = source.splitlines()

    call = "register_guarded_history_write(slot, save_task)"
    assert source.count(call) == 1, "rewind registers its truncating save exactly once"

    register_at = next(i for i, line in enumerate(lines) if call in line)
    dispatch_at = next(
        i for i, line in enumerate(lines) if "save_task = asyncio.ensure_future(" in line
    )
    await_at = next(i for i, line in enumerate(lines) if "await asyncio.shield(save_task)" in line)
    assert dispatch_at < register_at < await_at, (dispatch_at, register_at, await_at)

    fence_at = max(i for i in range(register_at) if "if slot.is_closing:" in lines[i])
    between = [
        line.strip()
        for line in lines[fence_at:register_at]
        if not line.strip().startswith("#") and "await " in line
    ]
    assert between == [], f"an await between rewind's fence read and its registration: {between}"


def test_rewind_is_refused_at_admission_while_the_slot_is_fenced() -> None:
    """Two reads of the fence, for the two ways a rewind meets a close.

    The admission read turns away a rewind that arrives after the close began.
    The read at the dispatch seam catches the other order: a rewind admitted
    first, which then suspends long enough for the close to fence the slot and
    finish waiting. One read alone leaves the other case open.
    """
    source = Path(chat_rewind.__file__).read_text(encoding="utf-8")
    lines = source.splitlines()

    fences = [i for i, line in enumerate(lines) if "if slot.is_closing:" in line]
    assert len(fences) == 2, f"rewind reads the close fence twice, found {len(fences)}"

    admission_at = next(
        i
        for i, line in enumerate(lines)
        if 'return web.json_response({"error": "slot is running"}, status=409)' in line
    )
    assert fences[0] > admission_at, "the admission fence sits beside the running check"
    assert source.count('"code": "slot_closing"') >= 1, "a refused rewind names the fence"


@pytest.mark.asyncio
async def test_a_fenced_slot_admits_the_retractions_own_drain(tmp_path, monkeypatch) -> None:
    """The fence asks whether a write races a retraction, so the drain is exempt.

    The handover drain runs inside the close that raised the fence, and it is the
    last chance the original slot's unsaved rows have to reach disk: nothing in
    the process visits that object again once the name is handed over. Refusing it
    would drop exactly the rows it exists to save.
    """
    state = _state_with_slot(tmp_path)
    slot = state.get_slot(NAME)
    assert slot is not None
    key = slot_history_key(slot)

    dispatched: list[str] = []

    def _record(*args, **kwargs):
        dispatched.append("called")
        return True

    monkeypatch.setattr(chat_persistence, "_save_slot_to_history", _record)

    slot.begin_close()
    saved = await chat_persistence.save_slot_off_loop(
        state,
        slot,
        list(slot.messages),
        best_effort=False,
        expected_history_key=key,
        rows_only=True,
        issued_by_the_retraction=True,
    )

    assert saved is True, "the retraction's own drain must not be refused by its own fence"
    assert dispatched == ["called"], "the drain must reach the worker"


def test_only_the_handover_drain_opts_out_of_the_fence() -> None:
    """An exemption that anything may pass is not a fence.

    The drain is exempt because the close sequences it. Any other caller passing
    the same flag would be a truncating write dispatched after the close stopped
    waiting, which is the whole defect. One call site keeps that reviewable.
    """
    flag = "issued_by_the_retraction=True"
    hits = []
    for module in (handlers, chat_persistence, chat_regenerate, chat_rewind):
        source = Path(module.__file__).read_text(encoding="utf-8")
        hits.extend([Path(module.__file__).name] * source.count(flag))

    assert hits == ["chat_handlers.py"], f"the fence exemption gained a call site: {hits}"

    source = Path(handlers.__file__).read_text(encoding="utf-8")
    lines = source.splitlines()
    flag_at = next(i for i, line in enumerate(lines) if flag in line)
    enclosing = next(
        lines[i].strip()
        for i in range(flag_at, -1, -1)
        if lines[i].startswith("async def ") or lines[i].startswith("def ")
    )
    assert enclosing.startswith("async def _persist_handover_tail("), enclosing


def test_rewind_never_cancels_the_write_it_registered() -> None:
    """A cancelled task is a done task, and a done task leaves the registry.

    The registry clears itself from a done callback, so cancelling the rewrite
    task drops it from the registry while its worker thread runs on to the
    rename: the close then drains an empty registry and retracts the name with
    the write still in flight. A bare ``await save_task`` in the cancellation
    path does exactly that, because the await is itself a cancellation point and
    ``CancelledError`` is a ``BaseException`` no ``except Exception`` absorbs.
    Every await on the task must therefore be shielded, and the bound stops a
    cancel storm from spinning.
    """
    source = Path(chat_rewind.__file__).read_text(encoding="utf-8")
    lines = source.splitlines()

    bare = [
        line.strip()
        for line in lines
        if "await save_task" in line
        and "asyncio.shield(save_task)" not in line
        and not line.strip().startswith("#")
    ]
    assert bare == [], f"an unshielded await on the registered write: {bare}"

    awaits = [line.strip() for line in lines if "await asyncio.shield(save_task)" in line]
    assert len(awaits) == 2, f"expected the first await plus the drain's, found {len(awaits)}"
    assert "for _ in range(_SAVE_DRAIN_ATTEMPTS):" in source, "the drain must be bounded"


def test_the_bulk_sweep_defers_a_slot_with_a_write_in_flight() -> None:
    """The sweep's ordering, and the invariant that makes it decidable.

    A sweep has no obligation to finish its retraction, so it does not wait: it
    reads the registry synchronously behind the fence and defers the slot when a
    write is in flight. Waiting would be worse than useless, because the handlers
    that produce a guarded write publish a task on the same slot in the same
    breath -- a wait would hold the sweep open exactly while the tab is being
    edited, and the pop after it would cancel that turn.

    So there must be NO await between the fence and the pop. With one, a slot can
    become active, or a fresh write can be admitted, inside the gap the fence was
    raised to close.
    """
    source = Path(handlers.__file__).read_text(encoding="utf-8")
    lines = source.splitlines()

    start = next(
        i for i, line in enumerate(lines) if line.startswith("async def api_chat_slots_cleanup")
    )
    end = next(
        i
        for i in range(start + 1, len(lines))
        if lines[i].startswith("async def ") or lines[i].startswith("def ")
    )
    body = lines[start:end]

    fence_at = next(i for i, line in enumerate(body) if "candidate.begin_close()" in line)
    check_at = next(
        i for i, line in enumerate(body) if "_pending_guarded_history_writes(candidate)" in line
    )
    pop_at = next(
        i for i, line in enumerate(body) if "removed = state._slots.pop(name, None)" in line
    )
    assert fence_at < check_at < pop_at, (fence_at, check_at, pop_at)

    between = [
        line.strip()
        for line in body[fence_at:pop_at]
        if "await " in line and not line.strip().startswith("#")
    ]
    assert between == [], f"an await between the sweep's fence and its pop: {between}"

    deferred = body[check_at:pop_at]
    assert any("failed.append(name)" in line for line in deferred), "a deferral must be reported"
    assert any("candidate.cancel_close()" in line for line in deferred), "a deferral releases it"
    assert not any(
        "raise SlotCloseError" in line for line in body
    ), "a sweep must not fail wholesale"


def test_a_restored_slot_gets_its_fence_back() -> None:
    """A fence left up on a live slot is a tab that refuses every edit for good.

    The sweep pops first and restores the slot when its archive fails, so that
    restore is the one path where a fenced slot becomes live again under its own
    name. ``close_slot`` releases the fence in its own ``finally`` for the
    single-tab path; the sweep has no such wrapper and must release it itself.
    """
    source = Path(handlers.__file__).read_text(encoding="utf-8")
    lines = source.splitlines()

    restore_at = next(
        i
        for i, line in enumerate(lines)
        if "state._slots[name] = removed" in line and not line.strip().startswith("#")
    )
    following = lines[restore_at : restore_at + 6]
    assert any(
        "removed.cancel_close()" in line for line in following
    ), "the restore must release the fence it left up"


def test_one_holder_releasing_does_not_drop_another_holders_fence(tmp_path) -> None:
    """The fence is owned per acquisition, because two retractions can overlap.

    A close the person asked for raises the fence and then suspends inside its
    wait for guarded writes. The bulk sweep can reach the same slot while it is
    suspended. With a shared flag, whichever finished first cleared the fence for
    both, and the other's remaining awaits ran unfenced -- and the dispatch-seam
    re-reads that are the last line of defence read exactly this value, so a late
    truncating save would then register after the drain and commit after the pop.
    """
    state = _state_with_slot(tmp_path)
    slot = state.get_slot(NAME)
    assert slot is not None

    slot.begin_close()
    slot.begin_close()
    assert slot.is_closing is True

    slot.cancel_close()
    assert slot.is_closing is True, "one holder releasing must not unfence the other"

    slot.cancel_close()
    assert slot.is_closing is False, "the last holder releasing lowers the fence"

    slot.cancel_close()
    assert slot.is_closing is False, "an unmatched release must not go negative"
    slot.begin_close()
    assert slot.is_closing is True, "a negative count would read as not-closing here"


def test_the_sweep_leaves_a_slot_another_retraction_owns() -> None:
    """Hands off, before the sweep's own fence goes up.

    A slot already being closed is already being archived, so joining that
    retraction buys nothing and risks two of them racing one name. The check has
    to come BEFORE ``begin_close()``: after it, the sweep cannot tell its own
    acquisition from the other holder's.
    """
    source = Path(handlers.__file__).read_text(encoding="utf-8")
    lines = source.splitlines()

    start = next(
        i for i, line in enumerate(lines) if line.startswith("async def api_chat_slots_cleanup")
    )
    end = next(
        i
        for i in range(start + 1, len(lines))
        if lines[i].startswith("async def ") or lines[i].startswith("def ")
    )
    body = lines[start:end]

    owned_at = next(i for i, line in enumerate(body) if "if candidate.is_closing:" in line)
    fence_at = next(i for i, line in enumerate(body) if "candidate.begin_close()" in line)
    assert owned_at < fence_at, (owned_at, fence_at)
    assert any(
        line.strip() == "continue" for line in body[owned_at:fence_at]
    ), "the sweep must skip, not fall through"


@pytest.mark.asyncio
async def test_a_refused_guarded_write_arms_the_retry(tmp_path, monkeypatch) -> None:
    """Refusing must not be final, because the callers that matter ignore the answer.

    A metadata-only mutation -- a recreate's title, a folder filing, a tag, a pin --
    saves with ``force=True``, sets no ``_dirty`` of its own, and publishes on the
    strength of the acknowledged edit without reading this return. A close can
    raise the fence and then leave the slot live, so an edit landing in that window
    would be dropped and the old value would come back after a restart. Arming the
    periodic flush is what makes the refusal a deferral instead of a loss.
    """
    state = _state_with_slot(tmp_path)
    slot = state.get_slot(NAME)
    assert slot is not None
    key = slot_history_key(slot)

    monkeypatch.setattr(chat_persistence, "_save_slot_to_history", lambda *a, **k: True)

    slot.drain()
    slot._dirty = False
    assert slot._dirty is False, "the arrange step must leave it clean or this proves nothing"

    slot.begin_close()
    saved = await chat_persistence.save_slot_off_loop(
        state,
        slot,
        list(slot.messages),
        force=True,
        best_effort=False,
        expected_history_key=key,
    )

    assert saved is False, "a guarded write on a fenced slot is still refused"
    assert slot._dirty is True, "a refused write must leave the flush armed to retry it"


def test_every_truncating_save_is_a_guarded_write() -> None:
    """The population the ordering must cover, enumerated so it cannot grow quietly.

    A truncating save rewrites the window rather than appending to it: it passes an
    explicit messages snapshot, or ``rewrite=True``. Each must carry an authorized
    transcript key, because that is what registers it in the slot's guarded-write
    registry and what subjects it to the close fence. A truncating save WITHOUT the
    key is less ordered against a retraction than the ones this change fences, not
    more.

    Read from the syntax tree, not the text: these call sites carry long comments
    that a window scan mistakes for arguments.

    ``chat_rewind`` is the exception on purpose -- it calls
    ``_save_slot_to_history`` directly and registers itself, which its own pin
    covers.
    """
    sites = []
    for module in (chat_regenerate, chat_fork, handlers):
        name = Path(module.__file__).name
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            called = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else getattr(node.func, "id", "")
            )
            if called != "save_slot_off_loop":
                continue
            keywords = {kw.arg for kw in node.keywords if kw.arg}
            snapshot = len(node.args) >= 3 or "messages" in keywords
            rewrite = any(
                kw.arg == "rewrite" and isinstance(kw.value, ast.Constant) and kw.value.value
                for kw in node.keywords
            )
            if not (snapshot or rewrite):
                continue
            authorized = "expected_history_key" in keywords or "expected_slot_name" in keywords
            sites.append((name, node.lineno, authorized))

    assert sites, "the scan found no truncating saves, so it is measuring nothing"
    unguarded = [(name, line) for name, line, ok in sites if not ok]
    assert unguarded == [], f"a truncating save carries no authorized key: {unguarded}"


def test_every_truncating_rewrite_pins_slot_identity_at_the_commit_boundary() -> None:
    """The population that needs the in-lock identity recheck, enumerated.

    A truncating rewrite passes an explicit messages snapshot or ``rewrite=True``.
    It is dispatched off the event loop, so the loop is free from the dispatch
    until the worker commits, and a same-name close-and-recreate is not
    serialized against the slot's own lock. Such a replacement resumes the SAME
    transcript, so ``expected_history_key`` stays satisfied and the routing pin
    waves it through; ``expected_slot_name`` re-reads ``state._slots`` inside the
    transcript lock, with no await before the write, and refuses.

    No loop-side check substitutes for it: a guard evaluated before an await
    decides the moment of evaluation, not the moment of commit. So EVERY
    truncating rewrite carries the pin, and the scan covers both dispatch
    shapes -- a direct call, and a saver handed to ``asyncio.to_thread`` -- or a
    site using the second shape would read as absent rather than as unpinned.
    """
    savers = {"save_slot_off_loop", "save_slot_to_history", "_save_slot_to_history"}
    root = Path(chat_persistence.__file__).parent

    def _called(node: ast.expr) -> str:
        if isinstance(node, ast.Attribute):
            return node.attr
        return getattr(node, "id", "")

    sites: list[tuple[str, int, bool]] = []
    for module_path in sorted(root.rglob("*.py")):
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _called(node.func)
            args = node.args
            if name == "to_thread" and args and _called(args[0]) in savers:
                name, args = _called(args[0]), args[1:]
            elif name not in savers:
                continue
            keywords = {kw.arg for kw in node.keywords if kw.arg}
            snapshot = len(args) >= 3 or "messages" in keywords
            rewrite = any(
                kw.arg == "rewrite" and isinstance(kw.value, ast.Constant) and kw.value.value
                for kw in node.keywords
            )
            if not (snapshot or rewrite):
                continue
            sites.append((module_path.name, node.lineno, "expected_slot_name" in keywords))

    assert sites, "the scan found no truncating rewrites, so it is measuring nothing"
    # The shapes the scan must see, so a scan that silently stops matching one of
    # them cannot read as a clean board.
    modules = {name for name, _, _ in sites}
    assert {
        "chat_regenerate.py",
        "chat_rewind.py",
        "chat_fork.py",
    } <= modules, f"the scan lost sight of a known truncating rewrite: {sorted(modules)}"

    unpinned = [(name, line) for name, line, pinned in sites if not pinned]
    assert unpinned == [], f"a truncating rewrite does not pin slot identity: {unpinned}"


def test_the_periodic_flush_stays_off_a_slot_being_retracted(tmp_path) -> None:
    """The refusal arms this writer, so this writer must respect the same fence.

    A fenced slot is still the occupant of its name until the pop, so the
    five-second pass still visits it. Its write carries no ``expected_slot_name``,
    which skips the in-lock recreate-won guard, and it is not a registered guarded
    write, which puts it outside the retraction's wait. A tick landing between the
    fence and the pop would therefore overwrite whatever adopts the name next.

    ``_dirty`` must stay armed across the skip, or the owed write is dropped
    instead of deferred -- which is the loss the arming existed to prevent.
    """
    state = _state_with_slot(tmp_path)
    slot = state.get_slot(NAME)
    assert slot is not None

    written: list[str] = []
    coordinator = SimpleNamespace(
        _slot_saver_provider=lambda: (lambda owner, saved, **kw: written.append(saved.key)),
        _logger_provider=lambda: logging.getLogger(__name__),
    )
    flush = DashboardPersistenceCoordinator.flush_slot_now

    slot._dirty = True
    slot.begin_close()
    flush(coordinator, state, slot)
    assert written == [], "the periodic writer must not write a slot being retracted"
    assert slot._dirty is True, "the owed write must stay owed, not be dropped"

    slot.cancel_close()
    flush(coordinator, state, slot)
    assert written == [NAME], "an abandoned close must let the next pass write it"


def test_the_periodic_save_carries_its_ownership_key(tmp_path) -> None:
    """The fence is read on a worker thread, so it cannot be the last word.

    ``flush_slot_now`` reads ``is_closing`` on the flush executor thread while the
    retraction runs on the loop, and the write reaches the transcript lock only
    after the snapshot, routing and retention stretch. That is the same shape as
    the defect this whole change exists to fix: event-loop state read from a worker
    thread, deciding a commit that is still ahead.

    ``expected_slot_name`` is what decides inside the lock, with no await before
    the write. It matters more here than elsewhere because a periodic save is a
    full metadata rebuild: it does not ask for the ``rows_only`` deferral that
    keeps another holder's folder, title and tag.
    """
    state = _state_with_slot(tmp_path)
    slot = state.get_slot(NAME)
    assert slot is not None

    seen: list[dict] = []
    coordinator = SimpleNamespace(
        _slot_saver_provider=lambda: (lambda owner, saved, **kw: seen.append(kw)),
        _logger_provider=lambda: logging.getLogger(__name__),
    )

    slot._dirty = True
    DashboardPersistenceCoordinator.flush_slot_now(coordinator, state, slot)

    assert seen == [{"expected_slot_name": NAME}], seen


def test_a_recreate_won_refusal_keeps_the_write_owed() -> None:
    """A refusal is not a commit, and the periodic writer cannot tell the difference.

    ``flush_slot_now`` clears ``_dirty`` on any return that did not raise, so a
    guard that refuses without re-arming erases the only in-memory witness of an
    edit it never wrote. For an in-place edit there is no substitute: the popped
    slot is out of the registry that the periodic pass walks, and the window
    length matches disk, so the handover's ``unsaved`` count cannot stand in for
    the flag either.

    The other retryable refusals in this function already call the helper whose
    docstring states exactly this; the recreate-won guard must join them.
    """
    source = Path(chat_persistence.__file__).read_text(encoding="utf-8")
    lines = source.splitlines()

    guard_at = next(
        i
        for i, line in enumerate(lines)
        if "state._slots.get(expected_slot_name) is not slot" in line
        and not line.strip().startswith("#")
    )
    ret_at = next(i for i in range(guard_at, len(lines)) if lines[i].strip() == "return False")
    between = [line.strip() for line in lines[guard_at:ret_at]]
    assert any(
        "_keep_owed_after_refusal(slot)" in line for line in between
    ), f"the recreate-won refusal does not keep the write owed: {between}"
