"""Tests for the channel connect/disconnect control.

Disconnecting a channel stops turn output reaching it while RETAINING the
binding, so a reply there resumes the same session. These tests pin the three
places that promise can break: the stored flag outliving its binding, the send
path not actually honouring it, and the wire not reporting it.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import inspect
import re
import textwrap
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.messaging.link import ChannelLink, binding_token, legacy_dashboard_mirror_key
from kiro_crew.session_map import SessionMap


def _real_map(tmp_path, monkeypatch) -> SessionMap:
    """A SessionMap on disk under *tmp_path*.

    `SessionMap` resolves its own path from `config_dir()`, so the redirect has to
    happen before construction rather than being passed in.
    """
    monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
    return SessionMap()


def _with_real_storage(state, sm: SessionMap):
    """Point a test state's `sessions` at real storage for the link/pause methods.

    The shared helper hands out a bare `MagicMock`, which returns a truthy child
    for every accessor — useful for most handlers, useless here, because the
    behaviour under test IS the stored flag.
    """
    state.sessions.set_slack_link = sm.set_slack_link
    state.sessions.get_slack_link = sm.get_slack_link
    state.sessions.slack_links_generation = sm.slack_links_generation
    state.sessions.clear_slack_link = sm.clear_slack_link
    state.sessions.set_slack_paused = sm.set_slack_paused
    state.sessions.is_slack_paused = sm.is_slack_paused
    state.sessions.set_mirror_link = sm.set_mirror_link
    state.sessions.get_mirror_link = sm.get_mirror_link
    state.sessions.clear_mirror_link = sm.clear_mirror_link
    state.sessions.set_mirror_paused = sm.set_mirror_paused
    state.sessions.is_mirror_paused = sm.is_mirror_paused
    state.sessions.mirror_accepts_inbound = sm.mirror_accepts_inbound
    # The row's `binding` token digests the binding's own nonce, read through
    # these two, so they must see the same storage the links live in.
    state.sessions.mirror_link_nonce = sm.mirror_link_nonce
    state.sessions.slack_link_nonce = sm.slack_link_nonce
    # The compare-and-clear the unlink routes call; a bare MagicMock here would
    # answer truthy to every token and clear nothing.
    state.sessions.clear_mirror_link_if = sm.clear_mirror_link_if
    state.sessions.clear_slack_link_if = sm.clear_slack_link_if
    # The durability point the unlink routes await before publishing a success;
    # bound to the real map so the on-disk pins read what the route landed.
    state.sessions.aflush = sm.aflush
    return sm


def _make_app(state):
    from kiro_crew.dashboard.chat_mirror import (
        api_chat_slot_mirror_link,
        api_chat_slot_mirror_pause,
        api_chat_slot_mirror_unlink,
    )
    from kiro_crew.dashboard.chat_slack import api_chat_slot_slack_link, api_chat_slot_slack_pause

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/slack-link", api_chat_slot_slack_link)
    app.router.add_post("/api/chat/slots/{slot}/slack-pause", api_chat_slot_slack_pause)
    app.router.add_post("/api/chat/slots/{name}/mirror-link", api_chat_slot_mirror_link)
    app.router.add_post("/api/chat/slots/{slot}/mirror-pause", api_chat_slot_mirror_pause)
    app.router.add_post("/api/chat/slots/{slot}/mirror-unlink", api_chat_slot_mirror_unlink)
    return app


class TestPauseNeverOutlivesItsBinding:
    """The flag is stored beside the binding and dies with it.

    A marker that survives its binding re-mutes the NEXT connection, which the
    user never disconnected — the failure is silent and looks like a bug in
    delivery rather than in bookkeeping.
    """

    def test_slack_pause_round_trip(self, tmp_path, monkeypatch):
        sm = _real_map(tmp_path, monkeypatch)
        sm.set_slack_link("dashboard:s1", "ts-1", "C-1")
        assert sm.is_slack_paused("dashboard:s1") is False
        assert sm.set_slack_paused("dashboard:s1", True) is False
        assert sm.is_slack_paused("dashboard:s1") is True
        # Idempotent, and it reports the PRIOR state so a caller can tell a real
        # transition from a repeat (only a transition posts the courtesy note).
        assert sm.set_slack_paused("dashboard:s1", True) is True

    def test_unlinking_drops_the_slack_pause(self, tmp_path, monkeypatch):
        sm = _real_map(tmp_path, monkeypatch)
        sm.set_slack_link("dashboard:s1", "ts-1", "C-1")
        sm.set_slack_paused("dashboard:s1", True)
        sm.clear_slack_link("dashboard:s1")

        # Asserted on STORAGE, not through `is_slack_paused`: that accessor already
        # answers False for an unlinked session, so reading through it would pass
        # whether or not the unlink cleared anything. A marker left on disk is
        # stale state hidden only by that accessor's binding check.
        assert "slack_paused" not in sm._data.get("dashboard:s1", {})

        # And the observable consequence: re-linking comes back CONNECTED.
        sm.set_slack_link("dashboard:s1", "ts-2", "C-2")
        assert sm.is_slack_paused("dashboard:s1") is False

    def test_a_same_coordinate_write_keeps_the_pause(self, tmp_path, monkeypatch):
        """A same-coordinate write is the SAME binding, so the mute survives it.

        This is not a rare path: the Slack inbound handler re-writes the same ts
        and channel on every turn as its thread registry, so clearing the mute on
        identical coordinates let ONE inbound message — or a cold start's
        ``set_channel`` — silently un-disconnect a thread, after which dashboard
        turns resumed delivering to it. Connecting does not depend on this: the
        row lifts a mute through ``set_slack_paused``.

        A REBIND still drops it, which the next test pins.
        """
        sm = _real_map(tmp_path, monkeypatch)
        sm.set_slack_link("dashboard:s1", "ts-1", "C-1")
        sm.set_slack_paused("dashboard:s1", True)
        sm.set_slack_link("dashboard:s1", "ts-1", "C-1")
        assert sm.is_slack_paused("dashboard:s1") is True

    def test_rebinding_to_a_different_thread_drops_the_pause(self, tmp_path, monkeypatch):
        """The mute belonged to the binding being replaced, so it goes with it.

        Carrying it forward would arrive on a thread the user never muted.
        """
        sm = _real_map(tmp_path, monkeypatch)
        sm.set_slack_link("dashboard:s1", "ts-1", "C-1")
        sm.set_slack_paused("dashboard:s1", True)
        sm.set_slack_link("dashboard:s1", "ts-2", "C-1")
        assert sm.is_slack_paused("dashboard:s1") is False

    def test_a_flag_with_no_link_reads_as_connected(self, tmp_path, monkeypatch):
        """A stale marker must not make an unlinked session look merely quiet."""
        sm = _real_map(tmp_path, monkeypatch)
        sm.set_slack_link("dashboard:s1", "ts-1", "C-1")
        sm.set_slack_paused("dashboard:s1", True)
        # Reach past the accessors to leave the marker with no binding.
        entry = sm._data["dashboard:s1"]
        entry.pop("slack_thread_ts", None)
        entry.pop("slack_channel_id", None)
        assert sm.is_slack_paused("dashboard:s1") is False

    def test_mirror_pause_round_trip_and_dies_with_the_binding(self, tmp_path, monkeypatch):
        sm = _real_map(tmp_path, monkeypatch)
        sm.set_mirror_link("dashboard:s1", ChannelLink("discord", "chan-1", None))
        assert sm.set_mirror_paused("dashboard:s1", False) is False
        assert sm.set_mirror_paused("dashboard:s1", True) is False
        assert sm.is_mirror_paused("dashboard:s1") is True
        sm.clear_mirror_link("dashboard:s1")
        sm.set_mirror_link("dashboard:s1", ChannelLink("discord", "chan-1", None))
        assert sm.is_mirror_paused("dashboard:s1") is False

    def test_a_channel_born_session_can_be_disconnected(self, tmp_path, monkeypatch):
        """Its conversation is permanent, so there is no binding to require.

        Addressed with ``origin=True`` because a channel-born session's home
        conversation is a DIFFERENT delivery from an explicit mirror, and the two
        carry separate flags — see the independence test below for why.
        """
        sm = _real_map(tmp_path, monkeypatch)
        sm.set("discord:chan-9", "sid-9")
        assert sm.set_mirror_paused("discord:chan-9", True, origin=True) is False
        assert sm.is_mirror_paused("discord:chan-9", origin=True) is True

    def test_an_origin_pause_survives_a_mirror_landing_on_the_canonical_row(
        self, tmp_path, monkeypatch
    ):
        """The origin flag is the SESSION's, so a mirror binding must not relocate it.

        ``_mirror_key`` resolves to the legacy ``dashboard:`` spelling while that
        row holds the only binding, and to the canonical row once one is written
        there. Keying the ORIGIN flag through it therefore stranded the pause: the
        lookup moved rows, the flag stayed behind on the old one, and a
        conversation the user had muted silently resumed delivering.
        """
        sm = _real_map(tmp_path, monkeypatch)
        sm.set("discord:chan-9", "sid-9")
        # A binding written before keys were unified sits on the sanitized spelling.
        sm._data[legacy_dashboard_mirror_key("discord:chan-9")] = {
            "mirror": ChannelLink("telegram", channel_id="tg-1").to_dict()
        }

        sm.set_mirror_paused("discord:chan-9", True, origin=True)
        assert sm.is_mirror_paused("discord:chan-9", origin=True) is True

        # A mirror now lands on the CANONICAL row, which moves _mirror_key.
        sm.set_mirror_link("discord:chan-9", ChannelLink("telegram", channel_id="tg-2"))
        assert (
            sm.is_mirror_paused("discord:chan-9", origin=True) is True
        ), "origin pause was stranded on the legacy row"

    def test_origin_and_mirror_mute_independently(self, tmp_path, monkeypatch):
        """One session, two non-Slack deliveries, two flags.

        A session born in Discord that ALSO mirrors to Telegram renders two rows.
        While both read one scalar, disconnecting either silently disconnected the
        other — the row the user did not touch went quiet with it.
        """
        sm = _real_map(tmp_path, monkeypatch)
        sm.set("discord:chan-9", "sid-9")
        sm.set_mirror_link("discord:chan-9", ChannelLink("telegram", channel_id="tg-1"))

        # Disconnect the born-in conversation only.
        sm.set_mirror_paused("discord:chan-9", True, origin=True)
        assert sm.is_mirror_paused("discord:chan-9", origin=True) is True
        assert sm.is_mirror_paused("discord:chan-9") is False, "mirror followed origin"

        # And the explicit mirror only, independently.
        sm.set_mirror_paused("discord:chan-9", True)
        sm.set_mirror_paused("discord:chan-9", False, origin=True)
        assert sm.is_mirror_paused("discord:chan-9") is True
        assert sm.is_mirror_paused("discord:chan-9", origin=True) is False, "origin followed mirror"


class TestTheSendPathHonoursIt:
    def test_predicates_fail_open_on_an_unstubbed_session_manager(self):
        """`sessions` is a bare MagicMock across much of the suite.

        A MagicMock returns a truthy child for any attribute, so truthiness here
        would silence every linked channel in the test suite. Failing open leaves
        a disconnected channel noisy at worst; failing closed makes a live one
        silently dead.
        """
        from kiro_crew.dashboard.chat_utils import mirror_is_paused, slack_mirror_is_paused

        state = MagicMock()  # is_slack_paused() returns a truthy MagicMock
        assert slack_mirror_is_paused(state, "dashboard:s1") is False
        assert mirror_is_paused(state, "dashboard:s1") is False

    def test_predicates_report_a_real_disconnect(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_utils import mirror_is_paused, slack_mirror_is_paused

        sm = _real_map(tmp_path, monkeypatch)
        sm.set_slack_link("dashboard:s1", "ts-1", "C-1")
        sm.set_mirror_link("dashboard:s2", ChannelLink("discord", "chan-1", None))
        sm.set_slack_paused("dashboard:s1", True)
        sm.set_mirror_paused("dashboard:s2", True)

        state = MagicMock()
        state.sessions = sm
        assert slack_mirror_is_paused(state, "dashboard:s1") is True
        assert mirror_is_paused(state, "dashboard:s2") is True

    def test_the_turn_path_asks_before_resolving_its_slack_target(self):
        """Structural: the gate must sit on the chokepoint, not on each sender.

        Leaving `_mirror_thread`/`_mirror_chan` empty is what silences the echo,
        the tool stream, the reply and the stream teardown together. Asserted on
        source order because the alternative is four independent gates that drift.
        """
        import inspect

        from kiro_crew.dashboard import chat_runner

        src = inspect.getsource(chat_runner)
        gate = src.index("and not slack_mirror_is_paused(state, session_key)")
        resolve = src.index("_mirror_thread, _mirror_chan = state.sessions.get_slack_link")
        assert gate < resolve, "the pause gate must precede link resolution"

    def test_both_cross_surface_legs_are_gated(self):
        """The user echo and the assistant reply both stop, or the remote
        conversation reads as a question that was never answered."""
        import inspect

        from kiro_crew.dashboard import chat_runner

        for fn in (
            chat_runner._deliver_cross_surface_reply,
            chat_runner._deliver_cross_surface_user_message,
        ):
            assert "mirror_is_paused(state, session_key)" in inspect.getsource(fn), (
                f"{fn.__name__} does not honour a disconnect"
            )


class TestTheWireReportsIt:
    def test_every_row_carries_paused(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        sm = _real_map(tmp_path, monkeypatch)
        _with_real_storage(state, sm)
        slot = state.get_or_create_slot("s1")
        sm.set_mirror_link(f"dashboard:{slot.key}", ChannelLink("discord", "chan-1", None))

        links, _linked, _chan, _ts = state._slot_links(slot)
        assert links, "expected a projected row for the bound channel"
        for row in links:
            assert "paused" in row, f"row for {row['channel']} omits paused"
        assert all(row["paused"] is False for row in links)

        sm.set_mirror_paused(f"dashboard:{slot.key}", True)
        links, _linked, _chan, _ts = state._slot_links(slot)
        assert [row["paused"] for row in links] == [True]

    def test_every_row_carries_the_binding_the_unlink_compares(self, tmp_path, monkeypatch):
        """The row's `binding` IS the identity the map's compare-and-clear recomputes.

        The redacted `target` drops the thread and the id's head, so it cannot
        tell a Slack thread from its same-channel replacement; the token is a
        digest of the whole binding -- its persisted nonce included -- and can,
        and neither the raw id nor the nonce is in it. The nonce is what tells a
        binding from its byte-identical recreation: an identical rewrite (the
        inbound paths re-write the same coordinates every turn) keeps the token,
        an unlink followed by a reconnect to the same target changes it.
        """

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        sm = _real_map(tmp_path, monkeypatch)
        _with_real_storage(state, sm)
        slot = state.get_or_create_slot("s1")
        key = f"dashboard:{slot.key}"
        mirror = ChannelLink("discord", "discord:dm-chan-9", None)
        sm.set_mirror_link(key, mirror)
        nonce = sm.mirror_link_nonce(key)
        assert nonce

        links, _linked, _chan, _ts = state._slot_links(slot)
        assert [row["channel"] for row in links] == ["discord"]
        row = links[0]
        assert row["target"] == "…chan-9"
        # The namespaced id and its bare spelling mint one token: the row is
        # drawn from the normalized id, the map holds the namespaced one.
        assert row["binding"] == binding_token(mirror, nonce)
        assert row["binding"] == binding_token(ChannelLink("discord", "dm-chan-9", None), nonce)
        assert row["binding"] != binding_token(ChannelLink("discord", "dm-chan-9", None))
        assert "dm-chan-9" not in row["binding"] and nonce not in row["binding"]

        # Same coordinates rewritten: the same binding, the same token.
        sm.set_mirror_link(key, mirror)
        assert state._slot_links(slot)[0][0]["binding"] == row["binding"]
        # Unlinked and reconnected to the very same target: a new binding, and
        # a token the old row never carried.
        assert sm.clear_mirror_link(key) is True
        sm.set_mirror_link(key, mirror)
        recreated = state._slot_links(slot)[0][0]
        assert recreated["target"] == row["target"]
        assert recreated["binding"] != row["binding"]
        assert recreated["binding"] == binding_token(mirror, sm.mirror_link_nonce(key))

        old_thread = ChannelLink("slack", "D-owner-dm", "ts-old")
        new_thread = ChannelLink("slack", "D-owner-dm", "ts-new")
        assert binding_token(old_thread) != binding_token(new_thread)
        sm.set_slack_link(key, "ts-new", "D-owner-dm")
        slack_nonce = sm.slack_link_nonce(key)
        assert slack_nonce and slack_nonce != sm.mirror_link_nonce(key)
        links, linked, _chan, ts = state._slot_links(slot)
        assert linked is True and ts == "ts-new"
        slack_row = next(row for row in links if row["channel"] == "slack")
        assert slack_row["binding"] == binding_token(new_thread, slack_nonce)

    def test_every_row_says_whether_it_drives_this_session(self, tmp_path, monkeypatch):
        """`drives_session` is the server's statement of inbound routing, per row.

        The menu's paused and Unlink sub-lines name what a sever destroys, and
        that differs between a binding whose conversation drives this session and
        one that only receives replies. Inferred client-side -- `both` on the
        wire, or the channel being Slack -- it is wrong for a paused Slack row.
        The projection owns the fact: a one-way mirror does not
        drive, a resume binding does, and a Slack thread does although its
        direction reads `out` (Slack routes inbound through its own thread
        index, not the mirror's marker).
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        sm = _real_map(tmp_path, monkeypatch)
        _with_real_storage(state, sm)
        slot = state.get_or_create_slot("s1")
        key = f"dashboard:{slot.key}"
        mirror = ChannelLink("discord", "dm-chan-9", None)

        sm.set_mirror_link(key, mirror)
        (row,) = state._slot_links(slot)[0]
        assert (row["direction"], row["drives_session"]) == ("out", False)

        sm.set_mirror_link(key, mirror, accepts_inbound=True)
        (row,) = state._slot_links(slot)[0]
        assert (row["direction"], row["drives_session"]) == ("both", True)

        sm.set_slack_link(key, "ts-1", "D-owner-dm")
        rows = {row["channel"]: row for row in state._slot_links(slot)[0]}
        assert (rows["slack"]["direction"], rows["slack"]["drives_session"]) == ("out", True)
        assert rows["discord"]["drives_session"] is True
        for row in rows.values():
            assert isinstance(row["drives_session"], bool)


class TestEndpoints:
    @pytest.mark.asyncio
    async def test_slack_pause_refuses_when_nothing_is_connected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        _with_real_storage(state, _real_map(tmp_path, monkeypatch))
        state.get_or_create_slot("s1")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/slack-pause")
            assert resp.status == 409
            assert (await resp.json())["code"] == "slack_not_linked"

    @pytest.mark.asyncio
    async def test_slack_pause_sets_and_clears_delivery(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        sm = _with_real_storage(state, _real_map(tmp_path, monkeypatch))
        slot = state.get_or_create_slot("s1")
        key = f"dashboard:{slot.key}"
        sm.set_slack_link(key, "ts-1", "C-1")
        state.slack_client = MagicMock()
        state.slack_client.post_message = AsyncMock(return_value="ts-note")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/slack-pause", json={"paused": True})
            assert resp.status == 200
            assert (await resp.json())["was_paused"] is False
            assert sm.is_slack_paused(key) is True

            # Idempotent, and the courtesy note fires only on the transition.
            resp = await client.post("/api/chat/slots/s1/slack-pause", json={"paused": True})
            assert (await resp.json())["was_paused"] is True
            assert state.slack_client.post_message.await_count == 1

            resp = await client.post("/api/chat/slots/s1/slack-pause", json={"paused": False})
            assert resp.status == 200
            assert sm.is_slack_paused(key) is False

    @pytest.mark.asyncio
    async def test_only_an_explicit_false_connects(self, tmp_path, monkeypatch):
        """Ambiguous input fails toward the quiet side.

        Disconnecting only ever reduces what leaves the process, so a malformed
        or absent flag must not be the thing that starts delivering into a
        channel. `null` is the interesting case: truthiness would read it as
        connect, which is the unsafe direction.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        sm = _with_real_storage(state, _real_map(tmp_path, monkeypatch))
        slot = state.get_or_create_slot("s1")
        key = f"dashboard:{slot.key}"
        sm.set_slack_link(key, "ts-1", "C-1")
        state.slack_client = None

        async with TestClient(TestServer(_make_app(state))) as client:
            # A real boolean false is the ONLY thing that connects.
            await client.post("/api/chat/slots/s1/slack-pause", json={"paused": False})
            assert sm.is_slack_paused(key) is False

            # null does not connect — it disconnects.
            await client.post("/api/chat/slots/s1/slack-pause", json={"paused": None})
            assert sm.is_slack_paused(key) is True

            await client.post("/api/chat/slots/s1/slack-pause", json={"paused": False})
            assert sm.is_slack_paused(key) is False

            # An absent key defaults to disconnect.
            await client.post("/api/chat/slots/s1/slack-pause", json={})
            assert sm.is_slack_paused(key) is True

    @pytest.mark.asyncio
    async def test_disconnect_survives_a_denied_courtesy_note(self, tmp_path, monkeypatch):
        """A denial silences the NOTE, never the disconnect.

        Refusing to disconnect because the channel is denied would strand the
        user connected to a channel they are trying to leave — a gate that makes
        the situation worse is not fail-closed, it is broken.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_slack.vet_and_audit",
            MagicMock(side_effect=RuntimeError("policy blew up")),
        )
        state = _make_state(tmp_path)
        sm = _with_real_storage(state, _real_map(tmp_path, monkeypatch))
        slot = state.get_or_create_slot("s1")
        key = f"dashboard:{slot.key}"
        sm.set_slack_link(key, "ts-1", "C-1")
        state.slack_client = MagicMock()
        state.slack_client.post_message = AsyncMock(return_value="ts-note")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/slack-pause", json={"paused": True})
            assert resp.status == 200
        assert sm.is_slack_paused(key) is True
        state.slack_client.post_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_mirror_pause_refuses_when_nothing_is_connected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        _with_real_storage(state, _real_map(tmp_path, monkeypatch))
        state.get_or_create_slot("s1")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/mirror-pause")
            assert resp.status == 409
            assert (await resp.json())["code"] == "mirror_not_linked"

    @pytest.mark.asyncio
    async def test_mirror_pause_sets_delivery(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        sm = _with_real_storage(state, _real_map(tmp_path, monkeypatch))
        slot = state.get_or_create_slot("s1")
        key = f"dashboard:{slot.key}"
        sm.set_mirror_link(key, ChannelLink("discord", "chan-1", None))

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/mirror-pause", json={"paused": True})
            assert resp.status == 200
            assert sm.is_mirror_paused(key) is True

    @pytest.mark.asyncio
    async def test_an_origin_disconnect_never_notifies_the_mirror(self, tmp_path, monkeypatch):
        """Two deliveries, two conversations — so one's courtesy note is the other's lie.

        A Discord-born session that ALSO mirrors to Telegram holds both at once.
        ``_resolve_mirror_target`` only ever resolves the EXPLICIT mirror, so
        sending the note on an origin disconnect told Telegram it had been
        disconnected while it was still connected and still receiving turns.

        The second half is what stops this passing vacuously: deleting the note
        block outright would satisfy the first assertion and fail the second.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        sm = _with_real_storage(state, _real_map(tmp_path, monkeypatch))
        slot = state.get_or_create_slot("s1")
        slot.linked_session_key = "discord:chan-9"
        sm.set("discord:chan-9", "sid-9")
        sm.set_mirror_link("discord:chan-9", ChannelLink("telegram", channel_id="tg-1"))

        # Returning None keeps the note itself out of scope: reaching the resolver
        # at all is the defect, so the call is the assertion.
        resolve = MagicMock(return_value=None)
        monkeypatch.setattr("kiro_crew.dashboard.chat_mirror._resolve_mirror_target", resolve)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-pause", json={"paused": True, "origin": True}
            )
            assert resp.status == 200
            assert sm.is_mirror_paused("discord:chan-9", origin=True) is True
            resolve.assert_not_called()

            resp = await client.post("/api/chat/slots/s1/mirror-pause", json={"paused": True})
            assert resp.status == 200
            assert resolve.call_count == 1, "the mirror's own disconnect must still notify"

    @pytest.mark.asyncio
    async def test_one_unlink_clears_the_superseded_legacy_row_too(self, tmp_path, monkeypatch):
        """One request, both rows: the binding an Unlink superseded must not resurface.

        A channel session that rebound from the dashboard can hold TWO mirror
        rows -- the canonical binding every read prefers and the pre-unification
        ``dashboard:`` row it superseded. Clearing the winner alone hands
        ``_mirror_key`` back to the older row: the request answers
        ``was_linked: true``, the menu drops the row, and the next slots frame
        redraws it pointing at the OLD target -- a mirror the user just removed,
        reported as removed, still delivering. The endpoint issues one clear and
        the map takes both rows in it, so the projection that follows is empty.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        sm = _with_real_storage(state, _real_map(tmp_path, monkeypatch))
        state.push_slots_update = MagicMock()
        slot = state.get_or_create_slot("s1")
        slot.linked_session_key = "discord:chan-9"
        sm.set("discord:chan-9", "sid-9")
        sm._data[legacy_dashboard_mirror_key("discord:chan-9")] = {
            "mirror": ChannelLink("telegram", channel_id="tg-old").to_dict()
        }
        sm.set_mirror_link("discord:chan-9", ChannelLink("telegram", channel_id="tg-new"))

        def mirror_rows():
            return [row for row in state._slot_links(slot)[0] if row["direction"] != "origin"]

        (row,) = mirror_rows()
        assert row["channel"] == "telegram"
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-unlink",
                json={"channel_type": row["channel"], "binding": row["binding"]},
            )
            assert resp.status == 200
            assert (await resp.json())["was_linked"] is True
        assert sm.get_mirror_link("discord:chan-9") is None
        assert sm._data[legacy_dashboard_mirror_key("discord:chan-9")].get("mirror") is None
        assert mirror_rows() == [], "the superseded legacy binding came back as the live row"

    @pytest.mark.asyncio
    async def test_a_slack_row_unlinked_here_gets_the_slack_teardown(self, tmp_path, monkeypatch):
        """The menu posts EVERY row's Unlink to this endpoint; the server routes it.

        Which store a binding lives in is the server's fact -- ``mirror-link``
        refuses Slack on channel type, so a ``slack`` row can only be the slot's
        thread -- and a client restating it as ``channel === 'slack'`` carries a
        transport assumption the server never asks it to make (the same
        inference reads a paused Slack row as one-way when ``driven`` makes it).
        Here a
        body naming the Slack thread is handed to ``slack-unlink``'s handler: both
        key spellings and the slot's own fields go, the thread's reverse index is
        dropped, the projection stops reporting the thread, and the Discord
        mirror standing beside it is untouched -- so the request did not fall
        through to the mirror clear.
        """

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        sm = _with_real_storage(state, _real_map(tmp_path, monkeypatch))
        state.push_slots_update = MagicMock()
        slot = state.get_or_create_slot("s1")
        key = f"dashboard:{slot.key}"
        sm.set_mirror_link(key, ChannelLink("discord", "dm-chan-9", None))
        sm.set_slack_link(key, "ts-1", "D-owner-dm")
        slot._slack_linked = True
        slot._slack_channel = "D-owner-dm"
        slot._slack_thread_ts = "ts-1"
        state._slack_to_slot["ts-1"] = slot.key

        rows = {row["channel"]: row for row in state._slot_links(slot)[0]}
        stale = binding_token(ChannelLink("slack", "D-owner-dm", "ts-old"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-unlink",
                json={"channel_type": "slack", "binding": stale},
            )
            assert resp.status == 409, "the stale guard must ride along to the Slack teardown"
            assert (await resp.json())["code"] == "mirror_changed"
            assert sm.get_slack_link(key) == ("ts-1", "D-owner-dm")
            assert slot._slack_linked is True

            resp = await client.post(
                "/api/chat/slots/s1/mirror-unlink",
                json={"channel_type": "slack", "binding": rows["slack"]["binding"]},
            )
            assert resp.status == 200
            assert (await resp.json()) == {"ok": True, "was_linked": True, "relinked": False}
        assert sm.get_slack_link(key) == (None, None)
        assert (slot._slack_linked, slot._slack_channel, slot._slack_thread_ts) == (False, "", "")
        assert "ts-1" not in state._slack_to_slot
        links, slack_linked, _chan, _ts = state._slot_links(slot)
        assert slack_linked is False
        assert [row["channel"] for row in links] == ["discord"], "the mirror beside it must survive"
        assert sm.get_mirror_link(key) == ChannelLink("discord", "dm-chan-9", None)


@contextlib.contextmanager
def _release_a_rebinder_mid_compare(
    sm: SessionMap, state, accessor: str, rebind, *, stall_after_release: float = 0.0
):
    """Wrap the nonce read the compare depends on so a rebinding thread is released INSIDE it.

    The wrapper returns what the real accessor returns, then lets *rebind* run
    on another thread and waits up to half a second for it to LAND. Under the
    map lock the rebind cannot land while the compare-and-clear holds it, so
    ``landed_inside`` stays False and the rebind completes only after the clear;
    with a route-level read followed by a separate clear the rebind lands inside
    the window and the stale unlink then clears the binding it never named.

    A context manager so the thread cannot outlive the test: leaving the block
    cancels and joins it. The rebinder writes to the map ONLY when the compare
    released it -- a wait that times out, or the release that cleanup sends,
    means the compare never ran, and the thread exits without a write, so no
    ``session_map.json`` flush can land after the test's ``tmp_path`` is gone.

    When the rebind lands after the clear is up to the scheduler: once the
    compare-and-clear releases the lock, the route and the rebinder race for
    it. ``seen["landed"]`` is the event a test waits on when it asserts what
    the route observes after the rebind; ``stall_after_release`` holds the
    rebinder off for that long after its release (a descheduled thread on a
    loaded runner) so a test can pin that it does not lean on scheduling order.
    """
    original = getattr(sm, accessor)
    release = threading.Event()
    cancelled = threading.Event()
    landed = threading.Event()
    seen: dict = {"landed_inside": None, "landed": landed}

    def rebinder() -> None:
        if not release.wait(5) or cancelled.is_set():
            return
        if stall_after_release and cancelled.wait(stall_after_release):
            return
        rebind()
        landed.set()

    def wrapper(key: str) -> str:
        value = original(key)
        release.set()
        seen["landed_inside"] = landed.wait(0.5)
        return value

    setattr(sm, accessor, wrapper)
    setattr(state.sessions, accessor, wrapper)
    thread = threading.Thread(target=rebinder, daemon=True)
    thread.start()
    seen["thread"] = thread
    try:
        yield seen
    finally:
        cancelled.set()
        release.set()
        thread.join(5)


class TestPersistBeforePublish:
    """A link, pause or unlink lands on disk before anything reports it done.

    The map's writer is debounced. Without a durability point in the route, the
    slots push and the ``{ok, ...}`` answer publish a state that a gateway exit
    before the deferred write undoes: an unlinked binding reloads on restart, a
    muted channel starts delivering again, a linked thread carries a transcript
    no session owns. Every route that publishes a state the user just acted on
    -- mirror and Slack, link, pause and unlink -- awaits ``aflush`` between
    the map write and the first publish. Proven with the debounce held off for a
    minute, so the only way the file can carry the new state when the route
    publishes is the route's own flush.
    """

    @staticmethod
    def _on_disk(tmp_path) -> str:
        # A map never written has no file yet; read that as empty so a pin on a
        # first write asserts the write, not the file's existence.
        path = tmp_path / "session_map.json"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    @pytest.mark.asyncio
    async def test_slack_pause_lands_on_disk_before_it_publishes(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.session_map._FLUSH_DEBOUNCE_SECS", 60.0)
        state = _make_state(tmp_path)
        sm = _with_real_storage(state, _real_map(tmp_path, monkeypatch))
        slot = state.get_or_create_slot("s1")
        sm.set_slack_link(f"dashboard:{slot.key}", "ts-1", "C-1")
        sm.flush()
        assert "slack_paused" not in self._on_disk(tmp_path), "precondition: not paused on disk"
        seen: dict = {}
        state.push_slots_update = MagicMock(
            side_effect=lambda: seen.setdefault("on_disk_at_publish", self._on_disk(tmp_path))
        )
        try:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/s1/slack-pause", json={"paused": True})
                assert resp.status == 200
                assert (await resp.json())["paused"] is True
                assert "slack_paused" in self._on_disk(tmp_path), (
                    "the answer was published while the pause was not yet on disk"
                )
            assert "slack_paused" in seen["on_disk_at_publish"], (
                "the slots push went out while the pause was not yet on disk"
            )
        finally:
            await sm.aclose()

    @pytest.mark.asyncio
    async def test_mirror_pause_lands_on_disk_before_it_publishes(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.session_map._FLUSH_DEBOUNCE_SECS", 60.0)
        state = _make_state(tmp_path)
        sm = _with_real_storage(state, _real_map(tmp_path, monkeypatch))
        slot = state.get_or_create_slot("s1")
        sm.set_mirror_link(f"dashboard:{slot.key}", ChannelLink("discord", "dm-chan-9", None))
        sm.flush()
        assert "mirror_paused" not in self._on_disk(tmp_path), "precondition: not paused on disk"
        seen: dict = {}
        state.push_slots_update = MagicMock(
            side_effect=lambda: seen.setdefault("on_disk_at_publish", self._on_disk(tmp_path))
        )
        try:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/s1/mirror-pause", json={"paused": True})
                assert resp.status == 200
                assert (await resp.json())["paused"] is True
                assert "mirror_paused" in self._on_disk(tmp_path), (
                    "the answer was published while the pause was not yet on disk"
                )
            assert "mirror_paused" in seen["on_disk_at_publish"], (
                "the slots push went out while the pause was not yet on disk"
            )
        finally:
            await sm.aclose()

    @pytest.mark.asyncio
    async def test_slack_link_lands_on_disk_before_it_publishes(self, tmp_path, monkeypatch):
        """The route's own slots push and its answer wait for the flush.

        ``state.link_slack`` redraws the slot row from inside the map write, so
        the FIRST push may still read the old file; a redraw the next push
        corrects is not a report the user acts on. The route's own push (the
        last one) and the ``{ok, thread_ts}`` answer are, and both wait.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.session_map._FLUSH_DEBOUNCE_SECS", 60.0)
        state = _make_state(tmp_path)
        sm = _with_real_storage(state, _real_map(tmp_path, monkeypatch))
        state.get_or_create_slot("s1")
        state.slack_client = MagicMock()
        state.slack_client.open_dm = AsyncMock(return_value="C123")
        state.slack_client.post_message = AsyncMock(return_value="newts")
        state.owner_id = "U123"
        sm.flush()
        assert "1700.42" not in self._on_disk(tmp_path), "precondition: the thread is not on disk"
        pushes: list[str] = []
        state.push_slots_update = MagicMock(
            side_effect=lambda: pushes.append(self._on_disk(tmp_path))
        )
        try:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/s1/slack-link",
                    json={"channel": "C999", "thread_ts": "1700.42"},
                )
                assert resp.status == 200
                assert (await resp.json())["thread_ts"] == "1700.42"
                assert "1700.42" in self._on_disk(tmp_path), (
                    "the answer was published while the thread was not yet on disk"
                )
            assert pushes, "the route publishes a slots push"
            assert "1700.42" in pushes[-1], (
                "the route's slots push went out while the thread was not yet on disk"
            )
        finally:
            await sm.aclose()

    @pytest.mark.asyncio
    async def test_mirror_link_lands_on_disk_before_it_publishes(self, tmp_path, monkeypatch):
        """The claim runs off the loop, where a batch writes inline; the route
        still awaits the durability point, so the contract reads the same at all
        four publish sites and does not hinge on which thread the claim ran on."""
        from types import SimpleNamespace

        from kiro_crew.messaging.transport import ConfiguredChannelTarget

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.session_map._FLUSH_DEBOUNCE_SECS", 60.0)
        state = _make_state(tmp_path)
        sm = _with_real_storage(state, _real_map(tmp_path, monkeypatch))
        state.sessions.batched_save = sm.batched_save
        state.get_or_create_slot("s1")
        transport = SimpleNamespace(
            channel_type="telegram",
            capabilities=SimpleNamespace(
                supports_proactive_send=True,
                max_message_chars=4096,
                supports_session_resume=False,
            ),
            send_message=AsyncMock(return_value="mid-1"),
            configured_targets=MagicMock(
                return_value=[ConfiguredChannelTarget("user:123", "Telegram DM · 123")]
            ),
            resolve_configured_target=AsyncMock(return_value=("123", None)),
            may_send_to=lambda conversation_id, thread_id=None, principal="": True,
        )
        state.register_channel_transport(transport)
        sm.flush()
        seen: dict = {}
        state.push_slots_update = MagicMock(
            side_effect=lambda: seen.setdefault("on_disk_at_publish", self._on_disk(tmp_path))
        )
        try:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/s1/mirror-link",
                    json={"channel_type": "telegram", "target_id": "user:123"},
                )
                assert resp.status == 200, await resp.text()
                assert '"123"' in self._on_disk(tmp_path), (
                    "the answer was published while the binding was not yet on disk"
                )
            assert '"123"' in seen["on_disk_at_publish"], (
                "the slots push went out while the binding was not yet on disk"
            )
        finally:
            await sm.aclose()

    @pytest.mark.asyncio
    async def test_mirror_unlink_lands_on_disk_before_it_publishes(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.session_map._FLUSH_DEBOUNCE_SECS", 60.0)
        state = _make_state(tmp_path)
        sm = _with_real_storage(state, _real_map(tmp_path, monkeypatch))
        slot = state.get_or_create_slot("s1")
        key = f"dashboard:{slot.key}"
        sm.set_mirror_link(key, ChannelLink("discord", "dm-chan-9", None))
        sm.flush()
        assert "dm-chan-9" in self._on_disk(tmp_path), "precondition: the binding is on disk"
        (row,) = state._slot_links(slot)[0]
        seen: dict = {}

        def publish() -> None:
            # What is on disk at the moment the slots push goes out.
            seen["on_disk_at_publish"] = self._on_disk(tmp_path)

        state.push_slots_update = MagicMock(side_effect=publish)
        try:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/s1/mirror-unlink",
                    json={"channel_type": row["channel"], "binding": row["binding"]},
                )
                assert resp.status == 200
                assert (await resp.json()) == {"ok": True, "was_linked": True}
                assert "dm-chan-9" not in self._on_disk(tmp_path), (
                    "the answer was published while the binding was still on disk"
                )
            assert "dm-chan-9" not in seen["on_disk_at_publish"], (
                "the slots push went out while the binding was still on disk"
            )
        finally:
            await sm.aclose()

    @pytest.mark.asyncio
    async def test_a_bodiless_mirror_unlink_lands_on_disk_too(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.session_map._FLUSH_DEBOUNCE_SECS", 60.0)
        state = _make_state(tmp_path)
        sm = _with_real_storage(state, _real_map(tmp_path, monkeypatch))
        state.push_slots_update = MagicMock()
        slot = state.get_or_create_slot("s1")
        sm.set_mirror_link(f"dashboard:{slot.key}", ChannelLink("discord", "dm-chan-9", None))
        sm.flush()
        try:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/s1/mirror-unlink")
                assert resp.status == 200
                assert "dm-chan-9" not in self._on_disk(tmp_path)
        finally:
            await sm.aclose()

    @pytest.mark.asyncio
    async def test_slack_unlink_lands_on_disk_before_it_publishes(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.session_map._FLUSH_DEBOUNCE_SECS", 60.0)
        state = _make_state(tmp_path)
        sm = _with_real_storage(state, _real_map(tmp_path, monkeypatch))
        slot = state.get_or_create_slot("s1")
        key = f"dashboard:{slot.key}"
        sm.set_slack_link(key, "ts-1", "D-owner-dm")
        slot._slack_linked = True
        slot._slack_channel = "D-owner-dm"
        slot._slack_thread_ts = "ts-1"
        sm.flush()
        assert "ts-1" in self._on_disk(tmp_path), "precondition: the thread is on disk"
        rows = {row["channel"]: row for row in state._slot_links(slot)[0]}
        seen: dict = {}
        state.push_slots_update = MagicMock(
            side_effect=lambda: seen.setdefault("on_disk_at_publish", self._on_disk(tmp_path))
        )
        try:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/s1/mirror-unlink",
                    json={"channel_type": "slack", "binding": rows["slack"]["binding"]},
                )
                assert resp.status == 200
                assert (await resp.json()) == {"ok": True, "was_linked": True, "relinked": False}
                assert "ts-1" not in self._on_disk(tmp_path), (
                    "the answer was published while the thread was still on disk"
                )
            assert "ts-1" not in seen["on_disk_at_publish"], (
                "the slots push went out while the thread was still on disk"
            )
        finally:
            await sm.aclose()


class TestCompareAndClearIsOneStep:
    """An unlink that names a binding compares and clears it in ONE guarded step.

    Every way a stale row can clear the wrong binding shares one shape: a route
    that reads the binding, compares it, then clears it as separate steps -- a
    thread id missing from the compare, a client completion keyed on the
    channel, a token that is a pure function of the coordinates, a rebind
    landing between the compare and the clear. The shape is closed
    structurally: both steps live in ``SessionMap.clear_mirror_link_if`` /
    ``clear_slack_link_if`` under the map's own lock, and the routes call
    nothing else (enumerated below).
    """

    @pytest.mark.asyncio
    async def test_a_rebind_cannot_land_between_the_compare_and_the_clear(
        self, tmp_path, monkeypatch
    ):
        """A rebinding thread released mid-compare waits for the clear, then lands.

        The unlink clears exactly the binding it named and the rival binding
        stands afterwards. Were the compare a route-level read followed by a
        separate clear, the rival would land inside the window and be cleared
        by the stale unlink -- the map would answer no mirror at all.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        sm = _with_real_storage(state, _real_map(tmp_path, monkeypatch))
        state.push_slots_update = MagicMock()
        slot = state.get_or_create_slot("s1")
        key = f"dashboard:{slot.key}"
        sm.set_mirror_link(key, ChannelLink("discord", "dm-chan-9", None))
        (row,) = state._slot_links(slot)[0]
        rival = ChannelLink("telegram", "tg-1", None)
        with _release_a_rebinder_mid_compare(
            sm, state, "mirror_link_nonce", lambda: sm.set_mirror_link(key, rival)
        ) as seen:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/s1/mirror-unlink",
                    json={"channel_type": row["channel"], "binding": row["binding"]},
                )
                assert resp.status == 200
                assert (await resp.json()) == {"ok": True, "was_linked": True}
            seen["thread"].join(5)
            assert seen["landed_inside"] is False, "the rebind landed inside the compare-and-clear"
            assert sm.get_mirror_link(key) == rival, (
                "the stale unlink cleared the binding it never named"
            )

    def test_a_rebinder_the_compare_never_released_exits_without_a_write(
        self, tmp_path, monkeypatch
    ):
        """The rebinding thread cannot outlive its test or write after teardown.

        If the compare never runs (an assertion fails first, or the route never
        reads the nonce), the thread must not fall through its wait and rebind
        anyway -- that write would land in a ``tmp_path`` the fixture has already
        removed. Leaving the block cancels and joins it, and the map is untouched.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        sm = _with_real_storage(state, _real_map(tmp_path, monkeypatch))
        rebind = MagicMock()
        with _release_a_rebinder_mid_compare(sm, state, "mirror_link_nonce", rebind) as seen:
            assert seen["thread"].is_alive()
        assert not seen["thread"].is_alive(), "the thread must be joined when the block exits"
        rebind.assert_not_called()
        assert seen["landed_inside"] is None

    def test_an_early_failure_inside_the_block_still_joins_the_rebinder_without_a_write(
        self, tmp_path, monkeypatch
    ):
        """The shape GPT named: a test that fails before its compare runs.

        The failure propagates, the block's cleanup releases and joins the
        thread on the way out, and the rebind never happens -- so nothing can
        recreate ``tmp_path`` after the fixture has removed it. Also holds when
        the map is REAL storage: the path is untouched afterwards.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        sm = _with_real_storage(state, _real_map(tmp_path, monkeypatch))
        key = "dashboard:s1"
        before = sm.get_mirror_link(key)
        with pytest.raises(AssertionError, match="zzq-early"):
            with _release_a_rebinder_mid_compare(
                sm,
                state,
                "mirror_link_nonce",
                lambda: sm.set_mirror_link(key, ChannelLink("telegram", "tg-1", None)),
            ) as seen:
                assert False, "zzq-early"
        assert not seen["thread"].is_alive(), "the thread must be joined on the failing exit"
        assert sm.get_mirror_link(key) == before, "the rebinder must not write after the failure"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stall_after_release", [0.0, 0.8], ids=["prompt", "descheduled"])
    async def test_a_slack_relink_cannot_land_between_the_compare_and_the_clear(
        self, tmp_path, monkeypatch, stall_after_release
    ):
        """The Slack twin: a re-link released mid-compare lands after the clear and stands.

        And the unlink SAYS so: the map holds the newer thread by the time the
        durability wait returns, so the answer carries ``relinked: true`` and the
        route leaves the slot's in-process fields to the newer link rather than
        tearing them down after it.

        Once the clear releases the lock, the route and the rebinder race for
        it, so the durability wait is held until the re-link has landed: the
        route's post-clear read then sees the newer thread by construction. The
        ``descheduled`` case holds the rebinder off past the route's own path,
        which is the ordering a loaded runner produces.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        sm = _with_real_storage(state, _real_map(tmp_path, monkeypatch))
        state.push_slots_update = MagicMock()
        slot = state.get_or_create_slot("s1")
        key = f"dashboard:{slot.key}"
        sm.set_slack_link(key, "ts-old", "D-owner-dm")
        slot._slack_linked = True
        slot._slack_channel = "D-owner-dm"
        slot._slack_thread_ts = "ts-old"
        rows = {row["channel"]: row for row in state._slot_links(slot)[0]}
        with _release_a_rebinder_mid_compare(
            sm,
            state,
            "slack_link_nonce",
            lambda: sm.set_slack_link(key, "ts-new", "D-owner-dm"),
            stall_after_release=stall_after_release,
        ) as seen:

            async def flush_once_the_relink_has_landed() -> None:
                assert await asyncio.to_thread(seen["landed"].wait, 5), "the re-link never landed"
                await sm.aflush()

            state.sessions.aflush = flush_once_the_relink_has_landed
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/s1/mirror-unlink",
                    json={"channel_type": "slack", "binding": rows["slack"]["binding"]},
                )
                assert resp.status == 200
                assert (await resp.json()) == {"ok": True, "was_linked": True, "relinked": True}
            seen["thread"].join(5)
            assert seen["landed_inside"] is False, "the re-link landed inside the compare-and-clear"
            assert sm.get_slack_link(key) == ("ts-new", "D-owner-dm"), (
                "the stale unlink cleared the thread it never named"
            )

    def test_a_rebind_that_lands_first_is_refused(self, tmp_path, monkeypatch):
        """The other ordering the lock allows: the rival is already there, so 409's mismatch."""
        sm = _real_map(tmp_path, monkeypatch)
        key = "dashboard:s1"
        old = ChannelLink("discord", "dm-chan-9", None)
        sm.set_mirror_link(key, old)
        stale_token = binding_token(old, sm.mirror_link_nonce(key))
        rival = ChannelLink("telegram", "tg-1", None)
        sm.set_mirror_link(key, rival)
        assert sm.clear_mirror_link_if(key, "discord", stale_token) is False
        assert sm.get_mirror_link(key) == rival
        # And the row drawn from the rival clears exactly the rival.
        assert sm.clear_mirror_link_if(key, "telegram", binding_token(rival, sm.mirror_link_nonce(key)))
        assert sm.get_mirror_link(key) is None
        # No binding matches nothing: a row whose binding is already gone is refused.
        assert sm.clear_mirror_link_if(key, "telegram", binding_token(rival)) is False

    def test_every_unlink_route_clears_only_through_the_primitive(self):
        """Enumerate the unlink routes; each compares-and-clears through the map alone.

        The routes registered under an ``-unlink`` path are exactly the two
        handlers below (a third would have to be added here). Neither reads the
        binding for a compare of its own -- no ``get_mirror_link`` /
        ``get_slack_link``, no nonce accessor, no token function ahead of the
        clear -- and each clears a NAMED binding only through its
        compare-and-clear primitive; the plain clear survives only for the
        bodiless caller with no row to name. The one read a route may make is
        AFTER its clear: what the map holds once the durability wait returns
        decides whether the in-process teardown is the old binding's (the map is
        empty) or must be skipped (a relink landed inside the wait and the map
        holds it). A read after the clear cannot be a compare-then-clear, so the
        rule is positional: forbidden before the primitive call, allowed after.
        The client has one caller (``api.unlinkMirror`` in
        ``LinkedSurfacesSection``), pinned in its own tests.
        """
        from kiro_crew.dashboard import chat_mirror, chat_slack, routes

        route_dir = Path(inspect.getfile(routes)).parent
        registered = set()
        for source in route_dir.glob("*.py"):
            registered.update(
                re.findall(r'"/api/chat/slots/\{[a-z]+\}/[a-z]+-unlink",\s*chat\.(\w+)', source.read_text())
            )
        assert registered == {"api_chat_slot_mirror_unlink", "api_chat_slot_slack_unlink"}

        forbidden = {
            "get_mirror_link",
            "get_slack_link",
            "mirror_link_nonce",
            "slack_link_nonce",
            "_mirror_link_nonce",
            "_slack_link_nonce",
            "binding_token",
            "_binding_matches",
            "_binding_identity",
        }
        for handler, primitive in (
            (chat_mirror.api_chat_slot_mirror_unlink, "clear_mirror_link_if"),
            (chat_slack.api_chat_slot_slack_unlink, "clear_slack_link_if"),
        ):
            tree = ast.parse(textwrap.dedent(inspect.getsource(handler)))
            named = [
                (node.lineno, node.attr if isinstance(node, ast.Attribute) else node.id)
                for node in ast.walk(tree)
                if isinstance(node, (ast.Attribute, ast.Name))
            ]
            names = {name for _, name in named}
            assert primitive in names, f"{handler.__name__} does not call {primitive}"
            clear_at = max(line for line, name in named if name == primitive)
            before_the_clear = {name for line, name in named if line <= clear_at} & forbidden
            assert not before_the_clear, (
                f"{handler.__name__} reads the binding for a compare of its own: "
                f"{sorted(before_the_clear)}"
            )
            # After the clear only the plain link read is allowed -- the
            # post-flush identity check -- never a nonce or token function.
            after_the_clear = {name for line, name in named if line > clear_at} & forbidden
            assert after_the_clear <= {"get_mirror_link", "get_slack_link"}, (
                f"{handler.__name__} recomputes the binding after its clear: "
                f"{sorted(after_the_clear)}"
            )
