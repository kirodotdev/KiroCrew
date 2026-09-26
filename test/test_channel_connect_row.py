"""Tests for the channel connect/disconnect control.

Disconnecting a channel stops turn output reaching it while RETAINING the
binding, so a reply there resumes the same session. These tests pin the three
places that promise can break: the stored flag outliving its binding, the send
path not actually honouring it, and the wire not reporting it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.messaging.link import ChannelLink, legacy_dashboard_mirror_key
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
    return sm


def _make_app(state):
    from kiro_crew.dashboard.chat_mirror import (
        api_chat_slot_mirror_pause,
        api_chat_slot_mirror_unlink,
    )
    from kiro_crew.dashboard.chat_slack import api_chat_slot_slack_pause

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/slack-pause", api_chat_slot_slack_pause)
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
        """The row's `binding` IS the identity the unlink endpoints recompute.

        The redacted `target` drops the thread and the id's head, so it cannot
        tell a Slack thread from its same-channel replacement; the token is a
        digest of the whole binding -- its persisted nonce included -- and can,
        and neither the raw id nor the nonce is in it. The nonce is what tells a
        binding from its byte-identical recreation: an identical rewrite (the
        inbound paths re-write the same coordinates every turn) keeps the token,
        an unlink followed by a reconnect to the same target changes it.
        """
        from kiro_crew.dashboard.state import _binding_identity, _link_binding_token

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
        assert (row["channel"], row["binding"]) == _binding_identity(mirror, nonce)
        assert row["binding"] == _link_binding_token(ChannelLink("discord", "dm-chan-9", None), nonce)
        assert row["binding"] != _link_binding_token(ChannelLink("discord", "dm-chan-9", None))
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
        assert (recreated["channel"], recreated["binding"]) == _binding_identity(
            mirror, sm.mirror_link_nonce(key)
        )

        old_thread = ChannelLink("slack", "D-owner-dm", "ts-old")
        new_thread = ChannelLink("slack", "D-owner-dm", "ts-new")
        assert _link_binding_token(old_thread) != _link_binding_token(new_thread)
        sm.set_slack_link(key, "ts-new", "D-owner-dm")
        slack_nonce = sm.slack_link_nonce(key)
        assert slack_nonce and slack_nonce != sm.mirror_link_nonce(key)
        links, linked, _chan, ts = state._slot_links(slot)
        assert linked is True and ts == "ts-new"
        slack_row = next(row for row in links if row["channel"] == "slack")
        assert slack_row["binding"] == _link_binding_token(new_thread, slack_nonce)
        assert (slack_row["channel"], slack_row["binding"]) == _binding_identity(new_thread, slack_nonce)

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
        from kiro_crew.dashboard.state import _link_binding_token

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
        stale = _link_binding_token(ChannelLink("slack", "D-owner-dm", "ts-old"))
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
            assert (await resp.json()) == {"ok": True, "was_linked": True}
        assert sm.get_slack_link(key) == (None, None)
        assert (slot._slack_linked, slot._slack_channel, slot._slack_thread_ts) == (False, "", "")
        assert "ts-1" not in state._slack_to_slot
        links, slack_linked, _chan, _ts = state._slot_links(slot)
        assert slack_linked is False
        assert [row["channel"] for row in links] == ["discord"], "the mirror beside it must survive"
        assert sm.get_mirror_link(key) == ChannelLink("discord", "dm-chan-9", None)
