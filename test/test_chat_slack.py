"""Unit tests for chat_slack.py — Slack link, handoff, channel listing."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state, drain_background_tasks


def _make_slack_app(state):
    from kiro_crew.dashboard.chat_slack import (
        api_chat_slot_handoff,
        api_chat_slot_slack_link,
        api_chat_slot_slack_unlink,
        api_handoff_channels,
        api_slack_channels,
    )

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/slack-link", api_chat_slot_slack_link)
    app.router.add_post("/api/chat/slots/{slot}/slack-unlink", api_chat_slot_slack_unlink)
    app.router.add_get("/api/slack/channels", api_slack_channels)
    app.router.add_post("/api/chat/slots/{slot}/handoff", api_chat_slot_handoff)
    app.router.add_get("/api/handoff-channels", api_handoff_channels)
    return app


class TestSlackLink:
    @pytest.mark.asyncio
    async def test_slot_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_slack_app(state))) as client:
            resp = await client.post("/api/chat/slots/nope/slack-link")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_no_slack_client(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")
        state.slack_client = None
        async with TestClient(TestServer(_make_slack_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/slack-link")
            assert resp.status == 503

    @pytest.mark.asyncio
    async def test_link_success(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hello")
        slot.drain()
        state.slack_client = MagicMock()
        state.slack_client.open_dm = AsyncMock(return_value="C123")
        state.slack_client.post_message = AsyncMock(return_value="ts123")
        state.owner_id = "U123"
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))
        state.sessions.set_slack_link = MagicMock()
        state.push_slots_update = MagicMock()
        async with TestClient(TestServer(_make_slack_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/slack-link", json={})
            assert resp.status == 200
            await drain_background_tasks(state)
            data = await resp.json()
            assert data["ok"] is True
            assert data["thread_ts"] == "ts123"

    @pytest.mark.asyncio
    async def test_link_to_existing_thread_no_new_post(self, tmp_path, monkeypatch):
        """challenge-redirect auto-link: link to an existing thread_ts.

        Must NOT post a new root thread message and must NOT replay context
        (the thread already has it), but MUST register the reverse link so a
        later reply in that thread routes back to this session.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hello")
        slot.append("assistant", "hi there")
        slot.drain()
        state.slack_client = MagicMock()
        state.slack_client.open_dm = AsyncMock(return_value="C123")
        state.slack_client.post_message = AsyncMock(return_value="newts")
        state.owner_id = "U123"
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))
        state.sessions.set_slack_link = MagicMock()
        state.push_slots_update = MagicMock()
        async with TestClient(TestServer(_make_slack_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/slack-link",
                json={"channel": "C999", "thread_ts": "1700.42"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            # Links to the supplied thread, not a freshly posted one.
            assert data["thread_ts"] == "1700.42"
            assert data["channel"] == "C999"
        # No message posted (neither a new root thread nor replayed context).
        assert state.slack_client.post_message.await_count == 0
        # Reverse link registered so future thread replies find this session.
        state.sessions.set_slack_link.assert_called_once()
        args = state.sessions.set_slack_link.call_args.args
        assert args[1] == "1700.42"
        assert args[2] == "C999"


class TestSlackUnlink:
    @pytest.mark.asyncio
    async def test_slot_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_slack_app(state))) as client:
            resp = await client.post("/api/chat/slots/nope/slack-unlink")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_unlink_clears_both_key_variants(self, tmp_path, monkeypatch):
        """The handler must clear BOTH the raw and the dashboard:-prefixed keys.
        chat_runner copies the link onto the dashboard:-prefixed key at turn
        start, so clearing only one leaves the next turn to re-inherit it."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._slack_linked = True
        slot._slack_channel = "C123"
        slot._slack_thread_ts = "ts123"
        state.slack_client = MagicMock()
        state.slack_client.post_message = AsyncMock()
        state.sessions.clear_slack_link = MagicMock(return_value=True)
        state.push_slots_update = MagicMock()
        async with TestClient(TestServer(_make_slack_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/slack-unlink")
            assert resp.status == 200
            data = await resp.json()
            assert data == {"ok": True, "was_linked": True}
        # Both key variants cleared: "dashboard:s1" (from _history_key_for) and "s1".
        cleared_keys = {c.args[0] for c in state.sessions.clear_slack_link.call_args_list}
        assert cleared_keys == {"dashboard:s1", "s1"}
        # Slot link state reset so the badge flips off and "Send to Slack" returns.
        assert slot._slack_linked is False
        assert slot._slack_channel == ""
        assert slot._slack_thread_ts == ""

    @pytest.mark.asyncio
    async def test_unlink_posts_courtesy_note(self, tmp_path, monkeypatch):
        """On a real unlink, a best-effort note is posted to the old thread."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._slack_linked = True
        slot._slack_channel = "C123"
        slot._slack_thread_ts = "ts123"
        state.slack_client = MagicMock()
        state.slack_client.post_message = AsyncMock()
        state.sessions.clear_slack_link = MagicMock(return_value=True)
        state.push_slots_update = MagicMock()
        async with TestClient(TestServer(_make_slack_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/slack-unlink")
            assert resp.status == 200
        state.slack_client.post_message.assert_awaited_once()
        args = state.slack_client.post_message.await_args.args
        assert args[0] == "C123"  # posted to the old channel
        assert args[2] == "ts123"  # in the old thread

    @pytest.mark.asyncio
    async def test_unlink_idempotent_when_not_linked(self, tmp_path, monkeypatch):
        """Unlinking an unlinked session is a no-op: was_linked False, no post."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")
        state.slack_client = MagicMock()
        state.slack_client.post_message = AsyncMock()
        state.sessions.clear_slack_link = MagicMock(return_value=False)
        state.push_slots_update = MagicMock()
        async with TestClient(TestServer(_make_slack_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/slack-unlink")
            assert resp.status == 200
            data = await resp.json()
            assert data == {"ok": True, "was_linked": False}
        # No courtesy note when nothing was linked.
        state.slack_client.post_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unlink_courtesy_note_failure_non_fatal(self, tmp_path, monkeypatch):
        """A Slack post failure during the courtesy note must not fail the unlink."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._slack_linked = True
        slot._slack_channel = "C123"
        slot._slack_thread_ts = "ts123"
        state.slack_client = MagicMock()
        state.slack_client.post_message = AsyncMock(side_effect=RuntimeError("slack down"))
        state.sessions.clear_slack_link = MagicMock(return_value=True)
        state.push_slots_update = MagicMock()
        async with TestClient(TestServer(_make_slack_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/slack-unlink")
            assert resp.status == 200
            data = await resp.json()
            assert data["was_linked"] is True
        assert slot._slack_linked is False


class TestSlackLinkUnlinkRoundTrip:
    @pytest.mark.asyncio
    async def test_link_then_unlink_real_session_map(self, tmp_path, monkeypatch):
        """End-to-end with a REAL SessionMap: link then unlink leaves
        get_slack_link == (None, None) and drops the reverse index.

        The slack endpoints only call the slack-link delegation methods
        (get/set/clear_slack_link, get_session_for_thread), all of which
        SessionMap implements directly — so a raw SessionMap stands in for
        the SessionManager across the endpoint calls below. It does NOT stand
        in for the allocator surface, and slot creation uses that (it
        supersedes any retirement arm left on a recycled key), so the slot is
        created before the swap rather than after it.
        """
        from unittest.mock import patch

        from kiro_crew.session_map import SessionMap

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        # Swap the mocked sessions for a real SessionMap backed by tmp_path.
        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            state.sessions = SessionMap()

        slot.append("user", "hello")
        slot.drain()
        state.slack_client = MagicMock()
        state.slack_client.open_dm = AsyncMock(return_value="C123")
        state.slack_client.post_message = AsyncMock(return_value="ts123")
        state.owner_id = "U123"
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(_make_slack_app(state))) as client:
            link = await client.post("/api/chat/slots/s1/slack-link", json={})
            assert link.status == 200
            await drain_background_tasks(state)
            link_data = await link.json()
            ts = link_data["thread_ts"]
            assert state.sessions.get_session_for_thread(ts) == "dashboard:s1"

            unlink = await client.post("/api/chat/slots/s1/slack-unlink")
            assert unlink.status == 200
            unlink_data = await unlink.json()
            assert unlink_data["was_linked"] is True

        assert state.sessions.get_slack_link("dashboard:s1") == (None, None)
        assert state.sessions.get_session_for_thread(ts) is None


class TestSlackChannels:
    @pytest.mark.asyncio
    async def test_list_channels(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mock_cfg = MagicMock()
        mock_cfg.slack.tracking_channels = [{"channel_id": "C1", "name": "general"}]
        mock_cfg.slack_channels = {}
        monkeypatch.setattr("kiro_crew.config.loader.KiroCrewConfig.load", lambda: mock_cfg)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_slack_app(state))) as client:
            resp = await client.get("/api/slack/channels")
            assert resp.status == 200
            data = await resp.json()
            assert data[0]["id"] == "dm"
            assert any(c["id"] == "C1" for c in data)

    @pytest.mark.asyncio
    async def test_resolves_names_for_slack_channels_dict(self, tmp_path, monkeypatch):
        """cfg.slack_channels entries (no name field) should have names resolved."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mock_cfg = MagicMock()
        mock_cfg.slack.tracking_channels = []
        cc = MagicMock()
        cc.activation = "always"
        mock_cfg.slack_channels = {"C0AU38Q0E4B": cc}
        monkeypatch.setattr("kiro_crew.config.loader.KiroCrewConfig.load", lambda: mock_cfg)
        state = _make_state(tmp_path)
        state.slack_client = MagicMock()
        state.slack_client.conversations_list = AsyncMock(
            return_value=[
                {"id": "C0AU38Q0E4B", "name": "pcn-orchestrator-interest"},
            ]
        )
        async with TestClient(TestServer(_make_slack_app(state))) as client:
            resp = await client.get("/api/slack/channels")
            assert resp.status == 200
            data = await resp.json()
            resolved = next((c for c in data if c["id"] == "C0AU38Q0E4B"), None)
            assert resolved is not None
            assert resolved["name"] == "pcn-orchestrator-interest"

    @pytest.mark.asyncio
    async def test_no_slack_client_falls_back_to_id(self, tmp_path, monkeypatch):
        """Without a Slack client, unresolved channels keep id as name (no crash)."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mock_cfg = MagicMock()
        mock_cfg.slack.tracking_channels = []
        cc = MagicMock()
        cc.activation = "always"
        mock_cfg.slack_channels = {"C0AU38Q0E4B": cc}
        monkeypatch.setattr("kiro_crew.config.loader.KiroCrewConfig.load", lambda: mock_cfg)
        state = _make_state(tmp_path)
        state.slack_client = None
        async with TestClient(TestServer(_make_slack_app(state))) as client:
            resp = await client.get("/api/slack/channels")
            assert resp.status == 200
            data = await resp.json()
            unresolved = next((c for c in data if c["id"] == "C0AU38Q0E4B"), None)
            assert unresolved is not None
            assert unresolved["name"] == "C0AU38Q0E4B"


class TestHandoff:
    @pytest.mark.asyncio
    async def test_handoff_no_slack(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")
        state.slack_client = None
        async with TestClient(TestServer(_make_slack_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/handoff")
            assert resp.status == 503


class TestHandoffChannels:
    @pytest.mark.asyncio
    async def test_deprecated_endpoint(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_slack_app(state))) as client:
            resp = await client.get("/api/handoff-channels")
            assert resp.status == 200
            assert await resp.json() == {}


class TestSlackLinkAnchorTitleFallback:
    """B-lite: the fresh-anchor title must never be the raw slot key —
    fallback chain: slot.title → first-prompt snippet → 'New session'."""

    def _state(self, tmp_path):
        state = _make_state(tmp_path)
        state.slack_client = MagicMock()
        state.slack_client.open_dm = AsyncMock(return_value="D123")
        state.slack_client.post_message = AsyncMock(return_value="newts")
        state.owner_id = "U123"
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))
        state.sessions.set_slack_link = MagicMock()
        state.push_slots_update = MagicMock()
        return state

    async def _link(self, state):
        async with TestClient(TestServer(_make_slack_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/slack-link", json={})
            assert resp.status == 200
            await drain_background_tasks(state)

    def _anchor_text(self, state) -> str:
        return state.slack_client.post_message.await_args_list[0].args[1]

    @pytest.mark.asyncio
    async def test_untitled_slot_uses_first_prompt_snippet(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = self._state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.title = ""
        slot.append("user", "fix the   build\nplease")
        slot.drain()
        await self._link(state)
        text = self._anchor_text(state)
        assert "fix the build please" in text  # whitespace collapsed, one line
        assert "s1" not in text  # raw slot key never user-visible

    @pytest.mark.asyncio
    async def test_untitled_slot_no_prompt_uses_default(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = self._state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.title = ""
        await self._link(state)
        text = self._anchor_text(state)
        assert "New session" in text
        assert "s1" not in text

    @pytest.mark.asyncio
    async def test_snippet_truncated(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = self._state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.title = ""
        long_prompt = "word " * 60  # ~300 chars
        slot.append("user", long_prompt)
        slot.drain()
        await self._link(state)
        text = self._anchor_text(state)
        assert long_prompt.strip() not in text  # truncated
        assert "word word word" in text

    @pytest.mark.asyncio
    async def test_titled_slot_keeps_title(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = self._state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.title = "Build triage"
        slot.append("user", "hello")
        slot.drain()
        await self._link(state)
        assert "Build triage" in self._anchor_text(state)

    @pytest.mark.asyncio
    async def test_default_key_title_never_leaks(self, tmp_path, monkeypatch):
        """Fork adaptation guard: a slot fresh from get_or_create_slot carries
        its raw key as the DEFAULT title (state.py initializes title to the
        key); the display_title predicate must treat that as untitled so the
        anchor shows 'New session', never the raw key."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = self._state(tmp_path)
        state.get_or_create_slot("s1")  # title defaults to the key "s1"
        await self._link(state)
        text = self._anchor_text(state)
        assert "New session" in text
        assert "s1" not in text
