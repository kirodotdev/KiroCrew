"""``format="markdown"`` on a proactive send renders on Telegram; plain stays the default.

Telegram's plain send shows ``**bold**`` with its asterisks, so a cron's report
or an agent's ``send_message`` carrying markup reached the chat literally. The
opt-in sends each part as Telegram HTML; a render Telegram cannot take goes out
as the plain send rather than as raw tags.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, create_autospec, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import api_send_message
from kiro_crew.mcp_core import _call_tool
from kiro_crew.messaging.link import ChannelLink
from kiro_crew.telegram.client import TELEGRAM_MAX_TEXT, TelegramClient
from kiro_crew.telegram.transport import TELEGRAM_CAPABILITIES, TelegramTransport


def _client(*results: int | None) -> Any:
    """A client held to ``TelegramClient``'s real signature, answering *results* in turn."""
    client = create_autospec(TelegramClient, instance=True)
    client.send_message.side_effect = list(results)
    return client


@pytest.mark.asyncio
async def test_markdown_send_goes_out_as_telegram_html() -> None:
    client = _client(11)
    transport = TelegramTransport(client)

    mid = await transport.send_markdown("42", "**41.4 kWh** and `code`", "5")

    assert mid == "11"
    client.send_message.assert_awaited_once_with(
        42,
        "<b>41.4 kWh</b> and <code>code</code>",
        parse_mode="HTML",
        retry_plain=False,
        message_thread_id=5,
        err_out={},
    )


def _refused_once_then(mid: int) -> Any:
    """A client whose first send is refused with a 400 and whose second answers *mid*."""
    client = create_autospec(TelegramClient, instance=True)
    answers = iter([None, mid])

    async def send(*_args: Any, err_out: dict | None = None, **_kw: Any) -> int | None:
        answer = next(answers)
        if answer is None and err_out is not None:
            err_out["error_code"] = 400
        return answer

    client.send_message.side_effect = send
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "client_for", "html_tries"),
    [
        pytest.param("**bold**", lambda: _refused_once_then(12), 1, id="html-refused"),
    ],
)
async def test_a_render_telegram_cannot_take_goes_out_plain(
    content: str, client_for: Any, html_tries: int
) -> None:
    client = client_for()
    transport = TelegramTransport(client)

    assert await transport.send_markdown("42", content) == "12"

    assert client.send_message.await_count == html_tries + 1
    assert client.send_message.await_args == call(42, content, message_thread_id=None)


