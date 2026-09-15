"""``include_session_link``: the opt-in "Open session" deep-link button.

Three halves, each a separate way to ship a flag that looks right and does
nothing:

* the ADVERTISEMENT -- the descriptor offers ``include_session_link`` and the
  argument validator that runs before the handler accepts it (a property the
  descriptor advertises but ``SEND_MESSAGE_SCHEMA`` omits is rejected by
  ``validate_tool_args`` and 0%% reachable over MCP);
* the TOOL -- the MCP ``send_message`` handler forwards the flag as a plain
  boolean, never a caller-supplied session key;
* the ROUTE -- the gateway builds the link SERVER-SIDE from the resolved caller
  identity and appends the button ONLY on a Slack delivery, degrading to no
  button (message still sent) for a headless caller, a non-Slack send, or a
  missing origin.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

# Importable stub so api_send_message's inline `from kiro_crew.slack.handler
# import ...` resolves even without the heavy transitive deps installed, exactly
# as test_send_message_targeted.py does.
if "kiro_crew.slack.handler" not in sys.modules:
    _stub = types.ModuleType("kiro_crew.slack.handler")
    _stub.is_allowed_user = lambda uid: False  # type: ignore[attr-defined]
    _stub.is_tracked_channel = lambda cid: False  # type: ignore[attr-defined]
    sys.modules["kiro_crew.slack.handler"] = _stub

from kiro_crew.dashboard.handlers import api_send_message  # noqa: E402
from kiro_crew.mcp_core import _call_tool  # noqa: E402
from kiro_crew.mcp_tools.messaging import schemas  # noqa: E402
from kiro_crew.validation import SEND_MESSAGE_SCHEMA, validate_tool_args  # noqa: E402

_SESSION_KEY = "dashboard:chat-7-1"
_BARE_KEY = "chat-7-1"
_DASH_ORIGIN = "https://dash.example.com"


# ── The advertisement + the validator ──


def _descriptor_properties() -> dict:
    for tool in schemas():
        if tool["name"] == "send_message":
            return tool["inputSchema"]["properties"]
    raise AssertionError("send_message is not advertised")


def test_descriptor_advertises_include_session_link_as_a_boolean() -> None:
    prop = _descriptor_properties().get("include_session_link")
    assert prop is not None, "send_message must advertise include_session_link"
    assert prop["type"] == "boolean"


def test_validator_accepts_include_session_link() -> None:
    # The pre-handler validator runs first: a value the descriptor advertises but
    # the schema omits is rejected before the handler ever sees it.
    cleaned = validate_tool_args(
        {"text": "hi", "include_session_link": True}, SEND_MESSAGE_SCHEMA
    )
    assert cleaned["include_session_link"] is True


def test_a_plain_send_still_validates_without_the_flag() -> None:
    # Additive: a caller that never sets it is unaffected.
    assert "include_session_link" not in validate_tool_args({"text": "hi"}, SEND_MESSAGE_SCHEMA)


# ── The tool: payload forwarding ──


@pytest.fixture
def cron_caller():
    """Run the MCP tool as a cron, whose bare sends default to Slack."""
    with patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "cron:abc123"}):
        yield


def _bypass_governance():
    return (
        patch("kiro_crew.mcp_core._vet_messaging_governance", return_value=""),
        patch("kiro_crew.mcp_core._vet_channel_governance", return_value=""),
        patch("kiro_crew.mcp_core._deny_channel_agent_messaging", return_value=""),
    )


def test_handler_forwards_include_session_link(cron_caller) -> None:
    gov_msg, gov_chan, chan_deny = _bypass_governance()
    with patch("kiro_crew.mcp_core._post") as post, gov_msg, gov_chan, chan_deny:
        post.return_value = {"ok": True, "delivered_to": "slack", "ts": "1.1"}
        _call_tool(
            "send_message",
            {"text": "hi", "session": "slack", "include_session_link": True},
        )
    # The flag rides as a plain boolean; no session key is added by the tool.
    payload = post.call_args[0][1]
    assert payload["include_session_link"] is True


def test_flag_absent_by_default(cron_caller) -> None:
    gov_msg, gov_chan, chan_deny = _bypass_governance()
    with patch("kiro_crew.mcp_core._post") as post, gov_msg, gov_chan, chan_deny:
        post.return_value = {"ok": True, "delivered_to": "slack", "ts": "1.1"}
        _call_tool("send_message", {"text": "hi", "session": "slack"})
    assert "include_session_link" not in post.call_args[0][1]


# ── The route: server-side link + button on the Slack leg ──


def _make_app(state) -> web.Application:
    app = web.Application()
    app.router.add_post("/api/send-message", api_send_message)
    app["state"] = state
    return app


def _mock_slack():
    slack = MagicMock()
    slack.open_dm = AsyncMock(return_value="D_OWNER")
    slack.post_message = AsyncMock(return_value="1712793600.000001")
    slack.post_blocks = AsyncMock(return_value="1712793600.000002")
    return slack


def _mock_state(slack):
    state = MagicMock()
    state.slack_client = slack
    state.owner_id = "U_OWNER"
    return state


def _cfg(*, use_tunnel_url=False, url=_DASH_ORIGIN):
    return SimpleNamespace(
        slack=SimpleNamespace(use_tunnel_url=use_tunnel_url),
        dashboard=SimpleNamespace(url=url),
    )


@pytest.fixture
def mock_sel():
    with patch("kiro_crew.sel.sel") as m:
        m.return_value = MagicMock()
        yield m.return_value


def _button(post_blocks_mock):
    """The single actions block posted by the button follow-up."""
    blocks = post_blocks_mock.call_args.args[1]
    return blocks[0]["elements"][0]


@pytest.mark.asyncio
async def test_button_appended_on_slack_send_using_dashboard_origin(mock_sel) -> None:
    slack = _mock_slack()
    state = _mock_state(slack)
    app = _make_app(state)
    with patch(
        "kiro_crew.dashboard.handlers.messaging.KiroCrewConfig"
    ) as cfg_cls:
        cfg_cls.load.return_value = _cfg(use_tunnel_url=False)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/send-message",
                json={"text": "status update", "session": "slack", "include_session_link": True},
                headers={"X-Session-Key": _SESSION_KEY},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["delivered_to"] == "slack"

    # Main message posted as plain text; the button rides a single follow-up.
    slack.post_message.assert_awaited_once()
    slack.post_blocks.assert_awaited_once()
    btn = _button(slack.post_blocks)
    assert btn["text"]["text"] == "Open session"
    assert btn["action_id"] == "open_session_link"
    assert btn["url"] == f"{_DASH_ORIGIN}/chat?sid={_BARE_KEY}"
    # Threaded with the (non-threaded) main post -> no thread_ts.
    assert slack.post_blocks.call_args.kwargs.get("thread_ts") is None


@pytest.mark.asyncio
async def test_button_uses_tunnel_url_when_opted_in(mock_sel) -> None:
    slack = _mock_slack()
    state = _mock_state(slack)
    app = _make_app(state)
    with (
        patch("kiro_crew.dashboard.handlers.messaging.KiroCrewConfig") as cfg_cls,
        patch("kiro_crew.tunnel.get_tunnel_url", return_value="https://tun.example.com/"),
    ):
        cfg_cls.load.return_value = _cfg(use_tunnel_url=True)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/send-message",
                json={"text": "hi", "session": "slack", "include_session_link": True},
                headers={"X-Session-Key": _SESSION_KEY},
            )
            assert resp.status == 200
    btn = _button(slack.post_blocks)
    assert btn["url"] == f"https://tun.example.com/chat?sid={_BARE_KEY}"


@pytest.mark.asyncio
async def test_no_button_without_a_resolvable_session(mock_sel) -> None:
    """A headless caller (no X-Session-Key, not a cron) gets no button, and the
    message is still delivered."""
    slack = _mock_slack()
    state = _mock_state(slack)
    app = _make_app(state)
    with patch("kiro_crew.dashboard.handlers.messaging.KiroCrewConfig") as cfg_cls:
        cfg_cls.load.return_value = _cfg()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/send-message",
                json={"text": "hi", "session": "slack", "include_session_link": True},
            )
            assert resp.status == 200
            assert (await resp.json())["delivered_to"] == "slack"
    slack.post_message.assert_awaited_once()
    slack.post_blocks.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_button_when_not_delivered_to_slack(mock_sel) -> None:
    """include_session_link on a notification-only send (no Slack) is a no-op."""
    slack = _mock_slack()
    state = _mock_state(slack)
    app = _make_app(state)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/send-message",
            json={"text": "hi", "include_session_link": True},
            headers={"X-Session-Key": _SESSION_KEY},
        )
        assert resp.status == 200
        assert (await resp.json())["delivered_to"] == "notification"
    slack.post_message.assert_not_awaited()
    slack.post_blocks.assert_not_awaited()


@pytest.mark.asyncio
async def test_button_failure_never_fails_the_send(mock_sel) -> None:
    """A button post Slack rejects is swallowed: the message is already delivered."""
    slack = _mock_slack()
    slack.post_blocks = AsyncMock(side_effect=RuntimeError("bad url"))
    state = _mock_state(slack)
    app = _make_app(state)
    with patch("kiro_crew.dashboard.handlers.messaging.KiroCrewConfig") as cfg_cls:
        cfg_cls.load.return_value = _cfg()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/send-message",
                json={"text": "hi", "session": "slack", "include_session_link": True},
                headers={"X-Session-Key": _SESSION_KEY},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert data["delivered_to"] == "slack"
    slack.post_message.assert_awaited_once()
    slack.post_blocks.assert_awaited_once()  # attempted, then swallowed
