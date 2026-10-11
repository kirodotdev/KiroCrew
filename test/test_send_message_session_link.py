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

import json
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
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
from kiro_crew.dashboard.handlers.messaging import _resolve_session_link_url  # noqa: E402
from kiro_crew.mcp_core import _call_tool  # noqa: E402
from kiro_crew.mcp_tools.messaging import schemas  # noqa: E402
from kiro_crew.session_surface import set_dashboard_surfaced  # noqa: E402
from kiro_crew.validation import SEND_MESSAGE_SCHEMA, validate_tool_args  # noqa: E402

_SESSION_KEY = "dashboard:chat-7-1"
_BARE_KEY = "chat-7-1"
_DASH_ORIGIN = "https://dash.example.com"
# A fixed presigned token so the deep-link URL is deterministic; the real
# generate_token does file I/O and registers a nonce, neither of which this
# unit test should exercise.
_TOKEN = "TESTTOKEN"


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
    cleaned = validate_tool_args({"text": "hi", "include_session_link": True}, SEND_MESSAGE_SCHEMA)
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


@pytest.fixture
def fixed_token():
    """Pin the minted click token so the deep link is deterministic."""
    with patch("kiro_crew.dashboard.handlers.messaging.generate_token", return_value=_TOKEN):
        yield _TOKEN


@pytest.fixture
def surfaced():
    """Publish/clear the dashboard-surface registry (global, so reset after)."""
    set_dashboard_surfaced(set())
    try:
        yield set_dashboard_surfaced
    finally:
        set_dashboard_surfaced(set())


def _button(post_blocks_mock):
    """The "Open session" button element in the LAST post_blocks call's blocks."""
    blocks = post_blocks_mock.call_args.args[1]
    actions = next(b for b in blocks if b.get("type") == "actions")
    return actions["elements"][0]


@pytest.mark.asyncio
async def test_button_appended_on_slack_send_using_dashboard_origin(mock_sel, fixed_token) -> None:
    slack = _mock_slack()
    state = _mock_state(slack)
    app = _make_app(state)
    with patch("kiro_crew.dashboard.handlers.messaging.KiroCrewConfig") as cfg_cls:
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

    # Single message: the status text and the button ride ONE post_blocks call
    # (one Slack notification), so there is no separate plain-text post and no
    # bare-button follow-up.
    slack.post_message.assert_not_awaited()
    slack.post_blocks.assert_awaited_once()
    posted = slack.post_blocks.call_args.args[1]
    assert posted[0] == {"type": "section", "text": {"type": "mrkdwn", "text": "status update"}}
    btn = _button(slack.post_blocks)
    assert btn["text"]["text"] == "Open session"
    assert btn["action_id"] == "open_session_link"
    # Surfaced dashboard slot key + a presigned token so the link authenticates.
    assert btn["url"] == f"{_DASH_ORIGIN}/chat?sid={_BARE_KEY}&token={_TOKEN}"
    # The (non-threaded) send carries no thread_ts.
    assert slack.post_blocks.call_args.kwargs.get("thread_ts") is None


@pytest.mark.asyncio
async def test_button_uses_tunnel_url_when_opted_in(mock_sel, fixed_token) -> None:
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
    assert btn["url"] == f"https://tun.example.com/chat?sid={_BARE_KEY}&token={_TOKEN}"


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
async def test_string_false_does_not_opt_in(mock_sel) -> None:
    """``include_session_link="false"`` (a non-empty string, e.g. from a script
    caller serializing booleans) is truthy in Python but is NOT an opt-in: only
    the literal ``True`` may mint a credential-bearing button."""
    slack = _mock_slack()
    state = _mock_state(slack)
    app = _make_app(state)
    with patch("kiro_crew.dashboard.handlers.messaging.KiroCrewConfig") as cfg_cls:
        cfg_cls.load.return_value = _cfg()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/send-message",
                json={"text": "hi", "session": "slack", "include_session_link": "false"},
                headers={"X-Session-Key": _SESSION_KEY},
            )
            assert resp.status == 200
            assert (await resp.json())["delivered_to"] == "slack"
    # Plain text delivery only: no blocks means no button was built.
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
async def test_button_failure_never_fails_the_send(mock_sel, fixed_token) -> None:
    """A combined post Slack REJECTS (answered ok=false) degrades to text-only:
    the message still delivers, and only the best-effort button is lost."""
    from slack_sdk.errors import SlackApiError

    slack = _mock_slack()
    slack.post_blocks = AsyncMock(
        side_effect=SlackApiError("invalid_blocks", {"ok": False, "error": "invalid_blocks"})
    )
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
    # The merged (section + button) post_blocks is rejected -> fall back to a
    # plain-text post_message so the message still delivers; the button is then
    # attempted as a trailing follow-up, which also fails and is swallowed.
    slack.post_message.assert_awaited_once()
    assert slack.post_blocks.await_count == 2


