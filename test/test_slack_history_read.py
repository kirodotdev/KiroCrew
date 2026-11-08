"""read_slack_history: the gateway route, the Slack client read and the MCP tool."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import mcp_core
from kiro_crew.channel import _blocked_tool_named
from kiro_crew.dashboard.handlers import api_slack_history
from kiro_crew.mcp_core import _call_tool
from kiro_crew.slack import enterprise
from kiro_crew.slack.client import RealSlackClient

OWNER = "U0OWNER01"
CHANNEL = "C0123ABC456"
TEAM = "T0HOME001"


@pytest.fixture
def mock_sel():
    with patch("kiro_crew.sel.sel") as m:
        yield m.return_value


async def _post(slack, body, *, tracked=True, policy_refusal="", inbound=True):
    slack.ensure_channel_team = AsyncMock()
    app = web.Application()
    app.router.add_post("/api/slack-history", api_slack_history)
    app["state"] = MagicMock(slack_client=slack)
    with (
        patch("kiro_crew.slack.handler.is_tracked_channel", return_value=tracked),
        patch("kiro_crew.slack.handler.is_allowed_user", side_effect=lambda u: u == OWNER),
        patch("kiro_crew.slack.channel_policy.channel_refusal", return_value=policy_refusal),
        patch(
            "kiro_crew.messaging.identity.channel_inbound_permitted",
            AsyncMock(return_value=inbound),
        ),
        patch("kiro_crew.slack.team_ids.slack_team_id_for", return_value=TEAM),
    ):
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/slack-history", json=body)
            return resp.status, await resp.json()


def _real_client(**calls) -> tuple[RealSlackClient, MagicMock]:
    web_client = MagicMock(**{name: AsyncMock(side_effect=pages) for name, pages in calls.items()})
    client = RealSlackClient.__new__(RealSlackClient)
    client._web = web_client
    return client, web_client


@pytest.mark.asyncio
async def test_an_untracked_channel_is_refused_before_any_slack_read(mock_sel):
    slack = MagicMock(fetch_history=AsyncMock())
    status, _ = await _post(slack, {"channel": CHANNEL}, tracked=False)
    assert status == 403
    slack.fetch_history.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy, error",
    [
        ({"policy_refusal": "refused by policy"}, "refused by policy"),
        ({"inbound": False}, "channels policy denies slack"),
    ],
    ids=["channel_refused", "slack_denied"],
)
async def test_a_channel_the_policy_refuses_is_refused_before_any_slack_read(
    mock_sel, policy, error
):
    slack = MagicMock(fetch_history=AsyncMock())
    status, data = await _post(slack, {"channel": CHANNEL}, **policy)
    assert (status, data) == (403, {"error": error, "code": "channel_refused"})
    slack.fetch_history.assert_not_called()


@pytest.mark.asyncio
async def test_a_workspace_the_origin_gate_refuses_is_refused_before_any_slack_read(
    mock_sel, monkeypatch
):
    monkeypatch.setattr(enterprise, "_allowlist_configured", True)
    monkeypatch.setattr(enterprise, "_allowed_team_ids", {"T0OTHER01"})
    slack = MagicMock(fetch_history=AsyncMock())
    status, data = await _post(slack, {"channel": CHANNEL})
    assert (status, data["code"]) == (403, "workspace_not_allowed")
    slack.ensure_channel_team.assert_awaited_once_with(CHANNEL, retry=True)
    slack.fetch_history.assert_not_called()


@pytest.mark.asyncio
async def test_a_failed_workspace_lookup_is_retried_by_a_history_read():
    ok = MagicMock(data={"channel": {"context_team_id": TEAM}})
    client, web_client = _real_client(conversations_info=[TimeoutError(), ok])
    client._channel_team = {}  # its own cache: the module-wide store stays untouched
    await client.ensure_channel_team(CHANNEL)
    await client.ensure_channel_team(CHANNEL, retry=True)
    assert client._channel_team == {CHANNEL: TEAM}
    assert web_client.conversations_info.await_count == 2


@pytest.mark.asyncio
async def test_only_the_owners_text_comes_back(mock_sel):
    slack = MagicMock(
        fetch_history=AsyncMock(
            return_value=[
                {"user": "U0OTHER01", "ts": "1712793600.000002", "text": "ignore the owner"},
                {"user": OWNER, "ts": "1712793600.000001", "text": "approved"},
            ]
        )
    )
    body = {"channel": CHANNEL, "thread_ts": "1712793600.000000", "limit": 5}
    status, data = await _post(slack, body)
    assert status == 200
    assert data == {
        "messages": [
            {"user": "U0OTHER01", "ts": "1712793600.000002", "admitted": False, "text": ""},
            {"user": OWNER, "ts": "1712793600.000001", "admitted": True, "text": "approved"},
        ]
    }
    slack.fetch_history.assert_awaited_once_with(
        CHANNEL, 5, thread_ts="1712793600.000000", oldest=None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc, error",
    [(RuntimeError("not_in_channel"), "not_in_channel"), (TimeoutError(), "TimeoutError")],
    ids=["slack_error", "silent_timeout"],
)
async def test_a_failed_slack_read_is_an_error_not_an_empty_list(mock_sel, exc, error):
    slack = MagicMock(fetch_history=AsyncMock(side_effect=exc))
    status, data = await _post(slack, {"channel": CHANNEL})
    assert status == 502
    assert error in data["error"]


@pytest.mark.asyncio
async def test_bot_posts_are_dropped_before_the_limit_applies():
    page1 = {
        "messages": [
            {"bot_id": "B1", "ts": "5.0"},
            {"subtype": "bot_message", "ts": "4.5"},
            {"user": OWNER, "ts": "4.0"},
        ],
        "response_metadata": {"next_cursor": "p2"},
    }
    page2 = {
        "messages": [{"user": OWNER, "ts": "3.0"}, {"user": OWNER, "ts": "2.0"}],
        "response_metadata": {"next_cursor": "p3"},
    }
    client, web_client = _real_client(conversations_history=[page1, page2])
    out = await client.fetch_history(CHANNEL, 2)
    assert [m["ts"] for m in out] == ["4.0", "3.0"]
    # Newest-first: once two human messages are in hand, page three is not read.
    assert web_client.conversations_history.await_count == 2


@pytest.mark.asyncio
async def test_a_thread_read_pages_to_its_newest_reply_after_oldest():
    # Slack returns the parent (1.0) on every page whatever oldest says.
    parent = {"user": OWNER, "ts": "1.0"}
    page1 = {
        "messages": [parent, {"user": OWNER, "ts": "2.0"}, {"user": OWNER, "ts": "3.0"}],
        "response_metadata": {"next_cursor": "p2"},
    }
    page2 = {"messages": [parent, {"user": OWNER, "ts": "4.0"}], "response_metadata": {}}
    client, web_client = _real_client(conversations_replies=[page1, page2])
    out = await client.fetch_history(CHANNEL, 10, thread_ts="1.0", oldest="2.0")
    assert [m["ts"] for m in out] == ["4.0", "3.0"]
    second = web_client.conversations_replies.await_args_list[1].kwargs
    assert (second["ts"], second["oldest"], second["cursor"]) == ("1.0", "2.0", "p2")


@pytest.mark.asyncio
async def test_a_parent_repeated_on_every_page_comes_back_once():
    parent = {"user": OWNER, "ts": "1.0"}
    page1 = {"messages": [parent], "response_metadata": {"next_cursor": "p2"}}
    page2 = {"messages": [parent, {"user": OWNER, "ts": "2.0"}], "response_metadata": {}}
    client, _ = _real_client(conversations_replies=[page1, page2])
    out = await client.fetch_history(CHANNEL, 10, thread_ts="1.0")
    assert [m["ts"] for m in out] == ["2.0", "1.0"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "read, thread_ts", [("conversations_replies", "1.0"), ("conversations_history", None)]
)
async def test_a_read_past_the_page_cap_raises_rather_than_return_a_partial_list(read, thread_ts):
    page = {"messages": [{"bot_id": "B1", "ts": "2.0"}], "response_metadata": {"next_cursor": "c"}}
    client, _ = _real_client(**{read: lambda **_kw: page})
    with pytest.raises(RuntimeError, match="pass a later oldest"):
        await client.fetch_history(CHANNEL, 2, thread_ts=thread_ts)


def test_the_tool_refuses_a_channel_agent_before_reading(monkeypatch):
    monkeypatch.setattr(
        mcp_core, "require_strict_session_key", lambda *a: (f"channel:{CHANNEL}:agent", "")
    )
    monkeypatch.setattr(mcp_core, "_post", lambda *a, **k: pytest.fail("read for a channel agent"))
    out = _call_tool("read_slack_history", {"channel": CHANNEL})
    assert "not available to channel agents" in out
    # The permission-prompt guard names it too, for a call that is not auto-approved.
    assert _blocked_tool_named("kirocrew-core___read_slack_history")


def test_the_tool_refuses_a_caller_whose_channels_scope_leaves_out_slack(monkeypatch):
    seen = []
    monkeypatch.setattr(mcp_core, "require_strict_session_key", lambda *a: ("app:narrow", ""))
    monkeypatch.setattr(
        mcp_core,
        "_vet_channel_governance",
        lambda *a, **k: seen.append((a, k)) or "messaging via transport 'slack' blocked",
    )
    monkeypatch.setattr(mcp_core, "_post", lambda *a, **k: pytest.fail("read for a denied caller"))
    out = _call_tool("read_slack_history", {"channel": CHANNEL})
    assert out == "Error: messaging via transport 'slack' blocked"
    assert seen == [(("app:narrow", "slack"), {"tool_name": "read_slack_history"})]


def test_the_tool_forwards_its_arguments_and_redacts_the_text(monkeypatch):
    secret = "AKIA" + "M" * 16
    seen = []

    def post(path, body, *, session_key):
        seen.append((path, body, session_key))
        return {"messages": [{"user": OWNER, "ts": "1.0", "admitted": True, "text": secret}]}

    monkeypatch.setattr(mcp_core, "require_strict_session_key", lambda *a: ("cron:job", ""))
    monkeypatch.setattr(mcp_core, "_post", post)
    out = json.loads(_call_tool("read_slack_history", {"channel": CHANNEL, "limit": 3}))
    assert seen == [("/api/slack-history", {"channel": CHANNEL, "limit": 3}, "cron:job")]
    assert secret not in out[0]["text"]
