"""``send_message`` reaching Discord: the advertised contract and the delivery.

Slack DM parity for Discord has three halves, and each one is a separate way to
ship a tool that looks right and delivers nothing:

* the ADVERTISEMENT -- the ``session`` enum, the argument validator that runs
  before the handler (a value the enum offers and the validator rejects is
  advertised and unreachable), and a description that says what the value does;
* the TOOL -- which transport the ``channels`` governance vet is asked about, and
  the refusal of Slack-only options that have no meaning off Slack;
* the ROUTE -- resolving the configured owner's DM through the channel-neutral
  transport contract, passing the same fail-closed egress gate an ordinary
  Discord message passes, and telling the caller which surface actually took the
  message.
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import api_send_message
from kiro_crew.discord.client import DISCORD_CHUNK_LIMIT
from kiro_crew.discord.transport import DiscordTransport
from kiro_crew.mcp_core import _call_tool
from kiro_crew.mcp_tools.messaging import (
    _CHANNEL_SESSIONS,
    _SESSION_TARGETS,
    _SLACK_ONLY_FIELDS,
    schemas,
)
from kiro_crew.messaging.transport import ConfiguredChannelTarget, TransportCapabilities
from kiro_crew.platform.governance import Decision
from kiro_crew.validation import SEND_MESSAGE_SCHEMA, ValidationError, validate_tool_args

CRON_CALLER = "cron:abc123"
OWNER_ID = "424242424242424242"
DM_CHANNEL = "999000111222333444"


def _descriptor() -> dict:
    return next(spec for spec in schemas() if spec["name"] == "send_message")


# ── The advertisement ──


def test_session_enum_offers_discord() -> None:
    """The model cannot ask for a surface the enum does not name."""
    enum = _descriptor()["inputSchema"]["properties"]["session"]["enum"]
    assert "discord" in enum
    assert enum == list(_SESSION_TARGETS)


@pytest.mark.parametrize("value", _SESSION_TARGETS)
def test_argument_validator_accepts_every_advertised_session(value: str) -> None:
    """The pre-handler validator runs first: it must accept the whole enum.

    ``validation.SEND_MESSAGE_SCHEMA`` spells the accepted values as a regex of
    its own, so a value added to the enum alone is advertised to the model and
    then rejected as malformed before the handler sees it.
    """
    cleaned = validate_tool_args({"text": "hi", "session": value}, SEND_MESSAGE_SCHEMA)
    assert cleaned["session"] == value


@pytest.mark.parametrize("value", ["unified", "pigeon", "webex:99887766", "Webex"])
def test_argument_validator_still_rejects_an_unadvertised_session(value: str) -> None:
    """The roster is derived, so the negative control must not be a real channel.

    This case previously used ``"telegram"``, which #6514 made legal: the roster
    now derives from ``CHANNEL_SESSION_NAMESPACES``, so every registered channel
    is advertised. The values here are the ones that must STAY refused —
    ``unified`` is the session-key bucket rather than a transport and is excluded
    on purpose, and the rest are a bare unknown name, a namespaced session key
    where a transport is expected, and a case variant.
    """
    with pytest.raises(ValidationError):
        validate_tool_args({"text": "hi", "session": value}, SEND_MESSAGE_SCHEMA)


def test_description_states_what_discord_does_and_what_it_refuses() -> None:
    """The description is the only place an LLM learns the routing rules."""
    description = _descriptor()["description"]
    assert "discord" in description
    assert "REFUSED" in description
    session_doc = _descriptor()["inputSchema"]["properties"]["session"]["description"]
    assert "discord" in session_doc


# ── The tool ──


@pytest.fixture
def cron_caller():
    """Run the tool as a cron, the caller whose bare sends default to Slack."""
    with patch.dict("os.environ", {"KIROCREW_SESSION_KEY": CRON_CALLER}):
        yield


def test_discord_session_reaches_the_gateway(cron_caller) -> None:
    with patch("kiro_crew.mcp_core._post") as post:
        post.return_value = {"ok": True, "delivered_to": "discord"}
        result = _call_tool("send_message", {"text": "hi", "session": "discord"})
    assert post.call_args[0][1]["session"] == "discord"
    assert "discord" in result


def test_discord_session_is_vetted_on_discord_and_not_on_slack(cron_caller) -> None:
    """The cron default routes to Slack; asking for Discord must not vet Slack.

    A Slack-denying ``channels`` policy would otherwise block a Discord DM that
    never touches Slack.
    """
    with (
        patch("kiro_crew.mcp_core._post") as post,
        patch("kiro_crew.mcp_core._vet_channel_governance", return_value=None) as vet,
    ):
        post.return_value = {"ok": True, "delivered_to": "discord"}
        _call_tool("send_message", {"text": "hi", "session": "discord"})
    assert [call.args[1] for call in vet.call_args_list] == ["discord"]


def test_slack_session_is_still_vetted_on_slack(cron_caller) -> None:
    with (
        patch("kiro_crew.mcp_core._post") as post,
        patch("kiro_crew.mcp_core._vet_channel_governance", return_value=None) as vet,
    ):
        post.return_value = {"ok": True, "delivered_to": "slack"}
        _call_tool("send_message", {"text": "hi", "session": "slack"})
    assert [call.args[1] for call in vet.call_args_list] == ["slack"]


def test_governance_denial_stops_the_discord_send(cron_caller) -> None:
    with (
        patch("kiro_crew.mcp_core._post") as post,
        patch(
            "kiro_crew.mcp_core._vet_channel_governance",
            return_value="messaging via transport 'discord' blocked by governance policy",
        ),
    ):
        result = _call_tool("send_message", {"text": "hi", "session": "discord"})
    post.assert_not_called()
    assert result.startswith("Error:")
    assert "discord" in result


@pytest.mark.parametrize(
    "field,value",
    [
        ("channel", "C0123ABC456"),
        ("user", "U0123ABC456"),
        ("blocks", [{"type": "divider"}]),
        ("thread_ts", "1712793600.123456"),
        ("reply_broadcast", True),
        ("unfurl_links", False),
        ("unfurl_media", False),
    ],
)
def test_slack_only_option_with_discord_is_refused(cron_caller, field: str, value) -> None:
    """Refused, not delivered with the option dropped: the caller cannot see a drop."""
    with patch("kiro_crew.mcp_core._post") as post:
        result = _call_tool("send_message", {"text": "hi", "session": "discord", field: value})
    post.assert_not_called()
    assert result.startswith("Error:")
    assert field in result


@pytest.mark.parametrize(
    "field,value",
    [
        ("channel", "C0123ABC456"),
        ("user", "U0123ABC456"),
        ("blocks", [{"type": "divider"}]),
        ("thread_ts", "1712793600.123456"),
        ("reply_broadcast", True),
        ("unfurl_links", False),
        ("unfurl_media", False),
        ("session", "discord"),
    ],
)
def test_channel_target_with_a_slack_routing_field_is_refused(cron_caller, field, value) -> None:
    """The pair addresses the destination directly, so a Slack-routing field
    travelling with it reaches nothing.

    Refused at the argument validator rather than dropped at the handler's early
    return: the pair wins the routing, and a silently discarded ``channel`` or
    ``thread_ts`` would let the caller read a private Webex DM as a threaded post
    to a named Slack channel. Nothing is posted.
    """
    with patch("kiro_crew.mcp_core._post") as post:
        result = _call_tool(
            "send_message",
            {"text": "hi", "channel_type": "webex", "target_id": "user:a@b.com", field: value},
        )
    post.assert_not_called()
    assert result.startswith("Error:")
    assert field in result


def test_a_channel_addressed_send_does_not_claim_a_dm_or_notification(cron_caller) -> None:
    """The addressed leg posts only to the named target and publishes no
    dashboard notification, and the target may be a room, so the success string
    must not borrow the DM leg's "DM + notification" claim.

    Runs with an injected identity (``cron_caller``) because the addressed leg now
    REQUIRES a strictly-resolved one; the refusal itself is pinned below.
    """
    with patch(
        "kiro_crew.mcp_core._post", return_value={"ok": True, "delivered_to": "webex", "parts": 1}
    ):
        result = _call_tool(
            "send_message",
            {"text": "hi", "channel_type": "webex", "target_id": "room:Y2lzY29z"},
        )
    assert "DM" not in result and "notification" not in result
    assert "webex" in result and "room:Y2lzY29z" in result


def test_an_addressed_send_is_refused_without_a_verifiable_identity() -> None:
    """The forwarded key becomes the governance SUBJECT at the egress chokepoint.

    The lenient resolver walks process ancestors, so a sub-agent whose identity was
    not injected resolves to its PARENT — forwarding that would hand the child the
    parent's channel permissions. Strict resolution accepts only the
    gateway-injected key or an HMAC-verified pid, and a channel-ADDRESSED send has
    no benign default to fall back to (the gateway would vet it as the host), so it
    is refused rather than sent under a borrowed identity. Nothing is posted.
    """
    with patch.dict("os.environ", {}, clear=True), patch("kiro_crew.mcp_core._post") as post:
        result = _call_tool(
            "send_message",
            {"text": "hi", "channel_type": "webex", "target_id": "room:Y2lzY29z"},
        )
    post.assert_not_called()
    assert result.startswith("Error:")
    assert "cannot verify caller identity" in result


def test_a_bare_send_still_works_without_a_verifiable_identity() -> None:
    """Non-vacuity: only the ADDRESSED leg is refused.

    A bare send has a benign destination (the dashboard notification), so refusing
    it would break the default path for every unidentified caller.
    """
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("kiro_crew.mcp_core._post", return_value={"ok": True}) as post,
    ):
        result = _call_tool("send_message", {"text": "hi"})
    post.assert_called_once()
    assert not result.startswith("Error:")


def test_every_slack_only_field_is_covered_by_the_refusal() -> None:
    """The refusal list is the whole Slack option set, so none is silently kept.

    ``channel_type`` and ``target_id`` are excluded alongside the core fields
    because together they are the OTHER destination mechanism, not Slack protocol
    options: ``channel_type`` selects a non-Slack transport and ``target_id``
    narrows it to one configured destination on that transport, and combining
    either with a Slack routing field is refused by its own guard rather than
    dropped. A genuinely new Slack option added to the schema still has to join
    ``_SLACK_ONLY_FIELDS`` or this fails, which is the point.
    """
    properties = set(_descriptor()["inputSchema"]["properties"])
    core = {"text", "title", "session", "channel_type", "target_id"}
    assert set(_SLACK_ONLY_FIELDS) == properties - core


def test_notification_only_fallback_warns_that_discord_missed(cron_caller) -> None:
    """A notification is not a DM; a success string here would hide the miss."""
    with patch("kiro_crew.mcp_core._post") as post:
        post.return_value = {"ok": True, "delivered_to": "notification"}
        result = _call_tool("send_message", {"text": "hi", "session": "discord"})
    assert "⚠️" in result
    assert "discord" in result


def test_channel_sessions_are_the_non_slack_surfaces() -> None:
    """Guards the routing predicate: a reserved value must never read as a channel."""
    assert "discord" in _CHANNEL_SESSIONS
    assert not {"origin", "slack"} & set(_CHANNEL_SESSIONS)


# ── The route ──


def _discord_transport(
    *,
    users: tuple[str, ...] = (OWNER_ID,),
    threads: tuple[str, ...] = (),
) -> DiscordTransport:
    """A real ``DiscordTransport`` over a stub client.

    Real, because the handler's whole claim is that it drives the transport's own
    configured-target allowlist -- a hand-rolled double would let the handler and
    Discord's spelling of a DM target drift apart.
    """
    client = MagicMock()
    client.create_dm_channel = AsyncMock(return_value=DM_CHANNEL)
    client.send_message = AsyncMock(return_value="1234567890")
    client.is_thread_channel = AsyncMock(return_value=True)
    return DiscordTransport(client, allowed_user_ids=users, allowed_thread_ids=threads)


def _state(transport=None, slack_client=None) -> MagicMock:
    state = MagicMock()
    state.slack_client = slack_client
    state.owner_id = ""
    state.get_channel_transport = MagicMock(
        side_effect=lambda channel_type: transport if channel_type == "discord" else None
    )
    return state


def _app(state) -> web.Application:
    app = web.Application()
    app.router.add_post("/api/send-message", api_send_message)
    app["state"] = state
    return app


@pytest.fixture
def audit():
    with patch("kiro_crew.sel.sel") as factory:
        recorder = MagicMock()
        factory.return_value = recorder
        yield recorder


@pytest.mark.asyncio
async def test_discord_send_resolves_the_owner_dm_and_delivers(audit) -> None:
    transport = _discord_transport()
    state = _state(transport)
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post(
            "/api/send-message", json={"text": "build is green", "session": "discord"}
        )
        assert resp.status == 200
        assert await resp.json() == {
            "ok": True,
            "slack": False,
            "session": False,
            "delivered_to": "discord",
        }
    # The DM was opened for the CONFIGURED user, not an id from the request.
    transport.client.create_dm_channel.assert_awaited_once_with(OWNER_ID)
    transport.client.send_message.assert_awaited_once_with(DM_CHANNEL, "build is green")
    state.notify.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("channel", "C0123ABC456"),
        ("thread_ts", "1712793600.123456"),
        ("blocks", [{"type": "divider"}]),
        ("session", "discord"),
    ],
)
async def test_endpoint_refuses_a_channel_target_with_a_slack_routing_field(
    audit, field, value
) -> None:
    """A DIRECT POST bypasses the MCP argument validator, so the handler owns the
    same refusal: the pair addresses the destination directly and the early
    return would drop a Slack-routing field without the caller seeing it."""
    transport = _discord_transport()
    async with TestClient(TestServer(_app(_state(transport)))) as client:
        resp = await client.post(
            "/api/send-message",
            json={"text": "hi", "channel_type": "discord", "target_id": "user:1", field: value},
        )
        assert resp.status == 400
        assert (await resp.json())["code"] == "slack_field_with_channel_target"
    # Fail-closed: refused BEFORE any send or DM resolution.
    transport.client.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_body_caller_session_is_ignored_without_internal_auth() -> None:
    """A body-supplied ``caller_session`` is a self-declared governance identity.

    Honoured only on the proven-internal transport (``internal_auth``, set solely
    after an ``X-Internal-Secret`` match). Absent it, the addressed leg must vet
    under the host sentinel, never the declared ``cron:*`` — otherwise a caller
    that reached this route without the internal secret could pick a permissive
    cron profile. This route is strict-internal today, so the guard is
    defence-in-depth against a future reclassification.
    """
    seen: list[str] = []

    async def _capture(state, channel_type, target_id, text, *, caller_session=""):
        seen.append(caller_session)
        return web.json_response({"ok": True, "delivered_to": channel_type, "parts": 1})

    with patch.object(
        __import__("kiro_crew.dashboard.handlers.messaging", fromlist=["_send_to_channel_target"]),
        "_send_to_channel_target",
        _capture,
    ):
        async with TestClient(TestServer(_app(_state(_discord_transport())))) as client:
            # No internal_auth set on the request (the test app runs no auth
            # middleware), so the declared cron identity must be dropped.
            resp = await client.post(
                "/api/send-message",
                json={
                    "text": "hi",
                    "channel_type": "discord",
                    "target_id": "user:1",
                    "caller_session": "cron:evil",
                },
            )
            assert resp.status == 200
    assert seen == [""]


@pytest.mark.asyncio
async def test_long_message_is_chunked_at_the_transports_own_ceiling(audit) -> None:
    """Discord rejects an over-cap message outright, so an unchunked report is lost."""
    transport = _discord_transport()
    body = "x" * (DISCORD_CHUNK_LIMIT + 10)
    async with TestClient(TestServer(_app(_state(transport)))) as client:
        resp = await client.post("/api/send-message", json={"text": body, "session": "discord"})
        assert resp.status == 200
    sent = [call.args[1] for call in transport.client.send_message.await_args_list]
    assert len(sent) == 2
    assert max(len(chunk) for chunk in sent) <= DISCORD_CHUNK_LIMIT
    assert "".join(sent) == body


@pytest.mark.asyncio
async def test_governance_denial_blocks_delivery_and_is_audited(audit) -> None:
    """The send passes the same fail-closed ``channels`` egress gate a message does."""
    transport = _discord_transport()
    denied = Decision(
        permitted=False, reason="channels denies discord", rule="rule1-deny", layer="policy"
    )
    with patch(
        "kiro_crew.platform.governance_profiles.governance_permits", return_value=denied
    ) as permits:
        async with TestClient(TestServer(_app(_state(transport)))) as client:
            resp = await client.post(
                "/api/send-message", json={"text": "secret", "session": "discord"}
            )
            assert resp.status == 403
            assert (await resp.json())["code"] == "channel_not_permitted"
    transport.client.create_dm_channel.assert_not_awaited()
    transport.client.send_message.assert_not_awaited()
    # Asked about the channels scope, fail-closed, for the discord member.
    assert permits.call_args.args[:2] == ("channels", "discord")
    assert permits.call_args.kwargs["fail_closed"] is True
    decisions = [
        call.kwargs
        for call in audit.log_governance_decision.call_args_list
        if call.kwargs.get("item") == "discord"
    ]
    assert decisions, "a governance denial must land in the SEL trail"
    assert decisions[0]["outcome"] == "denied"
    assert decisions[0]["scope"] == "channels"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("channel", "C0123ABC456"),
        ("thread_ts", "1712793600.123456"),
        ("blocks", [{"type": "divider"}]),
        ("unfurl_media", False),
    ],
)
async def test_slack_only_field_with_a_channel_session_is_refused(audit, field: str, value) -> None:
    transport = _discord_transport()
    async with TestClient(TestServer(_app(_state(transport)))) as client:
        resp = await client.post(
            "/api/send-message", json={"text": "hi", "session": "discord", field: value}
        )
        assert resp.status == 400
        payload = await resp.json()
        assert payload["code"] == "slack_only_field_with_channel_session"
        assert field in payload["error"]
    transport.client.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_cron_discord_send_does_not_also_post_to_slack(audit) -> None:
    """A cron asking for a Discord DM did not ask for a Slack one as well."""
    slack = MagicMock()
    slack.open_dm = AsyncMock(return_value="D_OWNER")
    slack.post_message = AsyncMock(return_value="1712793600.000001")
    transport = _discord_transport()
    state = _state(transport, slack_client=slack)
    state.owner_id = "U_OWNER"
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post(
            "/api/send-message",
            json={"text": "nightly done", "session": "discord", "caller_session": CRON_CALLER},
        )
        assert resp.status == 200
        assert (await resp.json())["delivered_to"] == "discord"
    slack.post_message.assert_not_awaited()
    transport.client.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_unconnected_channel_degrades_to_the_notification(audit) -> None:
    """Same degradation as an absent Slack client: the bell, and an honest answer."""
    state = _state(transport=None)
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post("/api/send-message", json={"text": "hi", "session": "discord"})
        assert resp.status == 200
        assert (await resp.json())["delivered_to"] == "notification"
    state.notify.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["Discord", "discord\nresources=spoofed", "x" * 64, "../etc"])
async def test_an_unrecognized_session_is_not_looked_up_as_a_channel(audit, value: str) -> None:
    """An agent-authored value reaches an error body and the audit trail.

    Anything that is not shaped like a channel type degrades to the notification
    (what an unknown ``session`` has always done) instead of being echoed back.
    """
    transport = _discord_transport()
    async with TestClient(TestServer(_app(_state(transport)))) as client:
        resp = await client.post("/api/send-message", json={"text": "hi", "session": value})
        assert resp.status == 200
        payload = await resp.json()
        assert payload["delivered_to"] == "notification"
        assert value not in str(payload)
    transport.client.send_message.assert_not_awaited()
    logged = [call.kwargs.get("resources", "") for call in audit.log_tool_invocation.call_args_list]
    assert logged and all(value not in entry for entry in logged)


@pytest.mark.asyncio
async def test_a_thread_target_is_not_an_owner_dm(audit) -> None:
    """An allow-listed guild thread is a wider audience than a DM, so it is skipped."""
    transport = _discord_transport(users=(), threads=("777888999000111222",))
    async with TestClient(TestServer(_app(_state(transport)))) as client:
        resp = await client.post("/api/send-message", json={"text": "hi", "session": "discord"})
        assert resp.status == 200
        assert (await resp.json())["delivered_to"] == "notification"
    transport.client.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unavailable_dm_target_is_not_used(audit) -> None:
    """A channel that cannot be DM'd proactively advertises it; honour that."""
    transport = MagicMock()
    transport.capabilities = TransportCapabilities(supports_proactive_send=True)
    transport.configured_targets = MagicMock(
        return_value=[
            ConfiguredChannelTarget(
                f"user:{OWNER_ID}",
                "WeCom DM",
                available=False,
                unavailable_reason="only replies to an inbound message",
            )
        ]
    )
    transport.resolve_configured_target = AsyncMock(return_value=(DM_CHANNEL, None))
    transport.send_message = AsyncMock(return_value="1")
    async with TestClient(TestServer(_app(_state(transport)))) as client:
        resp = await client.post("/api/send-message", json={"text": "hi", "session": "discord"})
        assert resp.status == 200
        assert (await resp.json())["delivered_to"] == "notification"
    transport.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_channel_delivery_is_reported_not_swallowed(audit) -> None:
    transport = _discord_transport()
    transport.client.send_message = AsyncMock(side_effect=RuntimeError("50013 Missing Access"))
    async with TestClient(TestServer(_app(_state(transport)))) as client:
        resp = await client.post("/api/send-message", json={"text": "hi", "session": "discord"})
        assert resp.status == 502
        payload = await resp.json()
        assert payload["ok"] is False
        assert payload["code"] == "channel_delivery_failed"