@pytest.mark.asyncio
async def test_ambiguous_combined_post_failure_never_posts_twice(mock_sel, fixed_token) -> None:
    """An AMBIGUOUS combined-post failure (timeout: Slack never answered, the
    post may have landed) must NOT trigger the text-only fallback -- a second
    post would deliver the message twice. It propagates to the delivery-failed
    path instead: exactly one post attempt, no post_message retry."""
    slack = _mock_slack()
    slack.post_blocks = AsyncMock(side_effect=TimeoutError("slack timed out"))
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
            # Slack delivery failed (ambiguous), and no duplicate was risked.
            assert resp.status != 200
    slack.post_message.assert_not_awaited()
    assert slack.post_blocks.await_count == 1


@pytest.mark.asyncio
async def test_an_unknown_outcome_tells_the_caller_the_message_may_be_there(
    mock_sel, fixed_token
) -> None:
    """A post that failed with an unknown outcome may be in the conversation, so
    the caller is told the delivery was not confirmed, not that it failed."""
    slack = _mock_slack()
    slack.post_blocks = AsyncMock(side_effect=TimeoutError("slack timed out"))
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
            assert resp.status == 502
            data = await resp.json()
    assert data["slack"] is False
    assert data["error"].startswith("Slack delivery was not confirmed: slack timed out")
    assert "may have been posted" in data["error"]


@pytest.mark.asyncio
async def test_a_refused_post_is_still_reported_as_failed(mock_sel) -> None:
    """A post Slack refused outright landed nowhere, so it reads as a failure."""
    from slack_sdk.errors import SlackApiError

    slack = _mock_slack()
    slack.post_message = AsyncMock(
        side_effect=SlackApiError("channel_not_found", {"ok": False, "error": "channel_not_found"})
    )
    state = _mock_state(slack)
    app = _make_app(state)
    with patch("kiro_crew.dashboard.handlers.messaging.KiroCrewConfig") as cfg_cls:
        cfg_cls.load.return_value = _cfg()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/send-message",
                json={"text": "hi", "session": "slack"},
                headers={"X-Session-Key": _SESSION_KEY},
            )
            assert resp.status == 502
            data = await resp.json()
    assert data["error"].startswith("Slack delivery failed: channel_not_found")
    slack.post_message.assert_awaited_once()


# ── The fallback through the real Slack SDK: which answers count as a refusal ──
#
# slack_sdk raises the same SlackApiError for a refusal and for an answer whose
# outcome is unknown, so these drive the real RealSlackClient and AsyncWebClient
# over a stand-in HTTP session that records which posts Slack stored.

_CALLER_TEXT = "Build finished: 3 tests failed"
_LINK_URL = f"{_DASH_ORIGIN}/chat?sid={_BARE_KEY}&token={_TOKEN}"

# (stored, HTTP status, content type, body) of the first chat.postMessage answer.
_FIRST_ANSWERS = {
    "gateway_502_page": (True, 502, "text/html", "<html><body>502 Bad Gateway</body></html>"),
    "internal_error": (True, 200, "application/json", '{"ok": false, "error": "internal_error"}'),
    "fatal_error": (True, 200, "application/json", '{"ok": false, "error": "fatal_error"}'),
    "truncated_body": (True, 200, "application/json", '{"ok": true, "channel": "D_OW'),
    "invalid_blocks": (False, 200, "application/json", '{"ok": false, "error": "invalid_blocks"}'),
    "rate_limited": (False, 429, "application/json", '{"ok": false, "error": "ratelimited"}'),
}


class _SlackHttpResponse:
    def __init__(self, status: int, content_type: str, body: str) -> None:
        self.status = status
        self.content_type = content_type
        self.headers = {"Content-Type": content_type}
        self._body = body

    async def json(self):
        # aiohttp refuses a body not labelled application/json.
        if self.content_type != "application/json":
            raise aiohttp.ContentTypeError(
                MagicMock(), (), status=self.status, message="unexpected mimetype"
            )
        return json.loads(self._body)

    async def text(self) -> str:
        return self._body


