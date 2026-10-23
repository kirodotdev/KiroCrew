"""The one queue-held gate: deriving the hold, and the idle-send gate that honours it."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_runner_coverage_helpers import _slot, _state
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard import chat_runner, chat_utils_completion
from kiro_crew.dashboard.chat_utils import (
    effective_session_key,
    teardown_hold_active,
)
from kiro_crew.dashboard.chat_utils_completion import SYNTHESIS_CLEAR, chat_done_payload
from kiro_crew.dashboard.chat_utils_injected_rows import (
    CRON_NOTIFICATION_KIND,
    STEER_INTENT_META,
)
from kiro_crew.start_priority import StartPriority
from kiro_crew.testing.wait import async_wait_until, await_until


class TestTeardownHoldActive:
    def test_a_pending_reset_does_not_hold(self, tmp_path, monkeypatch):
        """Every turn consumes a deferred reset before it acquires a session, so
        holding on one adds nothing and parks the queue whenever it cannot land."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")

        slot._pending_reset_history_key = "s1"

        assert teardown_hold_active(state, slot) is False
        assert teardown_hold_active(state, slot) is False

    def test_a_pending_discard_holds(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")

        slot._pending_discard_conversation_key = "s1"

        assert teardown_hold_active(state, slot) is True

    def test_clearing_a_cause_elsewhere_releases_the_hold_with_no_recompute(
        self, tmp_path, monkeypatch
    ):
        """The reason this is a predicate and not a stored flag.

        A cause is cleared at sites that know nothing about the queue
        (``eager_spawn``, the slash-command path). A cached copy would stay True
        until something recomputed it, leaving a queue nothing drains.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._pending_discard_conversation_key = "s1"
        assert teardown_hold_active(state, slot) is True

        slot._pending_discard_conversation_key = None

        assert teardown_hold_active(state, slot) is False

    def test_no_cause_does_not_hold(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")

        assert teardown_hold_active(state, slot) is False


@pytest.mark.asyncio
class TestSynthesisConsultsTheHold:
    """The automatic synthesis turn drains the queue too, so a held queue must
    withhold the dispatch: otherwise the prompts the hold kept back are dequeued
    behind synthesis and reach the same failure."""

    @staticmethod
    def _no_children() -> MagicMock:
        return MagicMock(
            running_agents_for=MagicMock(return_value=[]),
            queued_count_for_async=AsyncMock(return_value=0),
            queued_count_or_none_async=AsyncMock(return_value=0),
            has_in_memory_pending_work_for=MagicMock(return_value=False),
            published_queued_depths=MagicMock(return_value={}),
        )

    async def test_a_held_queue_withholds_the_synthesis_dispatch(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        state._slots[slot.key] = slot
        slot._pending_synthesis = True
        state.subagents = self._no_children()
        slot.queue_append("held until the discard lands")
        slot._pending_discard_conversation_key = slot.key

        with patch.object(chat_runner, "_run_pending_synthesis", new=AsyncMock()):
            await chat_runner._finish_queue_cycle(state, slot)
            await asyncio.sleep(0)

        assert slot._synthesis_inflight is False
        assert slot._pending_synthesis is True
        if slot.task is not None:
            slot.task.cancel()

    async def test_the_recheck_timer_does_not_fire_synthesis_while_the_queue_is_held(
        self, tmp_path, monkeypatch
    ):
        """The recheck reaches the dispatch WITHOUT passing the fire site.

        `_finish_queue_cycle` arms this timer when the store is unreadable, so
        a cycle can be held and still have a timer pending. The timer calls
        `_launch_synthesis` directly, so it must read the hold itself or it
        drains the held queue behind the synthesis turn.
        """
        monkeypatch.setattr(chat_runner, "_SYNTHESIS_RECHECK_SECS", 0.01)
        state, slot = _state(tmp_path), _slot()
        state._slots[slot.key] = slot
        slot._pending_synthesis = True
        state.subagents = self._no_children()
        slot.queue_append("held behind the discard")
        slot._pending_discard_conversation_key = slot.key

        launched: list[str] = []
        with (
            patch.object(chat_runner, "_launch_synthesis", lambda _st, sl: launched.append(sl.key)),
            patch.object(
                chat_runner, "synthesis_fire_verdict", AsyncMock(return_value=SYNTHESIS_CLEAR)
            ),
        ):
            chat_runner._arm_synthesis_recheck(state, slot)
            assert slot._synthesis_recheck is not None
            # Barrier, not a sleep: `_recheck` clears this handle as its first
            # statement, so observing None proves the timer fired and the body
            # ran. Without it the negative below could pass because nothing
            # happened yet.
            await async_wait_until(
                lambda: slot._synthesis_recheck is None,
                interval=0.005,
                describe=lambda: "the recheck timer to fire",
            )

        assert launched == [], "the recheck fired synthesis while the queue was held"
        assert [entry.get("content") for entry in slot._queue] == ["held behind the discard"]
        if slot.task is not None:
            slot.task.cancel()

    async def test_a_send_arriving_during_the_done_read_does_not_bypass_the_hold(self, tmp_path):
        """The arrival is dispatched by ID, which reads as an explicit request.

        Naming it at the choke point turns the teardown hold off, so the prompt
        would be answered by the conversation the person asked to drop. Being
        their next send does not change which conversation answers it.
        """
        state, slot = _state(tmp_path), _slot()
        state._slots[slot.key] = slot
        state.subagents = self._no_children()
        slot._pending_discard_conversation_key = slot.key
        arrivals = {"count": 0}

        async def done_payload_that_races(_st, sl, **_kwargs):
            # The send lands inside the awaited child-store read, exactly the
            # window the dispatch below exists for.
            arrivals["count"] += 1
            if arrivals["count"] == 1:
                sl.queue_append("sent during the done read")
            return {}

        started: list[object] = []

        async def record_start(_st, _sl, **kwargs):
            started.append(kwargs.get("required_queue_id"))
            return False

        with (
            patch.object(chat_runner, "chat_done_payload", new=done_payload_that_races),
            patch.object(chat_runner, "_start_next_queued_turn", new=record_start),
        ):
            await chat_runner._finish_queue_cycle(state, slot, drain=False)

        assert started == [], "the arrival was dispatched past the teardown hold"
        assert [entry.get("content") for entry in slot._queue] == ["sent during the done read"]
        if slot.task is not None:
            slot.task.cancel()

    async def test_a_send_arriving_during_the_done_read_runs_when_nothing_is_held(self, tmp_path):
        """Positive control: the arrival still gets the floor with no hold on."""
        state, slot = _state(tmp_path), _slot()
        state._slots[slot.key] = slot
        state.subagents = self._no_children()
        arrivals = {"count": 0}

        async def done_payload_that_races(_st, sl, **_kwargs):
            arrivals["count"] += 1
            if arrivals["count"] == 1:
                sl.queue_append("sent during the done read")
            return {}

        started: list[object] = []

        async def record_start(_st, _sl, **kwargs):
            started.append(kwargs.get("required_queue_id"))
            return False

        with (
            patch.object(chat_runner, "chat_done_payload", new=done_payload_that_races),
            patch.object(chat_runner, "_start_next_queued_turn", new=record_start),
        ):
            await chat_runner._finish_queue_cycle(state, slot, drain=False)

        assert started and started[0] is not None, "the arrival never got the floor"
        if slot.task is not None:
            slot.task.cancel()

    async def test_synthesis_still_dispatches_when_nothing_is_held(self, tmp_path):
        """Positive control for the gate above."""
        state, slot = _state(tmp_path), _slot()
        state._slots[slot.key] = slot
        slot._pending_synthesis = True
        state.subagents = self._no_children()

        with patch.object(chat_runner, "_run_pending_synthesis", new=AsyncMock()):
            await chat_runner._finish_queue_cycle(state, slot)
            await asyncio.sleep(0)

        assert slot._synthesis_inflight is True
        if slot.task is not None:
            slot.task.cancel()


@pytest.mark.asyncio
class TestIdleSendTeardownGate:
    async def test_an_idle_send_is_not_answered_by_a_conversation_awaiting_discard(
        self, tmp_path, monkeypatch
    ):
        """Fails on main: the send dispatches into the conversation the discard
        is about to drop, and is answered from it."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._pending_discard_conversation_key = "s1"
        state.sessions.discard_conversation = AsyncMock(return_value=False)
        app = _make_app(state)

        ran: list[str] = []

        async def fake_run_chat(st, sl, msg, **_kwargs):
            ran.append(msg)

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)

        try:
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/chat?ws=1",
                    json={"message": "hold me", "slot": "s1"},
                )
                assert resp.status == 200
                body = await resp.json()
        finally:
            retry = chat_runner._pending_reset_retries.pop(slot.key, None)
            if retry is not None:
                retry[1].cancel()

        assert ran == []
        assert body.get("queued") is True

    async def test_a_steer_send_is_not_answered_by_a_conversation_awaiting_discard(
        self, tmp_path, monkeypatch
    ):
        """The steer opt-out waives the SUB-AGENT wait, not the discard.

        The composer's Steer action on an idle slot with children still running
        posts ``steer: true``, and attached children are the main reason a
        discard is deferred -- so that is precisely the state in which the
        opt-out would otherwise answer from the conversation being dropped.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._pending_discard_conversation_key = "s1"
        state.sessions.discard_conversation = AsyncMock(return_value=False)
        app = _make_app(state)

        ran: list[str] = []

        async def fake_run_chat(st, sl, msg, **_kwargs):
            ran.append(msg)

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)

        try:
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/chat?ws=1",
                    json={"message": "steer past the children", "slot": "s1", "steer": True},
                )
                assert resp.status == 200
                body = await resp.json()
        finally:
            retry = chat_runner._pending_reset_retries.pop(slot.key, None)
            if retry is not None:
                retry[1].cancel()

        assert ran == [], "the steer send was answered by the conversation awaiting discard"
        assert body.get("queued") is True

    async def test_a_steer_send_still_skips_the_subagent_wait(self, tmp_path, monkeypatch):
        """Positive control: with no discard armed, the opt-out still works --
        otherwise the test above would pass for a steer that never dispatches."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")
        state.subagents = MagicMock(
            running_agents_for=MagicMock(return_value=["a1"]),
            queued_count_for_async=AsyncMock(return_value=0),
            queued_count_or_none_async=AsyncMock(return_value=0),
            has_in_memory_pending_work_for=MagicMock(return_value=False),
            published_queued_depths=MagicMock(return_value={}),
        )
        app = _make_app(state)

        ran: list[str] = []

        async def fake_run_chat(st, sl, msg, **_kwargs):
            ran.append(msg)

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat?ws=1",
                json={"message": "steer past the children", "slot": "s1", "steer": True},
            )
            assert resp.status == 200

        assert ran == ["steer past the children"], "the steer opt-out stopped working"

    async def test_an_idle_send_queues_behind_a_deferred_discard_until_the_retry_lands_it(
        self, tmp_path, monkeypatch
    ):
        """The send does not answer a conversation awaiting discard. It queues,
        and the retry it arms lands the discard and drains it."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr(chat_runner, "_PENDING_RESET_RETRY_DELAY_SECS", 0.01)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._pending_discard_conversation_key = "s1"
        released = asyncio.Event()

        async def discard_when_released(*_args, **_kwargs):
            await released.wait()
            return True

        state.sessions.discard_conversation = discard_when_released
        monkeypatch.setattr(chat_runner, "subagents_attached_async", AsyncMock(return_value=False))

        ran: list[str] = []

        async def fake_run_chat(st, sl, msg, **_kwargs):
            ran.append(msg)

        app = _make_app(state)
        monkeypatch.setattr(chat_runner, "_run_chat", fake_run_chat)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat?ws=1",
                json={"message": "hold me", "slot": "s1"},
            )
            assert resp.status == 200
            assert (await resp.json())["queued"] is True
            assert ran == []

            retry = chat_runner._pending_reset_retries.get(slot.key)
            assert retry is not None and retry[0] is slot
            released.set()
            await await_until(retry[1], what="the discard retry")
            if slot.task is not None:
                await await_until(slot.task, what="the drained turn")

        assert slot._pending_discard_conversation_key is None
        assert ran == ["hold me"]

    async def test_an_idle_send_wakes_an_armed_retry_instead_of_waiting_for_its_tick(
        self, tmp_path, monkeypatch
    ):
        """With the tick an hour away, only the send's wake can land the discard
        and run the prompt it queued."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr(chat_runner, "_PENDING_RESET_RETRY_DELAY_SECS", 3600.0)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._pending_discard_conversation_key = "s1"
        state.sessions.discard_conversation = AsyncMock(return_value=True)
        monkeypatch.setattr(chat_runner, "subagents_attached_async", AsyncMock(return_value=False))

        ran: list[str] = []

        async def fake_run_chat(st, sl, msg, **_kwargs):
            ran.append(msg)

        app = _make_app(state)
        monkeypatch.setattr(chat_runner, "_run_chat", fake_run_chat)
        chat_runner._arm_pending_reset_retry(state, slot)
        retry = chat_runner._pending_reset_retries.get(slot.key)
        assert retry is not None and retry[0] is slot

        try:
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/chat?ws=1",
                    json={"message": "hold me", "slot": "s1"},
                )
                assert resp.status == 200
                assert (await resp.json())["queued"] is True
                await await_until(retry[1], what="the woken discard retry")
                if slot.task is not None:
                    await await_until(slot.task, what="the drained turn")
        finally:
            retry[1].cancel()

        assert slot._pending_discard_conversation_key is None
        assert ran == ["hold me"]

    async def test_a_wake_discard_behind_a_pending_question_does_not_hold_the_answer(
        self, tmp_path, monkeypatch
    ):
        """That discard waits for the card, and only the answer run as a turn
        clears the card, so queueing the answer would hold both for good."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._pending_discard_conversation_key = "s1"
        slot._pending_discard_from_wake = True
        slot._question_pending = {"q1": {"blocking": False}}

        assert teardown_hold_active(state, slot) is False

        ran: list[str] = []

        async def fake_run_chat(st, sl, msg, *, _directive_user_origin):
            ran.append(msg)

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat?ws=1",
                json={"message": "my answer", "slot": "s1"},
            )
            assert resp.status == 200
            assert (await resp.json()).get("queued") is not True

        assert ran == ["my answer"]
        assert slot._pending_discard_conversation_key == "s1"

    async def test_a_persons_own_discard_still_holds_behind_a_pending_question(
        self, tmp_path, monkeypatch
    ):
        """Negative control for the wake exemption above."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._pending_discard_conversation_key = "s1"
        slot._pending_discard_from_wake = False
        slot._question_pending = {"q1": {"blocking": False}}

        assert teardown_hold_active(state, slot) is True

    @pytest.mark.parametrize("has_session", [False, True])
    async def test_an_idle_send_dispatches_with_only_a_reset_armed(
        self, tmp_path, monkeypatch, has_session
    ):
        """A deferred reset is the dispatched turn's own pre-acquire consume to
        land, so the send never waits on it, whether or not it can land."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        armed = effective_session_key(slot)
        slot._pending_reset_history_key = armed
        state.sessions.reset = AsyncMock(return_value=False)
        state.sessions.has_session = MagicMock(return_value=has_session)

        ran: list[str] = []

        async def fake_run_chat(st, sl, msg, *, _directive_user_origin):
            ran.append(msg)

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)

        async with TestClient(TestServer(_make_app(state))) as client:
            first = await client.post(
                "/api/chat?ws=1",
                json={"message": "first", "slot": "s1"},
            )
            assert first.status == 200
            assert (await first.json()).get("queued") is not True

            second = await client.post(
                "/api/chat?ws=1",
                json={"message": "second", "slot": "s1"},
            )
            assert second.status == 200
            assert (await second.json()).get("queued") is not True

        assert ran == ["first", "second"]
        assert slot._pending_reset_history_key == armed

    async def test_a_signed_out_slot_does_not_hold_an_idle_send(self, tmp_path, monkeypatch):
        """The send gate reads the teardown cause only. Only a turn that runs
        clears the sign-out cause, so holding the send on it would hold the turn
        that clears it."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._last_turn_auth_required = True

        ran: list[str] = []

        async def fake_run_chat(st, sl, msg, *, _directive_user_origin):
            ran.append(msg)

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat?ws=1",
                json={"message": "after the sign-in failure", "slot": "s1"},
            )
            assert resp.status == 200
            assert (await resp.json()).get("queued") is not True

        assert ran == ["after the sign-in failure"]


