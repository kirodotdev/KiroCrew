"""Coverage tests for ``kiro_crew.dashboard.chat_runner``: work armed between turns.

Covers requeue suppression, ``_consume_pending_reset`` with its discard boundary and
its wait for running subagents, the eager spawn, armed resume prefetches and their
TTL, and the steer settle and requeue rules.
``test_chat_runner_coverage.py`` describes the two harnesses the family uses; the
shared builders are in ``chat_runner_coverage_helpers.py``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_runner_coverage_helpers import _slot, _state

from kiro_crew.config import live
from kiro_crew.dashboard import chat_runner
from kiro_crew.testing.wait import await_until

# ── requeue suppression / pending reset ───────────────────────────────────


class TestRequeueSuppression:
    @pytest.mark.parametrize(
        "stop_state,expected", [("idle", False), ("soft_pending", True), ("killing", True)]
    )
    def test_stop_in_progress_suppresses_requeue(self, stop_state, expected):
        slot = _slot()
        slot._stop_state = stop_state

        assert chat_runner._should_suppress_requeue(slot) is expected


class TestConsumePendingReset:
    async def _drain_retry(self, slot_key: str) -> None:
        """Cancel and await the slot's pending-reset retry task, if armed.

        The decline paths arm a real background task; leaving it pending when
        the test's loop closes emits 'Task was destroyed but it is pending'.
        """
        entry = chat_runner._pending_reset_retries.pop(slot_key, None)
        if entry is not None:
            entry[1].cancel()
            try:
                # flake-ok: pre-existing await of a task the test has just cancelled, moved verbatim; tracked for a de-flake pass
                await entry[1]
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_no_pending_key_is_a_noop(self, tmp_path):
        state, slot = _state(tmp_path), _slot()

        await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_successful_reset_clears_the_flag(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot._pending_reset_history_key = "dashboard:chat-cov-1"
        state.sessions.reset = AsyncMock(return_value=True)

        await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        state.sessions.reset.assert_awaited_once_with("dashboard:chat-cov-1", skip_if_busy=True)
        assert slot._pending_reset_history_key is None

    @pytest.mark.asyncio
    async def test_busy_decline_leaves_the_flag_armed(self, tmp_path):
        # The pending key can name a LIVE channel session (a linked slot's
        # project change arms slack:<ts>): a busy decline must leave the flag
        # armed for a later consume instead of tearing down the streaming
        # reply — the discard branch's pattern. The withhold verdict survives
        # too: it describes the session that advertised the model list, and
        # that session is still live and accurate after a decline.
        state, slot = _state(tmp_path), _slot()
        slot.linked_session_key = "slack:123.456"
        slot._pending_reset_history_key = "slack:123.456"
        slot.model = "some-model"
        slot.record_model_withheld(True)
        state.sessions.reset = AsyncMock(return_value=False)
        state.sessions.get_provider = MagicMock(return_value=MagicMock())

        torn_down = await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        assert torn_down is False
        assert slot._pending_reset_history_key == "slack:123.456"
        assert slot.model_withheld is True
        await self._drain_retry(slot.key)

    @pytest.mark.asyncio
    async def test_busy_decline_arms_a_retry_that_lands_the_reset(self, tmp_path, monkeypatch):
        # A channel-linked slot's turns never pass a dashboard turn boundary,
        # so a busy decline arms a bounded retry task; once the channel turn
        # releases the session, the retry lands the teardown and spends the
        # flag — no eager reuse of the stale provider in between.
        monkeypatch.setattr(chat_runner, "_PENDING_RESET_RETRY_DELAY_SECS", 0.01)
        state, slot = _state(tmp_path), _slot()
        state._slots[slot.key] = slot
        slot.linked_session_key = "slack:123.456"
        slot._pending_reset_history_key = "slack:123.456"
        # First consume declines (turn in flight), the retry's consume lands.
        state.sessions.reset = AsyncMock(side_effect=[False, True])

        torn_down = await chat_runner._consume_pending_reset(state, slot, allow_discard=False)
        assert torn_down is False
        assert slot._pending_reset_history_key == "slack:123.456"

        task = chat_runner._pending_reset_retries.get(slot.key)
        assert task is not None
        assert task[0] is slot
        await await_until(task[1])
        assert slot._pending_reset_history_key is None
        assert state.sessions.reset.await_count == 2

    @pytest.mark.asyncio
    async def test_replacement_slot_retry_is_not_suppressed(self, tmp_path, monkeypatch):
        # A retiring owner's still-live retry task must not dedupe away a
        # REPLACEMENT slot's retry under the same key: the old task exits on
        # its own identity check and cannot clear the flag the replacement
        # owns, so suppressing the new registration would strand the
        # replacement on the stale project forever.
        monkeypatch.setattr(chat_runner, "_PENDING_RESET_RETRY_DELAY_SECS", 0.01)
        state = _state(tmp_path)
        old_owner = _slot()

        async def _sleep_forever() -> None:
            # flake-ok: pre-existing 30 s park: the stale retry task sleeps until the test cancels it, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 30 s park: the stale retry task sleeps until the test cancels it, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(30)

        stale_task = asyncio.get_running_loop().create_task(_sleep_forever())
        chat_runner._pending_reset_retries[old_owner.key] = (old_owner, stale_task)

        replacement = _slot()  # same key, different object
        replacement.linked_session_key = "slack:123.456"
        state._slots[replacement.key] = replacement
        replacement._pending_reset_history_key = "slack:123.456"
        state.sessions.reset = AsyncMock(side_effect=[False, True])

        await chat_runner._consume_pending_reset(state, replacement, allow_discard=False)
        entry = chat_runner._pending_reset_retries.get(replacement.key)
        assert entry is not None
        assert entry[0] is replacement
        await await_until(entry[1])
        assert replacement._pending_reset_history_key is None

        stale_task.cancel()
        try:
            # flake-ok: pre-existing await of a task the test has just cancelled, moved verbatim; tracked for a de-flake pass
            await stale_task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_no_teardown_leaves_the_flag_armed(self, tmp_path):
        # reset() returns False for a busy decline, for "nothing registered
        # under the key", AND for a key whose session is cold-starting and
        # not yet registered — the three cannot be told apart from here, so
        # the flag is spent only on a real teardown. Clearing on a None
        # provider probe would let a concurrent cold start carrying the old
        # CWD register after the clear and serve the stale project.
        state, slot = _state(tmp_path), _slot()
        slot._pending_reset_history_key = "dashboard:chat-cov-1"
        state.sessions.reset = AsyncMock(return_value=False)

        torn_down = await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        assert torn_down is False
        assert slot._pending_reset_history_key == "dashboard:chat-cov-1"
        await self._drain_retry(slot.key)

    @pytest.mark.asyncio
    async def test_a_rebind_re_arms_the_current_key_and_does_not_reset_the_stale_one(
        self, tmp_path, monkeypatch
    ):
        # The flag was armed for session A, then the slot REBOUND to B (a
        # cron/workflow slot gets linked when its first result is injected, a
        # channel link can land between arming and consume). Resetting A and
        # clearing the flag would tear down a session nobody is on and let B
        # keep the old CWD forever. The consume must re-arm the flag to B and
        # leave A alone, so a later consume lands the reset where the turns run.
        monkeypatch.setattr(chat_runner, "_PENDING_RESET_RETRY_DELAY_SECS", 0.01)
        state, slot = _state(tmp_path), _slot()
        state._slots[slot.key] = slot
        slot.linked_session_key = "slack:B.new"  # slot now runs turns on B
        slot._pending_reset_history_key = "slack:A.old"  # flag armed for A

        torn_down = await chat_runner._consume_pending_reset(state, slot, allow_discard=False)

        assert torn_down is False
        # A was never reset; the flag re-points at the slot's current session.
        state.sessions.reset.assert_not_awaited()
        assert slot._pending_reset_history_key == "slack:B.new"
        await self._drain_retry(slot.key)

    @pytest.mark.asyncio
    async def test_attached_subagents_defer_the_pending_reset(self, tmp_path):
        # The reset releases the shared runtime attached children run on —
        # a slot with children leaves the flag armed for a later consume,
        # the same policy as the discard branch.
        state, slot = _state(tmp_path), _slot()
        slot._pending_reset_history_key = "dashboard:chat-cov-1"
        subs = MagicMock()
        subs.running_agents_for = MagicMock(return_value=["child-1"])
        state.subagents = subs

        torn_down = await chat_runner._consume_pending_reset(state, slot, allow_discard=False)

        assert torn_down is False
        state.sessions.reset.assert_not_awaited()
        assert slot._pending_reset_history_key == "dashboard:chat-cov-1"
        await self._drain_retry(slot.key)

    @pytest.mark.asyncio
    async def test_a_key_queued_during_the_await_is_not_clobbered(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot.linked_session_key = "old-key"
        slot._pending_reset_history_key = "old-key"

        async def _reset(_key, **_kwargs):
            slot._pending_reset_history_key = "newer-key"
            return True

        state.sessions.reset = AsyncMock(side_effect=_reset)

        await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        assert slot._pending_reset_history_key == "newer-key"

    @pytest.mark.asyncio
    async def test_reset_failure_leaves_the_flag_armed(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot.linked_session_key = "old-key"
        slot._pending_reset_history_key = "old-key"
        state.sessions.reset = AsyncMock(side_effect=RuntimeError("no session"))

        await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        assert slot._pending_reset_history_key == "old-key"

    @pytest.mark.asyncio
    async def test_no_pending_discard_is_a_noop(self, tmp_path):
        state, slot = _state(tmp_path), _slot()

        await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        state.sessions.discard_conversation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_discard_passes_replay_through_and_clears_the_flag(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot._pending_discard_conversation_key = "dashboard:chat-cov-1"

        await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        state.sessions.discard_conversation.assert_awaited_once_with(
            "dashboard:chat-cov-1", replay=False, skip_if_busy=True
        )
        assert slot._pending_discard_conversation_key is None

    @pytest.mark.asyncio
    async def test_a_wake_discard_waits_while_a_question_is_pending(self, tmp_path):
        """A patrol wake's reset is refused at queue time while someone is being
        asked something; it must not land later into that state either."""
        state, slot = _state(tmp_path), _slot()
        slot._pending_discard_conversation_key = "dashboard:chat-cov-1"
        slot._pending_discard_from_wake = True
        slot._question_pending = {"card-1": {"blocking": False}}

        torn_down = await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        assert torn_down is False
        state.sessions.discard_conversation.assert_not_awaited()
        assert slot._pending_discard_conversation_key == "dashboard:chat-cov-1"

        slot._question_pending = {}
        await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        state.sessions.discard_conversation.assert_awaited_once()
        assert slot._pending_discard_conversation_key is None
        assert slot._pending_discard_from_wake is False

    @pytest.mark.asyncio
    async def test_a_persons_discard_does_not_wait_on_a_pending_question(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot._pending_discard_conversation_key = "dashboard:chat-cov-1"
        slot._question_pending = {"card-1": {"blocking": False}}

        await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        state.sessions.discard_conversation.assert_awaited_once()
        assert slot._pending_discard_conversation_key is None

    @pytest.mark.asyncio
    async def test_the_discard_always_asks_for_no_replay(self, tmp_path):
        """One value, not a plumbed choice: replaying the transcript into the
        fresh conversation returns most of what the reset reclaimed. The manager
        keeps the flag for the HTTP route, which does let a caller choose."""
        state, slot = _state(tmp_path), _slot()
        slot._pending_discard_conversation_key = "dashboard:chat-cov-1"

        await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        state.sessions.discard_conversation.assert_awaited_once_with(
            "dashboard:chat-cov-1", replay=False, skip_if_busy=True
        )

    @pytest.mark.asyncio
    async def test_discard_failure_leaves_the_flag_armed(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot._pending_discard_conversation_key = "old-key"
        state.sessions.discard_conversation = AsyncMock(side_effect=RuntimeError("no session"))

        await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        assert slot._pending_discard_conversation_key == "old-key"

    @pytest.mark.asyncio
    async def test_a_discard_key_queued_during_the_await_is_not_clobbered(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot._pending_discard_conversation_key = "old-key"

        async def _discard(_key, **_kw):
            slot._pending_discard_conversation_key = "newer-key"

        state.sessions.discard_conversation = AsyncMock(side_effect=_discard)

        await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        assert slot._pending_discard_conversation_key == "newer-key"

    @pytest.mark.asyncio
    async def test_both_deferrals_run_because_neither_subsumes_the_other(self, tmp_path):
        """A project reset recreates the session but leaves replay suppression
        alone, so skipping the discard would hand the next turn a rebuilt
        [CONVERSATION HISTORY] block for the conversation that was discarded."""
        state, slot = _state(tmp_path), _slot()
        slot._pending_reset_history_key = "dashboard:chat-cov-1"
        slot._pending_discard_conversation_key = "dashboard:chat-cov-1"

        await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        state.sessions.reset.assert_awaited_once_with("dashboard:chat-cov-1", skip_if_busy=True)
        state.sessions.discard_conversation.assert_awaited_once_with(
            "dashboard:chat-cov-1", replay=False, skip_if_busy=True
        )
        assert slot._pending_reset_history_key is None
        assert slot._pending_discard_conversation_key is None

    @pytest.mark.asyncio
    async def test_a_failed_project_reset_does_not_block_the_discard(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot.linked_session_key = "old-key"
        slot._pending_reset_history_key = "old-key"
        slot._pending_discard_conversation_key = "old-key"
        state.sessions.reset = AsyncMock(side_effect=RuntimeError("no session"))

        await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        state.sessions.discard_conversation.assert_awaited_once()
        assert slot._pending_reset_history_key == "old-key"
        assert slot._pending_discard_conversation_key is None


def _subs(running, *, queued: int = 0):
    """A subagent registry stub shaped like the two probes the guard reads."""
    subs = MagicMock()
    subs.running_agents_for = MagicMock(return_value=running)
    subs._queued_depth = MagicMock(return_value=queued)
    return subs


class TestConsumePendingDiscardBoundary:
    """The discard is a full provider teardown, so it is consumed ONLY at the
    end-of-turn boundary. The other two consume points run just before a turn
    acquires the session — a channel turn (Slack, Discord) runs on the linked
    session with no dashboard task at all, so a teardown there lands under a
    reply that is still streaming and loses it."""

    @pytest.mark.asyncio
    async def test_default_caller_may_not_consume_a_discard(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot._pending_discard_conversation_key = "dashboard:chat-cov-1"

        torn_down = await chat_runner._consume_pending_reset(state, slot)

        state.sessions.discard_conversation.assert_not_awaited()
        assert slot._pending_discard_conversation_key == "dashboard:chat-cov-1"
        assert torn_down is False

    @pytest.mark.asyncio
    async def test_the_end_of_turn_caller_may(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot._pending_discard_conversation_key = "dashboard:chat-cov-1"

        torn_down = await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        state.sessions.discard_conversation.assert_awaited_once_with(
            "dashboard:chat-cov-1", replay=False, skip_if_busy=True
        )
        assert torn_down is True

    @pytest.mark.asyncio
    async def test_the_default_caller_still_consumes_a_project_reset(self, tmp_path):
        """Scope pin: only the DISCARD moved to the end-of-turn boundary. The
        project reset must still run before get_or_create or the turn would reuse
        the stale session for one turn."""
        state, slot = _state(tmp_path), _slot()
        slot._pending_reset_history_key = "dashboard:chat-cov-1"

        torn_down = await chat_runner._consume_pending_reset(state, slot)

        state.sessions.reset.assert_awaited_once_with("dashboard:chat-cov-1", skip_if_busy=True)
        assert torn_down is True

    @pytest.mark.asyncio
    async def test_the_discard_goes_through_the_atomic_skip_if_busy_path(self, tmp_path):
        """The busy-check and the teardown must be ONE step under the session
        lock. Probing here and tearing down afterwards leaves a window in which a
        channel turn acquires the session's semaphore and begins streaming, and
        the teardown then removes its provider. So the consumer must delegate the
        check rather than perform it."""
        state, slot = _state(tmp_path), _slot()
        slot._pending_discard_conversation_key = "slack:C1:123"

        await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        state.sessions.discard_conversation.assert_awaited_once_with(
            "slack:C1:123", replay=False, skip_if_busy=True
        )

    @pytest.mark.asyncio
    async def test_a_manager_refusal_leaves_the_discard_armed(self, tmp_path):
        """False from the manager means it refused under the lock — a turn was in
        flight. Nothing was torn down, so the flag must stay armed."""
        state, slot = _state(tmp_path), _slot()
        state.sessions.discard_conversation = AsyncMock(return_value=False)
        slot._pending_discard_conversation_key = "slack:C1:123"

        torn_down = await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        assert slot._pending_discard_conversation_key == "slack:C1:123"
        assert torn_down is False

    @pytest.mark.asyncio
    async def test_a_refused_discard_lands_at_a_later_boundary(self, tmp_path):
        """The refusal is a wait, not a cancellation."""
        state, slot = _state(tmp_path), _slot()
        state.sessions.discard_conversation = AsyncMock(return_value=False)
        slot._pending_discard_conversation_key = "slack:A"

        await chat_runner._consume_pending_reset(state, slot, allow_discard=True)
        assert slot._pending_discard_conversation_key == "slack:A"

        state.sessions.discard_conversation = AsyncMock(return_value=True)
        torn_down = await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        assert slot._pending_discard_conversation_key is None
        assert torn_down is True

    @pytest.mark.asyncio
    async def test_an_accepted_discard_reports_a_teardown(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot._pending_discard_conversation_key = "dashboard:chat-cov-1"

        torn_down = await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        assert torn_down is True
        assert slot._pending_discard_conversation_key is None


class TestConsumePendingDiscardWaitsForSubagents:
    """``discard_conversation`` releases the shared runtime sub-agent children
    run on, and turn end is exactly when they outlive their parent — the parent
    turn ends first, so ``slot.running`` is already False while they keep going.
    Consuming the discard there would kill their work, so an attached child
    leaves the flag ARMED and a later consume applies it."""

    @pytest.mark.asyncio
    async def test_running_child_leaves_the_discard_armed(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        state.subagents = _subs([{"id": "a1"}])
        slot._pending_discard_conversation_key = "dashboard:chat-cov-1"

        torn_down = await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        state.sessions.discard_conversation.assert_not_awaited()
        assert slot._pending_discard_conversation_key == "dashboard:chat-cov-1"
        assert torn_down is False

    @pytest.mark.asyncio
    async def test_queued_child_leaves_the_discard_armed(self, tmp_path):
        """A spawn held by the concurrency gate is absent from the running list
        yet WILL start on its own, so it counts as attached."""
        state, slot = _state(tmp_path), _slot()
        state.subagents = _subs([], queued=2)
        slot._pending_discard_conversation_key = "dashboard:chat-cov-1"

        await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        state.sessions.discard_conversation.assert_not_awaited()
        assert slot._pending_discard_conversation_key == "dashboard:chat-cov-1"

    @pytest.mark.asyncio
    async def test_inflight_result_delivery_leaves_the_discard_armed(self, tmp_path):
        """The last child can finish — emptying both probes — while its
        completion-event injection is still landing."""
        state, slot = _state(tmp_path), _slot()
        state.subagents = _subs([])
        slot._subagent_deliveries_inflight = 1
        slot._pending_discard_conversation_key = "dashboard:chat-cov-1"

        await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        state.sessions.discard_conversation.assert_not_awaited()
        assert slot._pending_discard_conversation_key == "dashboard:chat-cov-1"

    @pytest.mark.asyncio
    async def test_a_failing_running_probe_leaves_the_discard_armed(self, tmp_path):
        """A None running-probe is the probe FAILING, not a slot with no
        children. Fail closed — unknown children are not zero children."""
        state, slot = _state(tmp_path), _slot()
        state.subagents = _subs(None)
        slot._pending_discard_conversation_key = "dashboard:chat-cov-1"

        await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        state.sessions.discard_conversation.assert_not_awaited()
        assert slot._pending_discard_conversation_key == "dashboard:chat-cov-1"

    @pytest.mark.asyncio
    async def test_an_unreadable_queue_leaves_the_discard_armed(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        subs = _subs([])
        subs._queued_depth = MagicMock(side_effect=RuntimeError("queue unreadable"))
        state.subagents = subs
        slot._pending_discard_conversation_key = "dashboard:chat-cov-1"

        await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        state.sessions.discard_conversation.assert_not_awaited()
        assert slot._pending_discard_conversation_key == "dashboard:chat-cov-1"

    @pytest.mark.asyncio
    async def test_the_armed_discard_lands_once_the_children_are_gone(self, tmp_path):
        """The deferral is a wait, not a cancellation: the caller's reset still
        happens, at the next consume that finds no children."""
        state, slot = _state(tmp_path), _slot()
        state.subagents = _subs([{"id": "a1"}])
        slot._pending_discard_conversation_key = "dashboard:chat-cov-1"

        await chat_runner._consume_pending_reset(state, slot, allow_discard=True)
        state.sessions.discard_conversation.assert_not_awaited()

        state.subagents = _subs([])
        torn_down = await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        state.sessions.discard_conversation.assert_awaited_once_with(
            "dashboard:chat-cov-1", replay=False, skip_if_busy=True
        )
        assert slot._pending_discard_conversation_key is None
        assert torn_down is True

    @pytest.mark.asyncio
    async def test_no_children_applies_the_discard(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        state.subagents = _subs([])
        slot._pending_discard_conversation_key = "dashboard:chat-cov-1"

        await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        state.sessions.discard_conversation.assert_awaited_once_with(
            "dashboard:chat-cov-1", replay=False, skip_if_busy=True
        )

    @pytest.mark.asyncio
    async def test_the_project_reset_is_deferred_on_children(self, tmp_path):
        """The project reset releases the shared runtime attached children run
        on, so a slot with children leaves the flag armed for a later consume
        (the same policy as the discard branch, and what
        test_attached_subagents_defer_the_pending_reset pins)."""
        state, slot = _state(tmp_path), _slot()
        state.subagents = _subs([{"id": "a1"}])
        slot._pending_reset_history_key = "dashboard:chat-cov-1"

        torn_down = await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        state.sessions.reset.assert_not_awaited()
        assert slot._pending_reset_history_key == "dashboard:chat-cov-1"
        assert torn_down is False
        _entry = chat_runner._pending_reset_retries.pop(slot.key, None)
        if _entry is not None:
            _entry[1].cancel()
            try:
                # flake-ok: pre-existing await of a task the test has just cancelled, moved verbatim; tracked for a de-flake pass
                await _entry[1]
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_both_deferrals_stay_armed_when_children_are_attached(self, tmp_path):
        """Both queued, children attached: neither the project reset nor the
        discard runs — both release the shared runtime attached children run
        on — so both flags stay armed for a later consume."""
        state, slot = _state(tmp_path), _slot()
        state.subagents = _subs([{"id": "a1"}])
        slot._pending_reset_history_key = "dashboard:chat-cov-1"
        slot._pending_discard_conversation_key = "dashboard:chat-cov-1"

        torn_down = await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        state.sessions.reset.assert_not_awaited()
        state.sessions.discard_conversation.assert_not_awaited()
        assert slot._pending_reset_history_key == "dashboard:chat-cov-1"
        assert slot._pending_discard_conversation_key == "dashboard:chat-cov-1"
        assert torn_down is False
        _entry = chat_runner._pending_reset_retries.pop(slot.key, None)
        if _entry is not None:
            _entry[1].cancel()
            try:
                # flake-ok: pre-existing await of a task the test has just cancelled, moved verbatim; tracked for a de-flake pass
                await _entry[1]
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_no_registry_abstains_rather_than_blocking_forever(self, tmp_path):
        """No registry means no runtime for a child to be attached to. Blocking
        on that would arm a discard that could never land."""
        state, slot = _state(tmp_path), _slot()
        state.subagents = None
        slot._pending_discard_conversation_key = "dashboard:chat-cov-1"

        await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        state.sessions.discard_conversation.assert_awaited_once()


# ── eager spawn / resume prefetch ─────────────────────────────────────────


class TestScheduleEagerSpawn:
    @staticmethod
    def _prime(enabled: bool) -> None:
        """Adopt a config carrying *enabled* on the process config watcher.

        The gate reads ``session.eager_spawn`` from the watcher's snapshot, a
        plain attribute read, because it runs on the event loop. The autouse
        watcher reset in ``test/conftest.py`` clears this after every test.
        """
        cfg = chat_runner.KiroCrewConfig()
        cfg.session.eager_spawn = enabled
        live.watch().prime(cfg)

    def test_disabled_config_returns_no_task(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        self._prime(False)

        assert chat_runner.schedule_eager_spawn(state, slot) is None

    def test_config_read_failure_returns_no_task(self, monkeypatch, tmp_path):
        """A failed read is swallowed: a speculative pre-warm must not raise
        into a slot-signal handler, which has a frame to answer."""
        state, slot = _state(tmp_path), _slot()
        monkeypatch.setattr(live, "snapshot", MagicMock(side_effect=RuntimeError("bad toml")))

        assert chat_runner.schedule_eager_spawn(state, slot) is None

    @pytest.mark.asyncio
    async def test_a_newer_signal_cancels_the_pending_task(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        self._prime(True)

        with patch.object(chat_runner, "_eager_spawn", new=AsyncMock()):
            first = chat_runner.schedule_eager_spawn(state, slot)
            second = chat_runner.schedule_eager_spawn(state, slot)
            # flake-ok: pre-existing zero-length yield that lets a task the call scheduled run, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0)

        assert first is not None and second is not None
        assert first.cancelled() or first.done()
        second.cancel()


class TestCapArmedPrefetches:
    @pytest.mark.asyncio
    async def test_eviction_failure_keeps_the_registry_entry(self, tmp_path):
        """A removal that raises leaves its entry registered: the process is
        still live, so it still counts, and the next eviction retries it."""
        state = _state(tmp_path)
        state.sessions.remove_if_unclaimed = AsyncMock(side_effect=RuntimeError("shutdown hung"))
        chat_runner._armed_prefetches.clear()
        try:
            for i in range(chat_runner._RESUME_PREFETCH_MAX_LIVE + 1):
                await chat_runner._cap_armed_prefetches(state.sessions, f"key-{i}")

            assert len(chat_runner._armed_prefetches) == chat_runner._RESUME_PREFETCH_MAX_LIVE + 1
            assert "key-0" in chat_runner._armed_prefetches
            # Only the oldest is attempted per pass; a failure stops the pass.
            assert state.sessions.remove_if_unclaimed.await_count == 1

            state.sessions.remove_if_unclaimed = AsyncMock(return_value=True)
            await chat_runner._cap_armed_prefetches(state.sessions, "newest")
            assert "key-0" not in chat_runner._armed_prefetches
            assert len(chat_runner._armed_prefetches) == chat_runner._RESUME_PREFETCH_MAX_LIVE
        finally:
            chat_runner._armed_prefetches.clear()

    @pytest.mark.asyncio
    async def test_rearming_moves_a_key_to_newest(self, tmp_path):
        state = _state(tmp_path)
        state.sessions.remove_if_unclaimed = AsyncMock(return_value=True)
        chat_runner._armed_prefetches.clear()
        try:
            for i in range(chat_runner._RESUME_PREFETCH_MAX_LIVE):
                await chat_runner._cap_armed_prefetches(state.sessions, f"key-{i}")
            await chat_runner._cap_armed_prefetches(state.sessions, "key-0")
            await chat_runner._cap_armed_prefetches(state.sessions, "newest")

            assert "key-0" in chat_runner._armed_prefetches
            assert "key-1" not in chat_runner._armed_prefetches
        finally:
            chat_runner._armed_prefetches.clear()


class TestPrefetchTtl:
    @pytest.mark.asyncio
    async def test_replaced_slot_owner_keeps_the_session(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        state.get_slot = MagicMock(return_value=_slot("other"))

        # flake-ok: pre-existing worker-wide patch of the stdlib asyncio.sleep through chat_runner's asyncio, moved verbatim; tracked for a de-flake pass
        with patch.object(chat_runner.asyncio, "sleep", new=AsyncMock()):
            await chat_runner._prefetch_ttl(state, slot, "dashboard:chat-cov-1")

        state.sessions.remove_if_unclaimed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_running_turn_claims_the_session(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        state.get_slot = MagicMock(return_value=slot)
        slot.task = MagicMock(done=MagicMock(return_value=False))

        # flake-ok: pre-existing worker-wide patch of the stdlib asyncio.sleep through chat_runner's asyncio, moved verbatim; tracked for a de-flake pass
        with patch.object(chat_runner.asyncio, "sleep", new=AsyncMock()):
            await chat_runner._prefetch_ttl(state, slot, "dashboard:chat-cov-1")

        state.sessions.remove_if_unclaimed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_deleted_slot_still_tears_down_a_linked_session(self, tmp_path):
        """A channel-born slot's prefetch key is not the slot-derived one."""
        state, slot = _state(tmp_path), _slot()
        state.get_slot = MagicMock(return_value=None)
        state.sessions.remove_if_unclaimed = AsyncMock(return_value=True)

        # flake-ok: pre-existing worker-wide patch of the stdlib asyncio.sleep through chat_runner's asyncio, moved verbatim; tracked for a de-flake pass
        with patch.object(chat_runner.asyncio, "sleep", new=AsyncMock()):
            await chat_runner._prefetch_ttl(state, slot, "telegram:123")

        state.sessions.remove_if_unclaimed.assert_awaited_once_with("telegram:123")

    @pytest.mark.asyncio
    async def test_cancellation_propagates(self, tmp_path):
        state, slot = _state(tmp_path), _slot()

        # flake-ok: pre-existing worker-wide patch of the stdlib asyncio.sleep through chat_runner's asyncio, moved verbatim; tracked for a de-flake pass
        with patch.object(
            chat_runner.asyncio, "sleep", new=AsyncMock(side_effect=asyncio.CancelledError)
        ):
            with pytest.raises(asyncio.CancelledError):
                await chat_runner._prefetch_ttl(state, slot, "dashboard:chat-cov-1")

    @pytest.mark.asyncio
    async def test_unexpected_failure_is_logged_not_raised(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        state.get_slot = MagicMock(side_effect=RuntimeError("map gone"))

        # flake-ok: pre-existing worker-wide patch of the stdlib asyncio.sleep through chat_runner's asyncio, moved verbatim; tracked for a de-flake pass
        with patch.object(chat_runner.asyncio, "sleep", new=AsyncMock()):
            await chat_runner._prefetch_ttl(state, slot, "dashboard:chat-cov-1")

    @pytest.mark.asyncio
    async def test_scheduling_cancels_the_previous_timer(self, tmp_path):
        state, slot = _state(tmp_path), _slot()

        with patch.object(chat_runner, "_prefetch_ttl", new=AsyncMock()):
            chat_runner._schedule_prefetch_ttl(state, slot, "k1")
            first = slot._prefetch_ttl_task
            chat_runner._schedule_prefetch_ttl(state, slot, "k2")
            second = slot._prefetch_ttl_task
            # flake-ok: pre-existing zero-length yield that lets a task the call scheduled run, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0)

        assert first is not second
        assert first.cancelled() or first.done()
        second.cancel()


# ── steer settle / requeue ────────────────────────────────────────────────


class TestSteerLifecycle:
    def test_settle_is_a_noop_without_pending_steers(self):
        slot = _slot()

        chat_runner._settle_consumed_steers(slot, "anything")

        assert slot._pending_steers == []

    def test_settle_delegates_to_the_shared_rules(self):
        slot = _slot()
        slot._pending_steers = ["a", "b"]

        with patch.object(chat_runner, "settle_consumed_steers", return_value=["b"]) as settle:
            chat_runner._settle_consumed_steers(slot, "a")

        assert slot._pending_steers == ["b"]
        # No opt-out kwarg: the shared rules settle nothing on an empty echo,
        # and this call site must not select anything else.
        assert settle.call_args.kwargs == {}

    def test_a_settled_steer_drops_its_possibly_delivered_mark(self):
        """A steer the turn consumed is settled: a later identical text must not
        inherit the possibly-delivered note."""
        slot = _slot()
        slot._pending_steers = ["a"]
        slot._steer_possibly_delivered = {"a"}

        with patch.object(chat_runner, "settle_consumed_steers", return_value=[]):
            chat_runner._settle_consumed_steers(slot, "a")

        assert slot._steer_possibly_delivered == set()

    def test_a_steer_requeued_with_its_rpc_in_flight_is_possibly_delivered(self, tmp_path):
        """The turn ended while the steer's RPC was still writing: its frame may
        already be in the pipe and the RPC's own verdict lands after the entry
        may have drained, so the requeue marks it now."""
        from kiro_crew.dashboard.chat_utils import STEER_POSSIBLY_DELIVERED_META

        state, slot = _state(tmp_path), _slot()
        slot._pending_steers = ["in flight", "settled"]
        slot._steer_rpc_in_flight = {"in flight"}

        chat_runner._requeue_unconsumed_steers(state, slot)

        marks = {
            e["content"]: bool((e.get("meta") or {}).get(STEER_POSSIBLY_DELIVERED_META))
            for e in slot._queue
        }
        assert marks == {"in flight": True, "settled": False}

    def test_a_possibly_delivered_steer_drains_alone(self, tmp_path):
        """Its note speaks for one message, so a merge never folds the user's
        later messages under it."""
        from kiro_crew.dashboard.chat_utils import _dequeue_next_message

        state, slot = _state(tmp_path), _slot()
        slot.queue_append("later one")
        slot._pending_steers = ["the steer"]
        slot._steer_possibly_delivered = {"the steer"}
        chat_runner._requeue_unconsumed_steers(state, slot)
        slot.queue_append("later two")

        content, consumed = _dequeue_next_message(slot, merge_enabled=True)
        assert content == "the steer" and len(consumed) == 1
        content, consumed = _dequeue_next_message(slot, merge_enabled=True)
        assert len(consumed) == 2

    def test_requeue_is_a_noop_without_pending_steers(self, tmp_path):
        state, slot = _state(tmp_path), _slot()

        chat_runner._requeue_unconsumed_steers(state, slot)

        assert slot._queue == []

    def test_unconsumed_steers_requeue_at_the_head_in_order(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot.queue_append("already queued")
        slot._pending_steers = ["first steer", "second steer"]

        chat_runner._requeue_unconsumed_steers(state, slot)

        assert [entry["content"] for entry in slot._queue] == [
            "first steer",
            "second steer",
            "already queued",
        ]
        assert slot._pending_steers == []

    def test_broadcast_failure_still_leaves_the_steer_queued(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot._pending_steers = ["steer me"]
        state.broadcast_ws = MagicMock(side_effect=RuntimeError("ws closed"))

        chat_runner._requeue_unconsumed_steers(state, slot)

        assert [entry["content"] for entry in slot._queue] == ["steer me"]
