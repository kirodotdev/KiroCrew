"""Tests for dashboard hook handlers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import ConversationLog
from kiro_crew.hooks import HOOK_EVENT_AGENT_SPAWN, HOOK_EVENT_USER_PROMPT_SUBMIT, ScriptHookStore


def _make_state(tmp_path):
    """Create a DashboardState wired for _run_chat hook tests."""
    sessions = MagicMock(count=0)
    sessions.remove = AsyncMock()
    sessions.get_pid = MagicMock(return_value=None)
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.context_builder = None
    return state


class TestHookHandlerIntegration:
    """Integration tests for hook store used by dashboard handlers."""

    def test_create_and_retrieve_hook(self, tmp_path):
        """Hook CRUD operations work through store."""
        store = ScriptHookStore(tmp_path)

        # Create
        hook = store.create(
            {
                "name": "test-hook",
                "event": HOOK_EVENT_USER_PROMPT_SUBMIT,
                "command": "echo test",
                "timeout": 30,
            }
        )
        assert hook.id is not None

        # Retrieve
        retrieved = store.get(hook.id)
        assert retrieved is not None
        assert retrieved.name == "test-hook"

        # Update
        updated = store.update(hook.id, {"name": "updated-name"})
        assert updated.name == "updated-name"

        # Delete
        assert store.delete(hook.id) is True
        assert store.get(hook.id) is None


class TestAgentSpawnHookInjection:
    """AgentSpawn hook stdout must be injected into session context on new sessions."""

    @pytest.mark.asyncio
    async def test_not_injected_on_existing_session(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import LLMEvent

        monkeypatch.setattr("kiro_crew.dashboard.chat.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.sel", lambda: MagicMock())

        state = _make_state(tmp_path)
        hook_store = ScriptHookStore(config_dir=tmp_path)
        hook_store.create({
            "name": "startup-prefs",
            "event": HOOK_EVENT_AGENT_SPAWN,
            "command": "echo 'Enable caveman mode'",
            "timeout": 5,
        })
        state._hook_store = hook_store

        captured_message = None
        fake_client = AsyncMock()

        async def _stream(msg):
            nonlocal captured_message
            captured_message = msg
            yield LLMEvent(kind="text_chunk", text="ok")
            yield LLMEvent(kind="complete")

        fake_client.stream = _stream
        fake_client.stream_command = _stream
        fake_client.context_usage_pct = MagicMock(return_value=0.0)
        # is_new=False — existing session
        state.sessions.get_or_create = AsyncMock(return_value=(fake_client, False, False))

        slot = state.get_or_create_slot("s1")
        await _run_chat(state, slot, "hello")

        assert captured_message is not None
        assert "Enable caveman mode" not in captured_message


class TestHookSessionKeyForwarding:
    """The chat path must forward its session key to fired hooks so downstream
    hook scripts can attribute each turn to the correct session identity."""

    @pytest.mark.asyncio
    async def test_session_key_passed_to_fire(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import LLMEvent

        monkeypatch.setattr("kiro_crew.dashboard.chat.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.sel", lambda: MagicMock())

        state = _make_state(tmp_path)
        # Mock the hook store so we can inspect the kwargs passed to fire().
        hook_store = MagicMock()
        hook_store.fire = AsyncMock(return_value=[])
        state._hook_store = hook_store

        fake_client = AsyncMock()

        async def _stream(msg):
            yield LLMEvent(kind="text_chunk", text="ok")
            yield LLMEvent(kind="complete")

        fake_client.stream = _stream
        fake_client.stream_command = _stream
        fake_client.context_usage_pct = MagicMock(return_value=0.0)
        state.sessions.get_or_create = AsyncMock(return_value=(fake_client, False, False))

        slot = state.get_or_create_slot("s1")
        await _run_chat(state, slot, "hello")

        # _history_key_for("s1") == "dashboard:s1" — every fired hook must carry it.
        assert hook_store.fire.await_count > 0
        for call in hook_store.fire.await_args_list:
            assert call.kwargs.get("parent_session_key") == "dashboard:s1"
