"""Slack Code channels: request validation and the typed agents.conversations client.

Every Slack exchange here is a recorded payload replayed by ``FakeApi``; no test
talks to Slack.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from kiro_crew.slack import features
from kiro_crew.slack.code_channels.client import CodeChannelsClient, parse_channel_info
from kiro_crew.slack.code_channels.models import (
    MAX_TITLE_CHARS,
    CodeChannelApiError,
    CodeChannelRequestError,
    CreateRequest,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _fresh_capability():
    # A recorded feature_disabled below reaches the process-wide latch.
    yield
    features.reset_capabilities()


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


@pytest.mark.parametrize(
    "request_obj",
    [
        CreateRequest(name="t", session_id=""),
        CreateRequest(name="t", session_id="s", origin_channel_id="C1"),
        CreateRequest(name="", session_id="s"),
        CreateRequest(name="x" * (MAX_TITLE_CHARS + 1), session_id="s"),
    ],
)
async def test_invalid_requests_are_refused_before_sending(request_obj: Any) -> None:
    client, api = _client()
    with pytest.raises(CodeChannelRequestError):
        await client.create(request_obj)
    assert api.calls == []


async def test_a_blank_channel_id_is_refused_before_sending() -> None:
    client, api = _client()
    with pytest.raises(CodeChannelRequestError):
        await client.channel_info("")
    assert api.calls == []


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
    client, _ = _client({"conversations.info": {"ok": False, "error": "channel_not_found"}})
    with pytest.raises(CodeChannelApiError) as info:
        await client.channel_info("C1")
    assert info.value.code == "channel_not_found"
    assert info.value.unavailable is False


async def test_transport_failure_on_a_write_may_have_applied() -> None:
    client, api = _client({"agents.conversations.create": ConnectionResetError("boom")})
    with pytest.raises(CodeChannelApiError) as info:
        await client.create(CreateRequest(name="t", session_id="s"))
    assert info.value.code == "transport_error"
    assert info.value.maybe_applied is True
    assert len(api.calls) == 1  # never retried by the client


async def test_transport_failure_on_a_read_wrote_nothing() -> None:
    client, _ = _client({"conversations.info": TimeoutError()})
    with pytest.raises(CodeChannelApiError) as info:
        await client.channel_info("C1")
    assert info.value.maybe_applied is False


async def test_tokens_in_errors_are_redacted_from_logs(caplog: pytest.LogCaptureFixture) -> None:
    # Assembled at runtime so no committed line is token-shaped.
    token = "-".join(("xox" + "b", "1" * 12, "2" * 12, "abcdefghijklmnopqrstuvwx"))
    client, _ = _client({"agents.conversations.create": RuntimeError(f"bad auth {token}")})
    with caplog.at_level(logging.DEBUG, logger="kiro_crew.slack.code_channels.client"):
        with pytest.raises(CodeChannelApiError):
            await client.create(CreateRequest(name="t", session_id="s"))
    assert token not in caplog.text
    assert "did not complete" in caplog.text


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
