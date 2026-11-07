"""A markdown heading picks Telegram's Rich Message sender, in replies and proactive sends.

The legacy HTML subset can only show ``# Title`` as bold text, and a proactive
send (an agent's ``send_message``, a cron's notify) never reached
``sendRichMessage`` at all. A heading or a pipe table now goes out rich; a
failed rich send falls back to the plain send only when nothing was posted.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import create_autospec

import pytest
from telegram_helpers import _drop_live_config_snapshot  # noqa: F401
from telegram_helpers import FakeClient

from kiro_crew.telegram.client import TelegramClient
from kiro_crew.telegram.renderer import TelegramRenderer
from kiro_crew.telegram.renderer_tables import _has_heading, _wants_rich
from kiro_crew.telegram.transport import TELEGRAM_CAPABILITIES, TelegramTransport

TABLE = "| a | b |\n| --- | --- |\n| 1 | 2 |"


@pytest.mark.parametrize(
    "text",
    ["# Title", "###### Six", "   # three spaces", "intro\n\n## Section\n\nbody", "#\tTab"],
)
def test_an_atx_heading_is_detected(text: str) -> None:
    assert _has_heading(text)
    assert _wants_rich(text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "#hashtag is not a heading",
        "####### seven hashes",
        "    # four spaces is a code block",
        "a line with # inside",
        "#",
        "plain **bold** text",
    ],
)
def test_text_without_a_heading_or_table_stays_plain(text: str) -> None:
    assert not _has_heading(text)
    assert not _wants_rich(text)


def test_a_table_alone_still_wants_rich() -> None:
    assert _wants_rich(TABLE)


def _client(rich_mid: int | None = None, rich_err: dict[str, Any] | None = None) -> Any:
    """A client held to ``TelegramClient``'s real signature.

    ``send_rich_message`` answers *rich_mid*, writing *rich_err* into ``err_out``
    when it fails; ``send_message`` answers 9.
    """
    client = create_autospec(TelegramClient, instance=True)

    async def rich(*_a: Any, err_out: dict | None = None, **_kw: Any) -> int | None:
        if rich_mid is None and err_out is not None and rich_err:
            err_out.update(rich_err)
        return rich_mid

    client.send_rich_message.side_effect = rich
    client.send_message.return_value = 9
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["# Brief\n\n- item", f"Costs:\n\n{TABLE}"])
async def test_a_proactive_heading_or_table_goes_out_rich(content: str) -> None:
    client = _client(rich_mid=7)

    assert await TelegramTransport(client).send_message("42", content, "5") == "7"

    client.send_rich_message.assert_awaited_once()
    call = client.send_rich_message.await_args
    assert call.args == (42, content)
    assert call.kwargs["message_thread_id"] == 5
    client.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_proactive_plain_message_never_touches_the_rich_api() -> None:
    client = _client(rich_mid=7)

    assert await TelegramTransport(client).send_message("42", "all quiet") == "9"

    client.send_rich_message.assert_not_awaited()
    client.send_message.assert_awaited_once_with(42, "all quiet", message_thread_id=None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rich_err",
    [
        pytest.param({"error_code": 400, "description": "Bad Request"}, id="refused-400"),
        pytest.param({"error_code": 404, "description": "Not Found"}, id="refused-404"),
        pytest.param({"error_code": 429, "description": "Too Many"}, id="refused-429"),
        pytest.param({"rich_unsupported": True}, id="latched-off"),
    ],
)
async def test_a_rich_send_that_posted_nothing_falls_back_to_plain(
    rich_err: dict[str, Any],
) -> None:
    client = _client(rich_mid=None, rich_err=rich_err)

    assert await TelegramTransport(client).send_message("42", "# Brief") == "9"

    client.send_message.assert_awaited_once_with(42, "# Brief", message_thread_id=None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rich_err",
    [
        pytest.param({}, id="timeout-or-transport-error"),
        pytest.param({"error_code": 502, "description": "Bad Gateway"}, id="server-5xx"),
        pytest.param(
            {"error_code": 400, "description": "", "body_is_json": False}, id="proxy-page"
        ),
    ],
)
async def test_a_rich_send_that_may_have_posted_is_not_sent_again(
    rich_err: dict[str, Any],
) -> None:
    client = _client(rich_mid=None, rich_err=rich_err)

    assert await TelegramTransport(client).send_message("42", "# Brief") == ""

    client.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_latched_client_reports_why_it_skipped_the_call() -> None:
    client = TelegramClient(token="0:test")
    client._rich_unsupported = True
    err: dict[str, Any] = {}

    assert await client.send_rich_message(1, "# Brief", err_out=err) is None

    assert err == {"rich_unsupported": True}


def test_a_heading_only_reply_is_sealed_as_a_rich_message() -> None:
    cli = FakeClient()
    r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]

    async def _go() -> None:
        await r.on_turn_start()
        await r.on_text_chunk("# Morning brief\n\n- 07:45 podcast ready")
        await r._stream_live(force=True)
        await r._seal_current()

    asyncio.run(_go())

    assert len(cli.rich_sent) == 1, "a heading is sealed via sendRichMessage"
    assert cli.rich_sent[0][0].startswith("# Morning brief")
    assert cli.deleted, "the streamed plaintext bubble is replaced"


def _failing_client(answers: list[tuple[int, str] | None]) -> tuple[TelegramClient, list[str]]:
    """A real client whose ``_api`` answers each ``(code, description)`` in turn."""
    client = TelegramClient(token="0:test")
    calls: list[str] = []
    seq = list(answers)

    async def _api(
        method: str,
        params: Any,
        timeout: int = 30,
        *,
        record: bool = True,
        err_out: dict | None = None,
    ) -> Any:
        calls.append(method)
        answer = seq.pop(0) if seq else None
        if answer is None:
            return {"message_id": 7}
        if err_out is not None:
            err_out["error_code"], err_out["description"] = answer
        return None

    client._api = _api  # type: ignore[method-assign]
    return client, calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        pytest.param((403, "Forbidden: bot was blocked by the user"), id="403-blocked"),
        pytest.param((400, "Bad Request: chat not found"), id="400-chat-not-found"),
        pytest.param((400, "Bad Request: message thread not found"), id="400-thread"),
    ],
)
async def test_a_per_chat_refusal_never_switches_rich_off_for_every_chat(
    answer: tuple[int, str],
) -> None:
    # Many such answers in a row, more than the 400 streak allows, and the
    # next chat still gets a rich message.
    client, calls = _failing_client([answer] * 5 + [None])

    for _ in range(5):
        assert await client.send_rich_message(1, "# Brief") is None
    assert client._rich_unsupported is False
    assert await client.send_rich_message(2, "# Brief") == 7
    assert len(calls) == 6


@pytest.mark.asyncio
async def test_a_server_level_refusal_still_switches_rich_off() -> None:
    client, calls = _failing_client([(404, "Not Found")])

    assert await client.send_rich_message(1, "# Brief") is None
    assert client._rich_unsupported is True
    assert await client.send_rich_message(1, "# Brief") is None
    assert calls == ["sendRichMessage"]
