"""The Slack, Teams, Webex, WhatsApp and Telegram approval prompts name the MCP server.

``email_send`` alone does not say which mail server sends the mail, and the model
writes the title, so each prompt names the trusted ``_meta.kiro`` server and tool
(``Renderer.prompt_mcp_identity``) through the one shared
``mcp_identity_line``, escaped for its own markup. A prompt with no proven server
carries no identity line at all. Each surface is driven through
``Renderer.dispatch`` with a real ``PROMPT_CHOICE`` event, on the same fake clients
its own renderer tests use, and the text it posts is read back.
"""

from __future__ import annotations

import html
import json
from collections.abc import Awaitable, Callable

import pytest
from test_slack_renderer import _RecSlack
from test_teams_renderer import _FakeClient as _TeamsClient
from test_telegram import FakeClient as _TelegramClient
from test_webex_renderer import FakeClient as _WebexClient
from test_whatsapp_turn_renderer import CAPS as _WHATSAPP_CAPS
from test_whatsapp_turn_renderer import _pair as _whatsapp_pair

from kiro_crew.messaging.approval import abandon_approval
from kiro_crew.messaging.renderer import PROMPT_CHOICE, OutputEvent
from kiro_crew.slack.renderer import SlackRenderer
from kiro_crew.teams.approvals import TeamsApprovalDecider
from kiro_crew.teams.renderer import TeamsRenderer
from kiro_crew.teams.transport import TEAMS_CAPABILITIES
from kiro_crew.telegram.renderer import TelegramRenderer
from kiro_crew.telegram.renderer_approval import TelegramApprovalDecider
from kiro_crew.telegram.transport import TELEGRAM_CAPABILITIES
from kiro_crew.testing.synthetic_credentials import github_classic
from kiro_crew.webex.renderer import WebexRenderer
from kiro_crew.webex.transport import WEBEX_CAPABILITIES
from kiro_crew.whatsapp.turn_renderer import WhatsAppRenderer

_TOKEN = github_classic()


def _event(server: str, tool: str = "email_send") -> OutputEvent:
    return OutputEvent(
        kind=PROMPT_CHOICE,
        request_id="r1",
        title="email_send",
        tool_name=tool,
        mcp_server=server,
    )


async def _slack(event: OutputEvent) -> list[str]:
    rec = _RecSlack()
    await SlackRenderer(rec, "C1", None, reactions_enabled=False).dispatch(event)
    (blocks,) = [kw["blocks"] for m, kw in rec.calls if m == "post_blocks"]
    return [json.dumps(blocks, ensure_ascii=False)]


async def _teams(event: OutputEvent) -> list[str]:
    client = _TeamsClient()
    decider = TeamsApprovalDecider(session_key="teams:s1")
    renderer = TeamsRenderer(
        client, "conv-1", "https://smba.trafficmanager.net/", TEAMS_CAPABILITIES, decider=decider
    )
    try:
        await renderer.dispatch(event)
    finally:
        decider.discard_reservations()
    (card,) = client.cards
    return ["\n".join(block["text"] for block in card["content"]["body"])]


async def _webex(event: OutputEvent) -> list[str]:
    client = _WebexClient()
    renderer = WebexRenderer(
        client, "ROOM", WEBEX_CAPABILITIES, mint_approval_nonce=lambda rid: "nonce"
    )
    await renderer.dispatch(event)
    ((_, text, kw),) = client.sent_full
    (card,) = kw["attachments"]
    # Both the text prompt and the card it carries: a card-capable client may show
    # the card alone.
    return [text, "\n".join(block["text"] for block in card["content"]["body"])]


async def _whatsapp(event: OutputEvent) -> list[str]:
    transport, client = _whatsapp_pair()
    renderer = WhatsAppRenderer(
        transport, client, "chat@s.whatsapp.net", _WHATSAPP_CAPS, approval_session_key="wa:s1"
    )
    try:
        await renderer.dispatch(event)
    finally:
        abandon_approval("wa:s1", "r1")
    return [text for _, text in transport.sent]


