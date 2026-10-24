"""Tests for the channel-neutral mirror-link / mirror-unlink endpoints (C3).

This file pins the mirror-link request contract (validation, the proactive-capability gate,
configured-target resolution, governance and the recipient check), the Discord DM mirror of
a dashboard session, mirror-unlink and its stale-row guard, reminder mode, mirror-pause and
the channel-target listing. The mirror-link edge cases (``TestMirrorLinkEdgeCases``), the
non-Slack backfill with the claim ordering and rollback around it, and the inbound claim
that follows the transport's capability are pinned in ``test_chat_mirror_link_delivery.py``.
``_prep``, ``_fake_transport``, ``_real_caps_transport``, ``_mirror_gate_capabilities`` and
``_make_mirror_app`` live in ``chat_mirror_helpers.py``; ``test_cross_surface_mirror.py``
imports ``NON_PROACTIVE_CHANNELS``, ``_channels_declaring_proactive`` and
``_real_caps_transport`` from this module.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_mirror_helpers import (
    _fake_transport,
    _make_mirror_app,
    _mirror_gate_capabilities,
    _prep,
    _real_caps_transport,
)

from kiro_crew.discord.client import DiscordClient as _RealDiscordClient
from kiro_crew.messaging.link import ChannelLink, binding_token

#: Channels whose REAL capabilities refuse a proactive send, so the mirror-link
#: gate must reject them. Pinned as a set as well as derived below, so unlocking
#: one is a NAMED failure in ``test_the_proactive_split_matches_the_shipped_caps``
#: rather than a parametrized suite that quietly stops driving anything.
#:
#: Membership tracks a capability declaration, not a policy: WeCom left when it
#: gained a proactive path over its long connection, and Feishu arrived declaring
#: ``supports_proactive_send=False`` because its v1 renderer only ever replies to
#: an inbound ``message_id``. The rejection branch below is nonetheless driven by a
#: SYNTHETIC capability rather than by whoever happens to be in this set: the set
#: has been empty before and will be again, and a branch whose only coverage is a
#: roster entry stops being covered the moment that entry graduates.
NON_PROACTIVE_CHANNELS: set[str] = {"feishu"}


def _channels_declaring_proactive(supported: bool) -> list[str]:
    """Channel types whose shipped capabilities declare (or refuse) proactive send."""
    return sorted(
        name
        for name, caps in _mirror_gate_capabilities().items()
        if caps.supports_proactive_send is supported
    )


def _caps_transport(channel_type: str, **overrides):
    """A fake transport whose capabilities are SYNTHETIC, for a branch no shipped
    channel exercises any more.

    Deliberately separate from :func:`_real_caps_transport`: that one exists so the
    gate is pinned against the real declaration and an unlock is observable, and
    this one must not be reachable from it. Every shipped channel now declares
    ``supports_proactive_send=True``, so the refusing branch has no real subject —
    but the gate still has to refuse, and this is the only honest way to say so.
    """
    from kiro_crew.messaging.transport import TransportCapabilities

    return _fake_transport(channel_type, capabilities=TransportCapabilities(**overrides))


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
        # return 403 and NOT persist the mirror link: a denial mid-delivery must
        # fail closed, not persist the link and return 200.
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
        # The claim is taken BEFORE the announcement (an inbound reply arriving in
        # the delivery window would otherwise run in the channel's native
        # session), so the invariant is not "never written" but "does not
        # SURVIVE": the denial releases it.
        assert state.sessions.get_mirror_link("dashboard:s1") is None
        state = _prep(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/nope/mirror-link",
                json={"channel_type": "telegram", "conversation_id": "1"},
            )
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_missing_channel_type(self, tmp_path, monkeypatch):
        # An empty JSON object is reminder mode with nothing to remind, so this
        # lands on the reminder path's own required-field refusal — the twin of
        # the explicit-body one, and it must answer with the same code.
        state = _prep(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/mirror-link", json={})
            assert resp.status == 400
            assert (await resp.json())["code"] == "channel_type_required"

    @pytest.mark.asyncio
    async def test_slack_rejected(self, tmp_path, monkeypatch):
        state = _prep(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "slack", "conversation_id": "C1"},
            )
            assert resp.status == 400
            body = await resp.json()
            assert "slack-link" in body["error"]
            # Slack is not unsupported, it is handled elsewhere; the code has to
            # say which, because that is the only part a localized client reads.
            assert body["code"] == "use_slack_link"

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

    def test_the_proactive_split_matches_the_shipped_caps(self):
        """The two suites below are only as honest as this roster.

        Derived from the shipped objects, so a channel that starts or stops
        declaring ``supports_proactive_send`` fails HERE, named, instead of
        migrating between the suites without a word. The acceptance half is
        checked for non-vacuity too: an empty parametrize list is a green test
        that drives nothing.
        """
        assert _channels_declaring_proactive(False) == sorted(NON_PROACTIVE_CHANNELS), (
            "A channel's supports_proactive_send declaration changed. The mirror-link "
            "gate rejects exactly the non-proactive channels, so update "
            "NON_PROACTIVE_CHANNELS — and check the endpoint's docstring, which names "
            "the channels it can and cannot mirror. "
            f"newly_locked={set(_channels_declaring_proactive(False)) - NON_PROACTIVE_CHANNELS} "
            f"newly_unlocked={NON_PROACTIVE_CHANNELS - set(_channels_declaring_proactive(False))}"
        )
        assert _channels_declaring_proactive(True), (
            "no shipped channel declares supports_proactive_send=True, so the "
            "acceptance suite drives nothing"
        )

    @pytest.mark.asyncio
    async def test_non_proactive_channel_rejected(self, tmp_path, monkeypatch):
        """The gate must refuse a transport that declares no proactive send.

        Driven by a SYNTHETIC capability rather than by a shipped channel that
        declares ``False``, so this branch keeps its coverage across a roster that
        churns: ``NON_PROACTIVE_CHANNELS`` has been empty before, and a test
        parametrized over it would have gone vacuously green instead of red. The
        real-capability version is `test_proactive_channel_accepted`, which covers
        every shipped channel; this one keeps the refusing branch alive, because a
        mirror binding on a transport that cannot send unattended promises a
        delivery it will never make.
        """
        channel = "synthetic"
        state = _prep(tmp_path, monkeypatch)
        state.register_channel_transport(_caps_transport(channel, supports_proactive_send=False))
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": channel, "target_id": "user:u1"},
            )
            assert resp.status == 400
            body = await resp.json()
            assert "proactive" in body["error"]
            # The dashboard renders `error` verbatim into a localized UI, so the
            # machine contract is the code, and the code is what a client that
            # cannot read English has to switch on.
            assert body["code"] == "channel_not_proactive"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("channel", _channels_declaring_proactive(True))
    async def test_proactive_channel_accepted(self, tmp_path, monkeypatch, channel):
        """The positive counterpart, and what makes a future unlock observable.

        Weixin ships ``supports_proactive_send=True``, so it must LINK. Without
        this half the suite only ever proved the gate says no, and a gate that
        refuses everything would pass it — including the day WeCom's declaration
        flips and the endpoint is supposed to start accepting it.
        """
        state = _prep(tmp_path, monkeypatch)
        state.register_channel_transport(_real_caps_transport(channel))
        state.sessions.set_mirror_link = MagicMock()
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": channel, "target_id": "user:123"},
            )
            assert resp.status == 200
            assert (await resp.json())["ok"] is True
        link = state.sessions.set_mirror_link.call_args.args[1]
        assert link == ChannelLink(channel, channel_id="123", thread_id=None)

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
    async def test_an_explicit_link_withdraws_the_automatic_mirroring_opt_out(
        self, tmp_path, monkeypatch
    ):
        """An explicit bind is explicit intent, so it clears a standing refusal.

        A channel that re-asserts its own conversation every turn (Telegram)
        declines while the flag is set. Leaving it set would make this endpoint
        write a binding the channel then refuses to honour — the user is looking
        at a link they made, and the chat stays silent.
        """
        state = _prep(tmp_path, monkeypatch)
        state.register_channel_transport(_fake_transport("telegram"))
        state.sessions.set_mirror_link = MagicMock()
        state.sessions.set_mirror_opt_out = MagicMock()
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "telegram", "target_id": "user:123"},
            )
            assert resp.status == 200
        state.sessions.set_mirror_opt_out.assert_called_once()
        assert state.sessions.set_mirror_opt_out.call_args.args[1] is False

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

    @pytest.mark.asyncio
    async def test_an_allow_listed_telegram_dm_links(self, tmp_path, monkeypatch):
        """The dashboard-direction repro, on the REAL transport's authorization.

        The permissive ``_fake_transport`` cannot catch this: its ``may_send_to``
        answers True for anything, while Telegram's real one tests the id against
        a roster of BARE user ids. A pre-check that hands it the configured
        -target spelling (``user:123``) can never match, so it refuses a
        recipient the user explicitly allow-listed.
        """
        from kiro_crew.telegram.transport import TelegramTransport

        client = MagicMock()
        client.send_message = AsyncMock(return_value=7)
        transport = TelegramTransport(client, allowed_user_ids=[123])
        state = _prep(tmp_path, monkeypatch)
        state.register_channel_transport(transport)
        state.sessions.set_mirror_link = MagicMock()
        async with TestClient(TestServer(_make_mirror_app(state))) as http:
            resp = await http.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "telegram", "target_id": "user:123"},
            )
            assert resp.status == 200
            assert (await resp.json())["conversation_id"] == "123"
        link = state.sessions.set_mirror_link.call_args.args[1]
        assert link == ChannelLink("telegram", channel_id="123", thread_id=None)

    @pytest.mark.asyncio
    async def test_a_non_allow_listed_telegram_target_is_still_refused(self, tmp_path, monkeypatch):
        """The companion negative, on the same real transport: the fix must not
        have widened anything. An id absent from ``allowed_user_ids`` is refused
        by ``resolve_configured_target`` (409), and the denial is SEL-audited."""
        from kiro_crew.telegram.transport import TelegramTransport

        recorded: list[dict[str, Any]] = []
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_mirror.sel",
            lambda: type("S", (), {"log_api_access": lambda self, **kw: recorded.append(kw)})(),
        )
        client = MagicMock()
        client.send_message = AsyncMock(return_value=7)
        transport = TelegramTransport(client, allowed_user_ids=[123])
        state = _prep(tmp_path, monkeypatch)
        state.register_channel_transport(transport)
        state.sessions.set_mirror_link = MagicMock()
        async with TestClient(TestServer(_make_mirror_app(state))) as http:
            resp = await http.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "telegram", "target_id": "user:999"},
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "configured_target_unavailable"
        state.sessions.set_mirror_link.assert_not_called()
        client.send_message.assert_not_awaited()
        assert ("chat.mirror_target_resolve", "denied") in [
            (kw["operation"], kw["outcome"]) for kw in recorded
        ]

    @pytest.mark.asyncio
    async def test_a_recipient_refusal_after_resolution_is_403_and_audited(
        self, tmp_path, monkeypatch
    ):
        """Moving the recipient leg after the resolve must not delete it.

        A transport whose resolver still yields a conversation while its roster
        refuses the recipient (a roster narrowed between the two, or a resolver
        that does not itself consult it) must be refused with the same 403
        contract, with nothing sent, nothing persisted, and the denial on the
        SEL trail — an unaudited authz denial is a security-contract regression.
        The audit is recorded by the shared ``_authorize_recipient`` helper in
        ``chat_runner``, which is why the seam patched here is that module's.
        """
        recorded: list[dict[str, Any]] = []
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.sel",
            lambda: type("S", (), {"log_api_access": lambda self, **kw: recorded.append(kw)})(),
        )
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("telegram")
        transport.may_send_to = lambda conversation_id, thread_id=None, principal="": False
        state.register_channel_transport(transport)
        state.sessions.set_mirror_link = MagicMock()
        async with TestClient(TestServer(_make_mirror_app(state))) as http:
            resp = await http.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "telegram", "target_id": "user:123"},
            )
            assert resp.status == 403
            assert (await resp.json())["code"] == "channel_not_permitted"
        state.sessions.set_mirror_link.assert_not_called()
        transport.send_message.assert_not_awaited()
        assert ("channel.proactive_send_authorize", "denied") in [
            (kw["operation"], kw["outcome"]) for kw in recorded
        ]

    @pytest.mark.asyncio
    async def test_a_raising_recipient_check_fails_closed(self, tmp_path, monkeypatch):
        """An allow-list check that errored has authorized nobody (egress boundary)."""

        def _boom(conversation_id, thread_id=None, principal=""):
            raise RuntimeError("roster unavailable")

        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("telegram")
        transport.may_send_to = _boom
        state.register_channel_transport(transport)
        state.sessions.set_mirror_link = MagicMock()
        async with TestClient(TestServer(_make_mirror_app(state))) as http:
            resp = await http.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "telegram", "target_id": "user:123"},
            )
            assert resp.status == 403
            assert (await resp.json())["code"] == "channel_not_permitted"
        state.sessions.set_mirror_link.assert_not_called()
        transport.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_recipient_check_receives_the_resolved_id_and_principal(
        self, tmp_path, monkeypatch
    ):
        """The re-decision runs against the RESOLVED conversation id, with the
        principal taken from the target spelling — the posture
        ``handlers/messaging._deliver_channel_dm`` already established for a
        ``user:<id>``-shaped target. That is what lets a transport whose
        conversation id is opaque (Discord's DM channel id) reach its roster.
        The decision is audited on the ALLOWED outcome too, like the resolver
        audit beside it: both are authorization decisions at an egress boundary.
        Recorded by the shared ``_authorize_recipient`` helper in ``chat_runner``,
        hence the patched seam."""
        recorded: list[dict[str, Any]] = []
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.sel",
            lambda: type("S", (), {"log_api_access": lambda self, **kw: recorded.append(kw)})(),
        )
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("discord")
        transport.resolve_configured_target = AsyncMock(return_value=("dm-chan-9", None))
        calls: list[tuple[str, str | None, str]] = []

        def _may_send_to(conversation_id, thread_id=None, *, principal=""):
            calls.append((conversation_id, thread_id, principal))
            return True

        transport.may_send_to = _may_send_to
        state.register_channel_transport(transport)
        state.sessions.set_mirror_link = MagicMock()
        async with TestClient(TestServer(_make_mirror_app(state))) as http:
            resp = await http.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "discord", "target_id": "user:42"},
            )
            assert resp.status == 200
        assert ("dm-chan-9", None, "42") in calls
        # And no call ever saw the unresolved configured-target spelling.
        assert all(not cid.startswith("user:") for cid, _, _ in calls)
        assert ("channel.proactive_send_authorize", "allowed") in [
            (kw["operation"], kw["outcome"]) for kw in recorded
        ]


class _FakeDiscordClient(_RealDiscordClient):
    """The real client with only its NETWORK edges replaced.

    ``_api`` answers the one route the mirror-link handler reaches -- ``POST
    /users/@me/channels``, Discord's own statement of which DM channel belongs to a
    user -- from ``dm_for_user``; ``send_message`` records instead of posting;
    ``is_thread_channel`` says yes. Everything that matters here is inherited and
    real: ``create_dm_channel`` leaving the ``dm_channel_id -> user_id`` pairing
    behind and ``cached_dm_recipient`` reading it back. A hand-written pairing
    store would test the fake, not the fix.
    """

    def __init__(self) -> None:
        super().__init__(token="bot-secret")
        self.dm_for_user: dict[str, str] = {"42": "dm-chan-9", "55": "dm-chan-55"}
        self.sent: list[tuple[str, str]] = []

    async def _api(self, method, path, payload, timeout=30, **kwargs):  # type: ignore[override]
        if method == "POST" and path == "/users/@me/channels":
            channel = self.dm_for_user.get(str((payload or {}).get("recipient_id", "")))
            return {"id": channel} if channel else None
        raise AssertionError(f"unexpected REST call {method} {path}")

    async def send_message(self, channel_id, text, **kwargs):  # type: ignore[override]
        self.sent.append((channel_id, text))
        return "mid-1"

    async def is_thread_channel(self, channel_id: str) -> bool:
        return True

    def forget_pairings(self) -> None:
        """What a gateway restart does to the in-process pairing store."""
        self._dm_recipients.clear()


class TestADiscordDmMirrorOfADashboardSession:
    """The dashboard menu link to a Discord DM, end to end through the REAL transport.

    A dashboard-born slot's session key (``dashboard:chat-<n>-<ts>``) names no
    peer, and Discord's DM channel id is unrelated to the user snowflake its roster
    holds. So the link handler's own post-claim ladder recheck, and every later
    dashboard-driven delivery, could only pass the recipient leg with a principal
    from somewhere else: the record the gateway writes when it admits the mirror --
    the peer, in the same write as the conversation id, under a MAC only the
    gateway can mint. The session store here is the shared double, which signs a
    principal-bearing row exactly as ``SessionMap.set_mirror_link`` does.
    """

    _SLOT = "chat-1-1700000000"
    _KEY = f"dashboard:{_SLOT}"

    @staticmethod
    def _discord():
        from kiro_crew.discord.transport import DiscordTransport

        return DiscordTransport(
            _FakeDiscordClient(), allowed_user_ids=["42", "55"], allowed_thread_ids=["thr-1"]
        )

    def _state(self, tmp_path, monkeypatch, transport):
        state = _prep(tmp_path, monkeypatch)
        state.get_or_create_slot(self._SLOT)
        state.register_channel_transport(transport)
        return state

    async def _link(self, state, target_id: str) -> None:
        async with TestClient(TestServer(_make_mirror_app(state))) as http:
            resp = await http.post(
                f"/api/chat/slots/{self._SLOT}/mirror-link",
                json={"channel_type": "discord", "target_id": target_id},
            )
            assert resp.status == 200, await resp.text()

    @staticmethod
    def _plant(state, link: ChannelLink) -> None:
        """Put *link* on the row the way a WRITE TO THE FILE would -- bypassing the
        map's writer and its signing -- so the ladder reads it back verbatim."""
        state.sessions.get_mirror_link = MagicMock(return_value=link)

    @pytest.mark.asyncio
    async def test_the_link_lands_records_the_peer_and_resolves_for_delivery(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.dashboard.chat_runner import _resolve_mirror_target
        from kiro_crew.mirror_admission import verify_mirror_admission

        transport = self._discord()
        state = self._state(tmp_path, monkeypatch, transport)
        await self._link(state, "user:42")
        stored = state.sessions.get_mirror_link(self._KEY)
        assert stored == ChannelLink("discord", channel_id="dm-chan-9")
        assert stored.principal == "42"
        assert verify_mirror_admission(self._KEY, stored) is True
        # Resolving the target opened the DM, so the client pairs the channel with
        # 42 and the handler's own recheck admitted the peer: the notice landed.
        assert transport.client.cached_dm_recipient("dm-chan-9") == "42"
        assert transport.client.sent and transport.client.sent[0][0] == "dm-chan-9"
        # And so does every later dashboard-driven reply, cron result or
        # completion notice: the persisted link resolves through the ladder.
        assert _resolve_mirror_target(state, self._KEY) == (stored, transport)

    @pytest.mark.asyncio
    async def test_delivery_survives_a_restart_with_no_inbound_dm(self, tmp_path, monkeypatch):
        """The pairing is in-process and a restart forgets it; the admitted record is
        on disk and verifies, so the very next dashboard reply is delivered with
        no message from the peer and nothing re-linked."""
        from kiro_crew.dashboard.chat_runner import _resolve_mirror_target

        transport = self._discord()
        state = self._state(tmp_path, monkeypatch, transport)
        await self._link(state, "user:42")
        stored = state.sessions.get_mirror_link(self._KEY)
        transport.client.forget_pairings()
        assert transport.direct_peer_of("dm-chan-9") == ""
        assert _resolve_mirror_target(state, self._KEY) == (stored, transport)

    @pytest.mark.asyncio
    async def test_a_peer_revoked_after_linking_is_refused_at_delivery(self, tmp_path, monkeypatch):
        """The record names the peer; the LIVE roster still judges each send."""
        from kiro_crew.dashboard.chat_runner import _resolve_mirror_target

        transport = self._discord()
        state = self._state(tmp_path, monkeypatch, transport)
        await self._link(state, "user:42")
        transport._allowed = frozenset({"99"})
        assert _resolve_mirror_target(state, self._KEY) is None

    @pytest.mark.asyncio
    async def test_a_row_rewritten_to_another_allowed_users_dm_is_refused(
        self, tmp_path, monkeypatch
    ):
        """The session map is writable by in-sandbox code. A consistent rewrite of the
        row -- another allow-listed user's DM channel AND that user's name, keeping
        the admission the gateway minted for the original location -- does not
        verify, so this session's replies cannot be aimed at that user."""
        import dataclasses

        from kiro_crew.dashboard.chat_runner import _resolve_mirror_target

        transport = self._discord()
        state = self._state(tmp_path, monkeypatch, transport)
        await self._link(state, "user:42")
        stored = state.sessions.get_mirror_link(self._KEY)
        transport.client.remember_dm_recipient("dm-chan-55", "55")  # 55 has written before
        self._plant(state, dataclasses.replace(stored, channel_id="dm-chan-55", principal="55"))
        assert _resolve_mirror_target(state, self._KEY) is None

    @pytest.mark.asyncio
    async def test_a_row_pointed_at_a_revoked_users_dm_is_refused(self, tmp_path, monkeypatch):
        """The other rewrite: the revoked user's DM channel with the allow-listed
        principal kept. The admission covers the channel id, so it fails; and the
        transport's own pairing of that channel with the revoked user refuses it a
        second time."""
        import dataclasses

        from kiro_crew.dashboard.chat_runner import _resolve_mirror_target

        transport = self._discord()
        state = self._state(tmp_path, monkeypatch, transport)
        await self._link(state, "user:42")
        stored = state.sessions.get_mirror_link(self._KEY)
        transport.client.remember_dm_recipient("dm-chan-revoked", "77")
        self._plant(state, dataclasses.replace(stored, channel_id="dm-chan-revoked"))
        assert _resolve_mirror_target(state, self._KEY) is None
        # Unsigned entirely -- a row written by anything but the gateway -- is
        # refused the same way, whatever the transport knows.
        self._plant(state, ChannelLink("discord", channel_id="dm-chan-9", principal="42"))
        assert _resolve_mirror_target(state, self._KEY) is None

    @pytest.mark.asyncio
    async def test_a_thread_target_records_no_peer(self, tmp_path, monkeypatch):
        """A room audience names nobody; the thread roster answers that route."""
        from kiro_crew.dashboard.chat_runner import _resolve_mirror_target

        transport = self._discord()
        state = self._state(tmp_path, monkeypatch, transport)
        await self._link(state, "thread:thr-1")
        stored = state.sessions.get_mirror_link(self._KEY)
        assert stored == ChannelLink("discord", channel_id="thr-1")
        assert stored.principal is None and stored.admission is None
        assert _resolve_mirror_target(state, self._KEY) == (stored, transport)

    @pytest.mark.parametrize("prior_is_genuine", [True, False], ids=["genuine", "forged"])
    @pytest.mark.asyncio
    async def test_a_failed_relink_restores_the_prior_row_without_laundering_it(
        self, tmp_path, monkeypatch, prior_is_genuine
    ):
        """A link that fails after its claim puts the previous binding back. That
        restore re-sets a row read from the agent-writable store, so it may carry the
        prior peer only under an admission that verifies: a genuine prior row comes
        back byte-for-byte and keeps delivering, a planted one comes back as its
        location alone and stays refused. Nothing is minted on the way."""
        import dataclasses

        from kiro_crew.dashboard.chat_runner import _resolve_mirror_target
        from kiro_crew.mirror_admission import sign_mirror_admission

        transport = self._discord()
        state = self._state(tmp_path, monkeypatch, transport)
        prior = ChannelLink("discord", channel_id="dm-chan-9", principal="42")
        prior = dataclasses.replace(
            prior,
            admission=sign_mirror_admission(self._KEY, prior) if prior_is_genuine else "f" * 64,
        )
        state.sessions.set_mirror_link(self._KEY, prior)  # as the store holds it

        async def _refuse(channel_id, text, **kwargs):
            raise RuntimeError("channel unavailable")

        transport.client.send_message = _refuse  # type: ignore[method-assign]
        async with TestClient(TestServer(_make_mirror_app(state))) as http:
            resp = await http.post(
                f"/api/chat/slots/{self._SLOT}/mirror-link",
                json={"channel_type": "discord", "target_id": "user:55"},
            )
            assert resp.status == 502, await resp.text()
        restored = state.sessions.get_mirror_link(self._KEY)
        assert restored == ChannelLink("discord", channel_id="dm-chan-9")
        if prior_is_genuine:
            assert restored.principal == "42" and restored.admission == prior.admission
            assert _resolve_mirror_target(state, self._KEY) == (restored, transport)
        else:
            assert restored.principal is None and restored.admission is None
            assert _resolve_mirror_target(state, self._KEY) is None

    @pytest.mark.asyncio
    async def test_a_stale_requests_rollback_leaves_a_refreshed_relink_in_place(
        self, tmp_path, monkeypatch
    ):
        """Two link requests for the same DM overlap: the first claims, then fails its
        test message; while it is failing, the signing key rotates and a second request
        re-links the same session to the same conversation under a fresh admission.
        The first request's rollback owns only its OWN row. A location match would
        call the refreshed row its own, clear it and put the obsolete state back, after
        which every reply refuses; whole-row identity leaves the newer row alone and
        it keeps delivering under the rotated key.
        """
        import dataclasses
        import secrets

        from kiro_crew.dashboard import token_secret
        from kiro_crew.dashboard.chat_runner import _resolve_mirror_target
        from kiro_crew.mirror_admission import sign_mirror_admission, verify_mirror_admission

        transport = self._discord()
        state = self._state(tmp_path, monkeypatch, transport)
        refreshed: dict[str, ChannelLink] = {}
        rotated = secrets.token_bytes(32)

        async def _fail_after_a_relink(channel_id, text, **kwargs):
            # The concurrent re-link: the key rotates, and the same session is bound
            # to the same DM again with an admission minted under the new key.
            monkeypatch.setattr(token_secret, "_get_secret", lambda: rotated)
            row = ChannelLink("discord", channel_id="dm-chan-9", principal="42")
            row = dataclasses.replace(row, admission=sign_mirror_admission(self._KEY, row))
            state.sessions.set_mirror_link(self._KEY, row, accepts_inbound=True)
            refreshed["row"] = row
            raise RuntimeError("channel unavailable")

        transport.client.send_message = _fail_after_a_relink  # type: ignore[method-assign]
        async with TestClient(TestServer(_make_mirror_app(state))) as http:
            resp = await http.post(
                f"/api/chat/slots/{self._SLOT}/mirror-link",
                json={"channel_type": "discord", "target_id": "user:42"},
            )
            assert resp.status == 502, await resp.text()
        current = state.sessions.get_mirror_link(self._KEY)
        assert current is not None, "the stale rollback cleared the newer binding"
        assert current.admission == refreshed["row"].admission and current.principal == "42"
        assert verify_mirror_admission(self._KEY, current) is True
        assert self._KEY in state.sessions.find_mirror_sessions(current, inbound_only=True)
        assert _resolve_mirror_target(state, self._KEY) == (current, transport)


class TestMirrorUnlink:
    @pytest.mark.asyncio
    async def test_slot_not_found(self, tmp_path, monkeypatch):
        state = _prep(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post("/api/chat/slots/nope/mirror-unlink")
            assert resp.status == 404

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "was_linked",
        [
            pytest.param(True, id="test_unlink_success"),
            pytest.param(False, id="test_unlink_noop"),
        ],
    )
    async def test_unlink_reports_whether_a_link_was_cleared(
        self, tmp_path, monkeypatch, was_linked
    ):
        state = _prep(tmp_path, monkeypatch)
        state.sessions.clear_mirror_link = MagicMock(return_value=was_linked)
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/mirror-unlink")
            assert resp.status == 200
            assert (await resp.json())["was_linked"] is was_linked
        state.sessions.clear_mirror_link.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_stale_row_cannot_unlink_a_replacement_binding(self, tmp_path, monkeypatch):
        """The menu names the binding it believes it is severing; a mismatch clears nothing.

        Two tabs: one misses a slots push while its socket reconnects and still
        shows the Discord row; the other rebinds the slot to Telegram. The first
        tab's Unlink click must not delete the Telegram binding it never saw --
        the answer is 409 ``mirror_changed`` and the store is untouched. The
        same for a replacement on the SAME channel whose id shares the row's
        redacted tail: the token is a digest of the whole binding, so a row drawn
        from ``777001`` never names ``999777001``.
        """
        state = _prep(tmp_path, monkeypatch)
        state.sessions.set_mirror_link(
            "dashboard:s1", ChannelLink(channel_type="telegram", channel_id="999777001")
        )
        state.sessions.clear_mirror_link = MagicMock(return_value=True)
        stale_rows = (
            ChannelLink(channel_type="discord", channel_id="dm-chan-9"),
            ChannelLink(channel_type="telegram", channel_id="777001"),
        )
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            for stale in stale_rows:
                resp = await client.post(
                    "/api/chat/slots/s1/mirror-unlink",
                    json={
                        "channel_type": stale.channel_type,
                        "binding": binding_token(stale),
                    },
                )
                assert resp.status == 409, stale
                body = await resp.json()
                assert body["code"] == "mirror_changed"
                assert body["error"] == (
                    "the session's linked channel changed; nothing was unlinked"
                )
        state.sessions.clear_mirror_link.assert_not_called()
        state.push_slots_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_matching_row_unlinks_and_a_namespaced_id_still_matches(
        self, tmp_path, monkeypatch
    ):
        """The compare spells the row exactly as the slots projection does.

        A Discord id can arrive namespaced (``discord:<id>``) in the store while the
        row's token was minted from the bare id; the token strips the namespace
        the same way, so a row drawn from the current binding always passes.
        """
        state = _prep(tmp_path, monkeypatch)
        state.sessions.set_mirror_link(
            "dashboard:s1", ChannelLink(channel_type="discord", channel_id="discord:dm-chan-9")
        )
        row_token = binding_token(
            ChannelLink(channel_type="discord", channel_id="dm-chan-9"),
            state.sessions.mirror_link_nonce("dashboard:s1"),
        )
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-unlink",
                json={"channel_type": "Discord", "binding": row_token},
            )
            assert resp.status == 200
            assert (await resp.json())["was_linked"] is True
        # Through the map's compare-and-clear, never a separate clear.
        state.sessions.clear_mirror_link_if.assert_called_once()
        state.sessions.clear_mirror_link.assert_not_called()
        assert state.sessions.get_mirror_link("dashboard:s1") is None

    @pytest.mark.asyncio
    async def test_a_row_from_before_an_unlink_cannot_unlink_the_same_target_relinked(
        self, tmp_path, monkeypatch
    ):
        """A binding recreated to the SAME target is a new binding, not the old row.

        The ABA the coordinates alone cannot see: tab A draws the Discord row and
        its Unlink request is delayed; meanwhile tab B unlinks and reconnects the
        very same DM. A's request arrives naming a binding that is byte-identical
        in channel, id and thread -- and must still be refused, because the
        binding it names is gone and the one standing was never on A's screen.
        The token digests the binding's own persisted nonce, minted on every
        create or target change and dropped with the binding, so unlink ->
        reconnect the same target yields a token the old row never carried.
        """
        state = _prep(tmp_path, monkeypatch)
        target = ChannelLink(channel_type="discord", channel_id="dm-chan-9")
        state.sessions.set_mirror_link("dashboard:s1", target)
        old_token = binding_token(target, state.sessions.mirror_link_nonce("dashboard:s1"))
        # Tab B: unlink, then reconnect the same target. Real clear -- the nonce
        # has to go with the binding for the recreation to mint a new one.
        assert state.sessions.clear_mirror_link("dashboard:s1") is True
        state.sessions.set_mirror_link("dashboard:s1", target)
        new_token = binding_token(target, state.sessions.mirror_link_nonce("dashboard:s1"))
        assert new_token != old_token
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            # Tab A's delayed request: refused, the new binding untouched.
            resp = await client.post(
                "/api/chat/slots/s1/mirror-unlink",
                json={"channel_type": "discord", "binding": old_token},
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "mirror_changed"
            assert state.sessions.get_mirror_link("dashboard:s1") == target
            state.push_slots_update.assert_not_called()
            # A row drawn from the new binding unlinks it.
            resp = await client.post(
                "/api/chat/slots/s1/mirror-unlink",
                json={"channel_type": "discord", "binding": new_token},
            )
            assert resp.status == 200
            assert (await resp.json())["was_linked"] is True
        assert state.sessions.get_mirror_link("dashboard:s1") is None

    @pytest.mark.asyncio
    async def test_a_body_that_names_a_row_is_judged_whole_and_no_body_clears(
        self, tmp_path, monkeypatch
    ):
        state = _prep(tmp_path, monkeypatch)
        current = ChannelLink(channel_type="telegram", channel_id="777001")
        state.sessions.set_mirror_link("dashboard:s1", current)
        state.sessions.clear_mirror_link = MagicMock(return_value=True)
        current_token = binding_token(current, state.sessions.mirror_link_nonce("dashboard:s1"))
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            # The right channel with no token: the caller tried to name a row and
            # failed, so the compare is armed with a token nothing matches --
            # refused, never the unconditional clear.
            resp = await client.post(
                "/api/chat/slots/s1/mirror-unlink", json={"channel_type": "telegram"}
            )
            assert resp.status == 409
            # The right token under the wrong channel: refused too.
            resp = await client.post(
                "/api/chat/slots/s1/mirror-unlink",
                json={"channel_type": "discord", "binding": current_token},
            )
            assert resp.status == 409
            state.sessions.clear_mirror_link.assert_not_called()
            # No binding left, and a row that names one: refused, not "unlinked".
            # The binding really goes (the route reads nothing itself; the map's
            # compare-and-clear is what answers), so the double's clear runs.
            state.sessions.set_mirror_link("dashboard:s1", None)
            resp = await client.post(
                "/api/chat/slots/s1/mirror-unlink",
                json={"channel_type": "telegram", "binding": current_token},
            )
            assert resp.status == 409
            # No body at all: the unconditional clear, for callers with no row.
            resp = await client.post("/api/chat/slots/s1/mirror-unlink")
            assert resp.status == 200
        state.sessions.clear_mirror_link.assert_called_once()

    @pytest.mark.asyncio
    async def test_unlink_names_the_dashboard_as_the_reason(self, tmp_path, monkeypatch):
        """The audit has to say which surface cleared the binding.

        A dashboard click is invisible to the bound channel, so it is the reason
        the notice exists for — an unattributed clear would land in the trail as
        ``unspecified`` and read as a path nobody threaded.
        """
        from kiro_crew.messaging.link import UNBIND_REASON_DASHBOARD_UNLINK

        state = _prep(tmp_path, monkeypatch)
        state.sessions.clear_mirror_link = MagicMock(return_value=True)
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/mirror-unlink")
            assert resp.status == 200

        assert state.sessions.clear_mirror_link.call_args.kwargs["reason"] == (
            UNBIND_REASON_DASHBOARD_UNLINK
        )

    @pytest.mark.asyncio
    async def test_a_garbled_body_is_refused_and_clears_nothing(self, tmp_path, monkeypatch):
        """Only an EMPTY body is "no body"; a body that is present but garbled is 400.

        Reading unparseable JSON as "no body" would hand a caller that tried to
        name a row -- and failed -- the unconditional clear, the one answer the
        stale-row guard exists to withhold from a caller with a row in hand. So
        malformed JSON and a JSON value that is not an object are both refused
        with ``invalid_body`` and the binding stands; an empty body keeps the
        unconditional clear for callers that have no row.
        """
        state = _prep(tmp_path, monkeypatch)
        state.sessions.set_mirror_link(
            "dashboard:s1", ChannelLink(channel_type="telegram", channel_id="999777001")
        )
        headers = {"Content-Type": "application/json"}
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            for garbled in ('{"channel_type": "telegram", ', "[]", '"telegram"', "42"):
                resp = await client.post(
                    "/api/chat/slots/s1/mirror-unlink", data=garbled, headers=headers
                )
                assert resp.status == 400, garbled
                body = await resp.json()
                assert body["code"] == "invalid_body"
                assert state.sessions.get_mirror_link("dashboard:s1") is not None, garbled
            state.push_slots_update.assert_not_called()
            # The empty body is the documented unconditional clear, unchanged.
            resp = await client.post("/api/chat/slots/s1/mirror-unlink")
            assert resp.status == 200
            assert (await resp.json()) == {"ok": True, "was_linked": True}
        assert state.sessions.get_mirror_link("dashboard:s1") is None

    @pytest.mark.asyncio
    async def test_an_undecodable_body_is_refused_like_a_garbled_one(self, tmp_path, monkeypatch):
        """Bytes that do not decode, or an unknown charset, are the garbled body: 400.

        ``request.text()`` raises ``UnicodeDecodeError`` on invalid UTF-8 and
        ``LookupError`` on a ``charset=`` Python does not know. Both raise before
        any read or mutation, so the guard's fail-closed answer already holds;
        letting them escape answered a 500 where the 400 the guard has built for
        exactly this caller belongs. The binding stands and nothing is pushed.
        """
        state = _prep(tmp_path, monkeypatch)
        state.sessions.set_mirror_link(
            "dashboard:s1", ChannelLink(channel_type="telegram", channel_id="999777001")
        )
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            for data, headers in (
                (b"\xff\xfe{", {"Content-Type": "application/json; charset=utf-8"}),
                (
                    b'{"channel_type": "telegram"}',
                    {"Content-Type": "application/json; charset=zzq-no-such"},
                ),
            ):
                resp = await client.post(
                    "/api/chat/slots/s1/mirror-unlink", data=data, headers=headers
                )
                assert resp.status == 400, headers
                assert (await resp.json())["code"] == "invalid_body"
                assert state.sessions.get_mirror_link("dashboard:s1") is not None, headers
            state.push_slots_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_link_reports_a_conversation_claimed_mid_flight(self, tmp_path, monkeypatch):
        """The genuine race: the precheck passed, then someone else claimed it.

        Reported as the same 409 conflict rather than a 500, so the client offers
        "unlink there first" instead of inviting a retry of a request that is
        behaving correctly.
        """
        from kiro_crew.session_map import ConversationOwnershipConflict

        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("telegram")
        state.register_channel_transport(transport)
        state.sessions.mirror_claim_blockers = MagicMock(return_value=[])
        state.sessions.set_mirror_link = MagicMock(
            side_effect=ConversationOwnershipConflict("claimed")
        )
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "telegram", "target_id": "user:123"},
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "conversation_occupied"


class TestMirrorReminder:
    @pytest.mark.asyncio
    async def test_existing_live_mirror_posts_reminder(self, tmp_path, monkeypatch):
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
            resp = await client.post("/api/chat/slots/s1/mirror-link")
            assert resp.status == 200
            assert await resp.json() == {
                "ok": True,
                "already_linked": True,
                "channel_type": "discord",
            }

        transport.send_message.assert_awaited_once_with(
            "356163505868767244",
            "🔗 Session linked from dashboard — continuing here.",
            thread_id=None,
        )

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
            body = await resp.json()
            assert body["error"] == "channel_type required"
            assert body["code"] == "channel_type_required"

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
    @pytest.mark.parametrize(
        "body,content_type",
        [
            pytest.param(
                b"\xff\xfe\x00bad",
                "application/json",
                id="test_invalid_utf8_body_is_400_not_500",
            ),
            pytest.param(
                b'{"channel_type":"discord"}',
                "application/json; charset=nosuchcharset",
                id="test_unknown_charset_is_400_not_500",
            ),
        ],
    )
    async def test_undecodable_body_is_400_not_500(self, tmp_path, monkeypatch, body, content_type):
        """A body that cannot be decoded is a client error, not a traceback."""
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("discord")
        state.register_channel_transport(transport)

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                data=body,
                headers={"Content-Type": content_type},
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


class TestMirrorPause:
    """Tests for the mirror-pause endpoint (api_chat_slot_mirror_pause)."""

    @pytest.fixture
    def mirror_pause_app(self):
        from kiro_crew.dashboard.chat_mirror import api_chat_slot_mirror_pause

        def _build(state):
            app = web.Application()
            app["state"] = state
            app.router.add_post("/api/chat/slots/{name}/mirror-pause", api_chat_slot_mirror_pause)
            return app

        return _build

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "slot_id,status,code",
        [
            pytest.param("ghost", 404, "slot_not_found", id="test_slot_not_found_returns_404"),
            # get_mirror_link returns None → no explicit mirror, and session key is
            # not a channel key → not origin-connected either.
            pytest.param("s1", 409, "mirror_not_linked", id="test_not_linked_returns_409"),
        ],
    )
    async def test_pause_is_refused_without_a_mirror(
        self, tmp_path, monkeypatch, mirror_pause_app, slot_id, status, code
    ):
        state = _prep(tmp_path, monkeypatch)
        async with TestClient(TestServer(mirror_pause_app(state))) as client:
            resp = await client.post(
                f"/api/chat/slots/{slot_id}/mirror-pause", json={"paused": True}
            )
            assert resp.status == status
            assert (await resp.json())["code"] == code

    @pytest.mark.asyncio
    async def test_pause_explicit_mirror_link(self, tmp_path, monkeypatch, mirror_pause_app):
        """Pausing an explicit mirror on a linked session returns ok."""
        state = _prep(tmp_path, monkeypatch)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="123")
        )
        state.sessions.set_mirror_paused = MagicMock(return_value=False)
        async with TestClient(TestServer(mirror_pause_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/mirror-pause", json={"paused": True})
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert data["paused"] is True
            assert data["was_paused"] is False

    @pytest.mark.asyncio
    async def test_resume_explicit_mirror_link(self, tmp_path, monkeypatch, mirror_pause_app):
        """Resuming (paused=false) an explicit mirror."""
        state = _prep(tmp_path, monkeypatch)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="123")
        )
        state.sessions.set_mirror_paused = MagicMock(return_value=True)
        async with TestClient(TestServer(mirror_pause_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/mirror-pause", json={"paused": False})
            assert resp.status == 200
            data = await resp.json()
            assert data["paused"] is False
            assert data["was_paused"] is True

    @pytest.mark.asyncio
    async def test_pause_sends_disconnect_note_when_governance_permits(
        self, tmp_path, monkeypatch, mirror_pause_app
    ):
        """The disconnect note is sent when governance allows it."""
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits",
            lambda *args, **kwargs: SimpleNamespace(permitted=True),
        )
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("telegram")
        state.register_channel_transport(transport)
        link = ChannelLink("telegram", channel_id="456", thread_id="t1")
        state.sessions.get_mirror_link = MagicMock(return_value=link)
        state.sessions.set_mirror_paused = MagicMock(return_value=False)

        async with TestClient(TestServer(mirror_pause_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/mirror-pause", json={"paused": True})
            assert resp.status == 200

        # The disconnect note was delivered to the mirror channel.
        transport.send_message.assert_awaited_once()
        call_args = transport.send_message.await_args
        assert call_args.args[0] == "456"
        assert "Disconnected" in call_args.args[1]
        assert call_args.kwargs["thread_id"] == "t1"

    @pytest.mark.asyncio
    async def test_pause_skips_note_when_governance_denies(
        self, tmp_path, monkeypatch, mirror_pause_app
    ):
        """No disconnect note when governance denies the send."""
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits",
            lambda *args, **kwargs: SimpleNamespace(permitted=False),
        )
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("telegram")
        state.register_channel_transport(transport)
        link = ChannelLink("telegram", channel_id="456")
        state.sessions.get_mirror_link = MagicMock(return_value=link)
        state.sessions.set_mirror_paused = MagicMock(return_value=False)

        async with TestClient(TestServer(mirror_pause_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/mirror-pause", json={"paused": True})
            assert resp.status == 200

        transport.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pause_skips_note_for_origin_disconnect(
        self, tmp_path, monkeypatch, mirror_pause_app
    ):
        """Origin disconnect must not send a note to the explicit mirror."""
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits",
            lambda *args, **kwargs: SimpleNamespace(permitted=True),
        )
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("telegram")
        state.register_channel_transport(transport)
        link = ChannelLink("telegram", channel_id="456")
        state.sessions.get_mirror_link = MagicMock(return_value=link)
        state.sessions.set_mirror_paused = MagicMock(return_value=False)
        # Make this an origin slot by giving it a channel session key.
        slot = state.get_or_create_slot("s1")
        slot.linked_session_key = "telegram:conv123"

        async with TestClient(TestServer(mirror_pause_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-pause", json={"paused": True, "origin": True}
            )
            assert resp.status == 200

        transport.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pause_noop_when_already_paused(self, tmp_path, monkeypatch, mirror_pause_app):
        """No disconnect note when already paused (not a transition)."""
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits",
            lambda *args, **kwargs: SimpleNamespace(permitted=True),
        )
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("telegram")
        state.register_channel_transport(transport)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="456")
        )
        state.sessions.set_mirror_paused = MagicMock(return_value=True)

        async with TestClient(TestServer(mirror_pause_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/mirror-pause", json={"paused": True})
            assert resp.status == 200
            data = await resp.json()
            assert data["was_paused"] is True

        # No disconnect note because it was already paused (not a transition).
        transport.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_disconnect_note_delivery_failure_is_silent(
        self, tmp_path, monkeypatch, mirror_pause_app
    ):
        """Disconnect note delivery failure does not affect the response."""
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits",
            lambda *args, **kwargs: SimpleNamespace(permitted=True),
        )
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("telegram")
        transport.send_message = AsyncMock(side_effect=RuntimeError("network"))
        state.register_channel_transport(transport)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="456")
        )
        state.sessions.set_mirror_paused = MagicMock(return_value=False)

        async with TestClient(TestServer(mirror_pause_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/mirror-pause", json={"paused": True})
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_invalid_body_defaults_to_pause(self, tmp_path, monkeypatch, mirror_pause_app):
        """Non-JSON or non-dict body defaults to paused=True."""
        state = _prep(tmp_path, monkeypatch)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="123")
        )
        state.sessions.set_mirror_paused = MagicMock(return_value=False)
        async with TestClient(TestServer(mirror_pause_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-pause",
                data=b"not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 200
            assert (await resp.json())["paused"] is True


class TestChannelTargetsSlackEnumeration:
    """Cover the Slack channel enumeration in api_channel_targets."""

    @pytest.mark.asyncio
    async def test_slack_channels_are_listed(self, tmp_path, monkeypatch):
        """When slack_client and owner_id are present, Slack channels appear."""
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_mirror.list_slack_channels",
            AsyncMock(
                return_value=[
                    {"id": "C001", "name": "general"},
                    {"id": "C002", "name": "random"},
                ]
            ),
        )
        state = _prep(tmp_path, monkeypatch)
        state.slack_client = MagicMock()
        state.owner_id = "U123"
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.get("/api/chat/channel-targets")
            assert resp.status == 200
            data = await resp.json()
            slack_targets = [t for t in data if t["channel_type"] == "slack"]
            assert len(slack_targets) == 2
            assert slack_targets[0]["target_id"] == "C001"
            assert slack_targets[0]["label"] == "Slack · general"

    @pytest.mark.asyncio
    async def test_slack_enumeration_failure_is_silent(self, tmp_path, monkeypatch):
        """Slack failure does not prevent other targets from listing."""
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_mirror.list_slack_channels",
            AsyncMock(side_effect=RuntimeError("slack down")),
        )
        state = _prep(tmp_path, monkeypatch)
        state.slack_client = MagicMock()
        state.owner_id = "U123"
        state.register_channel_transport(_fake_transport("telegram"))
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.get("/api/chat/channel-targets")
            assert resp.status == 200
            data = await resp.json()
            # Slack targets are absent, but telegram is listed
            assert all(t["channel_type"] != "slack" for t in data)
            assert any(t["channel_type"] == "telegram" for t in data)

    @pytest.mark.asyncio
    async def test_transport_enumeration_failure_is_silent(self, tmp_path, monkeypatch):
        """A transport that throws on configured_targets is skipped."""
        state = _prep(tmp_path, monkeypatch)
        broken_transport = _fake_transport("broken_channel")
        broken_transport.configured_targets = MagicMock(side_effect=RuntimeError("boom"))
        state.register_channel_transport(broken_transport)
        state.register_channel_transport(_fake_transport("telegram"))
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.get("/api/chat/channel-targets")
            assert resp.status == 200
            data = await resp.json()
            # The broken transport is skipped but telegram still appears
            assert any(t["channel_type"] == "telegram" for t in data)
            assert all(t["channel_type"] != "broken_channel" for t in data)

    @pytest.mark.asyncio
    async def test_slack_channel_with_empty_id_is_skipped(self, tmp_path, monkeypatch):
        """Slack channels with no id are excluded."""
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_mirror.list_slack_channels",
            AsyncMock(
                return_value=[
                    {"id": "", "name": "phantom"},
                    {"id": "C003", "name": "real"},
                ]
            ),
        )
        state = _prep(tmp_path, monkeypatch)
        state.slack_client = MagicMock()
        state.owner_id = "U123"
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.get("/api/chat/channel-targets")
            assert resp.status == 200
            data = await resp.json()
            slack_targets = [t for t in data if t["channel_type"] == "slack"]
            assert len(slack_targets) == 1
            assert slack_targets[0]["target_id"] == "C003"
