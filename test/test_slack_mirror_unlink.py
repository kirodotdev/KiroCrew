"""Mirror-suppression regression guard for Slack unlink.

The actual bug this feature fixes: a Slack-linked session mirrors EVERY
dashboard turn back to the thread. Unlink must stop that. This drives
_run_chat through the mirror gate (chat_runner.py:1316-1317) with a real
SessionMap so the link/unlink semantics are exercised end-to-end:

  link  -> run a turn -> post_message + start_stream ARE called (status quo)
  unlink -> run a turn -> NO mirror calls (the fix)
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_ready_kiro_prerequisite

from kiro_crew.dashboard import session_control
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import ConversationLog
from kiro_crew.messaging.link import ChannelLink
from kiro_crew.messaging.transport import TransportCapabilities


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
    sessions.get_mirror_link = session_map.get_mirror_link
    sessions.set_mirror_link = session_map.set_mirror_link
    sessions.set_approval_policy = MagicMock()
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )
    state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
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


def _fake_provider(*, include_tool: bool = False):
    from kiro_crew.providers.base import LLMEvent

    fake_client = AsyncMock()

    async def _stream(msg):
        yield LLMEvent(kind="text_chunk", text="hi")
        if include_tool:
            yield LLMEvent(
                kind="tool_call",
                tool_call_id="tool-1",
                title="Inspect private state",
                tool_purpose="Inspect private state",
            )
        yield LLMEvent(kind="complete")

    fake_client.stream = _stream
    fake_client.stream_command = _stream
    # These provider-client accessors are sync on the real client; leaving
    # them as AsyncMock attrs creates coroutines _run_chat calls but never
    # awaits (it reads them synchronously by design).
    fake_client.context_usage_pct = MagicMock(return_value=0.0)
    fake_client.context_window_tokens = MagicMock(return_value=0)
    fake_client.context_used_tokens = MagicMock(return_value=0)
    fake_client.mcp_session_report = MagicMock(return_value=None)
    # getattr(client, "client", None) reaches a nested ACP client for
    # pop_pending_oauth_requests(); a bare AsyncMock().client would make that
    # lookup return another AsyncMock whose call also goes unawaited.
    fake_client.client = None
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
        state.sessions.get_or_create = AsyncMock(return_value=(_fake_provider(), False, False))

        slot = state.get_or_create_slot("s1")
        session_key = _history_key_for(slot.key)

        # ── Link the session to a Slack thread ──
        state.sessions.set_slack_link(session_key, "thread-1", "C-1")
        slot._slack_linked = True
        slot._slack_channel = "C-1"
        slot._slack_thread_ts = "thread-1"

        # ── Linked turn: mirror MUST fire ──
        await _run_chat(state, slot, "first message")
        assert (
            state.slack_client.post_message.await_count >= 1
        ), "linked turn should mirror user msg"
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
        assert (
            state.slack_client.start_stream.await_count == 0
        ), "unlinked turn must NOT open a stream"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("change", ["unchanged", "added", "retargeted"])
    async def test_scheduled_fence_guards_slack_user_and_reply_egress(
        self, tmp_path, monkeypatch, change
    ):
        from kiro_crew.dashboard.chat import _history_key_for, _run_chat
        from kiro_crew.session_map import SessionMap

        monkeypatch.setattr("kiro_crew.dashboard.chat.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.sel", lambda: MagicMock())
        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            session_map = SessionMap()
        state = _make_state(tmp_path, session_map)
        state.slack_client = _make_slack_client()
        state.sessions.get_or_create = AsyncMock(return_value=(_fake_provider(), False, False))
        slot = state.get_or_create_slot("s1")
        session_key = _history_key_for(slot.key)
        if change in {"unchanged", "retargeted"}:
            state.sessions.set_slack_link(session_key, "thread-a", "C-A")
        admission = session_control.containment_meta(state, slot)

        def build(message, *_args, **_kwargs):
            if change == "added":
                state.sessions.set_slack_link(session_key, "thread-a", "C-A")
            elif change == "retargeted":
                state.sessions.set_slack_link(session_key, "thread-b", "C-B")
            return message, None

        state.context_builder.build_message.side_effect = build
        await _run_chat(
            state,
            slot,
            "scheduled text",
            _audience_containment_admission=admission,
        )

        if change == "unchanged":
            assert state.slack_client.post_message.await_count >= 2
            assert state.slack_client.start_stream.await_count == 1
        else:
            state.slack_client.post_message.assert_not_awaited()
            state.slack_client.start_stream.assert_not_awaited()
        assert slot._steer_audience_fences == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("change", ["unchanged", "added", "retargeted"])
    async def test_scheduled_fence_guards_channel_user_and_reply_egress(
        self, tmp_path, monkeypatch, change
    ):
        from kiro_crew.dashboard.chat import _history_key_for, _run_chat
        from kiro_crew.session_map import SessionMap

        monkeypatch.setattr("kiro_crew.dashboard.chat.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.sel", lambda: MagicMock())
        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            session_map = SessionMap()
        state = _make_state(tmp_path, session_map)
        transport = SimpleNamespace(
            channel_type="telegram",
            capabilities=TransportCapabilities(supports_proactive_send=True),
            may_send_to=lambda *_args, **_kwargs: True,
            send_message=AsyncMock(return_value="sent"),
        )
        state.register_channel_transport(transport)
        state.sessions.get_or_create = AsyncMock(return_value=(_fake_provider(), False, False))
        slot = state.get_or_create_slot("s1")
        session_key = _history_key_for(slot.key)
        mirror_a = ChannelLink("telegram", "chat-a")
        mirror_b = ChannelLink("telegram", "chat-b")
        if change in {"unchanged", "retargeted"}:
            state.sessions.set_mirror_link(session_key, mirror_a)
        admission = session_control.containment_meta(state, slot)

        def build(message, *_args, **_kwargs):
            if change == "added":
                state.sessions.set_mirror_link(session_key, mirror_a)
            elif change == "retargeted":
                state.sessions.set_mirror_link(session_key, mirror_b)
            return message, None

        state.context_builder.build_message.side_effect = build
        await _run_chat(
            state,
            slot,
            "scheduled text",
            _audience_containment_admission=admission,
        )

        if change == "unchanged":
            assert transport.send_message.await_count == 2
        else:
            transport.send_message.assert_not_awaited()
        assert slot._steer_audience_fences == {}

    @pytest.mark.asyncio
    async def test_slack_retarget_between_authorization_and_selection_never_substitutes_target(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.chat import _history_key_for, _run_chat
        from kiro_crew.session_map import SessionMap

        monkeypatch.setattr("kiro_crew.dashboard.chat.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.sel", lambda: MagicMock())
        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            session_map = SessionMap()
        state = _make_state(tmp_path, session_map)
        state.slack_client = _make_slack_client()
        state.sessions.get_or_create = AsyncMock(
            return_value=(_fake_provider(include_tool=True), False, False)
        )
        slot = state.get_or_create_slot("s1")
        session_key = _history_key_for(slot.key)
        state.sessions.set_slack_link(session_key, "thread-a", "C-A")
        admission = session_control.containment_meta(state, slot)

        real_gate = chat_runner.cross_surface_withheld
        retargeted = False

        def retarget_after_gate(state_arg, slot_arg, **kwargs):
            nonlocal retargeted
            withheld = real_gate(state_arg, slot_arg, **kwargs)
            if not retargeted:
                retargeted = True
                state.sessions.set_slack_link(session_key, "thread-b", "C-B")
            return withheld

        monkeypatch.setattr(chat_runner, "cross_surface_withheld", retarget_after_gate)

        await _run_chat(
            state,
            slot,
            "scheduled private text",
            _audience_containment_admission=admission,
        )

        assert retargeted is True
        assert state.slack_client.post_message.await_count >= 1
        assert all(
            call.args[0] == "C-A" for call in state.slack_client.post_message.await_args_list
        )
        assert all(
            call.args[0] != "C-B" for call in state.slack_client.start_stream.await_args_list
        )
        assert all(call.args[0] != "C-B" for call in state.slack_client.append_task.await_args_list)
        assert all(call.args[0] != "C-B" for call in state.slack_client.stop_stream.await_args_list)

    @pytest.mark.asyncio
    async def test_channel_retarget_between_authorization_and_selection_never_substitutes_target(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.chat import _history_key_for, _run_chat
        from kiro_crew.session_map import SessionMap

        monkeypatch.setattr("kiro_crew.dashboard.chat.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.sel", lambda: MagicMock())
        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            session_map = SessionMap()
        state = _make_state(tmp_path, session_map)
        transport = SimpleNamespace(
            channel_type="telegram",
            capabilities=TransportCapabilities(supports_proactive_send=True),
            may_send_to=lambda *_args, **_kwargs: True,
            send_message=AsyncMock(return_value="sent"),
        )
        state.register_channel_transport(transport)
        state.sessions.get_or_create = AsyncMock(return_value=(_fake_provider(), False, False))
        slot = state.get_or_create_slot("s1")
        session_key = _history_key_for(slot.key)
        mirror_a = ChannelLink("telegram", "chat-a")
        mirror_b = ChannelLink("telegram", "chat-b")
        state.sessions.set_mirror_link(session_key, mirror_a)
        admission = session_control.containment_meta(state, slot)

        real_gate = chat_runner.cross_surface_withheld
        retargeted = False

        def retarget_after_gate(state_arg, slot_arg, **kwargs):
            nonlocal retargeted
            withheld = real_gate(state_arg, slot_arg, **kwargs)
            if not retargeted:
                retargeted = True
                state.sessions.set_mirror_link(session_key, mirror_b)
            return withheld

        monkeypatch.setattr(chat_runner, "cross_surface_withheld", retarget_after_gate)

        await _run_chat(
            state,
            slot,
            "scheduled private text",
            _audience_containment_admission=admission,
        )

        assert retargeted is True
        assert transport.send_message.await_count == 1
        assert transport.send_message.await_args.args[0] == "chat-a"
        assert all(call.args[0] != "chat-b" for call in transport.send_message.await_args_list)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("delivery", ["user", "reply"])
    async def test_selected_channel_target_is_looked_up_once_and_sent_unchanged(
        self, tmp_path, monkeypatch, delivery
    ):
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.chat import _history_key_for
        from kiro_crew.session_map import SessionMap

        monkeypatch.setattr("kiro_crew.dashboard.chat.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.sel", lambda: MagicMock())
        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            session_map = SessionMap()
        state = _make_state(tmp_path, session_map)
        transport = SimpleNamespace(
            channel_type="telegram",
            capabilities=TransportCapabilities(supports_proactive_send=True),
            may_send_to=lambda *_args, **_kwargs: True,
            send_message=AsyncMock(return_value="sent"),
        )
        state.register_channel_transport(transport)
        slot = state.get_or_create_slot("s1")
        session_key = _history_key_for(slot.key)
        mirror_a = ChannelLink("telegram", "chat-a")
        mirror_b = ChannelLink("telegram", "chat-b")
        state.sessions.set_mirror_link(session_key, mirror_a)
        slot._steer_audience_fences["scheduled-message"] = session_control.containment_meta(
            state, slot
        )
        state.sessions.get_mirror_link = MagicMock(side_effect=[mirror_a, mirror_b])

        if delivery == "user":
            await chat_runner._deliver_cross_surface_user_message(
                state,
                session_key,
                "private user text",
                slot=slot,
            )
        else:
            await chat_runner._deliver_cross_surface_reply(
                state,
                session_key,
                "private reply text",
                slot=slot,
            )

        state.sessions.get_mirror_link.assert_called_once_with(session_key)
        assert transport.send_message.await_args.args[0] == "chat-a"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("delivery", ["user", "reply"])
    async def test_unreadable_channel_target_fails_closed_without_breaking_turn(
        self, tmp_path, delivery
    ):
        from kiro_crew.dashboard import chat_runner

        state = _make_state(tmp_path, MagicMock())
        transport = SimpleNamespace(
            channel_type="telegram",
            capabilities=TransportCapabilities(supports_proactive_send=True),
            may_send_to=lambda *_args, **_kwargs: True,
            send_message=AsyncMock(return_value="sent"),
        )
        state.register_channel_transport(transport)
        state.sessions.get_mirror_link = MagicMock(side_effect=OSError("map unreadable"))
        slot = state.get_or_create_slot("s1")
        slot._steer_audience_fences["scheduled-message"] = {
            session_control.QUEUED_CONTAINMENT_META_KEY: {
                "linked": False,
                "mirrored": False,
                "ephemeral": False,
                "app": False,
                "unattended": False,
                "workspace": "default",
                "mirror_identity": "",
            }
        }

        if delivery == "user":
            await chat_runner._deliver_cross_surface_user_message(
                state,
                "dashboard:s1",
                "private user text",
                slot=slot,
            )
        else:
            await chat_runner._deliver_cross_surface_reply(
                state,
                "dashboard:s1",
                "private reply text",
                slot=slot,
            )

        transport.send_message.assert_not_awaited()

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
        state.sessions.get_or_create = AsyncMock(return_value=(_fake_provider(), False, False))

        slot = state.get_or_create_slot("s1")
        session_key = _history_key_for(slot.key)  # "dashboard:s1"
        raw_key = session_key[len("dashboard:") :]  # "s1"

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
