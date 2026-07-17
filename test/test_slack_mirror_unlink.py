"""Mirror-suppression regression guard for Slack unlink (Mesh-1969).

The actual bug this feature fixes: a Slack-linked session mirrors EVERY
dashboard turn back to the thread. Unlink must stop that. This drives
_run_chat through the mirror gate (chat_runner.py:1316-1317) with a real
SessionMap so the link/unlink semantics are exercised end-to-end:

  link  -> run a turn -> post_message + start_stream ARE called (status quo)
  unlink -> run a turn -> NO mirror calls (the fix)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import ConversationLog


def _make_context_builder():
    """A context_builder whose build_message echoes the user message back.

    The mirror gate only captures _user_msg_for_mirror inside the
    `elif state.context_builder:` branch (chat_runner.py), so a truthy
    context_builder is required for the turn-start mirror path to run.
    """
    cb = MagicMock()
    cb.build_message.side_effect = lambda message, *a, **k: (message, None)
    cb.conversation_log = None
    return cb


def _make_state(tmp_path, session_map):
    """DashboardState wired for _run_chat with a REAL SessionMap."""
    sessions = MagicMock(count=0)
    sessions.remove = AsyncMock()
    sessions.get_pid = MagicMock(return_value=None)
    # Route the slack-link reads/writes the mirror gate uses through the real map.
    sessions.get_slack_link = session_map.get_slack_link
    sessions.set_slack_link = session_map.set_slack_link
    sessions.clear_slack_link = session_map.clear_slack_link
    sessions.get_session_for_thread = session_map.get_session_for_thread
    sessions.set_approval_policy = MagicMock()
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.context_builder = _make_context_builder()
    return state


def _make_slack_client():
    client = MagicMock()
    client.post_message = AsyncMock(return_value="mirror-ts")
    client.start_stream = AsyncMock(return_value="stream-ts")
    client.append_stream = AsyncMock()
    client.stop_stream = AsyncMock()
    return client


def _fake_provider():
    from kiro_crew.providers.base import LLMEvent

    fake_client = AsyncMock()

    async def _stream(msg):
        yield LLMEvent(kind="text_chunk", text="hi")
        yield LLMEvent(kind="complete")

    fake_client.stream = _stream
    fake_client.stream_command = _stream
    fake_client.context_usage_pct = MagicMock(return_value=0.0)
    return fake_client


class TestMirrorSuppressionAfterUnlink:
    @pytest.mark.asyncio
    async def test_linked_turn_mirrors_then_unlinked_turn_does_not(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat import _history_key_for, _run_chat
        from kiro_crew.dashboard.chat_slack import api_chat_slot_slack_unlink
        from kiro_crew.session_map import SessionMap

        monkeypatch.setattr("kiro_crew.dashboard.chat.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.sel", lambda: MagicMock())
        monkeypatch.setattr("kiro_crew.dashboard.chat_slack.sel", lambda: MagicMock())

        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            session_map = SessionMap()

        state = _make_state(tmp_path, session_map)
        state.slack_client = _make_slack_client()
        state.sessions.get_or_create = AsyncMock(
            return_value=(_fake_provider(), False, False)
        )

        slot = state.get_or_create_slot("s1")
        session_key = _history_key_for(slot.key)

        # ── Link the session to a Slack thread ──
        state.sessions.set_slack_link(session_key, "thread-1", "C-1")
        slot._slack_linked = True
        slot._slack_channel = "C-1"
        slot._slack_thread_ts = "thread-1"

        # ── Linked turn: mirror MUST fire ──
        await _run_chat(state, slot, "first message")
        assert state.slack_client.post_message.await_count >= 1, "linked turn should mirror user msg"
        assert state.slack_client.start_stream.await_count == 1, "linked turn should open a stream"

        # ── Unlink via the real endpoint ──
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/slack-unlink", api_chat_slot_slack_unlink)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/s1/slack-unlink")
            assert resp.status == 200
            assert (await resp.json())["was_linked"] is True

        # Reset call counters so the post-unlink turn is measured cleanly.
        state.slack_client.post_message.reset_mock()
        state.slack_client.start_stream.reset_mock()

        # ── Unlinked turn: NO mirror calls ──
        await _run_chat(state, slot, "second message")
        assert state.slack_client.post_message.await_count == 0, "unlinked turn must NOT mirror"
        assert state.slack_client.start_stream.await_count == 0, "unlinked turn must NOT open a stream"

    @pytest.mark.asyncio
    async def test_dual_key_inheritance_does_not_resurrect_link(self, tmp_path, monkeypatch):
        """Even when chat_runner has copied the link onto the dashboard:-prefixed
        key, unlink clears BOTH so a follow-up turn stays silent. This is the
        highest-risk path: clearing only one key lets mirroring silently resume."""
        from kiro_crew.dashboard.chat import _history_key_for, _run_chat
        from kiro_crew.dashboard.chat_slack import api_chat_slot_slack_unlink
        from kiro_crew.session_map import SessionMap

        monkeypatch.setattr("kiro_crew.dashboard.chat.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.sel", lambda: MagicMock())
        monkeypatch.setattr("kiro_crew.dashboard.chat_slack.sel", lambda: MagicMock())

        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            session_map = SessionMap()

        state = _make_state(tmp_path, session_map)
        state.slack_client = _make_slack_client()
        state.sessions.get_or_create = AsyncMock(
            return_value=(_fake_provider(), False, False)
        )

        slot = state.get_or_create_slot("s1")
        session_key = _history_key_for(slot.key)  # "dashboard:s1"
        raw_key = session_key[len("dashboard:"):]   # "s1"

        # Simulate the dual-key state: link on the dashboard:-prefixed key AND
        # the raw key (the runner's inheritance copy at chat_runner.py:807-817).
        state.sessions.set_slack_link(session_key, "thread-1", "C-1")
        state.sessions.set_slack_link(raw_key, "thread-1", "C-1")
        slot._slack_linked = True
        slot._slack_channel = "C-1"
        slot._slack_thread_ts = "thread-1"

        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/slack-unlink", api_chat_slot_slack_unlink)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/s1/slack-unlink")
            assert resp.status == 200

        # BOTH keys must be clear — no residual link to re-inherit.
        assert state.sessions.get_slack_link(session_key) == (None, None)
        assert state.sessions.get_slack_link(raw_key) == (None, None)
        # Inbound severance: the reverse index must be gone so a later Slack
        # reply in the old thread does not re-route and re-link this session.
        assert state.sessions.get_session_for_thread("thread-1") is None

        state.slack_client.post_message.reset_mock()
        state.slack_client.start_stream.reset_mock()

        await _run_chat(state, slot, "after unlink")
        assert state.slack_client.post_message.await_count == 0
        assert state.slack_client.start_stream.await_count == 0