@pytest.mark.asyncio
async def test_a_revoked_target_is_refused_rather_than_widened(audit) -> None:
    """The allowlist is re-consulted at use: a stale target does not fall back."""
    transport = _discord_transport()
    transport.resolve_configured_target = AsyncMock(return_value=None)
    async with TestClient(TestServer(_app(_state(transport)))) as client:
        resp = await client.post("/api/send-message", json={"text": "hi", "session": "discord"})
        assert resp.status == 502
        assert (await resp.json())["code"] == "channel_delivery_failed"
    transport.client.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_options_trailer_survives_as_a_numbered_list(audit) -> None:
    """No widget on this path, so the choices are re-attached rather than dropped."""
    transport = _discord_transport()
    async with TestClient(TestServer(_app(_state(transport)))) as client:
        resp = await client.post(
            "/api/send-message",
            json={"text": "Pick one:\n\n[OPTIONS: Alpha | Bravo]", "session": "discord"},
        )
        assert resp.status == 200
    body = transport.client.send_message.await_args.args[1]
    assert re.search(r"1\.\s*Alpha", body)
    assert re.search(r"2\.\s*Bravo", body)
    assert "[OPTIONS:" not in body


@pytest.mark.asyncio
async def test_a_non_cron_channel_session_vets_on_the_headers_attested_identity() -> None:
    """The owner-DM leg must not vet a non-cron caller under the host sentinel.

    ``_deliver_channel_dm`` resolves ``caller_session or HOST_SESSION_KEY``, and
    HOST_SESSION_KEY is the PERMISSIVE default -- so dropping a non-cron caller's
    identity here lets a session whose own profile denies the transport reach a
    host-permitted owner DM. The sibling ``channel_type``/``target_id`` leg already
    closed this; the channel-``session`` leg was not migrated with it.

    The identity comes from the ``X-Session-Key`` HEADER, which
    ``token_auth._verify_unix_peer`` kernel-attests against the peer's own process
    ancestry, never from the body -- so it matches the principal
    ``_channel_delivery_key`` resolves for delivery on this same path.
    """
    seen: list[str] = []

    async def _capture(state, channel_type, text, *, caller_session=""):
        seen.append(caller_session)
        return True, "", ""

    module = __import__("kiro_crew.dashboard.handlers.messaging", fromlist=["_deliver_channel_dm"])
    with patch.object(module, "_deliver_channel_dm", _capture):
        async with TestClient(TestServer(_app(_state(_discord_transport())))) as client:
            resp = await client.post(
                "/api/send-message",
                json={"text": "hi", "session": "discord"},
                headers={"X-Session-Key": "dashboard:9"},
            )
            assert resp.status == 200

    assert seen == ["dashboard:9"], (
        "a non-cron channel-session send must carry the attested caller identity "
        "into the governance vet, not degrade to the permissive host sentinel"
    )
