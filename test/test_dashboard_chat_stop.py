"""Dashboard chat: stopping a turn — cancelled stop reason, soft and hard stop, stop during preparation."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_ready_kiro_prerequisite, _make_state
from dashboard_chat_helpers import _provider_mock

from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.history import ConversationLog


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