@pytest.mark.asyncio
class TestDiscardFailureArmsTheRetry:
    async def test_a_raising_discard_still_releases_the_queue(self, tmp_path, monkeypatch):
        """Every branch that leaves the discard armed must arm the retry: on an
        idle slot nothing else ever consumes it, and the queue stays held."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr(chat_runner, "_PENDING_RESET_RETRY_DELAY_SECS", 0.01)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._pending_discard_conversation_key = "s1"
        slot.queue_append("parked behind the failed discard")
        state.sessions.discard_conversation = AsyncMock(side_effect=[RuntimeError("boom"), True])
        monkeypatch.setattr(chat_runner, "subagents_attached_async", AsyncMock(return_value=False))

        ran: list[str] = []

        async def fake_run_chat(_st, _sl, msg, **_kwargs):
            ran.append(msg)

        monkeypatch.setattr(chat_runner, "_run_chat", fake_run_chat)

        assert await chat_runner._consume_pending_reset(state, slot, allow_discard=True) is False
        retry = chat_runner._pending_reset_retries.get(slot.key)
        assert retry is not None and retry[0] is slot
        await await_until(retry[1], what="the discard retry")
        if slot.task is not None:
            await await_until(slot.task, what="the drained turn")

        assert slot._pending_discard_conversation_key is None
        assert ran == ["parked behind the failed discard"]

    async def test_a_discard_that_always_raises_gives_up_the_hold_and_says_so(
        self, tmp_path, monkeypatch
    ):
        """A wedged provider must not park every send on the slot forever.

        The retry keeps its unbounded cadence for the project reset. The queue
        hold is what gets the budget, because a held queue answers nothing and
        reports nothing, so an endless one is worse than answering from a
        conversation we failed to drop.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr(chat_runner, "_PENDING_RESET_RETRY_DELAY_SECS", 0.001)
        monkeypatch.setattr(chat_runner, "_TEARDOWN_HOLD_MAX_TICKS", 3)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._pending_discard_conversation_key = "s1"
        slot.queue_append("parked behind the wedged discard")
        attempts: list[int] = []

        async def always_raises(*_args, **_kwargs):
            attempts.append(1)
            raise RuntimeError("provider wedged")

        state.sessions.discard_conversation = always_raises
        monkeypatch.setattr(chat_runner, "subagents_attached_async", AsyncMock(return_value=False))

        ran: list[str] = []

        async def fake_run_chat(_st, _sl, msg, **_kwargs):
            ran.append(msg)

        monkeypatch.setattr(chat_runner, "_run_chat", fake_run_chat)

        assert await chat_runner._consume_pending_reset(state, slot, allow_discard=True) is False
        retry = chat_runner._pending_reset_retries.get(slot.key)
        assert retry is not None and retry[0] is slot
        await await_until(retry[1], what="the discard retry")
        if slot.task is not None:
            await await_until(slot.task, what="the released turn")

        assert len(attempts) > 1
        assert teardown_hold_active(state, slot) is False
        assert ran == ["parked behind the wedged discard"]
        assert any(
            row.get("role") == "system"
            and "Could not drop the previous conversation" in row.get("content", "")
            for row in slot.messages
        )

    async def test_a_drain_that_starts_nothing_still_reaches_the_recheck(
        self, tmp_path, monkeypatch
    ):
        """A queue that was there is not a turn that ran.

        ``_start_next_queued_turn`` returns False having started nothing when
        its own admission sweep drops every entry, when memory preparation is
        pending, or when a superseded continuation is purged. Returning on the
        queue being non-empty strands the synthesis the hold withheld, whose
        only other trigger is a fresh send.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr(chat_runner, "_PENDING_RESET_RETRY_DELAY_SECS", 0.001)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._pending_discard_conversation_key = "s1"
        slot._pending_synthesis = True
        slot.queue_append("dropped by the admission sweep")
        state.sessions.discard_conversation = AsyncMock(return_value=True)
        monkeypatch.setattr(chat_runner, "subagents_attached_async", AsyncMock(return_value=False))

        async def drain_that_drops_everything(_st, sl, **_kwargs):
            # What the real sweep does: every entry has stopped qualifying, so
            # the queue empties and the drain declines with nothing started.
            sl._queue.clear()
            return False

        monkeypatch.setattr(chat_runner, "_start_next_queued_turn", drain_that_drops_everything)

        chat_runner._arm_pending_reset_retry(state, slot)
        retry = chat_runner._pending_reset_retries.get(slot.key)
        assert retry is not None
        try:
            await async_wait_until(
                lambda: slot._pending_discard_conversation_key is None,
                interval=0.001,
                describe=lambda: "the retry to land the discard",
            )
            await async_wait_until(
                lambda: slot._synthesis_recheck is not None,
                interval=0.001,
                describe=lambda: "the stranded synthesis to be re-armed",
            )
        finally:
            if slot._synthesis_recheck is not None:
                slot._synthesis_recheck.cancel()
            retry[1].cancel()

    async def test_a_drain_that_starts_a_turn_does_not_also_arm_the_recheck(
        self, tmp_path, monkeypatch
    ):
        """Positive control: the fall-through must not fire on every release.

        Without this, the test above passes for a release that arms the recheck
        unconditionally, which would double-dispatch the synthesis behind a turn
        that is already going to reach the fire site itself.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr(chat_runner, "_PENDING_RESET_RETRY_DELAY_SECS", 0.001)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._pending_discard_conversation_key = "s1"
        slot._pending_synthesis = True
        slot.queue_append("drained normally")
        state.sessions.discard_conversation = AsyncMock(return_value=True)
        monkeypatch.setattr(chat_runner, "subagents_attached_async", AsyncMock(return_value=False))

        started: list[bool] = []

        async def drain_that_starts_a_turn(_st, _sl, **_kwargs):
            started.append(True)
            return True

        monkeypatch.setattr(chat_runner, "_start_next_queued_turn", drain_that_starts_a_turn)

        chat_runner._arm_pending_reset_retry(state, slot)
        retry = chat_runner._pending_reset_retries.get(slot.key)
        assert retry is not None
        try:
            # Wait on the drain HAVING RUN, so "no recheck" cannot pass merely
            # because the release had not reached the branch yet.
            await async_wait_until(
                lambda: bool(started),
                interval=0.001,
                describe=lambda: "the release to reach the drain",
            )
            assert slot._synthesis_recheck is None, "the recheck fired behind a running turn"
        finally:
            retry[1].cancel()

    async def test_the_release_leaves_a_queue_held_for_sign_in_alone(self, tmp_path, monkeypatch):
        """A sign-in failure holds the queue independently of the discard.

        ``_start_next_queued_turn`` carries no auth check -- every drain site
        gates on ``_last_turn_auth_required`` at the caller -- so this release
        must too. Draining here fails every queued prompt against the
        signed-out CLI, and ``_requeue_auth_retry`` restores only
        runner-authored, synthesis and sub-agent inputs, so a person's prompt
        pulled in would drop out of the post-login resume entirely.

        The positive control is the test above: with the flag clear, the same
        retry does drain the same queue.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr(chat_runner, "_PENDING_RESET_RETRY_DELAY_SECS", 0.001)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._pending_discard_conversation_key = "s1"
        slot._last_turn_auth_required = True
        slot.queue_append("waiting for the user to sign in")
        state.sessions.discard_conversation = AsyncMock(return_value=True)
        monkeypatch.setattr(chat_runner, "subagents_attached_async", AsyncMock(return_value=False))

        ran: list[str] = []

        async def fake_run_chat(_st, _sl, msg, **_kwargs):
            ran.append(msg)

        monkeypatch.setattr(chat_runner, "_run_chat", fake_run_chat)

        chat_runner._arm_pending_reset_retry(state, slot)
        retry = chat_runner._pending_reset_retries.get(slot.key)
        assert retry is not None
        try:
            await async_wait_until(
                lambda: slot._pending_discard_conversation_key is None,
                interval=0.001,
                describe=lambda: "the retry to land the discard",
            )
            # The discard landed, so the teardown hold is over; only the
            # sign-in hold remains, and it must keep the prompt queued.
            assert ran == [], "the release drained a queue held for sign-in"
            assert [e.get("content") for e in slot._queue] == ["waiting for the user to sign in"]
        finally:
            retry[1].cancel()

    async def test_attached_children_never_spend_the_hold_budget(self, tmp_path, monkeypatch):
        """A sub-agent batch outlasts the budget, and the wait is deliberate.

        Landing the discard with children attached kills them, so the deferral
        is correct and spending the budget on it would throw away a reset that
        was waiting for exactly the right thing.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr(chat_runner, "_PENDING_RESET_RETRY_DELAY_SECS", 0.001)
        monkeypatch.setattr(chat_runner, "_TEARDOWN_HOLD_MAX_TICKS", 2)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._pending_discard_conversation_key = "s1"
        slot.queue_append("waiting on the children")
        attached = {"value": True}
        probes = {"count": 0}

        async def children_attached(*_args, **_kwargs):
            probes["count"] += 1
            return attached["value"]

        monkeypatch.setattr(chat_runner, "subagents_attached_async", children_attached)
        state.sessions.discard_conversation = AsyncMock(return_value=True)

        ran: list[str] = []

        async def fake_run_chat(_st, _sl, msg, **_kwargs):
            ran.append(msg)

        monkeypatch.setattr(chat_runner, "_run_chat", fake_run_chat)

        assert await chat_runner._consume_pending_reset(state, slot, allow_discard=True) is False
        retry = chat_runner._pending_reset_retries.get(slot.key)
        assert retry is not None

        # Barrier on the tick COUNT: twenty times the budget must really have
        # elapsed, or the assertion below passes because the retry barely ran.
        await async_wait_until(
            lambda: probes["count"] >= 40,
            interval=0.005,
            describe=lambda: f"40 structural-wait ticks, saw {probes['count']}",
        )
        assert teardown_hold_active(state, slot) is True, "the budget was spent on a real wait"
        assert not any(
            "Could not drop the previous conversation" in row.get("content", "")
            for row in slot.messages
        )

        # The children detach: the same retry lands the discard and drains.
        attached["value"] = False
        await await_until(retry[1], what="the discard retry")
        if slot.task is not None:
            await await_until(slot.task, what="the drained turn")

        assert slot._pending_discard_conversation_key is None
        assert ran == ["waiting on the children"]

    async def test_a_pending_card_never_spends_the_hold_budget(self, tmp_path, monkeypatch):
        """Only the person's answer clears the card, and a person can take hours."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr(chat_runner, "_PENDING_RESET_RETRY_DELAY_SECS", 0.001)
        monkeypatch.setattr(chat_runner, "_TEARDOWN_HOLD_MAX_TICKS", 2)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._pending_discard_conversation_key = "s1"
        slot._pending_discard_from_wake = True
        slot._question_pending = {"q1": {"blocking": False}}
        probes = {"count": 0}

        async def children_absent(*_args, **_kwargs):
            probes["count"] += 1
            return False

        monkeypatch.setattr(chat_runner, "subagents_attached_async", children_absent)
        state.sessions.discard_conversation = AsyncMock(return_value=True)

        assert await chat_runner._consume_pending_reset(state, slot, allow_discard=True) is False
        retry = chat_runner._pending_reset_retries.get(slot.key)
        assert retry is not None
        await async_wait_until(
            lambda: probes["count"] >= 40,
            interval=0.005,
            describe=lambda: f"40 card-deferred ticks, saw {probes['count']}",
        )

        assert slot._pending_discard_conversation_key == "s1"
        assert not any(
            "Could not drop the previous conversation" in row.get("content", "")
            for row in slot.messages
        )
        retry[1].cancel()

    async def test_a_long_project_reset_does_not_spend_a_later_discards_budget(
        self, tmp_path, monkeypatch
    ):
        """The budget belongs to one discard, not to the retry task.

        A reset armed while sub-agents run keeps the task alive for as long as
        the children take. Those ticks carry no discard and no hold, so they
        must not count: otherwise a discard armed afterwards inherits a spent
        budget and is given up on its first tick, which is the case the
        structural exemption exists to prevent.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr(chat_runner, "_PENDING_RESET_RETRY_DELAY_SECS", 0.001)
        monkeypatch.setattr(chat_runner, "_TEARDOWN_HOLD_MAX_TICKS", 3)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        # A reset that can never land: its key has no session, so every
        # consume declines and the retry task stays alive indefinitely.
        slot._pending_reset_history_key = "s1"
        state.sessions.reset = AsyncMock(return_value=False)
        probes = {"count": 0}

        async def children_attached(*_args, **_kwargs):
            probes["count"] += 1
            return True

        monkeypatch.setattr(chat_runner, "subagents_attached_async", children_attached)

        ran: list[str] = []

        async def fake_run_chat(_st, _sl, msg, **_kwargs):
            ran.append(msg)

        monkeypatch.setattr(chat_runner, "_run_chat", fake_run_chat)
        # Present from the start: while only a reset is owed the slot holds
        # nothing, so the budget must not count and the give-up's release must
        # never fire on this entry.
        slot.queue_append("not held by anything yet")

        chat_runner._arm_pending_reset_retry(state, slot)
        retry = chat_runner._pending_reset_retries.get(slot.key)
        assert retry is not None
        # Barrier on the tick COUNT, not the clock: ten times the budget must
        # actually have elapsed, or "the budget was not spent" passes for the
        # trivial reason that the retry barely ran.
        await async_wait_until(
            lambda: probes["count"] >= 30,
            interval=0.005,
            describe=lambda: f"30 retry ticks, saw {probes['count']}",
        )
        assert ran == [], "the budget was spent while only a reset was owed"

        # Only NOW does a discard arrive, onto a task that has ticked 30 times.
        slot._pending_discard_conversation_key = "s1"
        slot.queue_append("must not be released on the first tick")
        before = probes["count"]
        await async_wait_until(
            lambda: probes["count"] >= before + 4,
            interval=0.005,
            describe=lambda: "four more ticks with the discard armed",
        )

        assert teardown_hold_active(state, slot) is True
        assert not any(
            "Could not drop the previous conversation" in row.get("content", "")
            for row in slot.messages
        )
        retry[1].cancel()

    async def test_a_busy_linked_session_does_not_spend_the_budget_on_an_empty_queue(
        self, tmp_path, monkeypatch
    ):
        """A hold that withholds nothing has nothing to buy by giving up.

        A channel turn holds the LINKED SESSION without appearing on this slot,
        so the loop's own foreign-turn check cannot see it and every attempt is
        refused. With no queued entry and no held synthesis, spending the budget
        throws the person's reset away to free nothing, and the give-up's row
        then claims queued messages were released when none were.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr(chat_runner, "_PENDING_RESET_RETRY_DELAY_SECS", 0.001)
        monkeypatch.setattr(chat_runner, "_TEARDOWN_HOLD_MAX_TICKS", 3)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._pending_discard_conversation_key = "s1"
        # No queue, no synthesis: the hold is withholding nothing at all.
        assert not slot._queue
        assert not slot._pending_synthesis
        attempts = {"count": 0}

        async def busy_elsewhere(*_args, **_kwargs):
            # What skip_if_busy does when a channel turn holds the session:
            # refuse, leaving the discard armed. No turn is on this slot.
            attempts["count"] += 1
            return False

        state.sessions.discard_conversation = busy_elsewhere
        monkeypatch.setattr(chat_runner, "subagents_attached_async", AsyncMock(return_value=False))

        chat_runner._arm_pending_reset_retry(state, slot)
        retry = chat_runner._pending_reset_retries.get(slot.key)
        assert retry is not None
        # Barrier on the refusal COUNT: ten times the budget must have been
        # refused, or "the budget was not spent" passes because the loop had
        # not yet reached the cap.
        await async_wait_until(
            lambda: attempts["count"] >= 30,
            interval=0.005,
            describe=lambda: f"30 refused attempts, saw {attempts['count']}",
        )

        assert slot._pending_discard_conversation_key == "s1", "the reset was thrown away"
        assert teardown_hold_active(state, slot) is True
        assert not any(
            "Could not drop the previous conversation" in row.get("content", "")
            for row in slot.messages
        ), "a give-up row was posted for a queue that held nothing"
        retry[1].cancel()

    async def test_a_held_synthesis_spends_the_budget_without_claiming_queued_messages(
        self, tmp_path, monkeypatch
    ):
        """Positive control for the gate above, and the row must tell the truth.

        A held synthesis IS something withheld, so the budget still applies --
        the gate narrows what counts, it does not switch the budget off. The
        row, though, cannot say queued messages were released when the queue
        was empty and a synthesis was what the hold held back.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr(chat_runner, "_PENDING_RESET_RETRY_DELAY_SECS", 0.001)
        monkeypatch.setattr(chat_runner, "_TEARDOWN_HOLD_MAX_TICKS", 3)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._pending_discard_conversation_key = "s1"
        slot._pending_synthesis = True
        assert not slot._queue

        async def busy_elsewhere(*_args, **_kwargs):
            return False

        state.sessions.discard_conversation = busy_elsewhere
        monkeypatch.setattr(chat_runner, "subagents_attached_async", AsyncMock(return_value=False))
        monkeypatch.setattr(chat_runner, "_arm_synthesis_recheck", lambda *_a, **_k: None)

        chat_runner._arm_pending_reset_retry(state, slot)
        retry = chat_runner._pending_reset_retries.get(slot.key)
        assert retry is not None
        await async_wait_until(
            lambda: slot._pending_discard_conversation_key is None,
            interval=0.005,
            describe=lambda: "the budget to be spent on the held synthesis",
        )

        rows = [
            row.get("content", "")
            for row in slot.messages
            if "Could not drop the previous conversation" in row.get("content", "")
        ]
        assert len(rows) == 1, "the budget did not apply to a held synthesis"
        assert "queued messages" not in rows[0], rows[0]
        retry[1].cancel()

    async def test_the_queue_drains_when_the_discard_lands_and_a_reset_stays_armed(
        self, tmp_path, monkeypatch
    ):
        """The release keys on the DISCARD clearing, not on every teardown.

        A project reset on a key with no session declines forever, so waiting
        for both would strand a queue whose own hold has already ended.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr(chat_runner, "_PENDING_RESET_RETRY_DELAY_SECS", 0.001)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._pending_reset_history_key = "s1"
        slot._pending_discard_conversation_key = "s1"
        slot.queue_append("released when the discard lands")
        state.sessions.reset = AsyncMock(return_value=False)
        state.sessions.discard_conversation = AsyncMock(return_value=True)
        monkeypatch.setattr(chat_runner, "subagents_attached_async", AsyncMock(return_value=False))

        ran: list[str] = []

        async def fake_run_chat(_st, _sl, msg, **_kwargs):
            ran.append(msg)

        monkeypatch.setattr(chat_runner, "_run_chat", fake_run_chat)

        chat_runner._arm_pending_reset_retry(state, slot)
        retry = chat_runner._pending_reset_retries.get(slot.key)
        assert retry is not None
        await async_wait_until(
            lambda: ran == ["released when the discard lands"],
            interval=0.001,
            describe=lambda: "the queue to drain behind the still-armed reset",
        )

        assert slot._pending_discard_conversation_key is None
        # Still armed, though not necessarily under the same key: a consume
        # re-points a stale key at the slot's live session.
        assert slot._pending_reset_history_key is not None, "the reset is still owed"
        if slot.task is not None:
            await await_until(slot.task, what="the drained turn")
        retry[1].cancel()

    async def test_the_retry_waits_while_a_turn_holds_admission(self, tmp_path, monkeypatch):
        """`skip_if_busy` cannot see a turn that has reserved but not acquired.

        A wake's discard waits behind a card; the person's answer retires it and
        enters admission. Consuming the discard in that window gives the answer
        a fresh conversation with replay suppressed, losing the question it
        answers. The turn's own end is the right boundary.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr(chat_runner, "_PENDING_RESET_RETRY_DELAY_SECS", 0.001)
        monkeypatch.setattr(chat_runner, "_TEARDOWN_HOLD_MAX_TICKS", 2)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._pending_discard_conversation_key = "s1"
        slot._turn_admission_reserved = True
        assert slot.running is True, "the fixture must model a reserved turn"
        discard = AsyncMock(return_value=True)
        state.sessions.discard_conversation = discard
        monkeypatch.setattr(chat_runner, "subagents_attached_async", AsyncMock(return_value=False))

        # Per-tick observable: the loop re-reads the live slot every tick, so
        # counting that read proves ticks really happened. Without it the
        # assertion below would pass because the retry had not run at all.
        ticks = {"count": 0}
        real_get_slot = state.get_slot

        def counting_get_slot(key):
            ticks["count"] += 1
            return real_get_slot(key)

        monkeypatch.setattr(state, "get_slot", counting_get_slot)

        chat_runner._arm_pending_reset_retry(state, slot)
        retry = chat_runner._pending_reset_retries.get(slot.key)
        assert retry is not None
        await async_wait_until(
            lambda: ticks["count"] >= 15,
            interval=0.005,
            describe=lambda: f"15 ticks with admission reserved, saw {ticks['count']}",
        )

        assert discard.await_count == 0, "the retry tore down under a reserved turn"
        assert slot._pending_discard_conversation_key == "s1"
        assert not any(
            "Could not drop the previous conversation" in row.get("content", "")
            for row in slot.messages
        ), "the budget was spent while a turn held admission"

        # Positive half: once admission is released the same retry lands it, so
        # the wait above was a deferral and not a dead task.
        slot._turn_admission_reserved = False
        await await_until(retry[1], what="the discard retry")
        assert slot._pending_discard_conversation_key is None

    async def test_the_consume_rechecks_admission_after_its_awaited_probes(
        self, tmp_path, monkeypatch
    ):
        """The window is INSIDE the consume, after it has already awaited.

        A turn can reserve admission during the attached-children probe, and
        `skip_if_busy` cannot see one that has reserved but not acquired the
        session. Checking only before the probe leaves exactly the window the
        retry's own guard is meant to close.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._pending_discard_conversation_key = "s1"
        discard = AsyncMock(return_value=True)
        state.sessions.discard_conversation = discard

        async def reserve_during_the_probe(*_args, **_kwargs):
            # The turn lands while this await is in flight.
            slot._turn_admission_reserved = True
            return False

        monkeypatch.setattr(chat_runner, "subagents_attached_async", reserve_during_the_probe)

        assert await chat_runner._consume_pending_reset(state, slot, allow_discard=True) is False

        assert discard.await_count == 0, "the consume tore down under a turn admitted mid-probe"
        assert slot._pending_discard_conversation_key == "s1"
        retry = chat_runner._pending_reset_retries.get(slot.key)
        assert retry is not None, "the deferral must arm the retry"
        retry[1].cancel()

    async def test_the_admission_check_is_evaluated_under_the_registry_lock(
        self, tmp_path, monkeypatch
    ):
        """A turn can reserve admission while the discard WAITS for the lock.

        Acquiring the registry lock is not instant: another key's teardown
        holds it across `record_session_ended`. A check made before the call
        is therefore stale by the time the pop runs, and `skip_if_busy` reads
        the session's semaphore, which a reservation has not reached. So the
        caller hands its own check in and the teardown evaluates it in the
        same lock hold as the pop.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._pending_discard_conversation_key = "s1"
        monkeypatch.setattr(chat_runner, "subagents_attached_async", AsyncMock(return_value=False))

        seen: list[bool] = []

        async def discard_that_waits_for_the_lock(_key, **kwargs):
            refuse_if = kwargs.get("refuse_if")
            assert refuse_if is not None, "the caller did not hand its check to the teardown"
            # The reservation lands while the real call would be waiting on
            # `owner._lock`, so a pre-call answer is already stale here.
            slot._turn_admission_reserved = True
            await asyncio.sleep(0)
            seen.append(refuse_if())
            return not seen[-1]

        state.sessions.discard_conversation = discard_that_waits_for_the_lock

        assert await chat_runner._consume_pending_reset(state, slot, allow_discard=True) is False

        assert seen == [True], "the check did not see the reservation that landed in the wait"
        assert slot._pending_discard_conversation_key == "s1"
        retry = chat_runner._pending_reset_retries.get(slot.key)
        assert retry is not None, "the refusal must arm the retry"
        retry[1].cancel()

    async def test_that_check_still_answers_no_for_an_unreserved_slot(self, tmp_path, monkeypatch):
        """Positive control: the predicate must not refuse every teardown.

        Without this, the test above passes just as well for a predicate hard-wired
        to true, which would strand every discard the retry ever attempts.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._pending_discard_conversation_key = "s1"
        monkeypatch.setattr(chat_runner, "subagents_attached_async", AsyncMock(return_value=False))

        seen: list[bool] = []

        async def discard_that_waits_for_the_lock(_key, **kwargs):
            refuse_if = kwargs["refuse_if"]
            await asyncio.sleep(0)
            seen.append(refuse_if())
            return not seen[-1]

        state.sessions.discard_conversation = discard_that_waits_for_the_lock

        assert await chat_runner._consume_pending_reset(state, slot, allow_discard=True) is True

        assert seen == [False]
        assert slot._pending_discard_conversation_key is None

    async def test_a_discard_that_lands_on_a_later_attempt_keeps_the_hold(
        self, tmp_path, monkeypatch
    ):
        """Negative control: the budget must not fire on a discard still landing."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr(chat_runner, "_PENDING_RESET_RETRY_DELAY_SECS", 0.001)
        monkeypatch.setattr(chat_runner, "_TEARDOWN_HOLD_MAX_TICKS", 3)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._pending_discard_conversation_key = "s1"
        slot.queue_append("parked once")
        state.sessions.discard_conversation = AsyncMock(
            side_effect=[RuntimeError("boom"), RuntimeError("boom"), True]
        )
        monkeypatch.setattr(chat_runner, "subagents_attached_async", AsyncMock(return_value=False))

        ran: list[str] = []

        async def fake_run_chat(_st, _sl, msg, **_kwargs):
            ran.append(msg)

        monkeypatch.setattr(chat_runner, "_run_chat", fake_run_chat)

        assert await chat_runner._consume_pending_reset(state, slot, allow_discard=True) is False
        retry = chat_runner._pending_reset_retries.get(slot.key)
        assert retry is not None
        await await_until(retry[1], what="the discard retry")
        if slot.task is not None:
            await await_until(slot.task, what="the drained turn")

        assert ran == ["parked once"]
        assert not any(
            "Could not drop the previous conversation" in row.get("content", "")
            for row in slot.messages
        )


