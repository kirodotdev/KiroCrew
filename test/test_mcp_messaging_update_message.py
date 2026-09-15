"""``update_message``: the in-place edit of one of the bot's own Slack messages.

Three halves, each a separate way to ship a tool that looks right:

* the ADVERTISEMENT -- a descriptor the model is told about, and a registered
  validation schema so a missing key is a clean error instead of a KeyError out
  of the stdio loop;
* the TOOL -- an EDIT publishes new agent-authored text, so it carries
  ``send_message``'s outbound gates (strict identity, channel-agent containment,
  the ``capabilities.messaging`` and per-transport ``channels`` vets), not
  ``delete_message``'s bare shape checks;
* the ROUTE -- the endpoint redacts and sanitizes what it forwards, because the
  replacement content reaches Slack as-is, AND it is actually wired up: a handler
  nobody can reach 404s the tool, and a route absent from
  ``_STRICT_INTERNAL_API_PATHS`` ignores the internal secret and 403s it instead.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import mcp_core
from kiro_crew.dashboard.handlers.messaging import api_update_message
from kiro_crew.dashboard.server import _STRICT_INTERNAL_API_PATHS, _register_mcp_routes
from kiro_crew.mcp_core import _call_tool
from kiro_crew.mcp_tools.messaging import schemas
from kiro_crew.validation import MCP_CORE_SCHEMAS

_CHANNEL = "C0ABC123"
_TS = "1780088134.952549"
_CALLER = "dashboard:chat-1-1"


# ── The advertisement ──


def _descriptor() -> dict:
    return next(spec for spec in schemas() if spec["name"] == "update_message")


def test_tool_is_advertised_with_channel_and_ts_required() -> None:
    spec = _descriptor()
    assert spec["inputSchema"]["required"] == ["channel", "ts"]
    props = spec["inputSchema"]["properties"]
    assert {"channel", "ts", "text", "blocks"} == set(props)


def test_tool_has_a_validation_schema() -> None:
    """Unregistered, a missing key raises out of the stdio loop and kills the server."""
    assert "update_message" in MCP_CORE_SCHEMAS
    required = {f.name for f in MCP_CORE_SCHEMAS["update_message"].fields if f.required}
    assert {"channel", "ts"} <= required


# ── The tool ──


@pytest.fixture()
def caller():
    """A strictly-resolvable caller identity, as the gateway injects one."""
    with patch.dict("os.environ", {"KIROCREW_SESSION_KEY": _CALLER}):
        yield _CALLER


class TestUpdateMessageTool:
    def test_invalid_channel_format(self, caller) -> None:
        result = _call_tool("update_message", {"channel": "invalid!", "ts": _TS, "text": "x"})
        assert "invalid channel" in result.lower()

    def test_invalid_ts_format(self, caller) -> None:
        result = _call_tool(
            "update_message", {"channel": _CHANNEL, "ts": "not-a-timestamp", "text": "x"}
        )
        assert "invalid" in result.lower() and "timestamp" in result.lower()

    def test_requires_text_or_blocks(self, caller) -> None:
        with patch("kiro_crew.mcp_core._post") as post:
            result = _call_tool("update_message", {"channel": _CHANNEL, "ts": _TS})
        post.assert_not_called()
        assert result.startswith("Error:")
        assert "text or blocks" in result

    def test_successful_update(self, caller) -> None:
        with patch("kiro_crew.mcp_core._post") as post:
            post.return_value = {"ok": True}
            result = _call_tool("update_message", {"channel": _CHANNEL, "ts": _TS, "text": "done"})
        assert "updated" in result.lower()
        assert post.call_args.args == (
            "/api/update-message",
            {"channel": _CHANNEL, "ts": _TS, "text": "done"},
        )
        # The key the strict gate returned is the key the write goes out under.
        assert post.call_args.kwargs["session_key"] == _CALLER

    def test_blocks_only_update_reaches_the_gateway(self, caller) -> None:
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "done"}}]
        with patch("kiro_crew.mcp_core._post") as post:
            post.return_value = {"ok": True}
            result = _call_tool(
                "update_message", {"channel": _CHANNEL, "ts": _TS, "blocks": blocks}
            )
        assert "updated" in result.lower()
        assert post.call_args.args[1]["blocks"] == blocks

    def test_api_error_is_surfaced(self, caller) -> None:
        with patch("kiro_crew.mcp_core._post") as post:
            post.return_value = {"error": "message_not_found"}
            result = _call_tool("update_message", {"channel": _CHANNEL, "ts": _TS, "text": "done"})
        assert "message_not_found" in result

    def test_missing_args_return_a_clean_error(self, caller) -> None:
        for args in ({"channel": _CHANNEL}, {"ts": _TS}, {}):
            result = _call_tool("update_message", args)
            assert isinstance(result, str)
            assert result.lower().startswith("error"), args


class TestUpdateMessageGates:
    """An edit is EGRESS: it must be refused wherever a Slack send would be."""

    def test_unattributable_caller_is_refused(self, monkeypatch) -> None:
        monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "")
        with patch("kiro_crew.mcp_core._post") as post:
            result = _call_tool("update_message", {"channel": _CHANNEL, "ts": _TS, "text": "done"})
        post.assert_not_called()
        assert result.startswith("Error:")

    def test_channel_agent_is_refused(self, monkeypatch) -> None:
        monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "channel:C1:a1")
        with patch("kiro_crew.mcp_core._post") as post:
            result = _call_tool("update_message", {"channel": _CHANNEL, "ts": _TS, "text": "done"})
        post.assert_not_called()
        assert result.startswith("Error:")
        assert "channel agents" in result

    def test_messaging_capability_denial_stops_the_edit(self, caller) -> None:
        with (
            patch("kiro_crew.mcp_core._post") as post,
            patch(
                "kiro_crew.mcp_core._vet_messaging_governance",
                return_value="outbound messaging blocked by governance policy",
            ),
        ):
            result = _call_tool("update_message", {"channel": _CHANNEL, "ts": _TS, "text": "done"})
        post.assert_not_called()
        assert result.startswith("Error:")
        assert "governance" in result

    def test_the_channels_vet_names_slack(self, caller) -> None:
        with (
            patch("kiro_crew.mcp_core._post") as post,
            patch("kiro_crew.mcp_core._vet_channel_governance", return_value=None) as vet,
        ):
            post.return_value = {"ok": True}
            _call_tool("update_message", {"channel": _CHANNEL, "ts": _TS, "text": "done"})
        assert [call.args[1] for call in vet.call_args_list] == ["slack"]
        # The persisted governance record must name the edit, not a send that
        # never happened.
        assert vet.call_args_list[0].kwargs["tool_name"] == "update_message"

    def test_channels_denial_stops_the_edit(self, caller) -> None:
        with (
            patch("kiro_crew.mcp_core._post") as post,
            patch(
                "kiro_crew.mcp_core._vet_channel_governance",
                return_value="messaging via transport 'slack' blocked by governance policy",
            ),
        ):
            result = _call_tool("update_message", {"channel": _CHANNEL, "ts": _TS, "text": "done"})
        post.assert_not_called()
        assert result.startswith("Error:")
        assert "slack" in result


# ── The route ──


class TestUpdateMessageWiring:
    """The two edits that make the handler reachable, derived from the router.

    ``_register_mcp_routes`` is the single registrar both ``start_dashboard`` and
    the headless ``start_api_server`` call, so one assertion covers both
    entrypoints.
    """

    def test_route_is_registered(self) -> None:
        app = web.Application()
        _register_mcp_routes(app)
        routes = {(r.method, r.resource.canonical) for r in app.router.routes() if r.resource}
        assert ("POST", "/api/update-message") in routes

    def test_route_is_strict_internal(self) -> None:
        """Registered but unlisted means the internal secret is ignored and the call 403s."""
        assert "/api/update-message" in _STRICT_INTERNAL_API_PATHS


def _make_app(state) -> web.Application:
    app = web.Application()
    app.router.add_post("/api/update-message", api_update_message)
    app["state"] = state
    return app


def _mock_state(slack_client=None, owner_id="UOWNER"):
    state = MagicMock()
    state.slack_client = slack_client
    state.owner_id = owner_id
    return state


_OWNER_DM = "D0OWNERDM"


def _slack(owner_dm: str = _OWNER_DM) -> MagicMock:
    slack = MagicMock()
    slack.update_message = AsyncMock()
    # The owner-DM resolution the handler uses to authorize a `D...` target.
    slack.open_dm = AsyncMock(return_value=owner_dm)
    return slack


class TestUpdateMessageEndpoint:
    @pytest.fixture(autouse=True)
    def _tracked(self, monkeypatch):
        """``_CHANNEL`` is a ROOM, and a room must be tracked to be editable.

        Patched at ``kiro_crew.slack.handler`` rather than at a ``handlers.messaging``
        name because the handler's import of the probe is function-local.
        """
        monkeypatch.setattr("kiro_crew.slack.handler.is_tracked_channel", lambda cid: True)

    @pytest.mark.asyncio
    async def test_missing_channel(self) -> None:
        async with TestClient(TestServer(_make_app(_mock_state(_slack())))) as client:
            resp = await client.post("/api/update-message", json={"ts": _TS, "text": "x"})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_bad_ts(self) -> None:
        async with TestClient(TestServer(_make_app(_mock_state(_slack())))) as client:
            resp = await client.post(
                "/api/update-message", json={"channel": _CHANNEL, "ts": "nope", "text": "x"}
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_missing_text_and_blocks(self) -> None:
        async with TestClient(TestServer(_make_app(_mock_state(_slack())))) as client:
            resp = await client.post("/api/update-message", json={"channel": _CHANNEL, "ts": _TS})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_blocks_must_be_a_list(self) -> None:
        async with TestClient(TestServer(_make_app(_mock_state(_slack())))) as client:
            resp = await client.post(
                "/api/update-message",
                json={"channel": _CHANNEL, "ts": _TS, "blocks": {"type": "section"}},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_no_slack_client(self) -> None:
        async with TestClient(TestServer(_make_app(_mock_state(slack_client=None)))) as client:
            resp = await client.post(
                "/api/update-message", json={"channel": _CHANNEL, "ts": _TS, "text": "x"}
            )
            assert resp.status == 503

    @pytest.mark.asyncio
    async def test_successful_update(self) -> None:
        slack = _slack()
        async with TestClient(TestServer(_make_app(_mock_state(slack)))) as client:
            resp = await client.post(
                "/api/update-message", json={"channel": _CHANNEL, "ts": _TS, "text": "done"}
            )
            assert resp.status == 200
            assert (await resp.json())["ok"] is True
        slack.update_message.assert_awaited_once_with(_CHANNEL, _TS, "done", None)

    @pytest.mark.asyncio
    async def test_slack_error_returns_502(self) -> None:
        slack = _slack()
        slack.update_message = AsyncMock(side_effect=Exception("message_not_found"))
        async with TestClient(TestServer(_make_app(_mock_state(slack)))) as client:
            resp = await client.post(
                "/api/update-message", json={"channel": _CHANNEL, "ts": _TS, "text": "done"}
            )
            assert resp.status == 502
            assert "message_not_found" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_outbound_text_is_redacted(self) -> None:
        slack = _slack()
        async with TestClient(TestServer(_make_app(_mock_state(slack)))) as client:
            resp = await client.post(
                "/api/update-message",
                json={
                    "channel": _CHANNEL,
                    "ts": _TS,
                    "text": "key AKIAIOSFODNN7EXAMPLE done",
                },
            )
            assert resp.status == 200
        posted = slack.update_message.await_args.args[2]
        assert "AKIAIOSFODNN7EXAMPLE" not in posted

    @pytest.mark.asyncio
    async def test_outbound_blocks_are_sanitized(self) -> None:
        slack = _slack()
        async with TestClient(TestServer(_make_app(_mock_state(slack)))) as client:
            resp = await client.post(
                "/api/update-message",
                json={
                    "channel": _CHANNEL,
                    "ts": _TS,
                    "blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": "key AKIAIOSFODNN7EXAMPLE",
                            },
                        }
                    ],
                },
            )
            assert resp.status == 200
        sent_blocks = slack.update_message.await_args.args[3]
        assert "AKIAIOSFODNN7EXAMPLE" not in str(sent_blocks)

    @pytest.mark.asyncio
    async def test_a_markup_split_credential_in_blocks_is_redacted(self) -> None:
        """The literal scan sees fragments; the display-form floor normalizes first.

        A credential broken across Block Kit markup — the key prefix in one run,
        the remainder bolded — survives a literal redactor because neither
        fragment matches. Block text is LLM-authored, so this is exactly where
        such a split arrives. The edit path must apply the same display-form
        floor the text path does.
        """
        slack = _slack()
        async with TestClient(TestServer(_make_app(_mock_state(slack)))) as client:
            resp = await client.post(
                "/api/update-message",
                json={
                    "channel": _CHANNEL,
                    "ts": _TS,
                    "blocks": [
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": "key AKIA*IOSFODNN7EXAMPLE*"},
                        }
                    ],
                },
            )
            assert resp.status == 200
        sent = str(slack.update_message.await_args.args[3])
        # Neither the joined secret nor its bolded tail may survive.
        assert "AKIAIOSFODNN7EXAMPLE" not in sent
        assert "IOSFODNN7EXAMPLE" not in sent

    @pytest.mark.asyncio
    async def test_untracked_room_is_refused(self, monkeypatch) -> None:
        """Tracking is the operator's revocation lever, so a revoked room is not editable."""
        monkeypatch.setattr("kiro_crew.slack.handler.is_tracked_channel", lambda cid: False)
        slack = _slack()
        async with TestClient(TestServer(_make_app(_mock_state(slack)))) as client:
            resp = await client.post(
                "/api/update-message", json={"channel": _CHANNEL, "ts": _TS, "text": "done"}
            )
            assert resp.status == 403
            assert (await resp.json())["code"] == "channel_not_tracked"
        slack.update_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_dm_is_not_authorized_by_the_room_allowlist(self, monkeypatch) -> None:
        """A DM is never tracked, so the room allowlist cannot be what admits it.

        The room allowlist returning False must not by itself decide a DM either
        way: a DM is authorized by identity. The admitted case is in
        ``TestUpdateMessageDmIsOwnerOnly``, keyed on the owner's resolved DM.
        """
        monkeypatch.setattr("kiro_crew.slack.handler.is_tracked_channel", lambda cid: False)
        slack = _slack()
        async with TestClient(TestServer(_make_app(_mock_state(slack)))) as client:
            resp = await client.post(
                "/api/update-message", json={"channel": "D0ABC123", "ts": _TS, "text": "done"}
            )
            assert resp.status == 403
            assert (await resp.json())["code"] == "not_owner_dm"
        slack.update_message.assert_not_awaited()


