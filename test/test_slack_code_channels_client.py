"""Slack Code channels: request validation and the typed agents.conversations client.

Every Slack exchange here is a recorded payload replayed by ``FakeApi``; no test
talks to Slack.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from kiro_crew.slack.code_channels import settings as cc_settings
from kiro_crew.slack.code_channels.client import CodeChannelsClient, parse_channel_info
from kiro_crew.slack.code_channels.models import (
    CANVAS_ALREADY_EXISTS,
    MAX_VIEW_CONTENT_BYTES,
    AgentCommand,
    CodeChannelApiError,
    CodeChannelRequestError,
    ContextBarItem,
    CreateRequest,
    SetCommandsRequest,
    SetPropertiesRequest,
    SetViewRequest,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _fresh_capability():
    # A recorded feature_disabled below reaches the process-wide latch.
    yield
    cc_settings.reset_capability()


class SlackApiErrorLike(Exception):
    """The shape slack_sdk raises on ok:false: the parsed body on ``.response``."""

    def __init__(self, data: dict[str, Any]) -> None:
        super().__init__(f"The request to the Slack API failed. {data}")
        self.response = data


class FakeApi:
    """Replays one recorded response (or raises) per method; records every call."""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, method: str, **kwargs: Any) -> Any:
        self.calls.append((method, kwargs.get("json") or kwargs.get("params") or {}))
        resp = self.responses.get(method, {"ok": True})
        if isinstance(resp, BaseException):
            raise resp
        return resp


def _client(responses: dict[str, Any] | None = None) -> tuple[CodeChannelsClient, FakeApi]:
    api = FakeApi(responses)
    return CodeChannelsClient(api), api


# ── Request validation (nothing is sent) ─────────────────────────────────────


def _bar(n: int) -> tuple[ContextBarItem, ...]:
    return tuple(ContextBarItem(key=f"k{i}", label=f"L{i}") for i in range(n))


@pytest.mark.parametrize(
    "request_obj",
    [
        SetPropertiesRequest("C1", context_bar_items=_bar(6)),
        SetPropertiesRequest(
            "C1", context_bar_items=(ContextBarItem("k", "a"), ContextBarItem("k", "b"))
        ),
        SetPropertiesRequest(
            "C1", context_bar_items=(ContextBarItem("k", "a", url="https://x", item_type="action"),)
        ),
        SetPropertiesRequest("C1", context_bar_items=(ContextBarItem("k", "a", url="http://x"),)),
        SetPropertiesRequest("C1", context_bar_items=(ContextBarItem("k", "a", icon="rocket"),)),
        SetPropertiesRequest("C1"),
        SetCommandsRequest("C1", tuple(AgentCommand(f"c{i}", "d") for i in range(11))),
        SetCommandsRequest("C1", (AgentCommand("/run", "d"),)),
        SetCommandsRequest("C1", (AgentCommand("a", "d"), AgentCommand("a", "e"))),
        SetViewRequest("C1", type="html", view_key="v", content="x" * (MAX_VIEW_CONTENT_BYTES + 1)),
        SetViewRequest("C1", type="html", content="<p>"),
        SetViewRequest("C1", type="diff"),
        SetViewRequest("C1", type="canvas", view_key="v"),
        SetViewRequest("C1", type="pull_request", pr_url="http://github.com/x"),
        CreateRequest(name="t", session_id=""),
        CreateRequest(name="t", session_id="s", origin_channel_id="C1"),
    ],
)
async def test_invalid_requests_are_refused_before_sending(request_obj: Any) -> None:
    client, api = _client()
    method = {
        SetPropertiesRequest: client.set_properties,
        SetCommandsRequest: client.set_commands,
        SetViewRequest: client.set_view,
        CreateRequest: client.create,
    }[type(request_obj)]
    with pytest.raises(CodeChannelRequestError):
        await method(request_obj)
    assert api.calls == []


async def test_view_content_limit_counts_bytes_not_characters() -> None:
    client, api = _client({"agents.conversations.setView": {"ok": True, "view_id": "V1"}})
    # 500,001 two-byte characters: under 1M characters, over 1M bytes.
    with pytest.raises(CodeChannelRequestError):
        await client.set_view(SetViewRequest("C1", type="diff", content="é" * 500_001))
    assert api.calls == []


async def test_five_context_bar_items_and_ten_commands_are_accepted() -> None:
    client, api = _client({"agents.conversations.setCommands": {"ok": True, "command_count": 10}})
    await client.set_properties(SetPropertiesRequest("C1", context_bar_items=_bar(5)))
    count = await client.set_commands(
        SetCommandsRequest("C1", tuple(AgentCommand(f"c{i}", "d") for i in range(10)))
    )
    assert count == 10
    assert len(api.calls[0][1]["code_channel"]["context_bar_items"]) == 5


async def test_create_body_inherits_privacy_by_omitting_is_private() -> None:
    client, api = _client({"agents.conversations.create": {"ok": True, "channel_id": "C9"}})
    channel = await client.create(
        CreateRequest(
            name="fix-login",
            session_id="abc",
            origin_channel_id="G1",
            origin_message_ts="1.000001",
            team_id="T1",
        )
    )
    assert channel == "C9"
    method, body = api.calls[0]
    assert method == "agents.conversations.create"
    assert body == {
        "name": "fix-login",
        "session_id": "abc",
        "origin_channel_id": "G1",
        "origin_message_ts": "1.000001",
        "team_id": "T1",
    }
    assert "is_private" not in body


async def test_diff_view_never_sends_a_view_key() -> None:
    client, api = _client(
        {"agents.conversations.setView": {"ok": True, "view_id": "V1", "content_version": 2}}
    )
    view = await client.set_view(
        SetViewRequest("C1", type="diff", view_key="ignored", content="--- a")
    )
    assert view.view_id == "V1" and view.content_version == 2
    assert "view_key" not in api.calls[0][1]


async def test_channel_status_has_no_thread_ts() -> None:
    client, api = _client()
    await client.set_channel_status("C1", "processing")
    assert api.calls == [
        ("agents.sessions.setStatus", {"channel_id": "C1", "status": "processing"})
    ]
    with pytest.raises(CodeChannelRequestError):
        await client.set_channel_status("C1", "busy")


async def test_remove_view_needs_exactly_one_selector() -> None:
    client, api = _client()
    with pytest.raises(CodeChannelRequestError):
        await client.remove_view("C1")
    with pytest.raises(CodeChannelRequestError):
        await client.remove_view("C1", view_key="a", view_id="b")
    await client.remove_view("C1", view_id="V1")
    assert api.calls == [("agents.conversations.removeView", {"channel_id": "C1", "view_id": "V1"})]


# ── Responses and errors ─────────────────────────────────────────────────────


@pytest.mark.parametrize("code", ["feature_disabled", "missing_scope"])
async def test_unavailable_codes_are_typed(code: str) -> None:
    client, _ = _client(
        {"agents.conversations.create": SlackApiErrorLike({"ok": False, "error": code})}
    )
    with pytest.raises(CodeChannelApiError) as info:
        await client.create(CreateRequest(name="t", session_id="s"))
    assert info.value.code == code
    assert info.value.unavailable is True
    assert info.value.maybe_applied is False


async def test_ok_false_without_an_exception_still_raises() -> None:
    client, _ = _client(
        {"agents.conversations.archive": {"ok": False, "error": "not_agent_channel"}}
    )
    with pytest.raises(CodeChannelApiError) as info:
        await client.archive("C1")
    assert info.value.code == "not_agent_channel"
    assert info.value.unavailable is False


async def test_canvas_already_exists_is_final_and_sent_once() -> None:
    client, api = _client(
        {
            "conversations.canvases.create": SlackApiErrorLike(
                {"ok": False, "error": CANVAS_ALREADY_EXISTS}
            )
        }
    )
    with pytest.raises(CodeChannelApiError) as info:
        await client.create_canvas("C1", "# Plan")
    assert info.value.code == CANVAS_ALREADY_EXISTS
    assert len(api.calls) == 1


async def test_transport_failure_on_a_write_may_have_applied() -> None:
    client, api = _client({"agents.conversations.create": ConnectionResetError("boom")})
    with pytest.raises(CodeChannelApiError) as info:
        await client.create(CreateRequest(name="t", session_id="s"))
    assert info.value.code == "transport_error"
    assert info.value.maybe_applied is True
    assert len(api.calls) == 1  # never retried by the client


async def test_transport_failure_on_a_read_wrote_nothing() -> None:
    client, _ = _client({"agents.conversations.listViews": TimeoutError()})
    with pytest.raises(CodeChannelApiError) as info:
        await client.list_views("C1")
    assert info.value.maybe_applied is False


async def test_tokens_in_errors_are_redacted_from_logs(caplog: pytest.LogCaptureFixture) -> None:
    # Assembled at runtime so no committed line is token-shaped.
    token = "-".join(("xox" + "b", "1" * 12, "2" * 12, "abcdefghijklmnopqrstuvwx"))
    client, _ = _client({"agents.conversations.setView": RuntimeError(f"bad auth {token}")})
    with caplog.at_level(logging.DEBUG, logger="kiro_crew.slack.code_channels.client"):
        with pytest.raises(CodeChannelApiError):
            await client.set_view(SetViewRequest("C1", type="diff", content="x"))
    assert token not in caplog.text
    assert "did not complete" in caplog.text


async def test_list_views_parses_recorded_payload() -> None:
    recorded = {
        "ok": True,
        "views": [
            {
                "view_id": "V1",
                "view_key": "plan.html",
                "file_id": "F1",
                "label": "Plan",
                "content_version": 3,
            },
            {"view_id": "V2", "type": "diff"},
            "junk",
        ],
    }
    client, _ = _client({"agents.conversations.listViews": recorded})
    views = await client.list_views("C1")
    assert [v.view_id for v in views] == ["V1", "V2"]
    assert views[0].label == "Plan" and views[0].content_version == 3


# ── conversations.info recognition payloads ──────────────────────────────────


async def test_channel_info_reads_record_type_bots_and_origin() -> None:
    recorded = {
        "id": "C1",
        "name": "fix-login",
        "is_private": True,
        "properties": {
            "record_channel": {"record_type": "agent_channel"},
            "agent_session": {
                "encoded_agent_bot_user_ids": ["UBOT", "UOTHER"],
                "origin_link": {"channel_id": "G1", "ts": "1.000001"},
            },
            # A fresh code channel's code_channel block is empty: never gate on it.
            "code_channel": {},
        },
    }
    info = parse_channel_info("C1", recorded)
    assert info.is_code_channel
    assert info.agent_bot_user_ids == {"UBOT", "UOTHER"}
    assert (info.origin_channel_id, info.origin_ts) == ("G1", "1.000001")
    assert info.is_private and info.name == "fix-login"


@pytest.mark.parametrize(
    "raw", [None, {}, {"properties": "x"}, {"properties": {"record_channel": {"record_type": "x"}}}]
)
async def test_channel_info_tolerates_any_ordinary_shape(raw: Any) -> None:
    info = parse_channel_info("C1", raw)
    assert not info.is_code_channel
    assert info.agent_bot_user_ids == frozenset()


async def test_channel_info_is_a_query_with_the_home_team() -> None:
    api = FakeApi({"conversations.info": {"ok": True, "channel": {}}})
    seen: dict[str, Any] = {}

    async def call(method: str, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return await api(method, **kwargs)

    client = CodeChannelsClient(call, team_for=lambda ch: "T9")
    await client.channel_info("C1")
    assert seen["http_verb"] == "GET"
    assert seen["params"] == {"channel": "C1", "team_id": "T9"}
