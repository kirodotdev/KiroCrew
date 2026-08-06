"""Tests for per-interaction telemetry at the two chat-success sites.

A stub ``TelemetryProvider`` installed via a test ``PlatformContext`` must
receive exactly one ``interaction`` event per successful dashboard turn and per
successful Slack turn, zero events on a cancelled turn, and the payload is
strictly metadata (session_key / surface / model — never message text).
"""

from __future__ import annotations

import dataclasses
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from conftest import MockSlackClient
from kiro_crew.acp.types import STOP_REASON_CANCELLED
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.platform import build_default_context
from kiro_crew.platform.context import reset_context, set_context
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

_SECRET_PROMPT = "please summarize my very private prompt text"


class _StubTelemetry:
    """Recording TelemetryProvider stub."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def record_event(self, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))

    def frontend_rum_config(self):
        return None

    def interactions(self) -> list[dict]:
        return [data for (etype, data) in self.events if etype == "interaction"]


class _RaisingTelemetry(_StubTelemetry):
    """TelemetryProvider whose record_event always raises."""

    def record_event(self, event_type: str, data: dict) -> None:
        raise RuntimeError("telemetry sink down")


@pytest.fixture
def stub_telemetry():
    """Install a PlatformContext whose telemetry adapter records events."""
    stub = _StubTelemetry()
    ctx = dataclasses.replace(build_default_context(KiroCrewConfig()), telemetry=stub)
    set_context(ctx)
    yield stub
    reset_context()


@pytest.fixture(autouse=True)
def _clean_slack_module_state():
    """Clear slack handler module-level state between tests (xdist hygiene)."""
    from kiro_crew.slack.handler import _pending_approvals, _thread_agents, _trusted_sessions

    _pending_approvals.clear()
    _trusted_sessions.clear()
    _thread_agents.clear()
    yield
    _pending_approvals.clear()
    _trusted_sessions.clear()
    _thread_agents.clear()


def _assert_metadata_only(payload: dict, surface: str) -> None:
    """The interaction payload is strictly metadata — never message content."""
    assert set(payload.keys()) == {"session_key", "surface", "model"}
    assert payload["surface"] == surface
    assert _SECRET_PROMPT not in str(payload)


# ── Dashboard (_run_chat) ──────────────────────────────────────────────────


class TestDashboardInteractionTelemetry:
    @staticmethod
    def _make_client(events):
        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=10.0)
        client._client = MagicMock()
        # AcpSessionProvider (post-startup client._client) exposes the model via
        # a ``model`` property backed by _handle.model.
        client._client.model = "test-model-id"
        client._client.last_prompt_stats = None

        async def _stream(msg):
            for ev in events:
                yield ev

        client.stream = _stream
        client.stream_command = _stream
        return client

    @staticmethod
    def _make_dash_state(tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.context_builder = None
        state.consolidator = None
        state._hook_store = None
        state._yolo = False
        return state

    @pytest.mark.asyncio
    async def test_successful_turn_records_one_interaction(
        self, tmp_path, monkeypatch, stub_telemetry
    ):
        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="hello there"),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        state = self._make_dash_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, _SECRET_PROMPT)

        interactions = stub_telemetry.interactions()
        assert len(interactions) == 1
        payload = interactions[0]
        _assert_metadata_only(payload, "dashboard")
        assert payload["session_key"]
        assert payload["model"] == "test-model-id"

    @pytest.mark.asyncio
    async def test_cancelled_turn_records_nothing(self, tmp_path, monkeypatch, stub_telemetry):
        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial"),
            LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_CANCELLED),
        ]
        state = self._make_dash_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        assert stub_telemetry.interactions() == []

    @pytest.mark.asyncio
    async def test_telemetry_failure_never_breaks_the_turn(self, tmp_path, monkeypatch):
        """record_event raising must not propagate — best-effort only."""
        stub = _RaisingTelemetry()
        ctx = dataclasses.replace(build_default_context(KiroCrewConfig()), telemetry=stub)
        set_context(ctx)
        try:
            events = [
                LLMEvent(kind=EVENT_TEXT_CHUNK, text="hello"),
                LLMEvent(kind=EVENT_COMPLETE),
            ]
            state = self._make_dash_state(tmp_path, monkeypatch)
            slot = state.get_or_create_slot("s1")
            client = self._make_client(events)
            state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

            from kiro_crew.dashboard.chat import _run_chat

            await _run_chat(state, slot, "hello")

            state.sessions.record_success.assert_called_once()
        finally:
            reset_context()


# ── Slack (handle_message) ─────────────────────────────────────────────────


class _FakeProvider:
    """Fake LLMProvider that yields the given events from stream()."""

    def __init__(self, events: list[LLMEvent]):
        self._events = events

    async def stream(self, message, timeout=120.0):
        for event in self._events:
            yield event
        yield LLMEvent(kind=EVENT_COMPLETE)

    async def approve_tool(self, request_id, option_id="allow_once"):
        pass

    async def reject_tool(self, request_id):
        pass

    async def start(self):
        pass

    async def shutdown(self):
        pass

    def context_usage_pct(self):
        return 0.0


class _FakeSessionManager:
    """Minimal SessionManager double for handle_message."""

    def __init__(self, provider: _FakeProvider):
        self._provider = provider
        self.success_calls: list[str] = []
        self._is_new = True

    async def get_or_create(self, key, agent=None, channel_id=None):
        was_new = self._is_new
        self._is_new = False
        return self._provider, was_new, False

    def check_context_usage(self, key, provider):
        return 0.0

    def record_success(self, key):
        self.success_calls.append(key)

    async def record_failure(self, key):
        return False

    def release(self, key):
        pass

    def begin_turn(self, key):
        pass

    async def set_channel(self, key, channel_id):
        pass

    def get_channel(self, key):
        return None

    def set_slack_link(self, key, thread_ts, channel_id):
        pass

    def get_slack_link(self, key):
        return None, None

    def get_session_for_thread(self, thread_ts):
        return None

    async def close_all(self):
        pass

    async def remove(self, key):
        pass

    async def destroy(self, key):
        pass

    def has_session(self, key):
        return False

    def get_provider(self, key):
        return None

    async def reset(self, key):
        pass

    def get_pid(self, key):
        return None

    def enqueue(self, key, msg_ts, text, **kwargs):
        return False

    def is_cancelled(self, key, msg_ts):
        return False

    def dequeue(self, key):
        return None

    def clear_queue(self, key):
        pass

    async def stop_turn(self, key, *, force=False, on_soft=None, on_hard=None):
        return "soft"


class TestSlackInteractionTelemetry:
    @pytest.mark.asyncio
    async def test_successful_turn_records_one_interaction(self, stub_telemetry):
        from kiro_crew.slack.handler import handle_message

        slack = MockSlackClient()
        provider = _FakeProvider([LLMEvent(kind=EVENT_TEXT_CHUNK, text="the answer is 42")])
        sessions = _FakeSessionManager(provider)

        await handle_message(slack, sessions, "C1", _SECRET_PROMPT, None, "msg1", "U1")

        interactions = stub_telemetry.interactions()
        assert len(interactions) == 1
        payload = interactions[0]
        _assert_metadata_only(payload, "slack")
        assert payload["session_key"]
        # _FakeProvider carries no model attribute — falls back to "".
        assert payload["model"] == ""
        # Telemetry fired on the same branch as record_success.
        assert len(sessions.success_calls) == 1
        assert payload["session_key"] == sessions.success_calls[0]

    @pytest.mark.asyncio
    async def test_cancelled_turn_records_nothing(self, stub_telemetry):
        from kiro_crew.slack.handler import handle_message

        slack = MockSlackClient()
        provider = _FakeProvider(
            [
                LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial"),
                LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_CANCELLED),
            ]
        )
        sessions = _FakeSessionManager(provider)

        await handle_message(slack, sessions, "C1", "hello", None, "msg1", "U1")

        assert stub_telemetry.interactions() == []
        assert sessions.success_calls == []

    @pytest.mark.asyncio
    async def test_telemetry_failure_never_breaks_the_turn(self):
        """record_event raising must not propagate — best-effort only."""
        from kiro_crew.slack.handler import handle_message

        stub = _RaisingTelemetry()
        ctx = dataclasses.replace(build_default_context(KiroCrewConfig()), telemetry=stub)
        set_context(ctx)
        try:
            slack = MockSlackClient()
            provider = _FakeProvider([LLMEvent(kind=EVENT_TEXT_CHUNK, text="done")])
            sessions = _FakeSessionManager(provider)

            await handle_message(slack, sessions, "C1", "hello", None, "msg1", "U1")

            assert len(sessions.success_calls) == 1
        finally:
            reset_context()