@pytest.mark.asyncio
class TestTheConsumeYieldsOnlyToAnotherTurn:
    """A turn-path caller is NEVER the task the slot records.

    ``slot.task`` is the outer ``spawn_guarded_turn`` task; ``_bounded_turn``
    runs the turn in a second task and ``run_to_completion`` runs
    ``_end_turn_tail`` in a third. So neither ``slot.running`` nor an
    ``asyncio.current_task()`` comparison can recognise the slot's own turn, and
    either one defers every end-of-turn discard and ``/clear`` to the retry. The
    caller declares it with ``own_turn``.
    """

    @staticmethod
    def _armed(tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._pending_discard_conversation_key = "s1"
        discard = AsyncMock(return_value=True)
        state.sessions.discard_conversation = discard
        monkeypatch.setattr(chat_runner, "subagents_attached_async", AsyncMock(return_value=False))
        return state, slot, discard

    async def test_the_turns_own_consume_lands_the_discard(self, tmp_path, monkeypatch):
        """``slot.task`` is live and is not this task -- production's shape."""
        state, slot, discard = self._armed(tmp_path, monkeypatch)
        outer = asyncio.ensure_future(asyncio.Event().wait())
        slot.task = outer
        assert slot.task is not asyncio.current_task()
        # `_pending_reset_retries` is module-global and a cancelled entry under
        # this key can outlive the test that armed it, so the claim is that THIS
        # call armed nothing new, not that the registry is empty.
        before = chat_runner._pending_reset_retries.get(slot.key)
        try:
            assert (
                await chat_runner._consume_pending_reset(
                    state, slot, allow_discard=True, own_turn=True
                )
                is True
            )

            assert discard.await_count == 1
            assert slot._pending_discard_conversation_key is None
            assert chat_runner._pending_reset_retries.get(slot.key) is before
        finally:
            outer.cancel()

    async def test_another_turns_consume_still_defers_the_discard(self, tmp_path, monkeypatch):
        """Negative control: the same live ``slot.task``, the retry's own
        ``own_turn=False``, must defer -- so the flag is what decides, not the task."""
        state, slot, discard = self._armed(tmp_path, monkeypatch)
        other = asyncio.ensure_future(asyncio.Event().wait())
        slot.task = other
        try:
            assert (
                await chat_runner._consume_pending_reset(state, slot, allow_discard=True) is False
            )

            assert discard.await_count == 0
            assert slot._pending_discard_conversation_key == "s1"
            retry = chat_runner._pending_reset_retries.get(slot.key)
            assert retry is not None, "the deferral must arm the retry"
            retry[1].cancel()
        finally:
            other.cancel()

    async def test_a_reservation_defers_even_the_turns_own_consume(self, tmp_path, monkeypatch):
        """``own_turn`` does not waive the reservation: ``chat_send`` clears its
        own in a ``finally`` that runs before the turn task's body, so one seen
        here belongs to a different turn."""
        state, slot, discard = self._armed(tmp_path, monkeypatch)
        slot._turn_admission_reserved = True

        assert (
            await chat_runner._consume_pending_reset(state, slot, allow_discard=True, own_turn=True)
            is False
        )

        assert discard.await_count == 0
        retry = chat_runner._pending_reset_retries.get(slot.key)
        assert retry is not None
        retry[1].cancel()

    async def test_the_real_end_of_turn_tail_lands_the_discard(self, tmp_path, monkeypatch):
        """The CALL SITE, not just the predicate.

        A predicate that recognises the slot's own turn is worth nothing if the
        turn path does not say it is one, and that is reachable only by running
        the tail itself: `_end_turn_tail` under a live `slot.task` that is not
        this task, exactly as `spawn_guarded_turn` leaves it.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._pending_discard_conversation_key = "s1"
        discard = AsyncMock(return_value=True)
        state.sessions.discard_conversation = discard
        monkeypatch.setattr(chat_runner, "subagents_attached_async", AsyncMock(return_value=False))
        monkeypatch.setattr(chat_runner, "schedule_eager_spawn", lambda *_a, **_kw: None)

        outer = asyncio.ensure_future(asyncio.Event().wait())
        slot.task = outer
        turn_exit = chat_runner._TurnExit()
        turn_exit.outcome = chat_runner._TurnOutcome(
            auth_required=False,
            memory_admitted=True,
            start_priority=StartPriority.BACKGROUND,
            nudge_facts={},
        )
        try:
            await chat_runner._end_turn_tail(state, slot, turn_exit)

            assert discard.await_count == 1, "the tail deferred its own discard to the retry"
            assert slot._pending_discard_conversation_key is None
        finally:
            outer.cancel()
            if slot.task is not None and slot.task is not outer:
                slot.task.cancel()

    async def test_an_in_turn_clear_reports_discarded_not_queued(self, tmp_path, monkeypatch):
        """The other call site, and the symptom a person sees.

        ``/clear`` runs inside the turn's slash gate. Deferred, it answers "the
        agent is busy right now", records ``queued``, and leaves the plan pill
        standing, because only the landed branch calls ``set_todo(None)``.
        """
        state, slot, discard = self._armed(tmp_path, monkeypatch)
        slot._pending_discard_conversation_key = None
        monkeypatch.setattr(chat_runner, "schedule_eager_spawn", lambda *_a, **_kw: None)
        outer = asyncio.ensure_future(asyncio.Event().wait())
        slot.task = outer
        try:
            outcome = await chat_runner._answer_slash_without_channel(
                state, slot, effective_session_key(slot), "/clear"
            )

            assert outcome == "discarded", "the in-turn /clear was deferred to the retry"
            assert discard.await_count == 1
        finally:
            outer.cancel()

    async def test_the_retry_respawns_the_session_it_dropped(self, tmp_path, monkeypatch):
        """A discard landed by the retry drops the session too, so without a
        respawn the next message pays the cold start the eager path hides. The
        turn-path consume already respawns for itself."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr(chat_runner, "_PENDING_RESET_RETRY_DELAY_SECS", 0.001)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._pending_discard_conversation_key = "s1"
        state.sessions.discard_conversation = AsyncMock(return_value=True)
        monkeypatch.setattr(chat_runner, "subagents_attached_async", AsyncMock(return_value=False))

        spawned: list[str] = []
        # The owner's functions carry the RUNNER's globals (chat_turn.compose),
        # so the bare call resolves there rather than in eager_spawn.
        monkeypatch.setattr(
            chat_runner, "schedule_eager_spawn", lambda _st, sl, **_kw: spawned.append(sl.key)
        )

        chat_runner._arm_pending_reset_retry(state, slot)
        retry = chat_runner._pending_reset_retries.get(slot.key)
        assert retry is not None
        await async_wait_until(
            lambda: spawned == ["s1"],
            interval=0.001,
            describe=lambda: "the retry to respawn after landing the discard",
        )

        assert slot._pending_discard_conversation_key is None


@pytest.mark.asyncio
class TestDrainChokePoint:
    @staticmethod
    async def _drain(state, slot, monkeypatch) -> list[str]:
        ran: list[str] = []

        async def fake_run_chat(_st, _sl, msg, **_kwargs):
            ran.append(msg)

        monkeypatch.setattr(chat_runner, "_run_chat", fake_run_chat)
        await chat_runner._start_next_queued_turn(state, slot)
        if slot.task is not None:
            await slot.task
        return ran

    async def test_a_queued_entry_drains_while_an_unlandable_reset_is_armed(
        self, tmp_path, monkeypatch
    ):
        """A reset whose key has no session declines forever; the queue behind
        it still drains, as it did before the hold existed."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._pending_reset_history_key = "gone"
        state.sessions.has_session = MagicMock(return_value=False)
        slot.queue_append("queued behind the reset")

        assert teardown_hold_active(state, slot) is False
        assert await self._drain(state, slot, monkeypatch) == ["queued behind the reset"]

    async def test_a_signed_out_slot_still_drains_the_send_that_resumes_it(
        self, tmp_path, monkeypatch
    ):
        """Only a turn that runs clears the sign-out cause, so the choke point
        must not hold on it or every send after sign-in stays queued."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._last_turn_auth_required = True
        slot.queue_append("after signing in")

        assert await self._drain(state, slot, monkeypatch) == ["after signing in"]

    async def test_a_system_entry_still_passes_a_held_queue_under_auto_priority(
        self, tmp_path, monkeypatch
    ):
        """Automatic promotion must not shut the system-only door.

        Promoting a waiting human entry sets the same `required_queue_id` a
        card's Run now sets, and that plus the hold returns before the
        system-only dequeue -- parking a cron injection the hold is supposed to
        let through. An explicit Run now still selects its own entry.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._pending_discard_conversation_key = "s1"
        slot.queue_append("the person's waiting send")
        slot.queue_append("[Cron notification] nightly digest", CRON_NOTIFICATION_KIND)

        from kiro_crew.dashboard import responsive_chat

        monkeypatch.setattr(responsive_chat, "first_priority_id", lambda sl: sl._queue[0].get("id"))

        ran = await self._drain(state, slot, monkeypatch)

        assert ran == ["[Cron notification] nightly digest"], "the system entry was parked"
        assert [entry.get("content") for entry in slot._queue] == ["the person's waiting send"]

    async def test_a_queued_steer_does_not_drain_into_a_conversation_awaiting_discard(
        self, tmp_path, monkeypatch
    ):
        """A Steer that fell back to the queue carries ``STEER_INTENT_META``,
        which the SUB-AGENT hold's selector releases on purpose. The teardown
        hold reuses that selector, so without narrowing it the person's own words
        are answered by the conversation they asked to drop -- and the successor's
        admission consume does not allow discards, so nothing downstream catches
        it."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._pending_discard_conversation_key = "s1"
        slot.queue_append("steer that fell back to the queue", "", {STEER_INTENT_META: True})

        assert await self._drain(state, slot, monkeypatch) == []
        assert [e.get("content") for e in slot._queue] == ["steer that fell back to the queue"]

    async def test_a_queued_steer_still_drains_past_the_subagent_hold(self, tmp_path, monkeypatch):
        """Positive control: with no discard armed, the steer exemption still
        releases the entry past a sub-agent wave -- otherwise the test above
        would pass for a selector that releases nothing at all."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        state.subagents = MagicMock(
            running_agents_for=MagicMock(return_value=["a1"]),
            queued_count_for_async=AsyncMock(return_value=0),
            queued_count_or_none_async=AsyncMock(return_value=0),
            has_in_memory_pending_work_for=MagicMock(return_value=False),
            published_queued_depths=MagicMock(return_value={}),
        )
        slot.queue_append("steer that fell back to the queue", "", {STEER_INTENT_META: True})

        assert await self._drain(state, slot, monkeypatch) == ["steer that fell back to the queue"]

    async def test_a_deferred_discard_holds_the_queued_user_entry(self, tmp_path, monkeypatch):
        """Negative control for the two drains above."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._pending_discard_conversation_key = "s1"
        slot.queue_append("held until the discard lands")

        assert await self._drain(state, slot, monkeypatch) == []
        assert len(slot._queue) == 1


@pytest.mark.asyncio
class TestTheCompletionHintCountsOnlyADrainableQueue:
    """``continuing`` tells the dashboard whether work follows the frame, so a
    queue counts only when something will dequeue it. The sign-out cause and the
    teardown cause each park it, and the hint reads both: one is the sound a
    signed-out person already got, the other is this change's own hold."""

    @staticmethod
    def _state(tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr(
            chat_utils_completion,
            "subagents_attached_async",
            AsyncMock(return_value=False),
        )
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.queue_append("queued behind the hold")
        return state, slot

    async def test_a_drainable_queue_reads_as_work_continuing(self, tmp_path, monkeypatch):
        """Positive control: without it the two cases below would pass for a slot
        whose hint never reports work at all."""
        state, slot = self._state(tmp_path, monkeypatch)

        payload = await chat_done_payload(state, slot)

        assert payload["continuing"] is True

    async def test_a_signed_out_slot_does_not_announce_work_continuing(self, tmp_path, monkeypatch):
        state, slot = self._state(tmp_path, monkeypatch)
        slot._last_turn_auth_required = True

        payload = await chat_done_payload(state, slot)

        assert payload["continuing"] is False

    async def test_a_held_teardown_does_not_announce_work_continuing(self, tmp_path, monkeypatch):
        state, slot = self._state(tmp_path, monkeypatch)
        slot._pending_discard_conversation_key = "s1"

        payload = await chat_done_payload(state, slot)

        assert payload["continuing"] is False
