"""The dashboard pre-prompt context build bounds only its wait to ENTER the embed pool.

A context-bearing dashboard turn builds its prompt on the memory embed pool, which
shares an 8-slot admission semaphore with background consolidation. On origin/main
that admission wait has no deadline: when the pool is saturated the turn's build sits
on ``await admission.acquire()`` forever, and the turn stays on "thinking" with no
error. A naive ``wait_for`` around the whole build is not enough, because the turn
consumes one-shot inputs (the replay-suppression flag, the drained subagent-failure
notices, ``prev_turn_cancelled``, the drained pending context) BEFORE the build and
would lose them on a timeout, and the build's own worker thread could still write
skill-body delivery records after the turn rolled them back.

The fix bounds ONLY the admission wait and takes it before any of those one-shot
inputs is consumed:

* ``acquire_embed_admission`` acquires the same per-loop semaphore with a deadline and
  raises ``EmbedAdmissionTimeout`` instead of waiting forever (Part A);
* ``_turn_build_prompt`` takes that slot first, so a timeout has consumed nothing to
  restore and leaves no abandoned build thread, and surfaces the gateway's existing
  retryable ``memory_unavailable`` error card instead of hanging (Part B).
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state, drain_background_tasks

from kiro_crew import executors
from kiro_crew.context import ContextBuilder
from kiro_crew.dashboard import chat_runner
from kiro_crew.executors import (
    _MAX_EMBED_WORKERS,
    EmbedAdmissionTimeout,
    acquire_embed_admission,
    run_in_embed_pool,
)
from kiro_crew.memory import MemoryStore
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent
from kiro_crew.skills import SkillsLoader
from kiro_crew.testing.wait import async_wait_until, await_until, finished, until_parked

SLOT_NAME = "preprompt-chat"


# ── Part A: the executor-level admission bound ─────────────────────────────


@pytest.mark.asyncio
async def test_plain_admission_wait_is_unbounded_when_saturated():
    """Repro: with the pool saturated, the unbounded ``run_in_embed_pool`` never starts.

    This is the behaviour the fix exists for: a build submitted while every
    admission slot is held makes no progress and raises nothing, so the turn
    around it would hang. The test holds every slot, submits a build whose body
    sets an event, and asserts the event stays clear -- the body never ran.
    """
    loop = asyncio.get_running_loop()
    admission = executors._memory_admission(loop)
    held = [await _acquire(admission) for _ in range(_MAX_EMBED_WORKERS)]
    ran = asyncio.Event()

    def _body() -> str:
        ran.set()
        return "built"

    task = asyncio.ensure_future(run_in_embed_pool(_body))
    try:
        # Prove the build actually reached the admission wait: it must PARK on the
        # saturated semaphore, not just "not have run yet". Waiting on this
        # positive signal is what makes the absence checks meaningful -- if
        # admission stopped gating, nothing would park here and this would fail.
        await until_parked(
            admission,
            describe=lambda: f"ran={ran.is_set()} task_done={task.done()}",
        )
        assert not ran.is_set(), "the build should not start while the pool is saturated"
        assert not task.done()
    finally:
        task.cancel()
        for h in held:
            h.release()


@pytest.mark.asyncio
async def test_embed_pool_admission_times_out_when_saturated():
    """The bounded helper raises ``EmbedAdmissionTimeout`` instead of waiting forever."""
    loop = asyncio.get_running_loop()
    admission = executors._memory_admission(loop)
    held = [await _acquire(admission) for _ in range(_MAX_EMBED_WORKERS)]
    try:
        with pytest.raises(EmbedAdmissionTimeout):
            await acquire_embed_admission(0.05)
        # The failed acquire released nothing of its own: we still hold every slot.
        assert admission._value == 0  # noqa: SLF001
    finally:
        for h in held:
            h.release()


@pytest.mark.asyncio
async def test_embed_pool_admission_grants_when_a_slot_frees():
    """A freed slot lets the bounded acquire through, proving it gates on the real pool."""
    loop = asyncio.get_running_loop()
    admission = executors._memory_admission(loop)
    held = [await _acquire(admission) for _ in range(_MAX_EMBED_WORKERS)]
    entered = asyncio.Event()

    async def _take() -> None:
        slot = await acquire_embed_admission(5.0)
        try:
            entered.set()
        finally:
            if not slot.handed_off:
                slot.release()

    task = asyncio.ensure_future(_take())
    try:
        # It cannot enter yet: prove it by waiting until the acquire actually
        # PARKS on the saturated semaphore, not merely until "entered is still
        # clear" (true on the first poll regardless of whether admission gates).
        await until_parked(
            admission,
            describe=lambda: f"entered={entered.is_set()} task_done={task.done()}",
        )
        assert not entered.is_set()
        # Free one slot; the waiter now gets in and exits, releasing it again.
        held.pop().release()
        await await_until(task, what="the bounded acquire after a slot freed")
        assert entered.is_set()
        # The acquire returned the slot it took, so one is free again while we
        # still hold the other seven.
        assert admission._value == 1  # noqa: SLF001
    finally:
        if not task.done():
            task.cancel()
        for h in held:
            h.release()


@pytest.mark.asyncio
async def test_timeout_before_submit_consumes_no_slot():
    """A timeout acquires no slot: the pool is whole and no body ran."""
    loop = asyncio.get_running_loop()
    admission = executors._memory_admission(loop)
    held = [await _acquire(admission) for _ in range(_MAX_EMBED_WORKERS)]
    try:
        with pytest.raises(EmbedAdmissionTimeout):
            slot = await acquire_embed_admission(0.05)
            # Not reached: admission never frees, so no slot and no submit.
            try:
                await run_in_embed_pool(lambda: "x", _admission_held=slot)
            finally:
                if not slot.handed_off:
                    slot.release()
    finally:
        for h in held:
            h.release()
    # Every slot we held is back; the timed-out acquire added nothing to undo.
    assert admission._value == _MAX_EMBED_WORKERS  # noqa: SLF001


@pytest.mark.asyncio
async def test_admission_held_run_does_not_acquire_a_second_slot():
    """A held build runs on the caller's slot, taking no other.

    The caller holds one slot through ``acquire_embed_admission``; the build must
    run even when every OTHER slot is held, which it could not if it tried to
    acquire its own. On exit the slot is returned (by the build future) and none
    is leaked.
    """
    loop = asyncio.get_running_loop()
    admission = executors._memory_admission(loop)
    # Hold every slot but one, which the acquire below will take.
    extra = [await _acquire(admission) for _ in range(_MAX_EMBED_WORKERS - 1)]
    ran = asyncio.Event()

    def _body() -> str:
        ran.set()
        return "built"

    try:
        slot = await acquire_embed_admission(5.0)
        try:
            # Every slot is now held (our one + the extras): a second acquire
            # would block, so a result proves no second acquire happened.
            result = await finished(
                run_in_embed_pool(_body, _admission_held=slot),
                "the held-admission build",
                timeout=5.0,
            )
        finally:
            if not slot.handed_off:
                slot.release()
        assert result == "built"
        assert ran.is_set()
    finally:
        for h in extra:
            h.release()
    # The build future released the held slot; with the extras freed, the pool
    # is whole -- no second slot was taken, none leaked.
    assert admission._value == _MAX_EMBED_WORKERS  # noqa: SLF001


@pytest.mark.asyncio
async def test_cancelled_held_build_keeps_the_slot_until_the_thread_finishes():
    """Cancelling the turn does NOT free the slot while the build thread still runs.

    The whole point of the ownership transfer: a submitted future cannot be
    cancelled once its thread starts, so releasing the slot on the caller's
    cleanup while that thread still held an embed-pool worker would let the
    semaphore stop bounding the pool. The slot must track the THREAD, not the
    awaiting caller. Here the build thread blocks on an event; the caller is
    cancelled; the slot stays taken until the thread is let finish, and is then
    released exactly once.
    """
    loop = asyncio.get_running_loop()
    admission = executors._memory_admission(loop)
    before = admission._value  # noqa: SLF001
    release_body = threading.Event()
    started = threading.Event()

    def _blocking_body() -> str:
        started.set()
        release_body.wait(5.0)
        return "built"

    future_holder: dict = {}

    async def _held_turn() -> None:
        slot = await acquire_embed_admission(5.0)
        try:
            # Keep a handle on the inner task so we can await the thread's end.
            inner = asyncio.ensure_future(run_in_embed_pool(_blocking_body, _admission_held=slot))
            future_holder["inner"] = inner
            await inner
        finally:
            if not slot.handed_off:
                slot.release()

    turn = asyncio.ensure_future(_held_turn())
    try:
        # Wait until the slot is taken and the worker thread is actually running.
        await async_wait_until(lambda: started.is_set(), timeout=5.0)
        assert admission._value == before - 1  # noqa: SLF001  -- one slot in use

        # Cancel the awaiting turn. The worker thread cannot be cancelled.
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn

        # The slot MUST still be taken: the thread is still inside _blocking_body.
        await asyncio.sleep(0)
        assert admission._value == before - 1, "a cancelled caller must not free the slot early"

        # Let the thread finish; its done-callback releases the slot exactly once.
        release_body.set()
        inner = future_holder["inner"]
        await async_wait_until(
            lambda: admission._value == before,  # noqa: SLF001
            timeout=5.0,
            describe=lambda: f"value={admission._value} inner_done={inner.done()}",  # noqa: SLF001
        )
        assert admission._value == before  # noqa: SLF001  -- released once, pool whole
    finally:
        release_body.set()
        if not turn.done():
            turn.cancel()


async def _acquire(sem: asyncio.Semaphore):
    """Acquire *sem* and return a tiny releasable handle (keeps call sites terse)."""
    await sem.acquire()
    return _Held(sem)


class _Held:
    def __init__(self, sem: asyncio.Semaphore) -> None:
        self._sem = sem
        self._released = False

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._sem.release()


# ── Part B: the turn surfaces an error and consumes nothing on timeout ──────


def _turn_state(tmp_path, monkeypatch):
    """A DashboardState whose only mocked seams are the provider and the builder."""
    builder = ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "workspace"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
    )
    state = _make_state(tmp_path, context_builder=builder)
    state.context_builder.build_message = MagicMock(return_value=("task", None))
    state.context_builder.ensure_store = AsyncMock(return_value=object())
    provider = MagicMock()

    async def stream(*args, **kwargs):
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Done.")
        yield LLMEvent(kind=EVENT_COMPLETE)

    provider.stream = stream
    state.sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    # A real one-shot the build consumes: it must survive a timed-out admission.
    state.sessions.consume_replay_suppression = MagicMock(return_value=False)
    state.sessions.consume_needs_reinjection = MagicMock(return_value=False)
    state.sessions.record_failure = AsyncMock()
    monkeypatch.setattr(chat_runner, "title_then_refresh", AsyncMock())
    monkeypatch.setattr(chat_runner, "generate_session_summary", AsyncMock())
    return state


async def _run_turn(state, slot, message="Prepare my context."):
    slot.append("user", message)
    await asyncio.wait_for(chat_runner._run_chat(state, slot, message), 30)
    await finished(drain_background_tasks(state), "draining background tasks", timeout=10)


def _error_cards(slot) -> list[dict]:
    return [m for m in slot.messages if m.get("role") == "error"]


@pytest.mark.asyncio
async def test_turn_surfaces_memory_unavailable_and_consumes_nothing_on_admission_timeout(
    tmp_path, monkeypatch
):
    """A saturated pool makes the turn show the retryable card and keep every one-shot.

    With the admission deadline tiny and every slot held, the build cannot be
    admitted, so ``_turn_build_prompt`` raises ``_MemoryUnavailable`` before any
    one-shot input is consumed. The turn ends on the existing ``memory_unavailable``
    error card -- not a hang -- and the pending context, the subagent-failure
    notices and the replay-suppression flag are all left untouched for the retry.
    """
    monkeypatch.setattr(chat_runner, "_BUILD_ADMISSION_TIMEOUT_SECS", 0.05)
    state = _turn_state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot(SLOT_NAME)
    # One-shot inputs that the build would otherwise consume.
    slot._pending_context = [{"content": "background note", "source": "panel"}]
    slot._pending_subagent_failures = ["[subagent X timed out]"]

    loop = asyncio.get_running_loop()
    admission = executors._memory_admission(loop)
    held = [await _acquire(admission) for _ in range(_MAX_EMBED_WORKERS)]
    try:
        await _run_turn(state, slot)
    finally:
        for h in held:
            h.release()

    # A visible, retryable error card -- never a hang, never the build.
    cards = _error_cards(slot)
    assert cards, "a saturated pool must surface an error card, not hang"
    assert any((c.get("meta") or {}).get("code") == "memory_unavailable" for c in cards), cards
    state.context_builder.build_message.assert_not_called()

    # Nothing was consumed: the retry sees each one-shot exactly once.
    assert slot._pending_context == [{"content": "background note", "source": "panel"}]
    assert slot._pending_subagent_failures == ["[subagent X timed out]"]
    state.sessions.consume_replay_suppression.assert_not_called()


@pytest.mark.asyncio
async def test_success_path_builds_and_consumes_inputs_once(tmp_path, monkeypatch):
    """With the pool free the turn builds normally and drains its one-shot inputs once.

    The admission is granted, the build runs on the held slot (no error card), and
    the pending context and subagent-failure notices are consumed exactly once --
    the unchanged success behaviour, confirming the bound adds no cost when the
    pool is healthy.
    """
    state = _turn_state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot(SLOT_NAME)
    slot._pending_context = [{"content": "background note", "source": "panel"}]
    slot._pending_subagent_failures = ["[subagent X timed out]"]

    await _run_turn(state, slot)

    assert _error_cards(slot) == []
    state.context_builder.build_message.assert_called_once()
    # Both one-shot queues drained exactly once.
    assert slot._pending_context == []
    assert slot._pending_subagent_failures == []
    # The build received the message with both one-shots prepended ahead of it.
    built_message = state.context_builder.build_message.call_args.args[0]
    assert "background note" in built_message
    assert "[subagent X timed out]" in built_message


@pytest.mark.asyncio
async def test_no_admission_slot_is_leaked_across_a_built_turn(tmp_path, monkeypatch):
    """A built turn returns its admission slot, so the pool is whole for the next turn."""
    state = _turn_state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot(SLOT_NAME)
    await _run_turn(state, slot)
    loop = asyncio.get_running_loop()
    admission = executors._memory_admission(loop)
    assert admission._value == _MAX_EMBED_WORKERS  # noqa: SLF001
