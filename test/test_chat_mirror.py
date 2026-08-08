"""Tests for the channel-neutral mirror-link / mirror-unlink endpoints (C3)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.messaging.link import ChannelLink
from kiro_crew.messaging.transport import ConfiguredChannelTarget
from kiro_crew.session_map import ConversationOwnershipConflict


def _wire_replace_mirror_owner(sessions, self_key="dashboard:s1"):
    """Make the double's `replace_mirror_owner` compose the parts it really uses.

    Mirrors `SessionMap.replace_mirror_owner`: snapshot each other occupant's
    binding and flags, evict them all, then claim. Returned as a real list so a
    caller's rollback loop actually iterates (a bare Mock yields nothing, which
    would let a rollback assertion pass without a rollback existing).
    """
    def _replace(key, link, *, accepts_inbound=True):
        occupants = [
            occupant
            for occupant in (sessions.find_mirror_sessions(link) or [])
            if occupant not in (key, self_key)
        ]
        displaced = []
        for occupant in occupants:
            held = sessions.get_mirror_link(occupant, link.channel_type)
            if held is None:
                continue
            displaced.append((
                occupant,
                held,
                bool(sessions.mirror_accepts_inbound(occupant, link.channel_type)),
                sessions.is_mirror_paused(occupant, link.channel_type) is True,
            ))
        # Eviction follows OCCUPANCY, not snapshot readability — same as the real
        # method, so a test whose `get_mirror_link` returns None still sees the
        # eviction happen.
        if occupants:
            sessions.clear_mirror_links_at(link)
        # All-or-nothing, like the real method: if the claim write fails, put the
        # displaced bindings back before propagating. Without this the fake would
        # let the exception escape carrying the only copy of the snapshot, and a
        # test asserting the eviction is undone would be asserting against a
        # weaker double than production.
        try:
            sessions.set_mirror_link(key, link, accepts_inbound=accepts_inbound)
        except Exception:
            sessions.clear_mirror_link(key, link.channel_type)
            for occupant, held, inbound, paused in displaced:
                sessions.set_mirror_link(occupant, held, accepts_inbound=inbound)
                if paused:
                    sessions.set_mirror_paused(occupant, True, held.channel_type)
            raise
        return displaced

    sessions.replace_mirror_owner = MagicMock(side_effect=_replace)

    def _restore(key, link, displaced, previous=None):
        """Mirror `SessionMap.restore_mirror_owner`.

        Interface parity matters here specifically: production stopped composing the
        rollback from `set_mirror_link`/`set_mirror_paused` calls and now makes ONE
        call, so a double without this method turns every rollback assertion into an
        AttributeError — and one that implemented it as a no-op would let those
        assertions pass with no rollback at all. It clears the whole LOCATION first,
        like the real one, so the restores cannot be refused.
        """
        # Same asymmetry as the real one: clearing the whole location is only
        # justified when this call evicted someone, otherwise a refused claim would
        # delete an innocent rival's binding.
        if displaced:
            sessions.clear_mirror_links_at(link)
        else:
            sessions.clear_mirror_link(key, link.channel_type)
        if previous is not None:
            prev_link, prev_inbound, prev_paused = previous
            sessions.set_mirror_link(key, prev_link, accepts_inbound=prev_inbound)
            if prev_paused:
                sessions.set_mirror_paused(key, True, prev_link.channel_type)
        for occupant, held, inbound, paused in displaced:
            sessions.set_mirror_link(occupant, held, accepts_inbound=inbound)
            if paused:
                sessions.set_mirror_paused(occupant, True, held.channel_type)

    sessions.restore_mirror_owner = MagicMock(side_effect=_restore)
    return sessions.replace_mirror_owner


def _make_mirror_app(state):
    from kiro_crew.dashboard.chat_mirror import (
        api_channel_targets,
        api_chat_slot_mirror_link,
        api_chat_slot_mirror_unlink,
    )

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{name}/mirror-link", api_chat_slot_mirror_link)
    app.router.add_post("/api/chat/slots/{name}/mirror-unlink", api_chat_slot_mirror_unlink)
    app.router.add_get("/api/chat/channel-targets", api_channel_targets)
    return app


def _fake_transport(
    channel_type="telegram", proactive=True, max_message_chars=4096, resumes=False
):
    return SimpleNamespace(
        channel_type=channel_type,
        capabilities=SimpleNamespace(
            supports_proactive_send=proactive,
            # The real TransportCapabilities always carries this; the mirror
            # backfill chunks to it instead of truncating, so the fake needs it
            # to exercise that path rather than the getattr fallback.
            max_message_chars=max_message_chars,
            # Defaults FALSE, like the real dataclass: only Discord declares it,
            # so the default fake models the common (outbound-only) transport.
            supports_session_resume=resumes,
        ),
        send_message=AsyncMock(return_value="mid-1"),
        configured_targets=MagicMock(
            return_value=[ConfiguredChannelTarget("user:123", f"{channel_type.title()} DM · 123")]
        ),
        resolve_configured_target=AsyncMock(return_value=("123", None)),
    )


def _wire_binding_state(sessions, initial=None):
    """Wire get/set/clear_mirror_link onto ONE shared dict, keyed by channel type.

    Delivery re-asks "is this session still bound to this exact conversation" before
    every send, so a double whose read ignores its own writes makes a connect refuse
    its own catch-up. Seeded with *initial* when a test needs a binding to exist
    before the request runs (the muted/reconnect fixtures).

    `get` honours `channel_type` because the real one does: named, it answers for
    that channel; unnamed, it refuses to guess between siblings.
    """
    bindings: dict[str, object] = {}
    if initial is not None:
        bindings[initial.channel_type] = initial

    def _get(_key, channel_type=""):
        if channel_type:
            return bindings.get(channel_type)
        return next(iter(bindings.values())) if len(bindings) == 1 else None

    def _set(_key, link, *, accepts_inbound=False):
        bindings[link.channel_type] = link

    def _clear(_key, channel_type=""):
        if channel_type:
            return bindings.pop(channel_type, None) is not None
        had = bool(bindings)
        bindings.clear()
        return had

    sessions.get_mirror_link = MagicMock(side_effect=_get)
    sessions.set_mirror_link = MagicMock(side_effect=_set)
    sessions.clear_mirror_link = MagicMock(side_effect=_clear)
    return bindings


def _prep(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    _wire_binding_state(state.sessions)
    state.sessions.get_slack_link = MagicMock(return_value=(None, None))
    # Interface parity with the real SessionManager: production claims a conversation
    # through ONE compound mutation, so the double must too. Wired here rather than
    # per-test so no setup silently gets a bare Mock back (which would iterate as
    # empty and make rollback assertions pass vacuously). Individual tests that
    # re-mock the parts can re-wire after doing so.
    _wire_replace_mirror_owner(state.sessions)
    state.get_or_create_slot("s1")
    state.push_slots_update = MagicMock()
    return state


class TestMirrorLink:
    @pytest.mark.asyncio
    async def test_configured_targets_are_listed(self, tmp_path, monkeypatch):
        state = _prep(tmp_path, monkeypatch)
        state.register_channel_transport(_fake_transport("telegram"))
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.get("/api/chat/channel-targets")
            assert resp.status == 200
            assert await resp.json() == [
                {
                    "channel_type": "telegram",
                    "target_id": "user:123",
                    "label": "Telegram DM · 123",
                    "available": True,
                    "unavailable_reason": "",
                }
            ]

    @pytest.mark.asyncio
    async def test_configured_target_is_resolved_server_side(self, tmp_path, monkeypatch):
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("telegram")
        state.register_channel_transport(transport)
        state.sessions.set_mirror_link = MagicMock()
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "telegram", "target_id": "user:123"},
            )
            assert resp.status == 200
        transport.resolve_configured_target.assert_awaited_once_with("user:123")
        link = state.sessions.set_mirror_link.call_args.args[1]
        assert link == ChannelLink("telegram", channel_id="123", thread_id=None)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            {"channel_type": "telegram", "target_id": "user:123"},
        ],
    )
    async def test_governance_deny_blocks_target_resolution_and_send(
        self, tmp_path, monkeypatch, body
    ):
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits",
            lambda *args, **kwargs: SimpleNamespace(permitted=False),
        )
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("telegram")
        state.register_channel_transport(transport)

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/mirror-link", json=body)
            assert resp.status == 403
            assert (await resp.json())["error"] == "channel is not permitted"

        transport.resolve_configured_target.assert_not_awaited()
        transport.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_governance_narrowing_mid_delivery_fails_closed(self, tmp_path, monkeypatch):
        # Permit the initial link + the announcement, then deny once the
        # historical context-delivery loop starts. The endpoint must fail closed:
        # return 403 and NOT persist the mirror link (regression for a denial
        # that only broke the loop and still persisted + returned 200).
        transport = _fake_transport("telegram")

        def _permits(*args, **kwargs):
            # A PREDICATE, not a call counter. The old version denied on the
            # third governance consult, which silently depended on exactly two
            # consults happening before the loop — change how many messages the
            # backfill selects, or add a pre-loop check, and the denial lands on
            # a pre-loop gate while every assertion below still passes, so the
            # test stops guarding the path it was written for.
            #
            # Keying on "has the transport already delivered?" pins the denial
            # to the first in-loop unit regardless of selection size: the
            # announcement is the only send before the loop.
            return SimpleNamespace(
                permitted=not transport.send_message.await_args_list,
                rule="",
                layer="",
                reason="",
            )

        monkeypatch.setattr("kiro_crew.platform.governance_profiles.governance_permits", _permits)
        state = _prep(tmp_path, monkeypatch)
        state.register_channel_transport(transport)
        state.sessions.set_mirror_link = MagicMock()
        # Give the slot history so the context-delivery loop iterates.
        slot = state.get_or_create_slot("s1")
        slot.messages.extend(
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
            ]
        )

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "telegram", "target_id": "user:123"},
            )
            assert resp.status == 403
            assert (await resp.json())["error"] == "channel is not permitted"

        # Non-vacuity: the announcement is sent only AFTER its own governance
        # check passes, so having sent it proves the denial came later than that
        # check — i.e. inside the context-delivery loop, which is the path under
        # test. Without this, a denial at the very first gate would produce the
        # same 403 and the same unpersisted link.
        assert transport.send_message.await_count >= 1
        # The claim IS the occupancy check now, so `set_mirror_link` DOES run —
        # before any delivery, deliberately. What must hold is that a denied
        # connect leaves NO binding behind, which is now the rollback's job. Assert
        # the release explicitly: without it the denial would 403 and still leave
        # the conversation claimed, which is the very regression this guards.
        state.sessions.set_mirror_link.assert_called_once()
        state.sessions.clear_mirror_link.assert_called_once_with("dashboard:s1", "telegram")
        state = _prep(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/nope/mirror-link",
                json={"channel_type": "telegram", "conversation_id": "1"},
            )
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_missing_channel_type(self, tmp_path, monkeypatch):
        state = _prep(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/mirror-link", json={})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_slack_rejected(self, tmp_path, monkeypatch):
        state = _prep(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "slack", "conversation_id": "C1"},
            )
            assert resp.status == 400
            assert "slack-link" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_missing_target_id(self, tmp_path, monkeypatch):
        state = _prep(tmp_path, monkeypatch)
        state.register_channel_transport(_fake_transport("telegram"))
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link", json={"channel_type": "telegram"}
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_channel_not_connected(self, tmp_path, monkeypatch):
        state = _prep(tmp_path, monkeypatch)  # no transport registered
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "telegram", "target_id": "user:1"},
            )
            assert resp.status == 503

    @pytest.mark.asyncio
    async def test_non_proactive_channel_rejected(self, tmp_path, monkeypatch):
        state = _prep(tmp_path, monkeypatch)
        state.register_channel_transport(_fake_transport("wecom", proactive=False))
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "wecom", "target_id": "user:u1"},
            )
            assert resp.status == 400
            assert "proactive" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_link_success(self, tmp_path, monkeypatch):
        state = _prep(tmp_path, monkeypatch)
        state.register_channel_transport(_fake_transport("telegram"))
        state.sessions.set_mirror_link = MagicMock()
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "telegram", "target_id": "user:123"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True and data["conversation_id"] == "123"
        state.sessions.set_mirror_link.assert_called_once()
        link = state.sessions.set_mirror_link.call_args.args[1]
        assert link == ChannelLink("telegram", channel_id="123", thread_id=None)

    @pytest.mark.asyncio
    async def test_link_passes_thread_id(self, tmp_path, monkeypatch):
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("telegram")
        # thread_id now flows from the configured-target resolution, not the body.
        transport.resolve_configured_target = AsyncMock(return_value=("C", "T"))
        state.register_channel_transport(transport)
        state.sessions.set_mirror_link = MagicMock()
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "telegram", "target_id": "user:C"},
            )
            assert resp.status == 200
        link = state.sessions.set_mirror_link.call_args.args[1]
        assert link.thread_id == "T"


class TestMirrorUnlink:
    @pytest.mark.asyncio
    async def test_slot_not_found(self, tmp_path, monkeypatch):
        state = _prep(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post("/api/chat/slots/nope/mirror-unlink")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_unlink_success(self, tmp_path, monkeypatch):
        state = _prep(tmp_path, monkeypatch)
        state.sessions.clear_mirror_link = MagicMock(return_value=True)
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/mirror-unlink")
            assert resp.status == 200
            assert (await resp.json())["was_linked"] is True

    @pytest.mark.asyncio
    async def test_unlink_noop(self, tmp_path, monkeypatch):
        state = _prep(tmp_path, monkeypatch)
        state.sessions.clear_mirror_link = MagicMock(return_value=False)
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/mirror-unlink")
            assert resp.status == 200
            assert (await resp.json())["was_linked"] is False

    @pytest.mark.asyncio
    async def test_unlink_is_scoped_to_the_channel_the_caller_names(
        self, tmp_path, monkeypatch
    ):
        """The header chip names ONE channel; the unnamed clear drops every binding.

        Releasing the Discord the chip labels must not also delete a Telegram
        binding the user never mentioned — that would strip its `accepts_inbound`
        and fork the next Telegram message into a fresh, historyless session.
        """
        state = _prep(tmp_path, monkeypatch)
        state.sessions.clear_mirror_link = MagicMock(return_value=True)
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-unlink", json={"channel_type": "discord"}
            )
            assert resp.status == 200
        _key, channel_type = state.sessions.clear_mirror_link.call_args[0]
        assert channel_type == "discord"

    @pytest.mark.asyncio
    async def test_unlink_reads_a_chunked_body_rather_than_content_length(
        self, tmp_path, monkeypatch
    ):
        """A chunked POST has `content_length is None`.

        Branching on Content-Length would read no `channel_type` and silently
        widen a scoped release into "clear everything" — the same defect the link
        handler documents.
        """
        state = _prep(tmp_path, monkeypatch)
        state.sessions.clear_mirror_link = MagicMock(return_value=True)

        async def chunked():
            yield b'{"channel_type": "discord"}'

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/mirror-unlink", data=chunked())
            assert resp.status == 200
        _key, channel_type = state.sessions.clear_mirror_link.call_args[0]
        assert channel_type == "discord"

    @pytest.mark.asyncio
    async def test_unlink_rejects_a_malformed_body_with_400_not_500(
        self, tmp_path, monkeypatch
    ):
        state = _prep(tmp_path, monkeypatch)
        state.sessions.clear_mirror_link = MagicMock(return_value=True)
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-unlink",
                data=b"{not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "body_not_json"
        state.sessions.clear_mirror_link.assert_not_called()


class TestTakeoverConsentIsStrict:
    """Consent is a boolean or it is absent.

    A body carrying `{"confirm": "false"}` — or any non-empty string, or 0/1 from a
    sloppy client — read as truthy and evicted another session's binding without
    the user ever seeing the prompt.
    """

    @staticmethod
    def _prepped(tmp_path, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits",
            lambda *args, **kwargs: SimpleNamespace(permitted=True),
        )
        state = _prep(tmp_path, monkeypatch)
        state.register_channel_transport(_fake_transport("discord"))
        state.sessions.find_mirror_sessions = MagicMock(return_value=["dashboard:other"])
        state.sessions.clear_mirror_links_at = MagicMock(return_value=["dashboard:other"])
        _wire_replace_mirror_owner(state.sessions)
        state.sessions.set_mirror_link = MagicMock()
        return state

    @staticmethod
    async def _connect(state, confirm):
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={
                    "channel_type": "discord", "target_id": "user:123", "confirm": confirm,
                },
            )
            return resp.status, await resp.json()

    @pytest.mark.asyncio
    async def test_the_string_false_does_not_count_as_confirmation(
        self, tmp_path, monkeypatch
    ):
        state = self._prepped(tmp_path, monkeypatch)

        status, body = await self._connect(state, "false")

        assert status == 409
        assert body["code"] == "conversation_occupied"
        state.sessions.set_mirror_link.assert_not_called()
        state.sessions.clear_mirror_links_at.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_truthy_non_boolean_does_not_count_either(self, tmp_path, monkeypatch):
        state = self._prepped(tmp_path, monkeypatch)

        status, _body = await self._connect(state, 1)

        assert status == 409
        state.sessions.set_mirror_link.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_real_boolean_true_still_takes_the_conversation(
        self, tmp_path, monkeypatch
    ):
        """Non-vacuity: the strict check must not break actual consent."""
        state = self._prepped(tmp_path, monkeypatch)

        status, _body = await self._connect(state, True)

        assert status == 200
        state.sessions.set_mirror_link.assert_called_once()
        state.sessions.clear_mirror_links_at.assert_called_once()


class TestTheConversationIsClaimedBeforeAnythingIsDelivered:
    """Claim first, deliver second — because delivery cannot be rolled back.

    A binding can be unwound; messages already posted into someone's conversation
    cannot. Checking occupancy, delivering the link notice plus the whole catch-up
    transcript, and only THEN discovering another session won the race means the
    loser has already pasted this session's history somewhere it does not belong.
    So the claim is written before any send, and released if delivery fails.
    """

    @staticmethod
    def _prepped(tmp_path, monkeypatch, *, occupants=(), deny_after_send=False):
        transport = _fake_transport("discord")

        def _permits(*args, **kwargs):
            return SimpleNamespace(
                permitted=not (deny_after_send and transport.send_message.await_args_list),
                rule="",
                layer="",
                reason="",
            )

        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits", _permits
        )
        state = _prep(tmp_path, monkeypatch)
        state.register_channel_transport(transport)
        state.sessions.find_mirror_sessions = MagicMock(return_value=list(occupants))
        state.sessions.clear_mirror_links_at = MagicMock(return_value=list(occupants))
        _wire_replace_mirror_owner(state.sessions)
        _wire_binding_state(state.sessions, None)
        state.sessions.set_mirror_link = MagicMock()
        state.sessions.clear_mirror_link = MagicMock()
        return state, transport

    @staticmethod
    async def _connect(state, **extra):
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "discord", "target_id": "user:123", **extra},
            )
            return resp.status, await resp.json()

    @pytest.mark.asyncio
    async def test_the_claim_is_written_before_any_message_is_sent(
        self, tmp_path, monkeypatch
    ):
        state, transport = self._prepped(tmp_path, monkeypatch)
        order: list[str] = []
        state.sessions.set_mirror_link.side_effect = lambda *a, **k: order.append("claim")
        original = transport.send_message

        async def _tracked(*args, **kwargs):
            order.append("send")
            return await original(*args, **kwargs)

        transport.send_message = _tracked

        status, _body = await self._connect(state)

        assert status == 200
        assert order and order[0] == "claim", (
            f"a message was delivered before the conversation was claimed: {order}"
        )

    @pytest.mark.asyncio
    async def test_a_failed_delivery_releases_the_claim(self, tmp_path, monkeypatch):
        """Otherwise a 502 leaves the conversation claimed by a connect that failed."""
        state, transport = self._prepped(tmp_path, monkeypatch)
        transport.send_message = AsyncMock(side_effect=RuntimeError("transport down"))

        status, _body = await self._connect(state)

        assert status == 502
        state.sessions.set_mirror_link.assert_called_once()
        state.sessions.clear_mirror_link.assert_called_once_with("dashboard:s1", "discord")

    @pytest.mark.asyncio
    async def test_a_confirmed_takeover_evicts_then_claims(self, tmp_path, monkeypatch):
        state, _transport = self._prepped(
            tmp_path, monkeypatch, occupants=["dashboard:other"]
        )

        status, _body = await self._connect(state, confirm=True)

        assert status == 200
        state.sessions.clear_mirror_links_at.assert_called_once()
        state.sessions.set_mirror_link.assert_called_once()
        # Claim survives a successful connect: no release on the happy path.
        state.sessions.clear_mirror_link.assert_not_called()


class TestATakeoverIsReversibleAndSerialized:
    """A confirmed takeover is a critical section, and a failed one puts things back.

    Two defects live here if it is not. (1) The takeover evicts the previous owner
    and then delivery fails — without a snapshot, the rollback tidies up the
    claimant and silently keeps the eviction, so the evicted session is unbound for
    nothing. (2) Two confirmed takeovers interleave across the awaited sends: the
    second claims while the first is still delivering, and the first streams its
    transcript into a conversation it no longer owns.
    """

    OCCUPANT = "dashboard:previous-owner"

    @staticmethod
    def _prepped(tmp_path, monkeypatch, *, fail_delivery=False):
        transport = _fake_transport("discord")
        if fail_delivery:
            transport.send_message = AsyncMock(side_effect=RuntimeError("transport down"))
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits",
            lambda *args, **kwargs: SimpleNamespace(permitted=True),
        )
        state = _prep(tmp_path, monkeypatch)
        state.register_channel_transport(transport)
        return state, transport

    @staticmethod
    async def _connect(state, **extra):
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "discord", "target_id": "user:123", **extra},
            )
            return resp.status, await resp.json()

    @pytest.mark.asyncio
    async def test_a_failed_takeover_restores_the_evicted_binding(
        self, tmp_path, monkeypatch
    ):
        state, _transport = self._prepped(tmp_path, monkeypatch, fail_delivery=True)
        held = ChannelLink(channel_type="discord", channel_id="user:123")
        state.sessions.find_mirror_sessions = MagicMock(return_value=[self.OCCUPANT])
        state.sessions.clear_mirror_links_at = MagicMock(return_value=[self.OCCUPANT])
        _wire_replace_mirror_owner(state.sessions)
        state.sessions.get_mirror_link = MagicMock(
            side_effect=lambda key, ct="": held if key == self.OCCUPANT else None
        )
        state.sessions.mirror_accepts_inbound = MagicMock(return_value=True)
        state.sessions.set_mirror_link = MagicMock()
        state.sessions.clear_mirror_link = MagicMock()

        status, _body = await self._connect(state, confirm=True)

        assert status == 502
        # The evicted owner is put back with its binding AND its inbound marker —
        # not left unbound because our own delivery happened to fail.
        restored = [
            call for call in state.sessions.set_mirror_link.call_args_list
            if call[0][0] == self.OCCUPANT
        ]
        assert len(restored) == 1, (
            f"evicted binding was not restored: {state.sessions.set_mirror_link.call_args_list}"
        )
        assert restored[0][0][1] is held
        assert restored[0][1]["accepts_inbound"] is True

    @pytest.mark.asyncio
    async def test_the_whole_claim_and_delivery_holds_the_conversation_lock(
        self, tmp_path, monkeypatch
    ):
        """Serialised per conversation, so a rival cannot claim mid-delivery."""
        from kiro_crew.dashboard import chat_mirror

        state, _transport = self._prepped(tmp_path, monkeypatch)
        state.sessions.find_mirror_sessions = MagicMock(return_value=[])
        _wire_binding_state(state.sessions, None)
        state.sessions.set_mirror_link = MagicMock()

        held_during_delivery: list[bool] = []
        original = chat_mirror._deliver_catch_up

        async def _spy(*args, **kwargs):
            link = args[3]
            held_during_delivery.append(chat_mirror._conversation_lock(link).locked())
            return await original(*args, **kwargs)

        monkeypatch.setattr(chat_mirror, "_deliver_catch_up", _spy)

        status, _body = await self._connect(state)

        assert status == 200
        assert held_during_delivery == [True], (
            "the conversation lock must still be held while the catch-up is delivered"
        )

    @pytest.mark.asyncio
    async def test_the_lock_table_does_not_leak_after_a_connect(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.dashboard import chat_mirror

        state, _transport = self._prepped(tmp_path, monkeypatch)
        state.sessions.find_mirror_sessions = MagicMock(return_value=[])
        _wire_binding_state(state.sessions, None)
        state.sessions.set_mirror_link = MagicMock()
        chat_mirror._CONVERSATION_LOCKS.clear()

        await self._connect(state)

        assert chat_mirror._CONVERSATION_LOCKS == {}, (
            "an uncontended lock must be dropped so the table cannot grow forever"
        )

    @pytest.mark.asyncio
    async def test_a_failed_rebind_restores_the_previous_INBOUND_MODE_too(
        self, tmp_path, monkeypatch
    ):
        """Restoring the link but not its flag silently promotes it to inbound.

        The claim overwrites the binding with `accepts_inbound=True`, so reading the
        flag during the rollback reads the value the claim just wrote — always True.
        An outbound-only binding would come back inbound, and that channel's replies
        would start routing into a session that never asked for them.
        """
        state, _transport = self._prepped(tmp_path, monkeypatch, fail_delivery=True)
        outbound_only = ChannelLink(channel_type="discord", channel_id="user:999")
        state.sessions.find_mirror_sessions = MagicMock(return_value=[])
        _wire_binding_state(state.sessions, outbound_only)

        # Model what the real map does: the flag reads False until the CLAIM writes
        # True, and True afterwards. A mock that always returns False would let a
        # rollback that reads the flag too late pass anyway — the whole defect is
        # WHEN the read happens, so the fake has to have the same before/after.
        claimed = {"inbound": False}

        def _set_link(_key, _link, accepts_inbound=False):
            if accepts_inbound:
                claimed["inbound"] = True

        state.sessions.set_mirror_link = MagicMock(side_effect=_set_link)
        state.sessions.mirror_accepts_inbound = MagicMock(
            side_effect=lambda *a, **k: claimed["inbound"]
        )

        status, _body = await self._connect(state, confirm=True)

        assert status == 502
        restores = [
            call for call in state.sessions.set_mirror_link.call_args_list
            if call[0][1] is outbound_only
        ]
        assert len(restores) == 1, (
            f"previous binding was not restored: {state.sessions.set_mirror_link.call_args_list}"
        )
        assert restores[0][1]["accepts_inbound"] is False, (
            "an outbound-only binding was restored as INBOUND — the rollback read the "
            "flag the claim had already overwritten instead of a pre-claim snapshot"
        )

    @pytest.mark.asyncio
    async def test_the_claiming_write_is_offloaded_from_the_event_loop(
        self, tmp_path, monkeypatch
    ):
        """`set_mirror_link` calls `_save`, which serialises the whole session map.

        Awaiting inside the critical section is safe only because the conversation
        lock is held — the lock, not the absence of awaits, is what serialises this.
        """
        from kiro_crew.dashboard import chat_mirror

        state, _transport = self._prepped(tmp_path, monkeypatch)
        state.sessions.find_mirror_sessions = MagicMock(return_value=[])
        _wire_binding_state(state.sessions, None)
        state.sessions.set_mirror_link = MagicMock()

        offloaded: list[str] = []
        original = chat_mirror.asyncio.to_thread

        async def _spy(fn, *args, **kwargs):
            offloaded.append(getattr(fn, "_mock_name", None) or getattr(fn, "__name__", str(fn)))
            return await original(fn, *args, **kwargs)

        monkeypatch.setattr(chat_mirror.asyncio, "to_thread", _spy)

        status, _body = await self._connect(state)

        assert status == 200
        # `replace_mirror_owner` is the claiming write now — eviction and claim are
        # one session-map mutation, so there is no vacancy between them. Either name
        # satisfies the property under test: the write that claims the conversation
        # does not run on the event loop.
        assert any(
            "set_mirror_link" in name or "replace_mirror_owner" in name
            for name in offloaded
        ), (
            f"the claiming write was not offloaded; offloaded calls were {offloaded}"
        )

    @pytest.mark.asyncio
    async def test_a_failed_takeover_restores_the_occupants_MUTE_too(
        self, tmp_path, monkeypatch
    ):
        """`set_mirror_link` drops the mute by design, so restoring needs both steps.

        Restoring only the link leaves the occupant CONNECTED after a takeover that
        failed — the failed attempt would have silently un-muted a channel the user
        had deliberately muted.
        """
        state, _transport = self._prepped(tmp_path, monkeypatch, fail_delivery=True)
        held = ChannelLink(channel_type="discord", channel_id="user:123")
        state.sessions.find_mirror_sessions = MagicMock(return_value=[self.OCCUPANT])
        state.sessions.clear_mirror_links_at = MagicMock(return_value=[self.OCCUPANT])
        _wire_replace_mirror_owner(state.sessions)
        state.sessions.get_mirror_link = MagicMock(
            side_effect=lambda key, ct="": held if key == self.OCCUPANT else None
        )
        state.sessions.mirror_accepts_inbound = MagicMock(return_value=True)
        # The occupant had MUTED this channel before the takeover attempt.
        state.sessions.is_mirror_paused = MagicMock(
            side_effect=lambda key, ct="": key == self.OCCUPANT
        )
        state.sessions.set_mirror_link = MagicMock()
        state.sessions.set_mirror_paused = MagicMock()

        status, _body = await self._connect(state, confirm=True)

        assert status == 502
        state.sessions.set_mirror_paused.assert_any_call(self.OCCUPANT, True, "discord")

    @pytest.mark.asyncio
    async def test_a_failed_claim_WRITE_restores_the_eviction(self, tmp_path, monkeypatch):
        """The eviction persists before the claim, so the claim's write is guarded.

        If `set_mirror_link` raises, an unguarded exception would escape with the
        previous owner already evicted for a connect that never happened — and the
        rollback helper is not even in scope at that point unless it is defined
        before the writes.
        """
        state, _transport = self._prepped(tmp_path, monkeypatch)
        held = ChannelLink(channel_type="discord", channel_id="user:123")
        state.sessions.find_mirror_sessions = MagicMock(return_value=[self.OCCUPANT])
        state.sessions.clear_mirror_links_at = MagicMock(return_value=[self.OCCUPANT])
        _wire_replace_mirror_owner(state.sessions)
        state.sessions.get_mirror_link = MagicMock(
            side_effect=lambda key, ct="": held if key == self.OCCUPANT else None
        )
        state.sessions.mirror_accepts_inbound = MagicMock(return_value=True)
        state.sessions.is_mirror_paused = MagicMock(return_value=False)

        # The CLAIM fails; the restore for the evicted occupant must still succeed.
        def _set_link(key, _link, accepts_inbound=False):
            if key != self.OCCUPANT:
                raise OSError("disk full")

        state.sessions.set_mirror_link = MagicMock(side_effect=_set_link)

        status, body = await self._connect(state, confirm=True)

        assert status == 500
        assert body["code"] == "claim_failed"
        restored = [
            call for call in state.sessions.set_mirror_link.call_args_list
            if call[0][0] == self.OCCUPANT
        ]
        assert len(restored) == 1, (
            "the evicted owner was left disconnected by a claim that never landed: "
            f"{state.sessions.set_mirror_link.call_args_list}"
        )
        assert restored[0][0][1] is held

    def test_the_snapshot_covers_every_restorable_field_of_a_binding(self):
        """Guard against restoring a partially-reconstructed binding.

        Three separate rounds of review found the same shape of bug: the rollback
        restored the link but dropped a flag stored ON it. This asserts the snapshot
        is EXHAUSTIVE against the binding's actual persisted keys, so a future field
        added to a binding fails here rather than being silently lost on rollback.
        """
        import inspect

        from kiro_crew.dashboard import chat_mirror
        from kiro_crew.messaging.link import ChannelLink as _Link

        persisted = set(_Link(channel_type="discord", channel_id="c").to_dict())
        # Flags the binding carries beyond the link's own coordinates.
        flags = {"accepts_inbound", "paused"}
        src = inspect.getsource(chat_mirror._claim_and_seed)
        for flag in flags:
            assert flag in src, (
                f"the rollback snapshot never mentions {flag!r}; a restored binding "
                "would silently lose it"
            )
        unaccounted = persisted - set(_Link.__dataclass_fields__) - flags
        assert not unaccounted, (
            f"a binding persists {sorted(unaccounted)} that the rollback does not "
            "snapshot — restoring it would drop that state"
        )


class TestOneSessionPerConversation:
    """A conversation hosts exactly one session, and taking it is confirmed.

    A Discord DM cannot hold threads, so there is nothing to scope two bindings
    to: with two, the inbound resolver refuses to pick and a message in that
    conversation reaches NOBODY. Eviction is what keeps "replying reconnects it"
    unambiguous — the same last-writer-wins guarantee Slack gets for free from its
    single-valued thread index.
    """

    @staticmethod
    def _prepped(tmp_path, monkeypatch, *, occupied_by=()):
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits",
            lambda *args, **kwargs: SimpleNamespace(permitted=True),
        )
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("discord", resumes=True)
        state.register_channel_transport(transport)
        state.sessions.find_mirror_sessions = MagicMock(return_value=list(occupied_by))
        state.sessions.clear_mirror_links_at = MagicMock(return_value=list(occupied_by))
        _wire_replace_mirror_owner(state.sessions)
        state.sessions.set_mirror_link = MagicMock()
        return state, transport

    @staticmethod
    async def _connect(state, **extra):
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "discord", "target_id": "user:123", **extra},
            )
            return resp.status, await resp.json()

    @pytest.mark.asyncio
    async def test_connecting_an_occupied_conversation_asks_first(self, tmp_path, monkeypatch):
        state, transport = self._prepped(tmp_path, monkeypatch, occupied_by=["dashboard:other"])

        status, body = await self._connect(state)

        assert status == 409
        assert body["code"] == "conversation_occupied"
        assert body["requires_confirm"] is True
        # Refused BEFORE any side effect: nothing announced, nothing evicted,
        # nothing bound. A confirm the user has not given cannot cost them a
        # session's connection.
        transport.send_message.assert_not_awaited()
        state.sessions.clear_mirror_links_at.assert_not_called()
        state.sessions.set_mirror_link.assert_not_called()

    @pytest.mark.asyncio
    async def test_confirmed_takeover_evicts_and_tells_the_conversation(
        self, tmp_path, monkeypatch
    ):
        state, transport = self._prepped(tmp_path, monkeypatch, occupied_by=["dashboard:other"])

        status, _ = await self._connect(state, confirm=True)

        assert status == 200
        state.sessions.clear_mirror_links_at.assert_called_once()
        # Whoever is reading that conversation is told which session they are
        # talking to now — the eviction is not silent on the channel side.
        sent = [call.args[1] for call in transport.send_message.await_args_list]
        assert any("different session is connected here now" in text for text in sent)

    @pytest.mark.asyncio
    async def test_a_free_conversation_needs_no_confirm(self, tmp_path, monkeypatch):
        state, transport = self._prepped(tmp_path, monkeypatch)

        status, _ = await self._connect(state)

        assert status == 200
        state.sessions.clear_mirror_links_at.assert_not_called()

    @pytest.mark.asyncio
    async def test_connect_makes_replies_route_back_to_this_session(
        self, tmp_path, monkeypatch
    ):
        """The reported defect: connect a session, reply in Discord, and the reply
        landed in a brand-new channel-born tab.

        The inbound resolver only counts bindings flagged ``accepts_inbound``, and
        the dashboard's connect never set it — so the resolver found no owner and
        fell through to the conversation's own session key.
        """
        state, _ = self._prepped(tmp_path, monkeypatch)

        status, _ = await self._connect(state)

        assert status == 200
        _, kwargs = state.sessions.set_mirror_link.call_args
        assert kwargs["accepts_inbound"] is True


class TestMirrorReminder:
    @pytest.mark.asyncio
    async def test_existing_live_mirror_is_a_silent_no_op(self, tmp_path, monkeypatch):
        """Connecting an already-connected channel does nothing, and says nothing.

        This used to post "Session linked from dashboard — continuing here." for
        the "Post reminder in <channel>" menu item. That row is gone, so the only
        ways to arrive here are a stale dashboard tab racing a connected one or a
        direct API call — and in both cases a stray message in the conversation
        explains nothing to whoever reads it.
        """
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits",
            lambda *args, **kwargs: SimpleNamespace(permitted=True),
        )
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("discord")
        state.register_channel_transport(transport)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("discord", channel_id="356163505868767244")
        )
        state.sessions.is_mirror_paused = MagicMock(return_value=False)

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/mirror-link")
            assert resp.status == 200
            assert await resp.json() == {
                "ok": True,
                "already_linked": True,
                "channel_type": "discord",
            }

        transport.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_muted_mirror_reconnects_and_catches_the_conversation_up(
        self, tmp_path, monkeypatch
    ):
        """The same empty-body call on a MUTED link is the reconnect.

        It lifts the mute through ``set_mirror_link`` — the rebind is what clears
        the flag — and seeds the conversation with the history it missed, because
        the gap in it is there precisely because delivery was off.
        """
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits",
            lambda *args, **kwargs: SimpleNamespace(permitted=True),
        )
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("discord", resumes=True)
        state.register_channel_transport(transport)
        link = ChannelLink("discord", channel_id="356163505868767244")
        _wire_binding_state(state.sessions, link)
        state.sessions.is_mirror_paused = MagicMock(return_value=True)
        state.sessions.set_mirror_link = MagicMock()
        # History for the catch-up to carry. Without it there is nothing to send
        # and the test would pass on an empty delivery.
        slot = state.get_or_create_slot("s1")
        slot.append("user", "what changed while I was away")
        slot.append("assistant", "the lint rule moved")
        slot.drain()

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/mirror-link")
            assert resp.status == 200
            body = await resp.json()

        assert body["reconnected"] is True
        assert body["conversation_id"] == "356163505868767244"
        # The rebind is the un-mute, and it lands BEFORE the catch-up: ownership is
        # reclaimed first so a concurrent takeover cannot hand the conversation away
        # mid-delivery, and the mute is restored if the catch-up then fails (see
        # TestReconnectReclaimsBeforeDelivering). accepts_inbound is re-asserted so a
        # reply in that conversation resumes THIS session rather than starting a
        # channel-born one.
        state.sessions.set_mirror_link.assert_called_once_with(
            "dashboard:s1", link, accepts_inbound=True
        )
        # Catch-up delivered the missed history: this is the difference between a
        # reconnect and the silent no-op above.
        sent = "\n".join(call.args[1] for call in transport.send_message.await_args_list)
        assert "what changed while I was away" in sent
        assert "the lint rule moved" in sent

    @pytest.mark.asyncio
    async def test_partial_body_validates_instead_of_posting(self, tmp_path, monkeypatch):
        """A non-empty partial payload must hit field validation, not send.

        ``{"thread_id": ...}`` carries neither channel_type nor conversation_id,
        so gating reminder mode on those two fields being absent would post an
        unsolicited message to the persisted channel instead of rejecting a
        malformed link attempt.
        """
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits",
            lambda *args, **kwargs: SimpleNamespace(permitted=True),
        )
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("discord")
        state.register_channel_transport(transport)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("discord", channel_id="356163505868767244")
        )

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link", json={"thread_id": "unexpected"}
            )
            assert resp.status == 400
            assert (await resp.json())["error"] == "channel_type required"

        transport.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_object_body_is_rejected(self, tmp_path, monkeypatch):
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("discord")
        state.register_channel_transport(transport)

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/mirror-link", json=["nope"])
            assert resp.status == 400

        transport.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_invalid_utf8_body_is_400_not_500(self, tmp_path, monkeypatch):
        """A body that cannot be decoded is a client error, not a traceback."""
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("discord")
        state.register_channel_transport(transport)

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                data=b"\xff\xfe\x00bad",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

        transport.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_charset_is_400_not_500(self, tmp_path, monkeypatch):
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("discord")
        state.register_channel_transport(transport)

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                data=b'{"channel_type":"discord"}',
                headers={"Content-Type": "application/json; charset=nosuchcharset"},
            )
            assert resp.status == 400

        transport.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_chunked_partial_body_validates_instead_of_posting(self, tmp_path, monkeypatch):
        """A CHUNKED partial payload must not read as an empty body.

        A chunked request has ``content_length is None``, so branching on
        Content-Length to decide whether to read JSON treats a real body as
        empty and falls into reminder mode — posting an unsolicited message.
        """
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits",
            lambda *args, **kwargs: SimpleNamespace(permitted=True),
        )
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("discord")
        state.register_channel_transport(transport)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("discord", channel_id="356163505868767244")
        )

        async def _chunked():
            yield b'{"thread_id": "unexpected"}'

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                data=_chunked(),
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
            assert (await resp.json())["error"] == "channel_type required"

        transport.send_message.assert_not_awaited()


class TestMirrorBackfillFidelity:
    """The non-Slack mirror seeds the same turn-aware history, chunked not cut.

    Deliberately asymmetric with the Slack path: this delivery stays INLINE
    because its per-message governance re-check has to be able to fail the
    request closed with 403, which a backgrounded drain could not do after the
    handler had already returned 200 and persisted the link.
    """

    # A LITERAL ceiling, deliberately NOT chat_mirror._MAX_INLINE_BACKFILL_UNITS:
    # asserting against the module constant would move with it, so raising the
    # cap — or deleting it — would still pass. This leaves headroom for a
    # deliberate tuning change while still failing an unbounded loop.
    _BOUND_CEILING = 16

    def _linked(self, tmp_path, monkeypatch, max_message_chars=4096):
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("telegram", max_message_chars=max_message_chars)
        state.register_channel_transport(transport)
        # No `set_mirror_link` override here: `_prep` already wires it statefully, and
        # replacing it with a no-op meant the claim never recorded — so the catch-up
        # that follows it read back "not bound" and refused to deliver anything. The
        # stateful version is still a MagicMock, so call assertions keep working.
        return state, transport

    async def _link(self, state):
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "telegram", "target_id": "user:123"},
            )
            assert resp.status == 200

    def _sent(self, transport):
        """Delivered bodies, excluding the link announcement."""
        texts = [call.args[1] for call in transport.send_message.await_args_list]
        return [t for t in texts if "Session linked from dashboard" not in t]

    @pytest.mark.asyncio
    async def test_filter_runs_before_slice(self, tmp_path, monkeypatch):
        state, transport = self._linked(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        for role, content in [
            ("user", "why is the build red"),
            ("assistant", "a lint rule changed"),
            ("tool", "grep ..."),
            ("tool", "cat ..."),
            ("tool", "pytest ..."),
        ]:
            slot.append(role, content)
        slot.drain()

        await self._link(state)
        body = "\n".join(self._sent(transport))
        assert "why is the build red" in body
        assert "a lint rule changed" in body
        assert "grep" not in body and "pytest" not in body

    @pytest.mark.asyncio
    async def test_long_message_is_chunked_not_truncated(self, tmp_path, monkeypatch):
        state, transport = self._linked(tmp_path, monkeypatch, max_message_chars=500)
        slot = state.get_or_create_slot("s1")
        long_answer = "".join(f"[{i:04d}]" for i in range(600))  # 3600 chars
        slot.append("user", "explain")
        slot.append("assistant", long_answer)
        slot.drain()

        await self._link(state)
        sent = self._sent(transport)
        assert all(len(text) <= 500 for text in sent), "a chunk exceeded the transport limit"
        body = "".join(sent)
        for i in (0, 300, 599):
            assert f"[{i:04d}]" in body, f"marker {i} lost — content was truncated"

    @pytest.mark.asyncio
    async def test_first_turn_and_gap_marker(self, tmp_path, monkeypatch):
        state, transport = self._linked(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        for i in range(1, 11):
            slot.append("user", f"question {i}")
            slot.append("assistant", f"answer {i}")
        slot.drain()

        await self._link(state)
        sent = self._sent(transport)
        body = "\n".join(sent)
        assert "question 1" in body
        assert "question 10" in body
        assert "question 3" not in body
        markers = [t for t in sent if "earlier turn" in t]
        assert len(markers) == 1
        # Slack would report 4 skipped (10 turns, 5 recent). The inline path is
        # additionally under a delivery budget, so the oldest recent turn is
        # folded into the marker instead of being sent -- 5, not 4. That fold is
        # the point of the budget: the marker absorbs the overflow.
        assert "5 earlier turns" in markers[0]

    @pytest.mark.asyncio
    async def test_history_that_fits_exactly_is_not_trimmed(self, tmp_path, monkeypatch):
        """No gap marker when there is no gap.

        Six two-message turns is exactly the 12-unit budget. An earlier version
        reserved the marker's slot unconditionally, so the reservation pushed the
        oldest turn out and then spent that slot announcing the omission it had
        itself caused — a false gap on history that fit.
        """
        state, transport = self._linked(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        for i in range(1, 7):
            slot.append("user", f"q{i}")
            slot.append("assistant", f"a{i}")
        slot.drain()

        await self._link(state)
        sent = self._sent(transport)
        body = "\n".join(sent)
        assert not any("earlier turn" in t for t in sent), f"false gap marker: {sent}"
        for i in range(1, 7):
            assert f"q{i}" in body, f"turn {i} was trimmed even though it fit"
        assert len(sent) == 12, f"expected all 12 units, got {len(sent)}"

    @pytest.mark.asyncio
    async def test_inline_delivery_is_bounded(self, tmp_path, monkeypatch):
        """The request cannot grow without limit just because history did.

        This path is inline (its governance re-check must be able to 403), so
        every extra unit is another governance hop plus a send on a channel that
        may accept ~1 msg/s. Long history must not push the request past a
        browser fetch timeout.
        """

        state, transport = self._linked(tmp_path, monkeypatch, max_message_chars=200)
        slot = state.get_or_create_slot("s1")
        for i in range(1, 9):
            slot.append("user", f"question {i}")
            slot.append("assistant", f"answer {i} " + "y" * 900)  # ~5 units each
        slot.drain()

        await self._link(state)
        sent = self._sent(transport)
        assert len(sent) <= self._BOUND_CEILING, (
            f"inline delivery sent {len(sent)} units, over the budget"
        )
        # Priority order: the newest turn is irreducible, then the marker, then
        # the opening turn, then older turns. Here each turn costs ~6 units, so
        # the opening turn cannot be afforded and is folded into the count.
        body = "\n".join(sent)
        assert "question 8" in body, "newest turn was trimmed away"
        assert any("earlier turn" in t for t in sent), "trim happened with no marker"

    @pytest.mark.asyncio
    async def test_delivery_scales_with_the_budget_not_with_history(
        self, tmp_path, monkeypatch
    ):
        """Ten times the history must not mean ten times the request duration."""

        counts = []
        for turn_count in (8, 80):
            state, transport = self._linked(tmp_path, monkeypatch)
            slot = state.get_or_create_slot("s1")
            for i in range(1, turn_count + 1):
                slot.append("user", f"q{i}")
                slot.append("assistant", f"a{i}")
            slot.drain()
            await self._link(state)
            counts.append(len(self._sent(transport)))

        assert all(c <= self._BOUND_CEILING for c in counts), counts
        assert counts[0] == counts[1], (
            f"unit count tracked history length ({counts}) instead of the budget"
        )

    @pytest.mark.asyncio
    async def test_no_slack_mrkdwn_conversion_on_a_non_slack_channel(self, tmp_path, monkeypatch):
        """Telegram is not Slack: markdown must pass through unconverted."""
        state, transport = self._linked(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "doc it")
        slot.append("assistant", "## Heading\n\n**bold** text")
        slot.drain()

        await self._link(state)
        body = "\n".join(self._sent(transport))
        assert "## Heading" in body
        assert "**bold**" in body

    @pytest.mark.asyncio
    async def test_credentials_are_redacted(self, tmp_path, monkeypatch):
        state, transport = self._linked(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        secret = "AKIAIOSFODNN7EXAMPLE"
        slot.append("user", "creds")
        slot.append("assistant", f"key is {secret}")
        slot.drain()

        await self._link(state)
        body = "\n".join(self._sent(transport))
        assert secret not in body

    @pytest.mark.asyncio
    async def test_compaction_rows_are_excluded(self, tmp_path, monkeypatch):
        state, transport = self._linked(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "real question")
        slot.append("assistant", "real answer")
        slot.append("assistant", "context compacted", meta={"kind": "compaction"})
        slot.drain()

        await self._link(state)
        body = "\n".join(self._sent(transport))
        assert "real question" in body and "real answer" in body
        assert "context compacted" not in body

    @pytest.mark.asyncio
    async def test_delivery_stays_inline_so_the_link_persists_after_seeding(
        self, tmp_path, monkeypatch
    ):
        """The 200 must not be returned before the seeding is delivered.

        This is the property that forbids backgrounding this path. It also pins the
        claim-first ordering: the binding is written BEFORE anything is delivered,
        so a losing racer cannot paste this session's transcript into a
        conversation it does not own. Delivery is still inline — every send lands
        before the handler returns — and a failure releases the claim (covered by
        `test_governance_narrowing_mid_delivery_fails_closed`).
        """
        state, transport = self._linked(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "one")
        slot.append("assistant", "two")
        slot.drain()

        order: list[str] = []
        original_send = transport.send_message

        async def _tracked_send(*args, **kwargs):
            order.append("send")
            return await original_send(*args, **kwargs)

        transport.send_message = _tracked_send
        # Wrap the STATEFUL writer rather than replacing it: delivery re-asks "is this
        # session still bound here" before each send, so a tracker that only records
        # the call left the map empty and the claim's own catch-up refused with a 403.
        persist = state.sessions.set_mirror_link

        def _tracked_persist(*args, **kwargs):
            order.append("persist")
            return persist(*args, **kwargs)

        state.sessions.set_mirror_link = MagicMock(side_effect=_tracked_persist)

        await self._link(state)
        assert "persist" in order, "link was never persisted"
        assert order[0] == "persist", (
            "the conversation must be CLAIMED before anything is delivered into it; "
            f"got {order}"
        )
        assert "send" in order[1:], (
            "the seeding must be delivered inline, before the handler returns; "
            f"got {order}"
        )
        assert order.count("send") >= 3, "announcement + both messages should have been sent"


class TestALostClaimRaceIsReportedAsOccupied:
    """The atomic claim's refusal must reach the client as a conflict, not a 500.

    The endpoint prechecks occupancy under its per-conversation lock, but the
    Discord session-selection path claims under a different lock entirely, so the
    precheck can lose. `set_mirror_link` catches that atomically and raises; the
    endpoint has to translate it into the SAME `conversation_occupied` 409 the
    precheck returns, so the client offers the takeover confirm it already knows
    how to show instead of surfacing an internal error.
    """

    @staticmethod
    def _prepped(tmp_path, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits",
            lambda *args, **kwargs: SimpleNamespace(permitted=True),
        )
        state = _prep(tmp_path, monkeypatch)
        state.register_channel_transport(_fake_transport("discord"))
        # Free at precheck time: this is the race, not a refusal the user should
        # have been asked about.
        state.sessions.find_mirror_sessions = MagicMock(return_value=[])
        state.sessions.clear_mirror_links_at = MagicMock(return_value=[])
        _wire_replace_mirror_owner(state.sessions)
        _wire_binding_state(state.sessions, None)
        state.sessions.set_mirror_link = MagicMock(
            side_effect=ConversationOwnershipConflict("discord conversation already resumes 1")
        )
        state.sessions.clear_mirror_link = MagicMock(return_value=True)
        return state

    @pytest.mark.asyncio
    async def test_the_refusal_becomes_a_409_with_a_confirm_offer(
        self, tmp_path, monkeypatch
    ):
        state = self._prepped(tmp_path, monkeypatch)

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "discord", "target_id": "user:123"},
            )
            status, body = resp.status, await resp.json()

        assert status == 409, f"a lost race must not surface as {status}"
        assert body["code"] == "conversation_occupied"
        assert body["requires_confirm"] is True

    @pytest.mark.asyncio
    async def test_nothing_is_delivered_into_the_conversation_it_lost(
        self, tmp_path, monkeypatch
    ):
        """The whole point of claiming before delivering: the loser posts nothing."""
        state = self._prepped(tmp_path, monkeypatch)
        transport = state.channel_transports["discord"]

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "discord", "target_id": "user:123"},
            )

        transport.send_message.assert_not_called()


class TestReconnectReclaimsBeforeDelivering:
    """A muted reconnect must own the conversation before it posts into it.

    Delivering first looked safer — a governance denial would leave the link
    untouched — but it meant a reconnect could stream this session's transcript into
    a conversation a concurrent confirmed takeover had already handed to someone
    else. Ownership is cheap to undo; a posted transcript is not. So the mute is
    lifted first and RESTORED if the catch-up is denied or raises, which keeps the
    property the old ordering was there to get.
    """

    @staticmethod
    def _prepped(tmp_path, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits",
            lambda *args, **kwargs: SimpleNamespace(permitted=True),
        )
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("discord")
        state.register_channel_transport(transport)
        link = ChannelLink("discord", channel_id="dm-1")
        _wire_binding_state(state.sessions, link)
        state.sessions.is_mirror_paused = MagicMock(return_value=True)
        state.sessions.set_mirror_link = MagicMock()
        state.sessions.set_mirror_paused = MagicMock()
        slot = state.get_or_create_slot("s1")
        slot.append("user", "what changed while I was away")
        slot.drain()
        return state, transport, link

    @pytest.mark.asyncio
    async def test_the_claim_lands_before_the_first_delivery(self, tmp_path, monkeypatch):
        state, transport, _link = self._prepped(tmp_path, monkeypatch)
        order: list[str] = []
        state.sessions.set_mirror_link.side_effect = lambda *a, **k: order.append("claim")
        original_send = transport.send_message

        async def _spy(*args, **kwargs):
            order.append("deliver")
            return await original_send(*args, **kwargs)

        transport.send_message = AsyncMock(side_effect=_spy)

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/mirror-link")
            assert resp.status == 200

        assert order, "neither the claim nor a delivery happened"
        assert order[0] == "claim", (
            f"the conversation was written into before it was reclaimed: {order}"
        )

    @pytest.mark.asyncio
    async def test_a_denied_catch_up_restores_the_mute(self, tmp_path, monkeypatch):
        """A denial must leave the binding exactly as the user left it: muted."""
        from kiro_crew.dashboard import chat_mirror

        state, _transport, link = self._prepped(tmp_path, monkeypatch)
        monkeypatch.setattr(
            chat_mirror,
            "_deliver_catch_up",
            AsyncMock(return_value=web.json_response({"error": "nope"}, status=403)),
        )

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/mirror-link")
            assert resp.status == 403

        state.sessions.set_mirror_paused.assert_called_once_with(
            "dashboard:s1", True, link.channel_type
        )

    @pytest.mark.asyncio
    async def test_a_raising_catch_up_also_restores_the_mute(self, tmp_path, monkeypatch):
        """Not just the denial path — an exception must not leave the link un-muted."""
        from kiro_crew.dashboard import chat_mirror

        state, _transport, link = self._prepped(tmp_path, monkeypatch)
        monkeypatch.setattr(
            chat_mirror, "_deliver_catch_up", AsyncMock(side_effect=RuntimeError("boom"))
        )

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/mirror-link")
            assert resp.status >= 500

        state.sessions.set_mirror_paused.assert_called_once_with(
            "dashboard:s1", True, link.channel_type
        )

    @pytest.mark.asyncio
    async def test_a_rival_owner_refuses_the_reconnect_as_occupied(
        self, tmp_path, monkeypatch
    ):
        """A conversation claimed while this one sat muted is a conflict, not a 500."""
        state, transport, _link = self._prepped(tmp_path, monkeypatch)
        state.sessions.set_mirror_link = MagicMock(
            side_effect=ConversationOwnershipConflict("taken")
        )

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/mirror-link")
            assert resp.status == 409
            assert (await resp.json())["code"] == "conversation_occupied"

        # And crucially nothing was posted into the conversation it does not own.
        transport.send_message.assert_not_called()


class TestAnUnconfirmedConnectNeverDisplacesAnyone:
    """No consent, no eviction — even if a rival appears after the precheck.

    `replace_mirror_owner` evicts whatever holds the location at claim time. That is
    correct for a CONFIRMED takeover and wrong for an ordinary connect: the precheck
    found the conversation free, so the user was never shown a confirm, and a rival
    claiming it in the window would be displaced without anyone agreeing to take
    anything. The unconfirmed path therefore uses the plain claim, whose exclusivity
    check refuses, and the refusal becomes the 409 that asks for consent.
    """

    OCCUPANT = "dashboard:other"

    @staticmethod
    def _prepped(tmp_path, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits",
            lambda *args, **kwargs: SimpleNamespace(permitted=True),
        )
        state = _prep(tmp_path, monkeypatch)
        state.register_channel_transport(_fake_transport("discord"))
        # The precheck sees a FREE conversation, so no confirm is requested.
        state.sessions.find_mirror_sessions = MagicMock(return_value=[])
        _wire_binding_state(state.sessions, None)
        state.sessions.clear_mirror_links_at = MagicMock(return_value=[])
        state.sessions.clear_mirror_link = MagicMock(return_value=True)
        _wire_replace_mirror_owner(state.sessions)
        return state

    @staticmethod
    async def _connect(state, confirm=None):
        body = {"channel_type": "discord", "target_id": "user:123"}
        if confirm is not None:
            body["confirm"] = confirm
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/mirror-link", json=body)
            return resp.status, await resp.json()

    @pytest.mark.asyncio
    async def test_it_does_not_reach_for_the_evicting_mutator_at_all(
        self, tmp_path, monkeypatch
    ):
        state = self._prepped(tmp_path, monkeypatch)

        status, _body = await self._connect(state)

        assert status == 200
        state.sessions.replace_mirror_owner.assert_not_called()
        state.sessions.clear_mirror_links_at.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_rival_that_appears_after_the_precheck_is_refused_not_evicted(
        self, tmp_path, monkeypatch
    ):
        """The race the finding described: free at precheck, taken by claim time."""
        state = self._prepped(tmp_path, monkeypatch)
        state.sessions.set_mirror_link = MagicMock(
            side_effect=ConversationOwnershipConflict("claimed while we were scheduling")
        )
        transport = state.channel_transports["discord"]

        status, body = await self._connect(state)

        assert status == 409
        assert body["code"] == "conversation_occupied"
        assert body["requires_confirm"] is True
        # Nothing was displaced, and nothing was posted into a conversation this
        # session does not own.
        state.sessions.replace_mirror_owner.assert_not_called()
        state.sessions.clear_mirror_links_at.assert_not_called()
        transport.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_confirmed_takeover_still_uses_the_atomic_evicting_claim(
        self, tmp_path, monkeypatch
    ):
        """Consent is what unlocks eviction — the feature must still work."""
        state = self._prepped(tmp_path, monkeypatch)
        held = ChannelLink(channel_type="discord", channel_id="user:123")
        state.sessions.find_mirror_sessions = MagicMock(return_value=[self.OCCUPANT])
        state.sessions.get_mirror_link = MagicMock(
            side_effect=lambda key, ct="": held if key == self.OCCUPANT else None
        )
        state.sessions.mirror_accepts_inbound = MagicMock(return_value=True)
        state.sessions.is_mirror_paused = MagicMock(return_value=False)

        status, _body = await self._connect(state, confirm=True)

        assert status == 200
        state.sessions.replace_mirror_owner.assert_called_once()
        state.sessions.clear_mirror_links_at.assert_called_once()


class TestAFailedCatchUpIsNotReportedAsSuccess:
    """A send failure during catch-up must fail the request, not be swallowed.

    The loop used to log the exception and carry on, then return success. On a
    reconnect that meant the mute was lifted and 200 returned while the missed
    history never arrived — the user asked to be caught up, was told it worked, and
    had no signal that nothing landed. On a fresh connect it meant a persisted
    binding for a conversation that had received nothing.
    """

    @staticmethod
    def _prepped(tmp_path, monkeypatch, *, muted):
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits",
            lambda *args, **kwargs: SimpleNamespace(permitted=True),
        )
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("discord")
        # Every unit fails, which is the interesting case: not even the first landed.
        transport.send_message = AsyncMock(side_effect=RuntimeError("transport down"))
        state.register_channel_transport(transport)
        link = ChannelLink("discord", channel_id="dm-1")
        _wire_binding_state(state.sessions, link if muted else None)
        state.sessions.is_mirror_paused = MagicMock(return_value=muted)
        state.sessions.find_mirror_sessions = MagicMock(return_value=[])
        state.sessions.clear_mirror_links_at = MagicMock(return_value=[])
        # `_wire_binding_state` owns set/clear_mirror_link: overriding them with
        # no-ops here stopped the claim recording, so the catch-up that follows it
        # read back "not bound" and refused with a 403 instead of reaching the send
        # failure this fixture exists to exercise.
        state.sessions.set_mirror_paused = MagicMock()
        _wire_replace_mirror_owner(state.sessions)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "history the channel missed")
        slot.drain()
        return state, transport, link

    @pytest.mark.asyncio
    async def test_a_reconnect_reports_502_and_restores_the_mute(
        self, tmp_path, monkeypatch
    ):
        state, _transport, link = self._prepped(tmp_path, monkeypatch, muted=True)

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/mirror-link")
            status, body = resp.status, await resp.json()

        assert status == 502, f"a failed catch-up was reported as {status}"
        assert body["code"] == "catch_up_delivery_failed"
        # The binding goes back to the state the user left it in: muted.
        state.sessions.set_mirror_paused.assert_called_once_with(
            "dashboard:s1", True, link.channel_type
        )

    @pytest.mark.asyncio
    async def test_a_fresh_connect_reports_502_and_releases_the_claim(
        self, tmp_path, monkeypatch
    ):
        state, transport, _link = self._prepped(tmp_path, monkeypatch, muted=False)
        # The LINK NOTICE must succeed and only the catch-up fail. Failing every send
        # made this test vacuous: the notice failed first and returned
        # `channel_link_failed`, so it passed with or without the fix and never
        # reached the catch-up at all.
        sent = {"n": 0}

        async def _first_ok(*_args, **_kwargs):
            sent["n"] += 1
            if sent["n"] == 1:
                return None
            raise RuntimeError("transport down")

        transport.send_message = AsyncMock(side_effect=_first_ok)

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "discord", "target_id": "dm-1"},
            )
            status, body = resp.status, await resp.json()

        assert status == 502, f"a failed catch-up was reported as {status}"
        assert body["code"] == "catch_up_delivery_failed"
        # The claim does not survive a connect whose history never arrived.
        state.sessions.clear_mirror_link.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_working_transport_still_reconnects(self, tmp_path, monkeypatch):
        """Non-vacuity: the new failure path must not break the ordinary reconnect."""
        state, transport, _link = self._prepped(tmp_path, monkeypatch, muted=True)
        transport.send_message = AsyncMock(return_value=None)

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/mirror-link")
            assert resp.status == 200
            assert (await resp.json())["reconnected"] is True

        state.sessions.set_mirror_paused.assert_not_called()


class TestOnlyResumingChannelsClaimInboundOwnership:
    """`accepts_inbound` is a claim about the INBOUND path, so only honour it there.

    Discord's dispatcher resolves the mirror binding for an incoming message
    (`DiscordSessionResume.resumed_session`). Telegram and the rest build a session
    key from the route alone and never consult the binding, so a reply there runs in
    its own session with none of this one's context. Setting the flag anyway was not
    a harmless over-declaration: `dashboard/state.py` derives the row's `direction`
    from it, so the dashboard promised a two-way link that silently dropped replies.
    """

    @staticmethod
    def _prepped(tmp_path, monkeypatch, channel, *, resumes):
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits",
            lambda *args, **kwargs: SimpleNamespace(permitted=True),
        )
        state = _prep(tmp_path, monkeypatch)
        state.register_channel_transport(
            _fake_transport(channel, resumes=resumes)
        )
        state.sessions.find_mirror_sessions = MagicMock(return_value=[])
        state.sessions.clear_mirror_links_at = MagicMock(return_value=[])
        state.sessions.clear_mirror_link = MagicMock(return_value=True)
        state.sessions.set_mirror_link = MagicMock()
        _wire_replace_mirror_owner(state.sessions)
        return state

    @staticmethod
    async def _connect(state, channel):
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": channel, "target_id": "user:123"},
            )
            return resp.status, await resp.json()

    @pytest.mark.asyncio
    async def test_a_resuming_channel_claims_inbound(self, tmp_path, monkeypatch):
        state = self._prepped(tmp_path, monkeypatch, "discord", resumes=True)

        status, body = await self._connect(state, "discord")

        assert status == 200
        assert state.sessions.set_mirror_link.call_args.kwargs["accepts_inbound"] is True
        assert body["direction"] == "both"

    @pytest.mark.asyncio
    async def test_a_non_resuming_channel_is_outbound_only(self, tmp_path, monkeypatch):
        state = self._prepped(tmp_path, monkeypatch, "telegram", resumes=False)

        status, body = await self._connect(state, "telegram")

        assert status == 200
        assert state.sessions.set_mirror_link.call_args.kwargs["accepts_inbound"] is False, (
            "a channel whose inbound path ignores the binding claimed to resume it"
        )
        # And the response says so, which is what the optimistic row reads.
        assert body["direction"] == "out"

    @pytest.mark.asyncio
    async def test_an_unknown_capability_shape_is_outbound_only(self, tmp_path, monkeypatch):
        """Conservative on a transport that declares nothing: never over-promise."""
        state = self._prepped(tmp_path, monkeypatch, "telegram", resumes=False)
        state.channel_transports["telegram"].capabilities = SimpleNamespace(
            supports_proactive_send=True, max_message_chars=4096
        )

        status, body = await self._connect(state, "telegram")

        assert status == 200
        assert state.sessions.set_mirror_link.call_args.kwargs["accepts_inbound"] is False
        assert body["direction"] == "out"


class TestAFailedReconnectRestoresTheInboundFlagToo:
    """The rollback must return all THREE fields, not two.

    A binding can be outbound-only even on a channel that resumes — an in-channel
    `!link` creates one without `accepts_inbound` — and the reconnect claim sets the
    flag unconditionally for such a channel. Restoring only the mute left the binding
    claiming inbound ownership it never had, so later replies in that conversation
    resumed THIS session instead of starting their own.

    Third occurrence of the same shape (link, accepts_inbound, paused), so it is
    asserted per field rather than by "the rollback ran".
    """

    @staticmethod
    def _prepped(tmp_path, monkeypatch, *, was_inbound):
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits",
            lambda *args, **kwargs: SimpleNamespace(permitted=True),
        )
        state = _prep(tmp_path, monkeypatch)
        # A resuming channel, so the claim really does set the flag.
        transport = _fake_transport("discord", resumes=True)
        transport.send_message = AsyncMock(side_effect=RuntimeError("transport down"))
        state.register_channel_transport(transport)
        link = ChannelLink("discord", channel_id="dm-1")
        _wire_binding_state(state.sessions, link)
        state.sessions.is_mirror_paused = MagicMock(return_value=True)
        state.sessions.mirror_accepts_inbound = MagicMock(return_value=was_inbound)
        state.sessions.find_mirror_sessions = MagicMock(return_value=[])
        state.sessions.set_mirror_link = MagicMock()
        state.sessions.set_mirror_paused = MagicMock()
        _wire_replace_mirror_owner(state.sessions)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "history the channel missed")
        slot.drain()
        return state, link

    @pytest.mark.asyncio
    async def test_an_outbound_only_binding_does_not_gain_inbound(
        self, tmp_path, monkeypatch
    ):
        state, link = self._prepped(tmp_path, monkeypatch, was_inbound=False)

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/mirror-link")
            assert resp.status == 502

        restores = [
            call for call in state.sessions.set_mirror_link.call_args_list
            if call.kwargs.get("accepts_inbound") is False
        ]
        assert restores, (
            "the failed reconnect left accepts_inbound=True on a binding that was "
            f"outbound-only: {state.sessions.set_mirror_link.call_args_list}"
        )
        # And the mute came back, in that order (the rebind drops it).
        state.sessions.set_mirror_paused.assert_called_once_with(
            "dashboard:s1", True, link.channel_type
        )

    @pytest.mark.asyncio
    async def test_a_binding_that_had_inbound_keeps_it(self, tmp_path, monkeypatch):
        """Non-vacuity: the restore must not strip a flag the binding really had."""
        state, _link = self._prepped(tmp_path, monkeypatch, was_inbound=True)

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/mirror-link")
            assert resp.status == 502

        assert not [
            call for call in state.sessions.set_mirror_link.call_args_list
            if call.kwargs.get("accepts_inbound") is False
        ], "the restore stripped inbound from a binding that had it"

    @pytest.mark.asyncio
    async def test_an_unreadable_flag_restores_outbound_only(self, tmp_path, monkeypatch):
        """Unknown must not be read as "had inbound".

        Restoring False on a binding that did have it costs a resume the user can
        re-establish; restoring True on one that did not silently hijacks replies.
        """
        state, _link = self._prepped(tmp_path, monkeypatch, was_inbound=False)
        state.sessions.mirror_accepts_inbound = MagicMock(
            side_effect=RuntimeError("unreadable")
        )

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/mirror-link")
            assert resp.status == 502

        assert [
            call for call in state.sessions.set_mirror_link.call_args_list
            if call.kwargs.get("accepts_inbound") is False
        ]


class TestCancellationStillRollsTheBindingBack:
    """`CancelledError` is a BaseException, so `except Exception` never saw it.

    A gateway shutdown that cancels the turn after the claim has persisted used to
    skip both rollbacks: the reconnect left the link un-muted, and the fresh connect
    left the evicted owner evicted while this session held a conversation it never
    delivered into. Both are states a restart loads as if they were intended.
    """

    @staticmethod
    def _prepped(tmp_path, monkeypatch, *, muted):
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits",
            lambda *args, **kwargs: SimpleNamespace(permitted=True),
        )
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("discord", resumes=True)
        state.register_channel_transport(transport)
        link = ChannelLink("discord", channel_id="dm-1")
        _wire_binding_state(state.sessions, link if muted else None)
        state.sessions.is_mirror_paused = MagicMock(return_value=muted)
        state.sessions.mirror_accepts_inbound = MagicMock(return_value=True)
        state.sessions.find_mirror_sessions = MagicMock(return_value=[])
        state.sessions.clear_mirror_links_at = MagicMock(return_value=[])
        state.sessions.clear_mirror_link = MagicMock(return_value=True)
        state.sessions.set_mirror_link = MagicMock()
        state.sessions.set_mirror_paused = MagicMock()
        _wire_replace_mirror_owner(state.sessions)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "history the channel missed")
        slot.drain()
        return state, transport, link

    @pytest.mark.asyncio
    async def test_a_cancelled_reconnect_restores_the_mute(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import chat_mirror

        state, _transport, link = self._prepped(tmp_path, monkeypatch, muted=True)
        monkeypatch.setattr(
            chat_mirror,
            "_deliver_catch_up",
            AsyncMock(side_effect=asyncio.CancelledError()),
        )

        with pytest.raises(asyncio.CancelledError):
            await chat_mirror._reconnect_muted(
                state, state.get_or_create_slot("s1"), "dashboard:s1", link,
                state.channel_transports["discord"],
            )
        # The shielded rollback runs as its own task; let it finish.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        state.sessions.set_mirror_paused.assert_called_once_with(
            "dashboard:s1", True, link.channel_type
        )

    @pytest.mark.asyncio
    async def test_a_cancelled_connect_releases_the_claim(self, tmp_path, monkeypatch):
        state, transport, _link = self._prepped(tmp_path, monkeypatch, muted=False)
        transport.send_message = AsyncMock(side_effect=asyncio.CancelledError())

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            with pytest.raises(Exception):
                await client.post(
                    "/api/chat/slots/s1/mirror-link",
                    json={"channel_type": "discord", "target_id": "dm-1"},
                )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert state.sessions.clear_mirror_link.called, (
            "a cancelled connect kept its claim on the conversation"
        )

    def test_both_post_claim_paths_name_CancelledError(self):
        """Structural backstop: `except Exception` silently omits it.

        Cheap to lose again — someone tidying the handlers would not see a test fail
        unless the omission itself is asserted, and the runtime cost of the bug is a
        binding that a restart reads as intentional.

        Asserted as an EQUALITY rather than a threshold. A `>= 2` floor went stale
        the moment a third post-claim await was shielded: deleting one shield still
        left two, so the probe that removes a shield passed. Tying the two counts
        together means a new cancellation handler without a shielded rollback fails
        no matter how many already exist.
        """
        import inspect

        from kiro_crew.dashboard import chat_mirror

        src = inspect.getsource(chat_mirror)
        handlers = src.count("except asyncio.CancelledError:")
        shields = src.count("asyncio.shield(asyncio.ensure_future(")
        assert handlers >= 3, (
            f"only {handlers} post-claim paths catch cancellation — one stopped, so "
            "a shutdown there leaves the binding half-changed"
        )
        assert shields == handlers, (
            f"{handlers} cancellation handlers but {shields} shielded rollbacks: one "
            "awaits its rollback unshielded, and in an already-cancelled task that "
            "raises at once and abandons it halfway"
        )


class TestTheEvictionNoticeAsksPolicyAgain:
    """The notice is posted after a whole catch-up, so its authorization is stale.

    `live_transport` is authorized before `_deliver_catch_up`, which is a sequence
    of sends long. If the policy narrows in there, the catch-up correctly stops and
    fails closed — but the notice then posted into the same conversation on the
    strength of the earlier decision. Suppressing it strands nobody: the connect has
    already delivered, the claim is confirmed, and the notice was best-effort.
    """

    NOTICE = "A different session is connected here now."
    OCCUPANT = "dashboard:other"

    @staticmethod
    def _prepped(tmp_path, monkeypatch, *, permits):
        """`permits` is consulted per call, so it can narrow mid-request."""
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits",
            lambda *args, **kwargs: SimpleNamespace(permitted=permits()),
        )
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("discord")
        state.register_channel_transport(transport)
        # An occupant exists and the user confirmed the takeover, so the notice path
        # is the one under test.
        state.sessions.find_mirror_sessions = MagicMock(
            return_value=[TestTheEvictionNoticeAsksPolicyAgain.OCCUPANT]
        )
        _wire_binding_state(state.sessions, None)
        state.sessions.clear_mirror_links_at = MagicMock(return_value=[])
        state.sessions.clear_mirror_link = MagicMock(return_value=True)
        _wire_replace_mirror_owner(state.sessions)
        return state, transport

    @staticmethod
    async def _connect(state):
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "discord", "target_id": "user:123", "confirm": True},
            )
            return resp.status, await resp.json()

    @pytest.mark.asyncio
    async def test_the_notice_is_posted_when_policy_still_permits(
        self, tmp_path, monkeypatch
    ):
        state, transport = self._prepped(tmp_path, monkeypatch, permits=lambda: True)

        status, _body = await self._connect(state)

        assert status == 200
        posted = [c.args[1] for c in transport.send_message.await_args_list]
        assert any(self.NOTICE in text for text in posted), (
            f"whoever is reading there was never told the session changed: {posted}"
        )

    @pytest.mark.asyncio
    async def test_the_notice_is_suppressed_once_policy_narrows(
        self, tmp_path, monkeypatch
    ):
        # Narrow the policy INSIDE the catch-up rather than counting governance
        # calls: counting made the very first resolve deny, so the connect 403'd at
        # the gate and the negative assertion below passed on a request that never
        # reached the notice at all.
        narrowed = {"yet": False}
        state, transport = self._prepped(
            tmp_path, monkeypatch, permits=lambda: not narrowed["yet"]
        )

        async def _catch_up_then_narrow(*_args, **_kwargs):
            narrowed["yet"] = True
            return None  # the catch-up itself succeeded

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_mirror._deliver_catch_up", _catch_up_then_narrow
        )

        status, body = await self._connect(state)

        posted = [c.args[1] for c in transport.send_message.await_args_list]
        # A negative assertion passes for free if the notice path was never reached,
        # so prove it WAS: the connect succeeded and its initial delivery went out.
        assert status == 200, f"the connect never got as far as the notice: {body}"
        assert posted, "nothing was delivered at all — the notice path never ran"
        assert not any(self.NOTICE in text for text in posted), (
            "the eviction notice posted into a conversation the newest decision "
            f"forbids: {posted}"
        )


class TestACancelledTakeoverCatchUpReleasesTheClaim:
    """The catch-up is the LONGER of the two post-claim awaits, and was unshielded.

    `TestCancellationStillRollsTheBindingBack` covers a cancel in the initial
    delivery. The catch-up runs after it and replays however much history the
    conversation missed, so a shutdown is likelier to land there — and the takeover
    would commit with the prior owner evicted and nobody having received anything.
    """

    OCCUPANT = "dashboard:other"

    @pytest.mark.asyncio
    async def test_a_cancel_during_the_catch_up_does_not_leave_the_takeover_committed(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.dashboard import chat_mirror

        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits",
            lambda *args, **kwargs: SimpleNamespace(permitted=True),
        )
        state = _prep(tmp_path, monkeypatch)
        state.register_channel_transport(_fake_transport("discord", resumes=True))
        _wire_binding_state(state.sessions, None)
        state.sessions.is_mirror_paused = MagicMock(return_value=False)
        state.sessions.find_mirror_sessions = MagicMock(return_value=[self.OCCUPANT])
        state.sessions.clear_mirror_links_at = MagicMock(return_value=[])
        state.sessions.clear_mirror_link = MagicMock(return_value=True)
        _wire_replace_mirror_owner(state.sessions)
        # The initial delivery succeeds; the cancel lands in the catch-up.
        monkeypatch.setattr(
            chat_mirror,
            "_deliver_catch_up",
            AsyncMock(side_effect=asyncio.CancelledError()),
        )

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            with pytest.raises(Exception):
                await client.post(
                    "/api/chat/slots/s1/mirror-link",
                    json={
                        "channel_type": "discord",
                        "target_id": "dm-1",
                        "confirm": True,
                    },
                )
        # The shielded rollback runs as its own task; let it finish.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert state.sessions.clear_mirror_link.called, (
            "a takeover cancelled inside the catch-up kept its claim, so the prior "
            "owner stays evicted and a restart reads the half-done takeover as "
            "intended"
        )


class TestAnUnlinkDuringTheCatchUpStopsIt:
    """The catch-up re-asks the BINDING, not only the policy.

    It re-resolved governance per unit, which answers "is this channel permitted" and
    nothing about whether the session still holds the binding. An in-channel `!unlink`
    mid-replay therefore kept pasting transcript history into a conversation the
    session had detached from. It does NOT re-ask the mute: this is the operation that
    lifts the mute, so asking would refuse every reconnect.
    """

    @pytest.mark.asyncio
    async def test_it_stops_replaying_once_the_binding_is_gone(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits",
            lambda *args, **kwargs: SimpleNamespace(permitted=True),
        )
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("discord")
        state.register_channel_transport(transport)
        slot = state.get_or_create_slot("s1")
        for i in range(6):
            slot.append("user", f"missed message {i}")
        slot.drain()

        # The claim records the binding; the unlink lands after the first unit.
        real_get = state.sessions.get_mirror_link
        reads = {"n": 0}

        def _unlinked_after_one(key, channel_type=""):
            answer = real_get(key, channel_type)
            if answer is not None:
                reads["n"] += 1
                if reads["n"] > 2:
                    return None
            return answer

        state.sessions.get_mirror_link = MagicMock(side_effect=_unlinked_after_one)

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "discord", "target_id": "user:123"},
            )

        assert resp.status != 200, (
            "an unlink mid-catch-up was reported as a successful connect, so the UI "
            "shows a link the session no longer holds"
        )
        # The initial delivery goes out; the replay must not run to completion.
        assert transport.send_message.await_count < 6, (
            "the catch-up kept replaying history into a conversation the session had "
            f"detached from: {transport.send_message.await_count} sends"
        )


class TestAReconnectRollbackDoesNotResurrectAnUnlink:
    """The reconnect's rollback re-mutes the binding it un-muted — unless it is gone.

    An in-channel `!unlink` during the catch-up REMOVES that binding, and putting the
    link and its mute back would resurrect something the user deleted on purpose.

    Asserted on the SOURCE, not through the endpoint. I could not reach
    `_restore_binding` with an injected catch-up failure in two attempts — the handler
    answers before the rollback for the failures it is possible to inject there — so a
    behavioural test here passed whether the guard existed or not. Saying that plainly
    is better than shipping a test that looks behavioural and proves nothing. The
    equivalent protection inside `SessionMap` IS covered behaviourally, in
    `test_session_map_mirror.py::TestTheUndoDoesNotResurrectARemovedBinding`.
    """

    def test_the_rollback_checks_the_binding_before_restoring_it(self):
        import inspect

        from kiro_crew.dashboard import chat_mirror

        src = inspect.getsource(chat_mirror)
        # Pinned to the GUARD, not to `get_mirror_link` appearing somewhere in the
        # module: it appears in a dozen unrelated places.
        assert "                if current != link:\n" in src, (
            "the reconnect rollback no longer checks that the binding it is about to "
            "restore is still the one it un-muted, so an unlink mid-catch-up gets "
            "overruled by the failure path"
        )
        # And that the check precedes the writes it guards.
        guard = src.index("if current != link:")
        restore = src.index("state.sessions.set_mirror_link,\n                        session_key")
        assert guard < restore, (
            "the binding check sits AFTER the restore it is supposed to gate"
        )