async def _telegram(event: OutputEvent) -> list[str]:
    client = _TelegramClient()
    renderer = TelegramRenderer(client, 42, TELEGRAM_CAPABILITIES, session_key="telegram:1")
    try:
        await renderer.dispatch(event)
    finally:
        TelegramApprovalDecider.retire(TelegramApprovalDecider.key("telegram:1", "r1"))
    return [entry[0] for entry in client.sent]


#: Each surface and the identity line it shows for server ``mail``, tool ``email_send``.
_SURFACES: list[tuple[str, Callable[[OutputEvent], Awaitable[list[str]]], list[str]]] = [
    ("slack", _slack, ["MCP tool: *mail* / *email_send*"]),
    ("teams", _teams, ["MCP tool: `mail` / `email_send`"]),
    ("webex", _webex, ["MCP tool: `mail` / `email_send`", "MCP tool: mail / email_send"]),
    ("whatsapp", _whatsapp, ["MCP tool: mail / email_send"]),
    ("telegram", _telegram, ["MCP tool: <code>mail</code> / <code>email_send</code>"]),
]


@pytest.mark.parametrize(
    ("render", "lines"), [(r, lines) for _, r, lines in _SURFACES], ids=[s for s, _, _ in _SURFACES]
)
class TestEveryPromptNamesTheServer:
    @pytest.mark.asyncio
    async def test_a_proven_identity_is_named(self, render, lines) -> None:
        shown = await render(_event("mail"))
        assert len(shown) == len(lines), shown
        for text, line in zip(shown, lines):
            assert line in text, text

    @pytest.mark.asyncio
    async def test_no_server_adds_no_identity_line(self, render, lines) -> None:
        shown = await render(_event(""))
        assert shown and all("MCP tool:" not in text for text in shown), shown


class TestEachChannelEscapesTheNames:
    @pytest.mark.asyncio
    async def test_a_backtick_cannot_close_a_code_span(self) -> None:
        (text,) = await _teams(_event("a`b`c"))
        assert "MCP tool: `aˋbˋc` / `email_send`" in text, text

    @pytest.mark.asyncio
    async def test_telegram_escapes_html(self) -> None:
        (text,) = await _telegram(_event("<b>mail</b>"))
        assert f"<code>{html.escape('<b>mail</b>')}</code>" in text, text
        assert "<b>mail</b>" not in text

    @pytest.mark.asyncio
    async def test_webex_strips_link_markup(self) -> None:
        text, card = await _webex(_event("[mail](https://evil.example)"))
        assert "MCP tool:" in text and "MCP tool:" in card, (text, card)
        assert "](" not in text and "](" not in card


class TestTheSharedLine:
    def test_blank_server_is_no_line(self) -> None:
        from kiro_crew.messaging.renderer import mcp_identity_line

        assert mcp_identity_line("", "email_send") == ""
        assert mcp_identity_line("   ", "email_send") == ""

    def test_blank_tool_reads_unnamed_tool(self) -> None:
        from kiro_crew.messaging.renderer import mcp_identity_line

        assert mcp_identity_line(" mail ", " ") == "MCP tool: mail / unnamed tool"

    def test_the_cut_happens_before_the_formatter(self) -> None:
        from kiro_crew.messaging.renderer import MCP_IDENTITY_NAME_MAX, mcp_identity_line

        line = mcp_identity_line("<" * 300, "&" * 300, fmt=html.escape)
        assert line == (
            f"MCP tool: {'&lt;' * MCP_IDENTITY_NAME_MAX} / {'&amp;' * MCP_IDENTITY_NAME_MAX}"
        )

    def test_a_credential_in_a_name_is_redacted(self) -> None:
        from kiro_crew.messaging.renderer import mcp_identity_line

        line = mcp_identity_line(f"mail-{_TOKEN}", f"send {_TOKEN}")
        assert _TOKEN not in line and "REDACTED" in line, line