class TestUpdateMessageDmIsOwnerOnly:
    """A `D` prefix routes; it must never authorize.

    `owner_id` changes when ownership does, but the old DM channel id does not, so
    admitting every `D...` on its prefix would leave a FORMER owner's DM
    permanently writable -- and this endpoint publishes replacement content into a
    message already sitting in it.
    """

    @pytest.fixture(autouse=True)
    def _untracked(self, monkeypatch):
        """No DM is ever a tracked channel, so the room allowlist must not save us."""
        monkeypatch.setattr("kiro_crew.slack.handler.is_tracked_channel", lambda cid: False)

    @pytest.mark.asyncio
    async def test_the_owners_own_dm_is_editable(self) -> None:
        slack = _slack()
        async with TestClient(TestServer(_make_app(_mock_state(slack)))) as client:
            resp = await client.post(
                "/api/update-message", json={"channel": _OWNER_DM, "ts": _TS, "text": "ok"}
            )
            assert resp.status == 200
        slack.update_message.assert_awaited_once_with(_OWNER_DM, _TS, "ok", None)

    @pytest.mark.asyncio
    async def test_a_former_owners_dm_is_refused(self) -> None:
        """A known DM id the bot posted in, belonging to someone other than the owner."""
        slack = _slack()
        async with TestClient(TestServer(_make_app(_mock_state(slack)))) as client:
            resp = await client.post(
                "/api/update-message", json={"channel": "D0FORMER", "ts": _TS, "text": "x"}
            )
            assert resp.status == 403
            assert (await resp.json())["code"] == "not_owner_dm"
        slack.update_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_owner_configured_fails_closed(self) -> None:
        slack = _slack()
        state = _mock_state(slack, owner_id="")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/update-message", json={"channel": _OWNER_DM, "ts": _TS, "text": "x"}
            )
            assert resp.status == 403
        slack.update_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_unresolvable_owner_fails_closed(self) -> None:
        """A raising `open_dm` must refuse, not fall through to the prefix."""
        slack = _slack()
        slack.open_dm = AsyncMock(side_effect=Exception("ratelimited"))
        async with TestClient(TestServer(_make_app(_mock_state(slack)))) as client:
            resp = await client.post(
                "/api/update-message", json={"channel": _OWNER_DM, "ts": _TS, "text": "x"}
            )
            assert resp.status == 403
        slack.update_message.assert_not_awaited()
