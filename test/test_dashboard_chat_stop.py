"""Dashboard chat: stopping a turn — cancelled stop reason, soft and hard stop, stop during preparation."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_ready_kiro_prerequisite, _make_state
from dashboard_chat_helpers import _provider_mock

from kiro_crew.acp.types import ACP_BACKEND_KIRO
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.history import ConversationLog
from kiro_crew.testing.wait import async_wait_until, await_until, until_parked


class TestStopReasonCancelled:
    """Phase 4: handler response to stopReason='cancelled'."""

    @staticmethod
    def _make_mock_client(events):
        client = _provider_mock()
        client.context_usage_pct = MagicMock(return_value=10.0)

        async def _stream(msg):
            for ev in events:
                yield ev

        client.stream = _stream
        client.stream_command = _stream
        return client

    @staticmethod
    def _make_state_for_run_chat(tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.context_builder = None
        state.consolidator = MagicMock()
        state._hook_store = None
        state._yolo = False
        return state

    @pytest.mark.asyncio
    async def test_handler_stop_reason_cancelled_skips_record_success(self, tmp_path, monkeypatch):
        """When EVENT_COMPLETE carries stop_reason='cancelled', neither
        record_success nor record_failure should be called."""
        from kiro_crew.acp.types import STOP_REASON_CANCELLED
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial"),
            LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_CANCELLED),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        state.sessions.record_success = MagicMock()
        state.sessions.record_failure = AsyncMock()

        await _run_chat(state, slot, "hello")

        state.sessions.record_success.assert_not_called()
        state.sessions.record_failure.assert_not_called()

    @staticmethod
    def _wire_reinjection(state, tmp_path, monkeypatch):
        """Give the state a context builder so the consume site actually runs.

        The class's default harness sets `context_builder = None`, which skips
        the leg that consumes the flag -- with no consume there is nothing to
        restore, so a test without this would pass vacuously.
        """
        from kiro_crew.context import ContextBuilder
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        ctx_builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )
        monkeypatch.setattr(
            ctx_builder,
            "build_message",
            lambda text, *a, **kw: (text, MagicMock(action=None, text="")),
        )
        state.context_builder = ctx_builder
        state.sessions.consume_needs_reinjection = MagicMock(return_value=True)
        state.sessions.mark_needs_reinjection = MagicMock()
        return state

    @pytest.mark.asyncio
    async def test_cancelled_turn_restores_the_reinjection_flag(self, tmp_path, monkeypatch):
        """A graceful cancel discards the prompt, so the one-shot
        post-compaction re-injection flag must be put back.

        Regression guard for the path a narrower fix missed: restoring only on
        the empty-response re-queue left a soft-stop losing the skills index for
        the rest of the session.
        """
        from kiro_crew.acp.types import STOP_REASON_CANCELLED
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial"),
            LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_CANCELLED),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        self._wire_reinjection(state, tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        await _run_chat(state, slot, "hello")

        state.sessions.consume_needs_reinjection.assert_called()
        state.sessions.mark_needs_reinjection.assert_called_once()

    @pytest.mark.asyncio
    async def test_exception_mid_turn_restores_the_reinjection_flag(self, tmp_path, monkeypatch):
        """An exception between the consume and the success check must not
        swallow the flag either -- the restore lives in the `finally`, so every
        non-landing exit is covered, not only the ones that reach the end."""
        from kiro_crew.dashboard.chat import _run_chat

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        self._wire_reinjection(state, tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")

        client = MagicMock()

        def _boom(_msg):
            raise RuntimeError("provider exploded mid-turn")

        client.stream = _boom
        client.context_usage_pct = MagicMock(return_value=0.0)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        state.sessions.record_failure = AsyncMock()

        await _run_chat(state, slot, "hello")

        state.sessions.mark_needs_reinjection.assert_called_once()

    @pytest.mark.asyncio
    async def test_landed_turn_does_not_restore_the_reinjection_flag(self, tmp_path, monkeypatch):
        """The complement: a turn that lands must leave the flag cleared,
        otherwise the index is re-paid on every subsequent turn."""
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="a real answer"),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        self._wire_reinjection(state, tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        state.sessions.record_success = MagicMock()

        await _run_chat(state, slot, "hello")

        state.sessions.mark_needs_reinjection.assert_not_called()

    @pytest.mark.asyncio
    async def test_handler_stop_reason_cancelled_skips_consolidation(self, tmp_path, monkeypatch):
        """When cancelled, maybe_consolidate must not be called."""
        from kiro_crew.acp.types import STOP_REASON_CANCELLED
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial"),
            LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_CANCELLED),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        await _run_chat(state, slot, "hello")

        state.consolidator.maybe_consolidate.assert_not_called()

    @pytest.mark.asyncio
    async def test_handler_stop_reason_end_turn_preserves_existing_behavior(
        self, tmp_path, monkeypatch
    ):
        """When stop_reason='end_turn', record_success and maybe_consolidate fire."""
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="done"),
            LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        state.sessions.record_success = MagicMock()

        await _run_chat(state, slot, "hello")

        state.sessions.record_success.assert_called_once()
        state.consolidator.maybe_consolidate.assert_called_once()

    @pytest.mark.asyncio
    async def test_monitor_wake_turn_does_not_write_user_memory(self, tmp_path, monkeypatch):
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.state import MONITOR_WAKE_PREFIX
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="monitor action complete"),
            LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        state.sessions.record_success = MagicMock()

        await _run_chat(state, slot, f"{MONITOR_WAKE_PREFIX}\nChecks failed.")

        state.sessions.record_success.assert_called_once()
        state.consolidator.maybe_consolidate.assert_not_called()

    @pytest.mark.asyncio
    async def test_handler_stop_reason_cancelled_flushes_partial_text(self, tmp_path, monkeypatch):
        """Partial text chunks before cancel must be flushed to the slot."""
        from kiro_crew.acp.types import STOP_REASON_CANCELLED
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial output here"),
            LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_CANCELLED),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        await _run_chat(state, slot, "hello")

        assistant_msgs = [m for m in slot.messages if m.get("role") == "assistant"]
        assert any("partial output here" in m["content"] for m in assistant_msgs)


# ── Phase 5: Soft-stop dashboard backend tests ──


class TestStopTurnSlotState:
    """Tests for api_chat_slot_stop soft/hard state transitions."""

    def _make_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        sessions = MagicMock(count=0)
        sessions.stop_turn = AsyncMock(return_value="soft")
        sessions.reset = AsyncMock()
        sessions.get_pid = MagicMock(return_value=None)
        state = DashboardState(
            sessions=sessions,
            crons=MagicMock(
                list_jobs=MagicMock(return_value=[]),
                status=MagicMock(return_value={}),
            ),
            lessons=MagicMock(load_all=MagicMock(return_value=[])),
            start_time=0.0,
            conversation_log=ConversationLog(base_dir=tmp_path),
        )
        state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
        return state

    @pytest.mark.asyncio
    async def test_stop_turn_slot_state_transitions_soft(self, tmp_path, monkeypatch):
        """POST stop → idle→soft_pending; after on_soft → idle."""
        state = self._make_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.task = asyncio.ensure_future(asyncio.sleep(999))

        captured_states: list[str] = []

        async def fake_stop_turn(
            key, *, force=False, preserve_queue=False, on_soft=None, on_hard=None
        ):
            captured_states.append(slot._stop_state)
            if on_soft:
                await on_soft()
            return "soft"

        state.sessions.stop_turn = fake_stop_turn

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/s1/stop")
            assert resp.status == 200

        assert captured_states == ["soft_pending"]
        assert slot._stop_state == "idle"
        slot.task.cancel()

    @pytest.mark.asyncio
    async def test_stop_turn_slot_state_transitions_hard(self, tmp_path, monkeypatch):
        """POST stop with hard outcome → idle→soft_pending→idle after on_hard."""
        state = self._make_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.task = asyncio.ensure_future(asyncio.sleep(999))

        async def fake_stop_turn(
            key, *, force=False, preserve_queue=False, on_soft=None, on_hard=None
        ):
            if on_hard:
                await on_hard()
            return "hard"

        state.sessions.stop_turn = fake_stop_turn

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/s1/stop")
            assert resp.status == 200

        assert slot._stop_state == "idle"
        slot.task.cancel()

    @pytest.mark.asyncio
    async def test_stop_turn_idle_cancels_orphaned_task(self, tmp_path, monkeypatch):
        """An idle provider outcome cannot leave a dashboard turn running."""
        state = self._make_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.task = asyncio.ensure_future(asyncio.sleep(999))
        # The orphan this test models is a turn stuck in PREPARATION — the only
        # span a terminal stop outcome may cancel the runner for.
        slot._preparing_task = slot.task
        state.sessions.stop_turn = AsyncMock(return_value="idle")

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/api/chat/slots/s1/stop")
            assert response.status == 200

        assert slot._stop_state == "idle"
        assert slot.task.cancelled()

    @pytest.mark.asyncio
    async def test_stop_turn_idle_does_not_cancel_successor_task(self, tmp_path, monkeypatch):
        """A stop settles its captured runner, never one claimed while it waited."""
        state = self._make_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        stopped_task = asyncio.ensure_future(asyncio.sleep(999))
        slot.task = stopped_task
        # The captured runner is still preparing when the stop lands.
        slot._preparing_task = slot.task
        successor_task: asyncio.Task[None] | None = None

        async def fake_stop_turn(*args, **kwargs):
            nonlocal successor_task
            successor_task = asyncio.ensure_future(asyncio.sleep(999))
            slot.task = successor_task
            return "idle"

        state.sessions.stop_turn = fake_stop_turn

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/api/chat/slots/s1/stop")
            assert response.status == 200

        assert stopped_task.cancelled()
        assert successor_task is not None
        assert not successor_task.cancelled()
        successor_task.cancel()
        await await_until(asyncio.gather(successor_task, return_exceptions=True))

    @pytest.mark.asyncio
    async def test_terminal_stop_task_propagates_request_cancellation(self):
        """The stop request must not swallow its own cancellation while draining."""
        from kiro_crew.dashboard.chat_handlers import _cancel_terminal_stop_task

        release = asyncio.Event()
        blocker = asyncio.Event()

        async def waits_after_cancel():
            try:
                await blocker.wait()
            except asyncio.CancelledError:
                await release.wait()

        runner = asyncio.create_task(waits_after_cancel())
        await until_parked(blocker)
        # The gate this helper adds is the preparing flag; the cancellation
        # behavior under test is unchanged, so model a preparing runner.
        slot = MagicMock()
        slot._preparing_task = runner
        request_task = asyncio.current_task()
        assert request_task is not None
        asyncio.get_running_loop().call_soon(request_task.cancel)
        try:
            with pytest.raises(asyncio.CancelledError):
                await _cancel_terminal_stop_task(MagicMock(), slot, runner)
        finally:
            release.set()
            await await_until(runner)

    @pytest.mark.asyncio
    async def test_interrupt_idle_runner_finally_starts_only_one_queued_turn(
        self, tmp_path, monkeypatch
    ):
        """A cancelled runner owns the preserved-queue handoff."""
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_interrupt

        state = self._make_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        runner_started = asyncio.Event()
        successor_release = asyncio.Event()
        dispatched: list[str] = []

        async def successor():
            await successor_release.wait()

        async def runner():
            runner_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                queued = slot._queue.pop(0)
                dispatched.append(queued["content"])
                slot.task = asyncio.create_task(successor())

        runner_task = asyncio.create_task(runner())
        slot.task = runner_task
        # The runner is still preparing (its finally drains the queue below).
        slot._preparing_task = slot.task
        await runner_started.wait()
        slot.queue_append("first queued prompt")
        slot.queue_append("second queued prompt")
        state.sessions.stop_turn = AsyncMock(return_value="idle")
        handler_dispatch = AsyncMock(return_value=True)

        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/interrupt", api_chat_slot_interrupt)
        try:
            with patch(
                "kiro_crew.dashboard.chat_handlers._start_next_queued_turn", handler_dispatch
            ):
                async with TestClient(TestServer(app)) as client:
                    response = await client.post("/api/chat/slots/s1/interrupt")
                    assert response.status == 200

            assert slot._stop_state == "idle"
            assert runner_task.cancelled()
            assert dispatched == ["first queued prompt"]
            assert [queued["content"] for queued in slot._queue] == ["second queued prompt"]
            handler_dispatch.assert_not_awaited()
        finally:
            successor_release.set()
            await await_until(asyncio.gather(slot.task, return_exceptions=True))

    @pytest.mark.asyncio
    async def test_interrupt_idle_dispatches_queue_when_no_successor(self, tmp_path, monkeypatch):
        """An idle outcome with no preparing turn and no live task must not
        strand the preserved queue: the handler dispatches it itself.

        A task that never published ``_preparing_task`` is one the
        terminal-settle helper refuses to touch: it owns its stop handling and
        settles on its own during the stop, clearing ``slot.task`` on exit.
        With no successor claimed, the handler recognises the cleared slot and
        dispatches the queue."""
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_interrupt

        state = self._make_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        loop_release = asyncio.Event()

        async def runner():
            await loop_release.wait()
            slot.task = None

        runner_task = asyncio.create_task(runner())
        slot.task = runner_task
        # A task that never published the preparing marker is not cancelled.

        async def fake_stop_turn(*args, **kwargs):
            # The loop settles (and clears slot.task) while the stop is in flight.
            loop_release.set()
            await asyncio.sleep(0)
            return "idle"

        slot.queue_append("first queued prompt")
        state.sessions.stop_turn = fake_stop_turn
        handler_dispatch = AsyncMock(return_value=True)

        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/interrupt", api_chat_slot_interrupt)
        with patch("kiro_crew.dashboard.chat_handlers._start_next_queued_turn", handler_dispatch):
            async with TestClient(TestServer(app)) as client:
                response = await client.post("/api/chat/slots/s1/interrupt")
                assert response.status == 200
        assert slot._stop_state == "idle"
        # The runner owns its stop handling: never cancelled, settled on its own.
        assert not runner_task.cancelled()
        assert runner_task.done()
        # The settled runner's exit cleared slot.task (no successor claimed it),
        # so the handler dispatches the preserved queue itself.
        assert slot.task is None
        handler_dispatch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_turn_force_query_param(self, tmp_path, monkeypatch):
        """POST stop?force=true when soft_pending → skips cancel, hard kill."""
        state = self._make_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.task = asyncio.ensure_future(asyncio.sleep(999))
        slot._stop_state = "soft_pending"

        force_called = []

        async def fake_stop_turn(
            key, *, force=False, preserve_queue=False, on_soft=None, on_hard=None
        ):
            force_called.append(force)
            if on_hard:
                await on_hard()
            return "hard"

        state.sessions.stop_turn = fake_stop_turn

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/s1/stop?force=true")
            assert resp.status == 200

        assert force_called == [True]
        assert slot._stop_state == "idle"
        slot.task.cancel()

    @pytest.mark.asyncio
    async def test_stop_turn_second_press_escalates_without_force_flag(self, tmp_path, monkeypatch):
        """A second stop press while soft_pending hard-kills even when the
        client did NOT send force=true. The client derives force from the
        WS-echoed stop_state, which lags on a slow connection; the backend's
        own soft_pending state is authoritative, so any second press escalates.
        """
        state = self._make_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.task = asyncio.ensure_future(asyncio.sleep(999))
        slot._stop_state = "soft_pending"

        force_called = []

        async def fake_stop_turn(
            key, *, force=False, preserve_queue=False, on_soft=None, on_hard=None
        ):
            force_called.append(force)
            if on_hard:
                await on_hard()
            return "hard"

        state.sessions.stop_turn = fake_stop_turn

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            # No ?force=true — the lagging client still thinks state is idle.
            resp = await client.post("/api/chat/slots/s1/stop")
            assert resp.status == 200

        # Backend escalated to a hard kill anyway.
        assert force_called == [True]
        assert slot._stop_state == "idle"
        slot.task.cancel()

    @pytest.mark.asyncio
    async def test_stop_turn_first_press_preserves_queue(self, tmp_path, monkeypatch):
        """Queue populated; POST stop; queue preserved for dequeue loop."""
        state = self._make_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.task = asyncio.ensure_future(asyncio.sleep(999))
        for content in ("msg1", "msg2"):
            slot.queue_append(content)

        async def fake_stop_turn(
            key, *, force=False, preserve_queue=False, on_soft=None, on_hard=None
        ):
            assert preserve_queue is True
            if on_soft:
                await on_soft()
            return "soft"

        state.sessions.stop_turn = fake_stop_turn

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/s1/stop")
            assert resp.status == 200
        assert len(slot._queue) == 2
        slot.task.cancel()

    @pytest.mark.asyncio
    async def test_double_press_force_kill_clears_queue(self, tmp_path, monkeypatch):
        """Double-press (force kill) clears slot._queue so no dequeue fires."""
        state = self._make_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.task = asyncio.ensure_future(asyncio.sleep(999))
        slot._stop_state = "soft_pending"
        for content in ("msg1", "msg2", "msg3"):
            slot.queue_append(content)

        async def fake_stop_turn(
            key, *, force=False, preserve_queue=False, on_soft=None, on_hard=None
        ):
            if on_hard:
                await on_hard()
            return "hard"

        state.sessions.stop_turn = fake_stop_turn

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/s1/stop")
            assert resp.status == 200
        assert len(slot._queue) == 0
        slot.task.cancel()

    @pytest.mark.asyncio
    async def test_single_press_empty_queue_goes_idle(self, tmp_path, monkeypatch):
        """Single press with no queued messages goes idle without error."""
        state = self._make_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.task = asyncio.ensure_future(asyncio.sleep(999))
        assert len(slot._queue) == 0

        async def fake_stop_turn(
            key, *, force=False, preserve_queue=False, on_soft=None, on_hard=None
        ):
            assert preserve_queue is True
            if on_soft:
                await on_soft()
            return "soft"

        state.sessions.stop_turn = fake_stop_turn

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/s1/stop")
            assert resp.status == 200
        assert len(slot._queue) == 0
        slot.task.cancel()

    @pytest.mark.asyncio
    async def test_double_press_empty_queue_no_crash(self, tmp_path, monkeypatch):
        """Double press with empty queue clears without error."""
        state = self._make_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.task = asyncio.ensure_future(asyncio.sleep(999))
        slot._stop_state = "soft_pending"
        assert len(slot._queue) == 0

        async def fake_stop_turn(
            key, *, force=False, preserve_queue=False, on_soft=None, on_hard=None
        ):
            if on_hard:
                await on_hard()
            return "hard"

        state.sessions.stop_turn = fake_stop_turn

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/s1/stop")
            assert resp.status == 200
        assert len(slot._queue) == 0
        slot.task.cancel()

    @pytest.mark.asyncio
    async def test_stop_event_appears_in_transcript(self, tmp_path, monkeypatch):
        """After stop, slot messages contain a stop_event entry."""
        import json

        def _is_stop_event(m: dict) -> bool:
            cls = m.get("cls", "")
            if not isinstance(cls, str) or not cls.startswith("{"):
                return False
            try:
                return json.loads(cls).get("kind") == "stop_event"
            except (json.JSONDecodeError, TypeError):
                return False

        state = self._make_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.task = asyncio.ensure_future(asyncio.sleep(999))

        async def fake_stop_turn(
            key, *, force=False, preserve_queue=False, on_soft=None, on_hard=None
        ):
            if on_soft:
                await on_soft()
            return "soft"

        state.sessions.stop_turn = fake_stop_turn

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/s1/stop")
            assert resp.status == 200

        stop_msgs = [m for m in slot.messages if _is_stop_event(m)]
        assert len(stop_msgs) == 1
        data = json.loads(stop_msgs[0]["content"])
        assert data["kind"] == "stop_event"
        assert data["state"] == "stopped"
        assert data["outcome"] == "soft"
        slot.task.cancel()

    @pytest.mark.asyncio
    async def test_stop_event_replace_in_place(self, tmp_path, monkeypatch):
        """Stop event has stable id across state transitions (one entry)."""
        import json

        def _is_stop_event(m: dict) -> bool:
            cls = m.get("cls", "")
            if not isinstance(cls, str) or not cls.startswith("{"):
                return False
            try:
                return json.loads(cls).get("kind") == "stop_event"
            except (json.JSONDecodeError, TypeError):
                return False

        state = self._make_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.task = asyncio.ensure_future(asyncio.sleep(999))

        async def fake_stop_turn(
            key, *, force=False, preserve_queue=False, on_soft=None, on_hard=None
        ):
            # Verify the stop_event was inserted before callbacks
            stop_msgs = [m for m in slot.messages if _is_stop_event(m)]
            assert len(stop_msgs) == 1
            pre_data = json.loads(stop_msgs[0]["content"])
            assert pre_data["state"] == "stopping"
            if on_soft:
                await on_soft()
            return "soft"

        state.sessions.stop_turn = fake_stop_turn

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            await client.post("/api/chat/slots/s1/stop")

        # Still only one stop_event message
        stop_msgs = [m for m in slot.messages if _is_stop_event(m)]
        assert len(stop_msgs) == 1
        data = json.loads(stop_msgs[0]["content"])
        assert data["state"] == "stopped"
        slot.task.cancel()


class TestStopHistoryBanner:
    """Tests for history re-injection banner skip on soft stop."""

    @staticmethod
    def _last_stop_soft(slot: _ChatSlot) -> bool:
        """Replicates the detection logic in chat.py:_run_chat."""
        import json

        for m in reversed(slot.messages):
            cls_val = m.get("cls", "")
            if not isinstance(cls_val, str) or not cls_val.startswith("{"):
                continue
            try:
                _cls = json.loads(cls_val)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(_cls, dict) or _cls.get("kind") != "stop_event":
                continue
            return _cls.get("outcome") == "soft"
        return False

    def test_soft_stop_preserves_session_no_history_banner(self):
        """After a soft stop, _build_history_prefix is skipped."""
        import json

        slot = _ChatSlot("s1")
        slot.append("user", "hello")
        slot.append("assistant", "hi there")
        # cls must be a JSON-encoded dict (same format api_chat_slot_stop uses)
        cls_json = json.dumps(
            {
                "kind": "stop_event",
                "id": "stop-abc",
                "state": "stopped",
                "outcome": "soft",
            }
        )
        slot.append("system", cls_json, cls_json)
        assert self._last_stop_soft(slot) is True

    def test_hard_stop_still_injects_history_banner(self):
        """After a hard stop, the banner detection returns False."""
        import json

        slot = _ChatSlot("s1")
        slot.append("user", "hello")
        slot.append("assistant", "hi there")
        cls_json = json.dumps(
            {
                "kind": "stop_event",
                "id": "stop-abc",
                "state": "stop_failed_reset",
                "outcome": "hard",
            }
        )
        slot.append("system", cls_json, cls_json)
        assert self._last_stop_soft(slot) is False

    def test_plain_string_cls_does_not_match(self):
        """Plain-string cls (legacy format) is ignored — no false positive."""
        slot = _ChatSlot("s1")
        slot.append("user", "hello")
        slot.append("system", "{}", "stop_event")  # plain string cls
        assert self._last_stop_soft(slot) is False


class TestStopDuringSessionPrep:
    """A Stop pressed while the turn is still being prepared must not open it.

    During ``get_or_create``'s cold start the session is not registered yet,
    so ``SessionManager.stop_turn`` answers ``"idle"`` and the stop resolves
    without cancelling anything. The dispatch gate in ``_run_chat`` is what
    honors that stop: it compares ``slot._stop_generation`` against the
    turn-entry snapshot and refuses to open the turn ([Stopped] card
    shown while the response streams to completion).
    """

    def _make_state_and_slot(self, tmp_path):
        from kiro_crew.dashboard.chat_runner import _run_chat

        state = _make_state(tmp_path)
        state.sessions.release = MagicMock()
        state.sessions.reset = AsyncMock()
        state.sessions.set_approval_policy = MagicMock()
        state.sessions.check_context_usage = MagicMock()
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.is_yolo_active = MagicMock(return_value=False)
        state._background_tasks = set()

        slot = state.get_or_create_slot("stop-prep-slot")
        slot.append("user", "hello", "msg msg-u")
        return state, slot, _run_chat

    def _make_client(self, stream_calls):
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        client = MagicMock()
        client.shutdown = AsyncMock()

        async def _stream(msg):
            stream_calls.append(msg)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="full response")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

        client.stream = _stream
        client.stream_command = _stream
        return client

    @pytest.mark.asyncio
    async def test_stop_during_get_or_create_aborts_dispatch(self, tmp_path: Path) -> None:
        """Stop lands mid-cold-start → the turn never opens, nothing streams.

        The stop is simulated exactly as the /stop handler leaves it for this
        race: ``_stop_state`` flips idle → soft_pending (bumping
        ``_stop_generation``) and, because ``stop_turn`` found no session and
        answered "idle", snaps straight back to idle. A point-in-time state
        check at dispatch sees nothing — only the generation delta survives.
        """
        state, slot, _run_chat = self._make_state_and_slot(tmp_path)
        stream_calls: list[str] = []
        client = self._make_client(stream_calls)

        async def _slow_create(*args, **kwargs):
            # Stop pressed while the session is still being created.
            slot._stop_state = "soft_pending"
            slot._stop_state = "idle"
            return client, True, False

        state.sessions.get_or_create = AsyncMock(side_effect=_slow_create)

        await _run_chat(state, slot, "hello")

        assert stream_calls == [], (
            "the turn was dispatched despite a Stop during session prep — "
            "the full response would stream behind a [Stopped] card (#5464)"
        )

    @pytest.mark.asyncio
    async def test_stop_during_get_or_create_does_not_accept_monitor_wake(
        self, tmp_path: Path
    ) -> None:
        """A stopped pre-stream wake stays retryable instead of becoming DISPATCHED."""
        from kiro_crew.monitoring.completion import MonitorCompletionHook

        state, slot, _run_chat = self._make_state_and_slot(tmp_path)
        stream_calls: list[str] = []
        client = self._make_client(stream_calls)

        async def _slow_create(*args, **kwargs):
            slot._stop_state = "soft_pending"
            slot._stop_state = "idle"
            return client, True, False

        state.sessions.get_or_create = AsyncMock(side_effect=_slow_create)
        accepted = MagicMock()
        completion = MonitorCompletionHook(
            "monitor1",
            "failure-a",
            AsyncMock(),
            authorization_callback=AsyncMock(return_value=True),
            acceptance_callback=accepted,
        )

        await _run_chat(state, slot, "hello", monitor_completion=completion)

        assert completion.accepted is False
        accepted.assert_not_called()
        assert stream_calls == []

    @pytest.mark.asyncio
    async def test_no_stop_dispatches_normally(self, tmp_path: Path) -> None:
        """Control: without a Stop, the same setup opens the turn exactly once."""
        state, slot, _run_chat = self._make_state_and_slot(tmp_path)
        stream_calls: list[str] = []
        client = self._make_client(stream_calls)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        await _run_chat(state, slot, "hello")

        assert len(stream_calls) == 1, "the dispatch gate must not fire without a Stop"

    @pytest.mark.asyncio
    async def test_stop_of_a_previous_turn_does_not_abort_the_next(self, tmp_path: Path) -> None:
        """A stop generation inherited from an EARLIER turn must not trip the gate.

        The gate compares against the snapshot taken at THIS turn's entry, so a
        slot whose previous turn was stopped (generation already > 0) still
        dispatches its next turn normally.
        """
        state, slot, _run_chat = self._make_state_and_slot(tmp_path)
        # A previous turn was stopped and resolved before this turn began.
        slot._stop_state = "soft_pending"
        slot._stop_state = "idle"

        stream_calls: list[str] = []
        client = self._make_client(stream_calls)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        await _run_chat(state, slot, "hello")

        assert (
            len(stream_calls) == 1
        ), "a stale stop generation from a previous turn aborted a fresh turn"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("guarded", [False, True])
    async def test_preparing_task_tracks_preparation_and_dispatch(
        self, tmp_path: Path, guarded: bool
    ) -> None:
        """The task owns preparation until the provider's first event.

        The Stop handler reads this task identity to decide whether an "idle" provider
        answer means "nothing running" or "still preparing, cancel the task". The
        stream can still await before the transport registers the turn as active, so
        the marker stands across that span and drops once the first event arrives.
        """
        from kiro_crew.dashboard.turn_dispatch import spawn_guarded_turn
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        state, slot, _run_chat = self._make_state_and_slot(tmp_path)
        seen: dict[str, bool] = {}
        client = MagicMock()
        client.shutdown = AsyncMock()

        async def _stream(msg):
            seen["before_first_event"] = slot._preparing_task is not None
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok")
            seen["after_first_event"] = slot._preparing_task is not None
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

        client.stream = _stream
        client.stream_command = _stream

        async def _create(*args, **kwargs):
            seen["preparing"] = slot._preparing_task is (slot.task or asyncio.current_task())
            return client, True, False

        state.sessions.get_or_create = AsyncMock(side_effect=_create)

        if guarded:
            slot.task = spawn_guarded_turn(state, slot, _run_chat(state, slot, "hello"))
            await await_until(slot.task)
        else:
            await _run_chat(state, slot, "hello")

        assert seen == {"preparing": True, "before_first_event": True, "after_first_event": False}
        assert slot._preparing_task is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stop_requested", [False, True])
    async def test_stop_during_acp_timeout_resolution_prevents_dispatch(
        self, tmp_path, monkeypatch, stop_requested
    ):
        from kiro_crew.acp.session_handle import AcpSessionHandle
        from kiro_crew.acp.session_provider import AcpSessionProvider
        from kiro_crew.acp.types import JsonRpcMessage
        from kiro_crew.dashboard.chat_handlers import stop_slot_turn
        from kiro_crew.dashboard.turn_dispatch import spawn_guarded_turn
        from kiro_crew.session_lifecycle import SessionLifecycleService, SessionLifecycleState

        state, slot, run_chat = self._make_state_and_slot(tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat_title._maybe_auto_title", AsyncMock())
        resolving = asyncio.Event()
        release = asyncio.Event()
        queue = asyncio.Queue()
        runtime = MagicMock(acp_backend=ACP_BACKEND_KIRO, supports_image_prompt=False)
        runtime.is_alive.return_value = True
        runtime._last_activity = time.monotonic()

        async def send_request(method, params):
            assert method == "session/prompt"
            queue.put_nowait(
                JsonRpcMessage(
                    method="session/update",
                    params={
                        "sessionId": "stop-gap",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "response"},
                        },
                    },
                )
            )
            queue.put_nowait(JsonRpcMessage(id=1, result={"stopReason": "end_turn"}))
            return 1

        runtime.send_request = AsyncMock(side_effect=send_request)
        handle = AcpSessionHandle("stop-gap", queue, runtime)
        provider = AcpSessionProvider(handle, runtime, owns_runtime=True)
        monkeypatch.setattr(provider, "reclaim", MagicMock())
        client = self._make_client([])
        client.stream = provider.stream
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        state.sessions._fold_key = lambda key: key
        state.sessions._sessions = {
            f"dashboard:{slot.key}": MagicMock(provider=provider, prev_turn_cancelled=False)
        }
        state.sessions._cfg.agent.soft_stop_budget_secs = 1.0
        lifecycle = SessionLifecycleService(
            state.sessions,
            MagicMock(logger=logging.getLogger(__name__), monotonic=time.monotonic),
            SessionLifecycleState(),
        )
        state.sessions.stop_turn = lifecycle.stop_turn

        async def resolve_timeout(timeout):
            assert timeout is None
            resolving.set()
            await await_until(release)
            return 5.0

        monkeypatch.setattr(
            "kiro_crew.acp.session_handle._effective_prompt_timeout_async", resolve_timeout
        )
        turn = spawn_guarded_turn(
            state, slot, state.run_background_turn(slot, run_chat(state, slot, "hello"))
        )
        slot.task = turn
        try:
            await await_until(resolving)
            assert not handle.is_turn_active
            runtime.send_request.assert_not_awaited()
            if stop_requested:
                await stop_slot_turn(state, slot)
                settled_at_stop = turn.done()
            release.set()
            if stop_requested:
                with pytest.raises(asyncio.CancelledError):
                    await await_until(turn)
            else:
                await await_until(turn)
            assert runtime.send_request.await_count == (0 if stop_requested else 1)
            if stop_requested:
                assert settled_at_stop
            assert not slot.running
            assert slot._preparing_task is None
        finally:
            release.set()
            turn.cancel()
            await await_until(asyncio.gather(turn, return_exceptions=True))

    @pytest.mark.asyncio
    @pytest.mark.parametrize("command", ["/goal", "/workflow", "/prompts"])
    async def test_local_command_does_not_leave_preparation_owned(self, tmp_path, command):
        state, slot, run_chat = self._make_state_and_slot(tmp_path)
        with (
            patch("kiro_crew.dashboard.chat_runner._handle_goal_command", AsyncMock()),
            patch("kiro_crew.dashboard.chat_runner._handle_workflow_command", AsyncMock()),
        ):
            await run_chat(state, slot, command)
        assert slot._preparing_task is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("guarded", [False, True])
    async def test_exiting_runner_preserves_successors_preparation(self, tmp_path, guarded):
        from kiro_crew.dashboard.turn_dispatch import spawn_guarded_turn

        state, slot, run_chat = self._make_state_and_slot(tmp_path)
        state.sessions.get_or_create = AsyncMock(side_effect=RuntimeError("cold start failed"))
        failure_entered = asyncio.Event()
        release_failure = asyncio.Event()

        async def record_failure(*args, **kwargs):
            failure_entered.set()
            await release_failure.wait()

        state.sessions.record_failure = record_failure
        coro = run_chat(state, slot, "hello")
        predecessor = (
            spawn_guarded_turn(state, slot, coro) if guarded else asyncio.create_task(coro)
        )
        slot.task = predecessor
        successor = None
        try:
            await await_until(failure_entered)
            successor = asyncio.create_task(asyncio.Event().wait())
            slot.task = successor
            slot._preparing_task = successor
            release_failure.set()
            await await_until(predecessor)
            assert slot._preparing_task is successor
        finally:
            release_failure.set()
            predecessor.cancel()
            tasks = [predecessor]
            if successor is not None:
                successor.cancel()
                tasks.append(successor)
            await await_until(asyncio.gather(*tasks, return_exceptions=True))

    @pytest.mark.asyncio
    @pytest.mark.parametrize("dispatch", ["direct", "guarded", "bounded"])
    async def test_interrupt_during_memory_admission_dispatches_preserved_queue(
        self, tmp_path, monkeypatch, dispatch
    ):
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_interrupt
        from kiro_crew.dashboard.turn_dispatch import bounded_chat_turn, spawn_guarded_turn

        state, slot, run_chat = self._make_state_and_slot(tmp_path)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def wait_for_memory(*args):
            entered.set()
            await release.wait()

        monkeypatch.setattr("kiro_crew.memory_startup.wait_for_memory_preparation", wait_for_memory)
        state.sessions.stop_turn = AsyncMock(return_value="idle")
        slot.queue_append("next prompt")
        coro = state.run_background_turn(slot, run_chat(state, slot, "hello"))
        if dispatch == "guarded":
            turn = spawn_guarded_turn(state, slot, coro)
        elif dispatch == "bounded":
            turn = asyncio.create_task(bounded_chat_turn(coro))
        else:
            turn = asyncio.create_task(coro)
        slot.task = turn
        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/interrupt", api_chat_slot_interrupt)
        dispatched = AsyncMock(return_value=True)
        try:
            await await_until(entered)
            with patch("kiro_crew.dashboard.chat_handlers._start_next_queued_turn", dispatched):
                async with TestClient(TestServer(app)) as client:
                    response = await client.post(f"/api/chat/slots/{slot.key}/interrupt")
                    assert response.status == 200
            assert turn.done()
            assert slot.task is None
            assert [item["content"] for item in slot._queue] == ["next prompt"]
            dispatched.assert_awaited_once_with(state, slot)
        finally:
            release.set()
            turn.cancel()
            await await_until(asyncio.gather(turn, return_exceptions=True))

    @pytest.mark.asyncio
    @pytest.mark.parametrize("dispatch", ["direct", "guarded", "bounded"])
    async def test_preparing_stop_keeps_runner_ownership_of_queued_handoff(
        self, tmp_path, monkeypatch, dispatch
    ):
        from kiro_crew.dashboard import chat_handlers, chat_runner
        from kiro_crew.dashboard.turn_dispatch import bounded_chat_turn, spawn_guarded_turn

        state, slot, run_chat = self._make_state_and_slot(tmp_path)
        stream_calls = []
        provider = self._make_client(stream_calls)
        preparing = asyncio.Event()
        cleaning = asyncio.Event()
        release_cleanup = asyncio.Event()
        real_wait_for = asyncio.wait_for
        settle_waits = []

        async def acquire(*args, **kwargs):
            if not preparing.is_set():
                preparing.set()
                await asyncio.Event().wait()
            return provider, True, False

        async def cleanup(*args, **kwargs):
            if slot._stop_teardown_task is turn:
                cleaning.set()
                await await_until(release_cleanup)
            return False

        async def settle_wait(awaitable, timeout):
            if timeout == 2.0 and slot._stop_teardown_task is turn:
                settle_waits.append(awaitable)
                await real_wait_for(cleaning.wait(), timeout=5)
                awaitable.cancel()
                raise asyncio.TimeoutError
            return await real_wait_for(awaitable, timeout=timeout)

        monkeypatch.setattr(chat_runner, "_consume_pending_reset", cleanup)
        monkeypatch.setattr(asyncio, "wait_for", settle_wait)
        state.sessions.get_or_create = AsyncMock(side_effect=acquire)
        state.sessions.stop_turn = AsyncMock(return_value="idle")
        slot.queue_append("preserved message")
        coro = state.run_background_turn(slot, run_chat(state, slot, "hello"))
        if dispatch == "guarded":
            turn = spawn_guarded_turn(state, slot, coro)
        elif dispatch == "bounded":
            turn = asyncio.create_task(bounded_chat_turn(coro))
        else:
            turn = asyncio.create_task(coro)
        slot.task = turn
        app = web.Application()
        app["state"] = state
        app.router.add_post(
            "/api/chat/slots/{slot}/interrupt", chat_handlers.api_chat_slot_interrupt
        )
        app.router.add_post("/api/chat", chat_handlers.api_chat)
        try:
            await await_until(preparing)
            async with TestClient(TestServer(app)) as client:
                response = await client.post(f"/api/chat/slots/{slot.key}/interrupt")
                assert (await response.json())["queue_held"] is True
                assert len(settle_waits) == 1
                assert not turn.done()
                assert not await chat_runner._start_next_queued_turn(state, slot)
                response = await client.post(
                    "/api/chat?ws=1", json={"message": "arriving message", "slot": slot.key}
                )
                assert (await response.json())["queued"] is True
                assert [entry["content"] for entry in slot._queue] == [
                    "preserved message",
                    "arriving message",
                ]
                release_cleanup.set()
                if dispatch == "direct":
                    await await_until(turn)
                else:
                    with pytest.raises(asyncio.CancelledError):
                        await await_until(turn)
                for _ in range(3):
                    successor = slot.task
                    if successor is None:
                        break
                    await await_until(successor)
            delivered = "\n".join(stream_calls)
            assert "preserved message" in delivered
            assert "arriving message" in delivered
            assert not slot._queue
            assert not slot.running
        finally:
            release_cleanup.set()
            tasks = {task for task in (turn, slot.task) if task is not None}
            for task in tasks:
                if not task.done():
                    task.cancel()
            await await_until(asyncio.gather(*tasks, return_exceptions=True))

    @pytest.mark.asyncio
    @pytest.mark.parametrize("normal_message", [False, True])
    async def test_interrupt_timeout_waits_for_teardown_before_queue_dispatch(
        self, tmp_path, monkeypatch, caplog, normal_message
    ):
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_interrupt
        from kiro_crew.memory_startup import MemoryStartupUnavailable

        state, slot, run_chat = self._make_state_and_slot(tmp_path)
        preparing = asyncio.Event()
        teardown = asyncio.Event()
        release_teardown = asyncio.Event()

        async def wait_for_memory(*args):
            preparing.set()
            await asyncio.Event().wait()

        async def send_chat_done(*args, **kwargs):
            teardown.set()
            await release_teardown.wait()

        def memory_unprepared():
            raise MemoryStartupUnavailable("Memory is being prepared.")

        monkeypatch.setattr("kiro_crew.memory_startup.wait_for_memory_preparation", wait_for_memory)
        # Memory admission held the queue: teardown's end leaves it for the retry.
        monkeypatch.setattr("kiro_crew.memory_startup.require_memory_prepared", memory_unprepared)
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._send_chat_done", send_chat_done)
        state.sessions.stop_turn = AsyncMock(return_value="idle")
        slot.queue_append("next prompt")
        queue_id = slot._queue[0]["id"]
        turn = asyncio.create_task(run_chat(state, slot, "hello"))
        slot.task = turn
        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/interrupt", api_chat_slot_interrupt)
        from kiro_crew.dashboard.chat_handlers import api_chat
        from kiro_crew.dashboard.chat_runner import _start_next_queued_turn

        app.router.add_post("/api/chat", api_chat)
        dispatched = AsyncMock(return_value=True)
        try:
            await await_until(preparing)
            with patch("kiro_crew.dashboard.chat_handlers._start_next_queued_turn", dispatched):
                async with TestClient(TestServer(app)) as client:
                    response = await client.post(f"/api/chat/slots/{slot.key}/interrupt")
                    assert response.status == 200
                    assert (await response.json())["queue_held"] is True
                    assert teardown.is_set()
                    assert slot.task is None
                    assert not turn.done()
                    projection = slot.to_dict()
                    assert projection["running"] is False
                    assert projection["stop_teardown"] is True
                    if normal_message:
                        assert slot.running
                        assert not slot.turn_running
                        response = await client.post(
                            "/api/chat?ws=1",
                            json={"message": "sent during teardown", "slot": slot.key},
                        )
                        assert response.status == 200
                        assert (await response.json())["queued"] is True
                        assert slot.task is None
                        assert not await _start_next_queued_turn(state, slot)
                    expected_queue = ["next prompt"]
                    if normal_message:
                        expected_queue.append("sent during teardown")
                    assert any(
                        record.name == "kiro_crew.dashboard.chat_handlers"
                        and "did not settle within 2 seconds" in record.getMessage()
                        for record in caplog.records
                    )
                    dispatched.assert_not_awaited()
                    assert [item["content"] for item in slot._queue] == expected_queue

                    response = await client.post(
                        f"/api/chat/slots/{slot.key}/interrupt", json={"queue_id": queue_id}
                    )
                    assert response.status == 200
                    assert (await response.json())["queue_held"] is True
                    dispatched.assert_not_awaited()
                    pushes_before_settle = state.push_slots_update.call_count
                    release_teardown.set()
                    await await_until(turn)
                    assert slot._stop_teardown_task is None
                    assert slot.to_dict()["stop_teardown"] is False
                    assert state.push_slots_update.call_count > pushes_before_settle
                    dispatched.assert_not_awaited()
                    response = await client.post(
                        f"/api/chat/slots/{slot.key}/interrupt", json={"queue_id": queue_id}
                    )
                    assert response.status == 200
                    dispatched.assert_awaited_once_with(
                        state, slot, allow_user_during_subagents=True, required_queue_id=queue_id
                    )
        finally:
            release_teardown.set()
            turn.cancel()
            await await_until(asyncio.gather(turn, return_exceptions=True))

    @pytest.mark.asyncio
    @pytest.mark.parametrize("runner_holds_task", [False, True])
    async def test_interrupt_retry_during_teardown_is_held_without_a_new_stop(
        self, tmp_path, runner_holds_task
    ):
        """A lightning retry while teardown is live answers held and stops nothing again."""
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_interrupt

        state, slot, _ = self._make_state_and_slot(tmp_path)
        release = asyncio.Event()
        teardown = asyncio.create_task(release.wait())
        slot._stop_teardown_task = teardown
        if runner_holds_task:
            slot.task = teardown
        slot.queue_append("held prompt")
        queue_id = slot._queue[0]["id"]
        state.sessions.stop_turn = AsyncMock(return_value="idle")
        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/interrupt", api_chat_slot_interrupt)
        dispatched = AsyncMock(return_value=True)
        try:
            with (
                patch("kiro_crew.dashboard.chat_handlers._start_next_queued_turn", dispatched),
                patch("kiro_crew.dashboard.chat_handlers.sel") as sel_factory,
            ):
                async with TestClient(TestServer(app)) as client:
                    response = await client.post(
                        f"/api/chat/slots/{slot.key}/interrupt", json={"queue_id": queue_id}
                    )
                    assert response.status == 200
                    assert await response.json() == {"ok": True, "queue_held": True}
            state.sessions.stop_turn.assert_not_awaited()
            dispatched.assert_not_awaited()
            assert not teardown.done()
            assert slot._stop_state == "idle"
            assert slot._stop_event_id is None
            assert [
                c.kwargs.get("outcome")
                for c in sel_factory.return_value.log_tool_invocation.call_args_list
            ] == ["queue_held"]
            assert [item["content"] for item in slot._queue] == ["held prompt"]
        finally:
            release.set()
            await await_until(teardown)

    @pytest.mark.asyncio
    async def test_send_queued_during_stop_teardown_starts_once_teardown_ends(
        self, tmp_path, monkeypatch
    ):
        """A send queued behind a Stop teardown that outlived the settle wait starts once."""
        from kiro_crew.dashboard import chat_handlers, chat_runner

        state, slot, run_chat = self._make_state_and_slot(tmp_path)
        stream_calls: list[str] = []
        provider = self._make_client(stream_calls)
        preparing = asyncio.Event()
        tearing_down = asyncio.Event()
        release_teardown = asyncio.Event()
        real_send_chat_done = chat_runner._send_chat_done
        real_start = chat_runner._start_next_queued_turn
        starts: list[object] = []

        async def acquire(*args, **kwargs):
            if not preparing.is_set():
                preparing.set()
                await asyncio.Event().wait()
            return provider, True, False

        async def send_chat_done(*args, **kwargs):
            if not tearing_down.is_set():
                tearing_down.set()
                await release_teardown.wait()
            return await real_send_chat_done(*args, **kwargs)

        async def counted_start(*args, **kwargs):
            started = await real_start(*args, **kwargs)
            if started:
                starts.append(slot.task)
            return started

        monkeypatch.setattr(chat_runner, "_send_chat_done", send_chat_done)
        monkeypatch.setattr(chat_runner, "_start_next_queued_turn", counted_start)
        state.sessions.get_or_create = AsyncMock(side_effect=acquire)
        state.sessions.stop_turn = AsyncMock(return_value="idle")
        turn = asyncio.create_task(run_chat(state, slot, "hello"))
        slot.task = turn
        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/stop", chat_handlers.api_chat_slot_stop)
        app.router.add_post("/api/chat", chat_handlers.api_chat)
        try:
            await await_until(preparing)
            async with TestClient(TestServer(app)) as client:
                response = await client.post(f"/api/chat/slots/{slot.key}/stop")
                assert response.status == 200
                assert tearing_down.is_set()
                assert slot.task is None
                assert slot.running
                response = await client.post(
                    "/api/chat?ws=1", json={"message": "sent during teardown", "slot": slot.key}
                )
                assert (await response.json())["queued"] is True
                release_teardown.set()
                await await_until(turn)
                await async_wait_until(
                    lambda: not slot._queue and not slot.running,
                    describe=lambda: (slot._queue, slot.task, slot._stop_teardown_task),
                )
            assert sum("sent during teardown" in call for call in stream_calls) == 1
            assert len(starts) == 1
            assert slot._stop_teardown_task is None
        finally:
            release_teardown.set()
            tasks = {task for task in (turn, slot.task) if task is not None}
            for task in tasks:
                if not task.done():
                    task.cancel()
            await await_until(asyncio.gather(*tasks, return_exceptions=True))

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "hazard", ["none", "replacement", "rebind", "auth", "successor", "stop", "generation"]
    )
    async def test_interrupt_rechecks_handoff_after_stop(self, tmp_path, hazard):
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_interrupt

        state, slot, _ = self._make_state_and_slot(tmp_path)
        runner = asyncio.create_task(asyncio.Event().wait())
        slot.task = runner
        slot.queue_append("keep queued")

        async def stop_turn(*args, **kwargs):
            if hazard != "successor":
                runner.cancel()
                await await_until(asyncio.gather(runner, return_exceptions=True))
            slot.task = None
            if hazard == "replacement":
                state._slots[slot.key] = _ChatSlot(slot.key)
            elif hazard == "rebind":
                slot.linked_session_key = "cron:another-owner"
            elif hazard == "auth":
                slot._last_turn_auth_required = True
            elif hazard == "successor":
                slot.task = runner
            elif hazard == "generation":
                slot._stop_state = "idle"
                slot._stop_state = "soft_pending"
                slot._stop_state = "idle"
            return "idle"

        def authorize(request, checked_slot, operation, key):
            if key == "cron:another-owner":
                return web.json_response({"error": "not authorized"}, status=403)
            return None

        state.sessions.stop_turn = stop_turn
        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/interrupt", api_chat_slot_interrupt)
        dispatched = AsyncMock(return_value=True)

        class SupersedingStopLock:
            async def __aenter__(self):
                slot._stop_state = "soft_pending"

            async def __aexit__(self, *args):
                return False

        if hazard == "stop":
            slot._lock = SupersedingStopLock()
        try:
            with (
                patch("kiro_crew.dashboard.chat_handlers._start_next_queued_turn", dispatched),
                patch("kiro_crew.dashboard.chat_handlers._app_cancel_denied", authorize),
                patch("kiro_crew.dashboard.chat_handlers.sel") as sel_factory,
            ):
                async with TestClient(TestServer(app)) as client:
                    response = await client.post(f"/api/chat/slots/{slot.key}/interrupt")
                    # The interrupt happened before the handoff checks, so a
                    # refused handoff still answers success and is audited.
                    assert response.status == 200
                    assert (await response.json())["outcome"] == "idle"
            audited = [
                c.kwargs.get("tool_name")
                for c in sel_factory.return_value.log_tool_invocation.call_args_list
            ]
            assert "dashboard_interrupt" in audited
            if hazard == "none":
                dispatched.assert_awaited_once_with(state, slot)
            else:
                dispatched.assert_not_awaited()
            assert [item["content"] for item in slot._queue] == ["keep queued"]
        finally:
            runner.cancel()
            await await_until(asyncio.gather(runner, return_exceptions=True))

    @pytest.mark.asyncio
    async def test_cancel_during_slow_cold_start_ends_the_turn(self, tmp_path: Path) -> None:
        """Cancelling a turn stuck in ``get_or_create`` ends it without streaming.

        This is what the Stop handler now does when the provider answers
        "idle" for a preparing turn: rather than wait for the cold start to
        reach the dispatch gate, it cancels the task. The turn must still close
        out (done row, chat_done, slot idle) and never open the stream.
        """
        state, slot, _run_chat = self._make_state_and_slot(tmp_path)
        stream_calls: list[str] = []
        client = self._make_client(stream_calls)
        entered = asyncio.Event()

        async def _hung_create(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()  # a cold start that never returns
            return client, True, False

        state.sessions.get_or_create = AsyncMock(side_effect=_hung_create)
        turn = asyncio.create_task(_run_chat(state, slot, "hello"))
        slot.task = turn
        await await_until(entered)
        assert slot._preparing_task is turn

        turn.cancel()
        await asyncio.wait({turn}, timeout=5)

        assert turn.done()
        assert stream_calls == []
        assert slot.task is None
        assert slot._preparing_task is None
        assert slot.messages[-1]["role"] == "done"
        assert any(call.args[0] == "chat_done" for call in state.broadcast_ws.call_args_list)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("guarded", [False, True])
    async def test_stop_button_during_slow_cold_start_frees_the_slot(
        self, tmp_path: Path, guarded: bool
    ) -> None:
        """End to end: the real Stop handler against a real preparing turn.

        Before the fix the card settled "stopped" while the turn kept waiting on
        its cold start, so the slot stayed busy (the reported "[Stopped] but
        still running" delay). Now the same press leaves the slot idle.
        """
        from kiro_crew.dashboard.chat_handlers import stop_slot_turn
        from kiro_crew.dashboard.turn_dispatch import spawn_guarded_turn

        state, slot, _run_chat = self._make_state_and_slot(tmp_path)
        stream_calls: list[str] = []
        client = self._make_client(stream_calls)
        entered = asyncio.Event()

        async def _hung_create(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
            return client, True, False

        state.sessions.get_or_create = AsyncMock(side_effect=_hung_create)
        # What SessionManager.stop_turn answers when no provider turn exists yet.
        state.sessions.stop_turn = AsyncMock(return_value="idle")
        coro = state.run_background_turn(slot, _run_chat(state, slot, "hello"))
        turn = spawn_guarded_turn(state, slot, coro) if guarded else asyncio.create_task(coro)
        slot.task = turn
        await await_until(entered)
        assert slot.running

        await stop_slot_turn(state, slot)

        assert turn.done()
        assert not slot.running
        assert stream_calls == []
        cards = [
            json.loads(m["cls"])
            for m in slot.messages
            if isinstance(m.get("cls"), str) and '"stop_event"' in m["cls"]
        ]
        assert [c["state"] for c in cards] == ["stopped"]