@pytest.mark.asyncio
async def test_a_part_whose_render_outgrows_the_cap_is_split_and_still_rendered() -> None:
    # Each "**x**" grows by three characters once rendered, so a part that fits the
    # source budget renders past Telegram's cap.
    content = "**x** " * (TELEGRAM_MAX_TEXT // 6 + 200)
    client = create_autospec(TelegramClient, instance=True)
    client.send_message.side_effect = lambda *_a, **_kw: 7
    transport = TelegramTransport(client)

    assert await transport.send_markdown("42", content) == "7"

    calls = client.send_message.await_args_list
    assert len(calls) > 1
    assert all(c.kwargs.get("parse_mode") == "HTML" for c in calls)
    assert all(len(c.args[1]) <= TELEGRAM_MAX_TEXT for c in calls)
    assert all("**" not in c.args[1] for c in calls)


@pytest.mark.asyncio
async def test_the_second_split_grades_its_cuts_with_the_composed_redactor() -> None:
    # The outer split grades with redact_via_context; the re-split by rendered
    # length must too, or a companion-only credential could be cut across pieces.
    from kiro_crew.platform.context import redact_via_context
    from kiro_crew.telegram import transport as transport_module

    content = "**x** " * (TELEGRAM_MAX_TEXT // 6 + 200)
    client = create_autospec(TelegramClient, instance=True)
    client.send_message.side_effect = lambda *_a, **_kw: 7
    real = transport_module._split_markdown_bounded
    with patch.object(transport_module, "_split_markdown_bounded", side_effect=real) as split:
        assert await TelegramTransport(client).send_markdown("42", content) == "7"

    assert split.call_args_list[0].args == (content, TELEGRAM_MAX_TEXT, redact_via_context)


@pytest.mark.asyncio
async def test_a_failed_html_send_that_telegram_did_not_refuse_is_not_sent_again() -> None:
    # A timeout or 5xx leaves err_out without a 400: the HTML copy may already be in the chat.
    client = _client(None)
    transport = TelegramTransport(client)

    assert await transport.send_markdown("42", "**bold**") == ""

    client.send_message.assert_awaited_once()


def _route_state(transport: Any) -> MagicMock:
    state = MagicMock()
    state.slack_client = None
    state._slots = {}
    state.sessions.get_origin_link.return_value = ChannelLink(
        channel_type="telegram", channel_id="99887766"
    )
    state.sessions.get_mirror_link.return_value = None
    state.get_channel_transport.return_value = transport
    state.channel_transports = {"telegram": transport}
    state.crons.list_jobs.return_value = []
    return state


async def _post(state: MagicMock, body: dict[str, Any]):
    app = web.Application()
    app.router.add_post("/api/send-message", api_send_message)
    app["state"] = state
    permitted = MagicMock(permitted=True)
    with (
        patch("kiro_crew.sel.sel", return_value=MagicMock()),
        patch("kiro_crew.platform.governance_profiles.vet_and_audit", return_value=permitted),
    ):
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/send-message",
                json=body,
                headers={"X-Session-Key": "telegram:kirocrew:dm:99887766"},
            )
            return resp.status, await resp.json()


def _transport() -> MagicMock:
    transport = MagicMock()
    transport.send_message = AsyncMock(return_value="tg-1")
    transport.send_markdown = AsyncMock(return_value="tg-1")
    transport.capabilities = TELEGRAM_CAPABILITIES
    transport.configured_targets.return_value = [
        SimpleNamespace(target_id="user:99887766", available=True, label="DM")
    ]
    transport.resolve_configured_target = AsyncMock(return_value=("99887766", None))
    return transport


_LEGS = [
    pytest.param({"channel_type": "telegram"}, id="own-conversation"),
    pytest.param({"channel_type": "telegram", "target_id": "user:99887766"}, id="target"),
    pytest.param({"session": "telegram"}, id="owner-dm"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("leg", _LEGS)
@pytest.mark.parametrize(
    ("extra", "verb"),
    [
        pytest.param({"format": "markdown"}, "send_markdown", id="markdown"),
        pytest.param({}, "send_message", id="plain-default"),
    ],
)
async def test_each_channel_leg_sends_through_the_verb_the_format_names(
    leg: dict[str, str], extra: dict[str, str], verb: str
) -> None:
    transport = _transport()

    status, data = await _post(_route_state(transport), {"text": "*Title*", **leg, **extra})

    assert (status, data["delivered_to"]) == (200, "telegram")
    getattr(transport, verb).assert_awaited_once_with("99887766", "*Title*", thread_id=None)
    other = "send_message" if verb == "send_markdown" else "send_markdown"
    getattr(transport, other).assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("leg", _LEGS)
async def test_a_long_markdown_send_never_splits_inside_a_code_fence(leg: dict[str, str]) -> None:
    transport = _transport()
    text = "```\n" + "x = 1\n" * 670 + 'pattern = "**literal**"\n```'

    status, _ = await _post(_route_state(transport), {"text": text, **leg, "format": "markdown"})

    assert status == 200
    parts = [c.args[1] for c in transport.send_markdown.await_args_list]
    assert len(parts) > 1
    assert all(part.count("```") % 2 == 0 for part in parts)


@pytest.mark.asyncio
async def test_an_unknown_format_is_refused_before_any_send() -> None:
    transport = _transport()

    status, data = await _post(
        _route_state(transport), {"text": "hi", "channel_type": "telegram", "format": "html"}
    )

    assert (status, data["code"]) == (400, "format_unknown")
    transport.send_message.assert_not_awaited()
    transport.send_markdown.assert_not_awaited()


def test_the_tool_forwards_format_to_the_gateway() -> None:
    with (
        patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "cron:abc123"}),
        patch("kiro_crew.mcp_core._post") as post,
    ):
        post.return_value = {"ok": True, "delivered_to": "telegram"}
        _call_tool("send_message", {"text": "*hi*", "session": "telegram", "format": "markdown"})

    assert post.call_args[0][1]["format"] == "markdown"