class _SlackHttpSession:
    """Stands in for the aiohttp session under slack_sdk's AsyncWebClient."""

    closed = False

    def __init__(self, first_answer: str) -> None:
        self.first_answer = first_answer
        self.posts: list[dict] = []
        self.stored: list[dict] = []

    def request(self, verb: str, url: str, **kwargs):
        session = self

        class _Exchange:
            async def __aenter__(self):
                return session.answer(url, kwargs)

            async def __aexit__(self, *exc) -> bool:
                return False

        return _Exchange()

    def answer(self, url: str, kwargs: dict) -> _SlackHttpResponse:
        if url.endswith("conversations.open"):
            body = '{"ok": true, "channel": {"id": "D_OWNER"}}'
            return _SlackHttpResponse(200, "application/json", body)
        assert url.endswith("chat.postMessage"), url
        payload = dict(kwargs.get("json") or {})
        self.posts.append(payload)
        if len(self.posts) == 1:
            stored, status, content_type, body = _FIRST_ANSWERS[self.first_answer]
            if stored:
                self.stored.append(payload)
            return _SlackHttpResponse(status, content_type, body)
        self.stored.append(payload)
        body = json.dumps({"ok": True, "ts": f"1712793600.00000{len(self.posts)}"})
        return _SlackHttpResponse(200, "application/json", body)


async def _send_through_the_real_sdk(first_answer: str, *, caller_blocks: bool):
    from slack_sdk.web.async_client import AsyncWebClient

    from kiro_crew.dashboard.messaging_api import proactive_send
    from kiro_crew.slack.client import RealSlackClient

    http = _SlackHttpSession(first_answer)
    client = RealSlackClient("test-token")
    client._web = AsyncWebClient(token="test-token", session=http)
    outcome = proactive_send._SendMessageOutcome()
    section = {"type": "section", "text": {"type": "mrkdwn", "text": _CALLER_TEXT}}
    await proactive_send._post_send_message_to_slack(
        SimpleNamespace(slack_client=client, owner_id="U_OWNER"),
        outcome,
        blocks=[section] if caller_blocks else None,
        text=_CALLER_TEXT,
        options=[],
        target_channel="",
        target_user="",
        thread_ts=None,
        reply_broadcast=None,
        session_link_url=_LINK_URL,
    )
    copies = [post for post in http.stored if post.get("text") == _CALLER_TEXT]
    return http, outcome, copies


@pytest.mark.asyncio
@pytest.mark.parametrize("caller_blocks", [False, True], ids=["text", "blocks"])
@pytest.mark.parametrize(
    "first_answer", ["gateway_502_page", "internal_error", "fatal_error", "truncated_body"]
)
async def test_an_unknown_outcome_is_never_posted_again(first_answer, caller_blocks) -> None:
    """A gateway page, a body that is not JSON, internal_error and fatal_error all
    raise SlackApiError, and after each the post may have landed: the message is
    not posted a second time without the button, and the failure is unconfirmed."""
    http, outcome, copies = await _send_through_the_real_sdk(
        first_answer, caller_blocks=caller_blocks
    )
    assert len(http.posts) == 1
    assert len(copies) == 1
    assert outcome.sent_slack is False
    assert outcome.slack_failure == "unconfirmed"


@pytest.mark.asyncio
@pytest.mark.parametrize("caller_blocks", [False, True], ids=["text", "blocks"])
async def test_a_refused_combined_post_falls_back_once_without_the_button(caller_blocks) -> None:
    """Slack refused the combined post, so nothing landed: the message posts once
    without the button, and the button trails as its own follow-up."""
    http, outcome, copies = await _send_through_the_real_sdk(
        "invalid_blocks", caller_blocks=caller_blocks
    )
    assert outcome.sent_slack is True
    assert len(copies) == 1
    assert "actions" not in json.dumps(copies[0].get("blocks", []))
    assert len(http.posts) == 3
    assert "actions" in json.dumps(http.posts[2]["blocks"])


