"""Unit tests for chat_slack.py — Slack link, channel listing."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state, drain_background_tasks

from kiro_crew.messaging.link import ChannelLink, binding_token


def _make_slack_app(state):
    from kiro_crew.dashboard.chat_slack import (
        api_chat_slot_slack_link,
        api_chat_slot_slack_unlink,
        api_slack_channels,
    )

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/slack-link", api_chat_slot_slack_link)
    app.router.add_post("/api/chat/slots/{slot}/slack-unlink", api_chat_slot_slack_unlink)
    app.router.add_get("/api/slack/channels", api_slack_channels)
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
    @pytest.mark.parametrize(
        "relink_ts", ["ts-2", "ts-1"], ids=["another-thread", "the-same-thread"]
    )
    async def test_a_relink_that_lands_inside_the_flush_window_survives_the_unlink(
        self, tmp_path, monkeypatch, relink_ts
    ):
        """The teardown is conditional on what the map holds after the flush.

        The durability wait is a real thread hop, and a second same-slot
        ``slack-link`` can run its whole handler inside it (the existing-thread
        branch reaches ``link_slack`` with no network await). An unconditional
        teardown afterwards stripped that NEW link's fields and reverse-index
        entry while the map kept asserting it, and a re-link then short-circuited
        on ``already_linked`` -- no in-process recovery. Now the route reads the
        map once the await returns: a link there is the newer write (the route
        cleared the old one), so its fields and index stand, the thread gets no
        "unlinked" note and no struck control, and the answer says ``relinked``.
        Both a different thread and the very same thread relinked -- the latter
        is the case a field-by-field compare cannot see.
        """
        from kiro_crew.session_map import SessionMap

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.session_map._FLUSH_DEBOUNCE_SECS", 60.0)
        sm = SessionMap()
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        key = f"dashboard:{slot.key}"
        sm.set_slack_link(key, "ts-1", "C-1")
        sm.flush()
        slot._slack_linked = True
        slot._slack_channel = "C-1"
        slot._slack_thread_ts = "ts-1"
        state._slack_to_slot["ts-1"] = slot.key
        for name in (
            "set_slack_link",
            "clear_slack_link",
            "clear_slack_link_if",
            "get_slack_link",
            "batched_save",
        ):
            setattr(state.sessions, name, getattr(sm, name))
        state.slack_client = MagicMock()
        state.slack_client.post_message = AsyncMock()
        state.push_slots_update = MagicMock()
        strikes = AsyncMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_slack.expire_slack_options", strikes)

        async def relink_inside_the_write_window() -> None:
            # The concurrent request, landing while the unlink waits on the disk:
            # the real link path, fields and reverse index included.
            state.link_slack(slot.key, relink_ts, "C-1")
            await sm.aflush()

        state.sessions.aflush = relink_inside_the_write_window
        try:
            async with TestClient(TestServer(_make_slack_app(state))) as client:
                resp = await client.post("/api/chat/slots/s1/slack-unlink")
                assert resp.status == 200
                answer = await resp.json()
            # The newer link stands, in the map and in the process alike.
            assert sm.get_slack_link(key) == (relink_ts, "C-1")
            assert slot._slack_linked is True, "the unlink stripped the relinked slot's fields"
            assert slot._slack_channel == "C-1"
            assert slot._slack_thread_ts == relink_ts
            assert state._slack_to_slot.get(relink_ts) == slot.key
            assert answer == {"ok": True, "was_linked": True, "relinked": True}
            # Nothing told the relinked thread it was unlinked.
            state.slack_client.post_message.assert_not_awaited()
            strikes.assert_not_awaited()
            # The row redraw still goes out: it carries the newer link.
            state.push_slots_update.assert_called()
        finally:
            await sm.aclose()

    @pytest.mark.asyncio
    async def test_a_plain_unlink_still_tears_down_the_slot_and_the_reverse_index(
        self, tmp_path, monkeypatch
    ):
        """No relink inside the window: the old binding's fields and index go, the note is posted."""
        from kiro_crew.session_map import SessionMap

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.session_map._FLUSH_DEBOUNCE_SECS", 60.0)
        sm = SessionMap()
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        key = f"dashboard:{slot.key}"
        sm.set_slack_link(key, "ts-1", "C-1")
        sm.flush()
        slot._slack_linked = True
        slot._slack_channel = "C-1"
        slot._slack_thread_ts = "ts-1"
        state._slack_to_slot["ts-1"] = slot.key
        for name in ("clear_slack_link", "clear_slack_link_if", "get_slack_link", "aflush"):
            setattr(state.sessions, name, getattr(sm, name))
        state.slack_client = MagicMock()
        state.slack_client.post_message = AsyncMock()
        state.push_slots_update = MagicMock()
        try:
            async with TestClient(TestServer(_make_slack_app(state))) as client:
                resp = await client.post("/api/chat/slots/s1/slack-unlink")
                assert resp.status == 200
                assert (await resp.json()) == {"ok": True, "was_linked": True, "relinked": False}
            assert sm.get_slack_link(key) == (None, None)
            assert slot._slack_linked is False
            assert slot._slack_channel == ""
            assert slot._slack_thread_ts == ""
            assert "ts-1" not in state._slack_to_slot
            state.slack_client.post_message.assert_awaited_once()
            assert "ts-1" not in (tmp_path / "session_map.json").read_text(encoding="utf-8")
        finally:
            await sm.aclose()

    @pytest.mark.asyncio
    async def test_a_failed_flush_still_tears_down_the_slot_and_the_reverse_index(
        self, tmp_path, monkeypatch
    ):
        """The in-process teardown follows the map's clear, in failure too.

        The route clears the persisted link, then awaits the map's durability
        point before it tells the user. ``SessionMap.aflush`` re-raises a
        failed write (a full or read-only data home), and the failure must
        surface -- but the map is already clear in memory, so a teardown
        skipped by the raise would leave the slot's fields and the thread's
        reverse-index entry asserting a thread the map does not hold: the
        row keeps rendering, a reply in the thread still resolves here, and a
        retried Unlink is 409 with nothing left to compare. Driven against a
        REAL map with the debounce held off, so the route's own flush is the
        write that fails.
        """
        import errno
        import os

        from kiro_crew.session_map import SessionMap

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.session_map._FLUSH_DEBOUNCE_SECS", 60.0)
        sm = SessionMap()
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        sm.set_slack_link(f"dashboard:{slot.key}", "ts-1", "C-1")
        sm.flush()
        on_disk = tmp_path / "session_map.json"
        assert "ts-1" in on_disk.read_text(encoding="utf-8"), "precondition: the thread is on disk"
        slot._slack_linked = True
        slot._slack_channel = "C-1"
        slot._slack_thread_ts = "ts-1"
        state._slack_to_slot["ts-1"] = slot.key
        state.sessions.clear_slack_link = sm.clear_slack_link
        state.sessions.clear_slack_link_if = sm.clear_slack_link_if
        state.sessions.get_slack_link = sm.get_slack_link
        state.sessions.aflush = sm.aflush
        state.push_slots_update = MagicMock()
        # The disk fails for as long as the switch is on -- the route's flush
        # raises; the teardown at the end of the test writes normally again.
        disk = {"full": True}
        real_write = SessionMap._write_payload

        def failing_write(self, payload, seq):
            if disk["full"]:
                raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
            return real_write(self, payload, seq)

        monkeypatch.setattr(SessionMap, "_write_payload", failing_write)
        try:
            async with TestClient(TestServer(_make_slack_app(state))) as client:
                resp = await client.post("/api/chat/slots/s1/slack-unlink")
                # The failure surfaces: no `ok`, no was_linked, nothing published.
                assert resp.status == 500
            state.push_slots_update.assert_not_called()
            # The map is clear in memory; the fields and the reverse index follow it.
            assert sm.get_slack_link(f"dashboard:{slot.key}") == (None, None)
            assert slot._slack_linked is False
            assert slot._slack_channel == ""
            assert slot._slack_thread_ts == ""
            assert "ts-1" not in state._slack_to_slot
            # The residue, exactly: the write failed, so the file still holds the
            # thread -- a restart reloads the link, and the user was told nothing
            # to the contrary.
            assert "ts-1" in on_disk.read_text(encoding="utf-8")
        finally:
            disk["full"] = False
            await sm.aclose()

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
            assert data == {"ok": True, "was_linked": True, "relinked": False}
        # Both key variants cleared: "dashboard:s1" (from _history_key_for) and "s1".
        cleared_keys = {c.args[0] for c in state.sessions.clear_slack_link.call_args_list}
        assert cleared_keys == {"dashboard:s1", "s1"}
        # Slot link state reset so the badge flips off and "Send to Slack" returns.
        assert slot._slack_linked is False
        assert slot._slack_channel == ""
        assert slot._slack_thread_ts == ""

    @pytest.mark.asyncio
    async def test_a_stale_row_cannot_unlink_a_relinked_thread(self, tmp_path, monkeypatch):
        """Same guard as mirror-unlink, on the Slack fields the row is drawn from.

        The thread id is the only discriminator a Slack link has: a re-link after
        an unlink (or a Slack-side resume) lands in the SAME owner DM channel on
        a fresh thread. A tab still showing the old thread's row must not tear
        down the replacement: the old row's token is a 409 ``mirror_changed``
        that clears nothing, the current row's token unlinks, and no body keeps
        the unconditional clear.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._slack_linked = True
        slot._slack_channel = "D-owner-dm"
        slot._slack_thread_ts = "ts-new"
        state.sessions.set_slack_link("dashboard:s1", "ts-new", "D-owner-dm")
        state.slack_client = MagicMock()
        state.slack_client.post_message = AsyncMock()
        state.sessions.clear_slack_link = MagicMock(return_value=True)
        state.push_slots_update = MagicMock()
        old_row = ChannelLink("slack", channel_id="D-owner-dm", thread_id="ts-old")
        current_row = ChannelLink("slack", channel_id="D-owner-dm", thread_id="ts-new")
        current_token = binding_token(current_row, state.sessions.slack_link_nonce("dashboard:s1"))
        assert binding_token(old_row) != current_token
        async with TestClient(TestServer(_make_slack_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/slack-unlink",
                json={"channel_type": "slack", "binding": binding_token(old_row)},
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "mirror_changed"
            state.sessions.clear_slack_link.assert_not_called()
            assert slot._slack_linked is True
            resp = await client.post(
                "/api/chat/slots/s1/slack-unlink",
                json={"channel_type": "slack", "binding": current_token},
            )
            assert resp.status == 200
            assert (await resp.json()) == {"ok": True, "was_linked": True, "relinked": False}
        assert slot._slack_linked is False

    @pytest.mark.asyncio
    async def test_a_garbled_body_is_refused_on_the_slack_route_too(self, tmp_path, monkeypatch):
        """The one body reader serves both routes: garbled JSON is 400 here as well.

        Only an EMPTY body reaches the unconditional clear; a body that is present
        but unparseable (or not an object) is refused with ``invalid_body`` and
        the thread stays linked.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._slack_linked = True
        slot._slack_channel = "D-owner-dm"
        slot._slack_thread_ts = "ts-new"
        state.sessions.set_slack_link("dashboard:s1", "ts-new", "D-owner-dm")
        state.slack_client = MagicMock()
        state.slack_client.post_message = AsyncMock()
        state.sessions.clear_slack_link = MagicMock(return_value=True)
        state.push_slots_update = MagicMock()
        headers = {"Content-Type": "application/json"}
        async with TestClient(TestServer(_make_slack_app(state))) as client:
            for garbled in ('{"channel_type": "slack", ', "[]"):
                resp = await client.post(
                    "/api/chat/slots/s1/slack-unlink", data=garbled, headers=headers
                )
                assert resp.status == 400, garbled
                assert (await resp.json())["code"] == "invalid_body"
        state.sessions.clear_slack_link.assert_not_called()
        state.push_slots_update.assert_not_called()
        assert slot._slack_linked is True

    @pytest.mark.asyncio
    async def test_an_undecodable_body_is_refused_on_the_slack_route_too(
        self, tmp_path, monkeypatch
    ):
        """Invalid UTF-8 or an unknown charset is the garbled body here as well: 400, nothing cleared."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._slack_linked = True
        slot._slack_channel = "D-owner-dm"
        slot._slack_thread_ts = "ts-new"
        state.sessions.set_slack_link("dashboard:s1", "ts-new", "D-owner-dm")
        state.sessions.clear_slack_link = MagicMock(return_value=True)
        state.push_slots_update = MagicMock()
        async with TestClient(TestServer(_make_slack_app(state))) as client:
            for data, headers in (
                (b"\xff\xfe{", {"Content-Type": "application/json; charset=utf-8"}),
                (
                    b'{"channel_type": "slack"}',
                    {"Content-Type": "application/json; charset=zzq-no-such"},
                ),
            ):
                resp = await client.post(
                    "/api/chat/slots/s1/slack-unlink", data=data, headers=headers
                )
                assert resp.status == 400, headers
                assert (await resp.json())["code"] == "invalid_body"
        state.sessions.clear_slack_link.assert_not_called()
        state.push_slots_update.assert_not_called()
        assert slot._slack_linked is True

    @pytest.mark.asyncio
    async def test_a_row_from_before_an_unlink_cannot_unlink_the_same_thread_relinked(
        self, tmp_path, monkeypatch
    ):
        """A thread re-linked to the SAME coordinates after an unlink is a new binding.

        Same ABA as the mirror side: the coordinates alone cannot tell the old
        binding from its byte-identical recreation, so the row's token digests
        the link's own persisted nonce -- minted by ``set_slack_link`` on every
        create or rebind, dropped by ``clear_slack_link`` -- and a delayed unlink
        naming the old row is refused instead of tearing down the new link.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._slack_linked = True
        slot._slack_channel = "D-owner-dm"
        slot._slack_thread_ts = "ts-same"
        state.sessions.set_slack_link("dashboard:s1", "ts-same", "D-owner-dm")
        state.slack_client = MagicMock()
        state.slack_client.post_message = AsyncMock()
        state.push_slots_update = MagicMock()
        row = ChannelLink("slack", channel_id="D-owner-dm", thread_id="ts-same")
        old_token = binding_token(row, state.sessions.slack_link_nonce("dashboard:s1"))
        # Another tab: unlink, then re-link the same thread.
        assert state.sessions.clear_slack_link("dashboard:s1") is True
        state.sessions.set_slack_link("dashboard:s1", "ts-same", "D-owner-dm")
        new_token = binding_token(row, state.sessions.slack_link_nonce("dashboard:s1"))
        assert new_token != old_token
        async with TestClient(TestServer(_make_slack_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/slack-unlink",
                json={"channel_type": "slack", "binding": old_token},
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "mirror_changed"
            assert state.sessions.get_slack_link("dashboard:s1") == ("ts-same", "D-owner-dm")
            assert slot._slack_linked is True
            resp = await client.post(
                "/api/chat/slots/s1/slack-unlink",
                json={"channel_type": "slack", "binding": new_token},
            )
            assert resp.status == 200
        assert state.sessions.get_slack_link("dashboard:s1") == (None, None)
        assert slot._slack_linked is False

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
            assert data == {"ok": True, "was_linked": False, "relinked": False}
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
