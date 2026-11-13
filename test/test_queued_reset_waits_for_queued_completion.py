"""A conversation reset queued during a turn waits for a completion already in the tab's queue.

The trigger is a model calling ``reset_conversation`` mid-turn, so these drive the
real end-of-turn consume and the real queue hand-off against a dashboard slot.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from chat_test_helpers import _make_state

# chat_handlers binds the runner's _run_chat when first imported, which the drain does
# lazily; importing it here keeps it from binding a test's patched copy.
from kiro_crew.dashboard import chat_handlers, chat_runner  # noqa: F401
from kiro_crew.dashboard.chat_utils_injected_rows import (
    SUBAGENT_COMPLETION_KIND,
    SYNTHETIC_RECOVERY_KIND,
)
from kiro_crew.testing.wait import async_wait_until, await_until

pytestmark = pytest.mark.asyncio

COMPLETION = "[Subagent completion event] run r1 finished"


@pytest.fixture
def armed(tmp_path, monkeypatch):
    """A tab whose turn armed a discard, with no sub-agent still attached."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    slot._pending_discard_conversation_key = "s1"
    monkeypatch.setattr(chat_runner, "subagents_attached_async", AsyncMock(return_value=False))
    # The retry never ticks on its own; the test that needs it wakes it.
    monkeypatch.setattr(
        chat_runner, "_PENDING_RESET_RETRY_DELAY_SECS", 3600  # flake-ok: a tick that never fires
    )
    events: list[str] = []

    async def discard(*_args, **_kwargs):
        events.append("discard")
        return True

    state.sessions.discard_conversation = discard
    yield state, slot, events
    retry = state._pending_reset_retries.pop(slot.key, None)
    if retry is not None:
        retry[1].cancel()


def _record_runs(monkeypatch, events: list[str], slot) -> None:
    async def fake_run_chat(_st, _sl, msg, **_kwargs):
        armed = slot._pending_discard_conversation_key is not None
        events.append(f"turn:{msg}:{'old' if armed else 'fresh'}")

    monkeypatch.setattr(chat_runner, "_run_chat", fake_run_chat)


async def _end_turn(state, slot) -> None:
    """The end-of-turn consume, then the hand-off, as ``_end_turn_tail`` orders them."""
    await chat_runner._consume_pending_reset(state, slot, allow_discard=True, own_turn=True)
    if await chat_runner._start_next_queued_turn(state, slot):
        await await_until(slot.task, what="the drained turn")


async def test_a_queued_completion_runs_in_the_old_conversation_before_the_reset_lands(
    armed, monkeypatch
):
    state, slot, events = armed
    _record_runs(monkeypatch, events, slot)
    slot.queue_append(COMPLETION, kind=SUBAGENT_COMPLETION_KIND)
    await _end_turn(state, slot)
    assert events == [f"turn:{COMPLETION}:old"]
    assert slot._pending_discard_conversation_key == "s1"
    # The completion turn's own end: nothing queued any more, so the reset lands.
    await _end_turn(state, slot)
    assert events == [f"turn:{COMPLETION}:old", "discard"]
    assert slot._pending_discard_conversation_key is None


async def test_a_verbatim_requeue_of_a_completion_counts(armed):
    state, slot, events = armed
    qid = slot.queue_append(COMPLETION, kind=SYNTHETIC_RECOVERY_KIND)
    next(e for e in slot._queue if e["id"] == qid)[chat_runner._REPLAYS_COMPLETION_KEY] = True
    await chat_runner._consume_pending_reset(state, slot, allow_discard=True, own_turn=True)
    assert events == []
    assert slot._pending_discard_conversation_key == "s1"


async def test_another_recovery_entry_does_not_hold_the_reset(armed):
    state, slot, events = armed
    slot.queue_append("Continue where you left off.", kind=SYNTHETIC_RECOVERY_KIND)
    assert await chat_runner._consume_pending_reset(state, slot, allow_discard=True, own_turn=True)
    assert events == ["discard"]


async def test_a_held_queue_does_not_strand_the_reset(armed, monkeypatch):
    """Signed out, the completion cannot start, so the retry lands the reset."""
    state, slot, events = armed
    _record_runs(monkeypatch, events, slot)
    slot._last_turn_auth_required = True
    slot.queue_append(COMPLETION, kind=SUBAGENT_COMPLETION_KIND)
    await chat_runner._consume_pending_reset(state, slot, allow_discard=True, own_turn=True)
    assert events == []
    chat_runner._wake_pending_reset_retry(state, slot)
    await async_wait_until(
        lambda: slot._pending_discard_conversation_key is None,
        describe=lambda: "the retry landing the reset",
    )
    assert events == ["discard"]
    assert [e.get("kind") for e in slot._queue] == [SUBAGENT_COMPLETION_KIND]


async def test_a_landed_reset_settles_the_pending_failure_notices(armed, monkeypatch):
    state, slot, events = armed
    settled: list[list[str]] = []
    monkeypatch.setattr(
        chat_runner,
        "settle_drained_subagent_notices",
        lambda _st, _sl, notices: settled.append(notices),
    )
    slot._pending_subagent_failures.append("run r2 failed")
    assert await chat_runner._consume_pending_reset(state, slot, allow_discard=True, own_turn=True)
    assert settled == [["run r2 failed"]]
    assert slot._pending_subagent_failures == []


async def test_with_no_completion_queued_the_reset_lands_at_the_turn_end(armed, monkeypatch):
    state, slot, events = armed
    _record_runs(monkeypatch, events, slot)
    await _end_turn(state, slot)
    assert events == ["discard"]
    assert slot._pending_discard_conversation_key is None