@pytest.mark.asyncio
@pytest.mark.parametrize("caller_blocks", [False, True], ids=["text", "blocks"])
async def test_a_rate_limited_post_is_not_posted_again_and_says_why(caller_blocks) -> None:
    """A 429 is a refusal, so nothing was posted: the message is not posted again
    without the button, and the caller is told the rate limit stopped it."""
    from kiro_crew.dashboard.messaging_api import proactive_send

    http, outcome, copies = await _send_through_the_real_sdk(
        "rate_limited", caller_blocks=caller_blocks
    )
    assert len(http.posts) == 1
    assert copies == []
    assert outcome.slack_failure == "rate_limited"
    resp = proactive_send._send_message_response(
        outcome, sent_session=False, channel_target="", channel_type=""
    )
    error = json.loads(resp.body.decode("utf-8"))["error"]
    assert resp.status == 502
    assert error.startswith(
        "Slack delivery failed: the message was not posted because Slack rate-limited"
    )
    assert "not confirmed" not in error


# ── The link: server-side slot-key surfacing + presigned token ──
#
# _resolve_session_link_url is exercised directly here: the surfacing rule is
# where the pre-review bug lived (a bare removeprefix("dashboard:") left a
# channel-born key untouched), and driving it through the resolver is far less
# brittle than staging every session shape through the full HTTP handler.


def _link_state():
    state = MagicMock()
    state.owner_id = "U_OWNER"
    return state


@pytest.mark.asyncio
async def test_resolver_dashboard_key_uses_bare_slot_key_with_token(fixed_token) -> None:
    """A dashboard-origin session key -> the bare slot key plus a presigned token."""
    with patch("kiro_crew.dashboard.handlers.messaging.KiroCrewConfig") as cfg_cls:
        cfg_cls.load.return_value = _cfg()
        url = await _resolve_session_link_url(_link_state(), "", _SESSION_KEY)
    assert url == f"{_DASH_ORIGIN}/chat?sid={_BARE_KEY}&token={_TOKEN}"


@pytest.mark.asyncio
async def test_resolver_slack_origin_key_uses_surfaced_slot_key(fixed_token, surfaced) -> None:
    """A Slack-origin caller surfaces as slack_<ts>, never the raw slack:<ts>.

    removeprefix("dashboard:") left the colon key untouched, so the SPA's ?sid=
    matched no tab and the button opened a missing session.
    """
    key = "slack:1712793600.000001"
    surfaced({key})
    with patch("kiro_crew.dashboard.handlers.messaging.KiroCrewConfig") as cfg_cls:
        cfg_cls.load.return_value = _cfg()
        url = await _resolve_session_link_url(_link_state(), "", key)
    assert url == f"{_DASH_ORIGIN}/chat?sid=slack_1712793600.000001&token={_TOKEN}"
    assert "sid=slack:" not in url


@pytest.mark.asyncio
async def test_resolver_omits_link_for_unsurfaced_channel_key(fixed_token, surfaced) -> None:
    """A channel key with no open dashboard tab -> no link, rather than a ?sid=
    the SPA cannot resolve."""
    with patch("kiro_crew.dashboard.handlers.messaging.KiroCrewConfig") as cfg_cls:
        cfg_cls.load.return_value = _cfg()
        url = await _resolve_session_link_url(_link_state(), "", "slack:1712793600.000002")
    assert url == ""


@pytest.mark.asyncio
async def test_resolver_channel_origin_cron_omits_link(fixed_token, surfaced) -> None:
    """A cron whose origin session is a channel one: _channel_delivery_key returns
    job.session_key verbatim ('slack:<ts>'), and dashboard_slot_key finds no open
    tab, so the link is omitted rather than emitting /chat?sid=slack:<ts>."""
    state = _link_state()
    state.crons.list_jobs.return_value = [
        SimpleNamespace(id="abc123", session_key="slack:1712793600.000009", name="watch")
    ]
    with patch("kiro_crew.dashboard.handlers.messaging.KiroCrewConfig") as cfg_cls:
        cfg_cls.load.return_value = _cfg()
        url = await _resolve_session_link_url(state, "cron:abc123", "")
    assert url == ""


@pytest.mark.asyncio
async def test_resolver_mints_no_token_without_owner(fixed_token) -> None:
    """No owner id means no owner DM to post to, so no token is minted."""
    state = MagicMock()
    state.owner_id = ""
    with patch("kiro_crew.dashboard.handlers.messaging.KiroCrewConfig") as cfg_cls:
        cfg_cls.load.return_value = _cfg()
        url = await _resolve_session_link_url(state, "", _SESSION_KEY)
    assert url == f"{_DASH_ORIGIN}/chat?sid={_BARE_KEY}"


@pytest.mark.asyncio
async def test_no_button_to_a_named_channel(mock_sel, fixed_token) -> None:
    """Owner-DM only: a send addressed to a named (tracked) channel gets the
    message but never the session-link button, so the presigned token it carries
    can never leak into a shared channel."""
    slack = _mock_slack()
    state = _mock_state(slack)
    app = _make_app(state)
    stub = sys.modules["kiro_crew.slack.handler"]
    with (
        patch("kiro_crew.dashboard.handlers.messaging.KiroCrewConfig") as cfg_cls,
        patch.object(stub, "is_tracked_channel", lambda cid: True),
    ):
        cfg_cls.load.return_value = _cfg()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/send-message",
                json={
                    "text": "hi",
                    "session": "slack",
                    "channel": "C0123456789",
                    "include_session_link": True,
                },
                headers={"X-Session-Key": _SESSION_KEY},
            )
            assert resp.status == 200
            assert (await resp.json())["delivered_to"] == "slack"
    slack.post_message.assert_awaited_once()
    slack.post_blocks.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolver_omits_link_for_closed_dashboard_tab(fixed_token) -> None:
    """A dashboard-origin key resolves to a slot key even after its tab is closed
    (``has_dashboard_surface`` short-circuits for ``dashboard:`` keys), so the
    resolver confirms a LIVE slot still exists and omits the link when it does
    not -- no button that opens "Session not found." (GPT F1)."""
    state = _link_state()
    state.get_slot = MagicMock(return_value=None)  # tab closed / slot gone
    with patch("kiro_crew.dashboard.handlers.messaging.KiroCrewConfig") as cfg_cls:
        cfg_cls.load.return_value = _cfg()
        url = await _resolve_session_link_url(state, "", _SESSION_KEY)
    assert url == ""
    state.get_slot.assert_called_once_with(_BARE_KEY)


@pytest.mark.asyncio
async def test_button_rides_caller_blocks_as_one_message(mock_sel, fixed_token) -> None:
    """A caller that sends its own Block Kit blocks gets the button APPENDED to
    them -- one message (one notification), not a blocks message plus a
    bare-button follow-up (UX double-buzz fix)."""
    slack = _mock_slack()
    state = _mock_state(slack)
    app = _make_app(state)
    caller_blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "rich body"}}]
    with patch("kiro_crew.dashboard.handlers.messaging.KiroCrewConfig") as cfg_cls:
        cfg_cls.load.return_value = _cfg()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/send-message",
                json={
                    "text": "rich body",
                    "session": "slack",
                    "blocks": caller_blocks,
                    "include_session_link": True,
                },
                headers={"X-Session-Key": _SESSION_KEY},
            )
            assert resp.status == 200
    slack.post_message.assert_not_awaited()
    slack.post_blocks.assert_awaited_once()
    posted = slack.post_blocks.call_args.args[1]
    # Caller's block(s) first, then the button, then the expiry-expectation
    # context line (the sign-in window hint, so a late tap is not a surprise).
    assert posted[0]["type"] == "section"
    assert posted[-2]["type"] == "actions"
    assert posted[-1]["type"] == "context"
    assert "min" in posted[-1]["elements"][0]["text"]
    assert (
        _button(slack.post_blocks)["url"] == f"{_DASH_ORIGIN}/chat?sid={_BARE_KEY}&token={_TOKEN}"
    )


def test_tunnel_origin_if_opted_in_returns_tunnel_when_opted_in() -> None:
    """Opted in -> the live tunnel URL (shared fold, one place for the decision)."""
    from kiro_crew.dashboard.urls import tunnel_origin_if_opted_in

    with patch("kiro_crew.tunnel.get_tunnel_url", return_value="https://tun.example.com/"):
        assert tunnel_origin_if_opted_in(True) == "https://tun.example.com/"


def test_tunnel_origin_if_opted_in_empty_and_silent_when_opted_out() -> None:
    """Opted out -> "" WITHOUT even consulting the tunnel manager."""
    from kiro_crew.dashboard.urls import tunnel_origin_if_opted_in

    with patch("kiro_crew.tunnel.get_tunnel_url") as get_url:
        assert tunnel_origin_if_opted_in(False) == ""
        get_url.assert_not_called()
