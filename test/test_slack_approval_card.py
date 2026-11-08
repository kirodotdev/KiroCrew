"""What a Slack approval card tells the approver besides its buttons.

A card reading only ``Approve tool *email_send*?`` asks for consent to something
the reader cannot see: the server the call goes to, the recipient, the body, the
command. These tests drive the real ``TurnDriver`` into a ``SlackRenderer`` and
read the card it posts, pin the mrkdwn safety of every string on it, and pin that
the native card and the dashboard mirror name the server too.

The decision is made from inside ``post_blocks``, the moment the card is posted,
because the renderer opens the decision window before it posts: the driver then
adopts an already-decided window, so no test here waits on a loop pass count.
"""

from __future__ import annotations

import asyncio
import json
import re
from unittest.mock import AsyncMock, MagicMock

from test_slack_renderer import _Provider, _RecSlack

import kiro_crew.slack.handler as handler
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TOOL_CALL,
    AcpEvent,
)
from kiro_crew.messaging import APPROVAL_INTERACTIVE, PROMPT_CHOICE, OutputEvent, TurnDriver
from kiro_crew.slack import renderer as slack_renderer
from kiro_crew.slack.handler import _TRUNCATION_MARKER
from kiro_crew.slack.renderer import (
    TOOL_APPROVE_ACTION_PREFIX,
    TOOL_DENY_ACTION_PREFIX,
    TOOL_TRUST_ACTION_PREFIX,
    SlackApprovalDecider,
    SlackRenderer,
    build_approval_blocks,
    split_approval_token,
)

#: Slack refuses a whole post whose section or context text passes this.
_SLACK_TEXT_MAX = 3000


def _approve_value(blocks: list[dict]) -> str:
    """The ``value`` of the card's Approve button, or "" when *blocks* is no card."""
    for block in blocks:
        for element in block.get("elements", []):
            if str(element.get("action_id", "")).startswith(TOOL_APPROVE_ACTION_PREFIX):
                return str(element.get("value", ""))
    return ""


class _ApprovingSlack(_RecSlack):
    """Records like ``_RecSlack`` and presses Approve as soon as a card is posted.

    The press sends back the button's own value, as a click does, and goes through
    the registry the interaction handler uses.
    """

    async def post_blocks(self, channel, blocks, text, thread_ts=None, **kw):
        ts = await super().post_blocks(channel, blocks, text, thread_ts, **kw)
        value = _approve_value(blocks)
        if value:
            key, nonce = split_approval_token(value)
            assert SlackApprovalDecider.resolve_global(key, True, nonce=nonce)
        return ts


def _card_for(
    event: AcpEvent, *before: AcpEvent, renderer_cls: type[SlackRenderer] = SlackRenderer
) -> list[dict]:
    """The blocks of the approval card a permission request posts.

    *before* are events the provider streams ahead of the request.
    """
    return _turn_for(event, *before, renderer_cls=renderer_cls)[0]


def _turn_for(
    event: AcpEvent, *before: AcpEvent, renderer_cls: type[SlackRenderer] = SlackRenderer
) -> tuple[list[dict], SlackRenderer]:
    """The approval card a permission request posts, and the renderer that posted it."""
    rec = _ApprovingSlack()
    decider = SlackApprovalDecider(session_key="slack:t1")
    renderer = renderer_cls(rec, "C1", "t1", reactions_enabled=False, decider=decider)
    provider = _Provider([*before, event, AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")])
    driver = TurnDriver(provider, renderer, approval_mode=APPROVAL_INTERACTIVE, decider=decider)
    asyncio.run(driver.run("hi"))
    assert provider.approved == [event.request_id], rec.calls
    cards = [
        kw["blocks"] for m, kw in rec.calls if m == "post_blocks" and _approve_value(kw["blocks"])
    ]
    assert len(cards) == 1, rec.calls
    return cards[0], renderer


def _texts(blocks: list[dict]) -> list[str]:
    """Every mrkdwn string a reader sees on *blocks*, in order."""
    out: list[str] = []
    for block in blocks:
        if block.get("type") == "section":
            out.append(block["text"]["text"])
        elif block.get("type") == "context":
            out.extend(e["text"] for e in block["elements"] if e.get("type") == "mrkdwn")
    return out


def _fenced(blocks: list[dict]) -> list[str]:
    return [t for t in _texts(blocks) if t.startswith("```")]


class TestTheCardShowsWhatIsApproved:
    def test_the_card_shows_the_tool_arguments(self):
        card = _card_for(
            AcpEvent(
                kind=EVENT_PERMISSION_REQUEST,
                request_id="rq1",
                title="email_send",
                tool_input='{"to": "ceo@example.com", "subject": "Q3"}',
            )
        )
        shown = "\n".join(_texts(card))
        assert "email_send" in shown, card
        assert "ceo@example.com" in shown, card
        assert '"subject": "Q3"' in shown, card

    def test_the_card_shows_the_purpose_its_own_tool_call_declared(self):
        card = _card_for(
            AcpEvent(
                kind=EVENT_PERMISSION_REQUEST,
                request_id="rq1",
                title="email_send",
                tool_call_id="tc1",
                tool_input='{"to": "ceo@example.com"}',
            ),
            AcpEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id="tc1",
                title="email_send",
                tool_purpose="Send the Q3 summary",
            ),
        )
        assert any("Send the Q3 summary" in t for t in _texts(card)), card

    def test_a_purpose_renders_between_the_header_and_the_buttons(self):
        blocks = build_approval_blocks(
            "email_send", "rq1", "s", "n", purpose="Send the Q3 summary", tool_input="{}"
        )
        kinds = [b["type"] for b in blocks]
        assert kinds == ["section", "context", "section", "actions"], blocks
        assert "Send the Q3 summary" in blocks[1]["elements"][0]["text"], blocks

    def test_no_input_draws_no_detail_block(self):
        # "" means the provider sent no detail, not that the tool takes none, so
        # an empty fence would claim something nobody said.
        blocks = build_approval_blocks("email_send", "rq1", "s", "n")
        assert [b["type"] for b in blocks] == ["section", "actions"], blocks

    def test_the_buttons_and_tokens_do_not_change(self):
        plain = build_approval_blocks("grep", "rq9", "s", "n")
        detailed = build_approval_blocks("grep", "rq9", "s", "n", purpose="p", tool_input="x")
        assert plain[-1] == detailed[-1]
        ids = {e["action_id"] for e in detailed[-1]["elements"]}
        assert ids == {
            f"{TOOL_APPROVE_ACTION_PREFIX}s:rq9",
            f"{TOOL_TRUST_ACTION_PREFIX}s:rq9",
            f"{TOOL_DENY_ACTION_PREFIX}s:rq9",
        }


class TestTheCardIsSafeMrkdwn:
    def test_a_long_input_is_cut_under_slacks_section_limit(self):
        blocks = build_approval_blocks("write", "rq1", "s", "n", tool_input="x" * 5000)
        (fence,) = _fenced(blocks)
        assert len(fence) <= _SLACK_TEXT_MAX, len(fence)
        assert fence.endswith(_TRUNCATION_MARKER + "\n```"), fence[-40:]

    def test_every_text_on_the_card_fits_slacks_limit(self):
        # Escaping grows text (one `<` is four characters), so the bound is taken
        # on what is posted, not on what arrived.
        blocks = build_approval_blocks(
            "<" * 5000,
            "rq1",
            "s",
            "n",
            purpose="&" * 5000,
            tool_input="<" * 5000,
            mcp_identity=("<" * 600, "&" * 600),
        )
        assert len(_texts(blocks)) == 4, blocks
        for text in _texts(blocks):
            assert len(text) <= _SLACK_TEXT_MAX, len(text)

    def test_a_cut_never_leaves_half_an_entity(self):
        blocks = build_approval_blocks("w", "rq1", "s", "n", tool_input="<" * 5000)
        (fence,) = _fenced(blocks)
        body = fence[len("```\n") : -len(_TRUNCATION_MARKER + "\n```")]
        assert re.fullmatch(r"(&lt;)+", body), body[-20:]

    def test_backticks_in_the_input_cannot_close_the_fence(self):
        blocks = build_approval_blocks(
            "write", "rq1", "s", "n", tool_input="a ``` b ````` c *bold* <!here>"
        )
        (fence,) = _fenced(blocks)
        inner = fence[len("```") : -len("```")]
        assert "``" not in inner, inner
        # The defang is invisible: dropping the zero-width spaces gives the input back.
        assert "a ``` b ````` c" in inner.replace("\u200b", ""), inner

    def test_a_broadcast_in_the_title_or_input_is_escaped(self):
        card = _card_for(
            AcpEvent(
                kind=EVENT_PERMISSION_REQUEST,
                request_id="rq1",
                title="post <!channel>",
                tool_input='{"text": "hi <!here> & <@U123>"}',
            )
        )
        shown = "\n".join(_texts(card))
        assert "<!channel>" not in shown and "<!here>" not in shown, shown
        assert "<@U123>" not in shown, shown
        assert "&lt;!channel&gt;" in shown, shown
        assert "&lt;!here&gt; &amp; &lt;@U123&gt;" in shown, shown

    def test_a_key_split_by_markup_is_redacted_on_the_card(self):
        # The driver's byte-level pass reads `AKIA**...**` as no key at all; the
        # renderer's display-form pass is what catches it.
        key = "AKIA" + "**" + "1234567890ABCDEX" + "**"
        card = _card_for(
            AcpEvent(
                kind=EVENT_PERMISSION_REQUEST,
                request_id="rq1",
                title=f"aws {key}",
                tool_input=f'{{"key": "{key}"}}',
            )
        )
        shown = "\n".join(_texts(card))
        assert "1234567890ABCDEX" not in shown, shown
        assert "REDACTED" in shown, shown


class TestTheCardScansOnlyWhatItShows:
    """The display scan runs on the event loop every turn shares, and the card shows
    a few thousand characters of each string, so the scan reads a bounded head of a
    large argument while what the card shows stays redacted."""

    _KEY = "AKIA" + "**" + "1234567890ABCDEX" + "**"

    @staticmethod
    def _bound() -> int:
        # Imported at call time: on a tree without the module (the fix reverted), an
        # import at the top would fail collection and hide every other test's verdict.
        from kiro_crew.slack import approval_card

        return approval_card._SCAN_CHARS

    def test_a_large_argument_is_scanned_only_as_far_as_the_card_shows(self, monkeypatch):
        scanned: list[int] = []
        real = slack_renderer._display_safe

        def _counting(text: str) -> str:
            scanned.append(len(text))
            return real(text)

        monkeypatch.setattr(slack_renderer, "_display_safe", _counting)
        tool_input = json.dumps({"path": "a.py", "content": f"{self._KEY}\n" + "x = 1\n" * 40_000})
        bound = self._bound()
        assert len(tool_input) > 10 * bound
        card = _card_for(
            AcpEvent(
                kind=EVENT_PERMISSION_REQUEST,
                request_id="rq1",
                title="fs_write",
                tool_input=tool_input,
            )
        )
        (fence,) = _fenced(card)
        assert "1234567890ABCDEX" not in fence and "REDACTED" in fence, fence[:200]
        assert fence.endswith(_TRUNCATION_MARKER + "\n```"), fence[-40:]
        assert scanned and max(scanned) <= bound, max(scanned)

    def test_a_head_the_scan_shrinks_is_scanned_whole(self):
        # The scan strips colour escapes, so a head made of them shrinks to the key
        # the bound cuts at its end, which the card would then show: the whole text
        # is scanned instead, and that scan sees the key whole.
        bound = self._bound()
        pad = "\x1b[0m" * ((bound - 20) // 4)
        assert len(pad) + len("AKIA**1234567890ABCD") == bound
        card = _card_for(
            AcpEvent(
                kind=EVENT_PERMISSION_REQUEST,
                request_id="rq1",
                title="fs_write",
                tool_input=f"{pad}{self._KEY} tail",
            )
        )
        shown = "\n".join(_texts(card))
        assert "1234567890ABCD" not in shown, shown[-200:]
        assert "REDACTED" in shown, shown[-200:]


def _mcp_event(*, trusted: bool, server: str = "aws-outlook-mcp") -> AcpEvent:
    """An aws-outlook-mcp ``email_send`` permission request, its identity proven or not."""
    return AcpEvent(
        kind=EVENT_PERMISSION_REQUEST,
        request_id="rq1",
        title="email_send",
        tool_input='{"to": "alice@example.com", "body": "Q3"}',
        mcp_server_name=server,
        tool_name="email_send",
        mcp_identity_trusted=trusted,
    )


class TestEveryCardNamesTheServer:
    """``email_send`` alone does not say which mail server sends the mail, and the
    model writes the title, so each card names the trusted ``_meta.kiro`` server."""

    def test_the_transport_card_names_the_mcp_server_it_runs_on(self):
        card = _card_for(_mcp_event(trusted=True))
        assert [b["type"] for b in card] == ["section", "context", "section", "actions"], card
        assert card[1]["elements"][0]["text"] == "MCP tool: *aws-outlook-mcp* / *email_send*"

    def test_an_unproven_identity_names_no_server(self):
        # Only the ACP layer's provenance flag makes the pair the call's identity;
        # an unproven server name is not shown as if it were.
        card = _card_for(_mcp_event(trusted=False))
        assert "aws-outlook-mcp" not in json.dumps(card), card

    def test_the_identity_does_not_outlive_its_prompt(self):
        card, renderer = _turn_for(_mcp_event(trusted=True))
        assert "aws-outlook-mcp" in json.dumps(card), card
        assert renderer.prompt_mcp_identity == ("", "")

    def test_a_server_name_is_escaped(self):
        blocks = build_approval_blocks("t", "rq1", "s", "n", mcp_identity=("<!channel>", ""))
        assert blocks[1]["elements"][0]["text"] == "MCP tool: *&lt;!channel&gt;* / *unnamed tool*"

    def test_the_driver_passes_on_only_a_proven_identity(self):
        seen: list[OutputEvent] = []

        class _Recorder(SlackRenderer):
            async def dispatch(self, event: OutputEvent) -> None:
                if event.kind == PROMPT_CHOICE:
                    seen.append(event)
                await super().dispatch(event)

        for trusted in (True, False):
            _card_for(_mcp_event(trusted=trusted), renderer_cls=_Recorder)
        assert [(e.mcp_server, e.tool_name) for e in seen] == [
            ("aws-outlook-mcp", "email_send"),
            ("", ""),
        ]

    def test_the_native_card_names_the_server(self):
        footer = handler._build_approval_blocks(_mcp_event(trusted=True))[-1]
        texts = [e["text"] for e in footer["elements"]]
        assert texts[1] == "MCP tool: *aws-outlook-mcp* / *email_send*", footer
        assert "aws-outlook-mcp" not in json.dumps(
            handler._build_approval_blocks(_mcp_event(trusted=False))
        )

    def test_the_dashboard_mirror_names_the_server(self):
        slack = MagicMock()
        slack.post_blocks = AsyncMock(return_value="1781300000.0001")
        event = _mcp_event(trusted=True)
        try:
            ts = asyncio.run(
                handler.post_linked_approval(
                    slack,
                    "C_LINK",
                    "1781290000.0001",
                    event.request_id,
                    "dashboard:chat-1",
                    event.title,
                    event.tool_input,
                    mcp_server_name=event.mcp_server_name,
                    tool_name=event.tool_name,
                    mcp_identity_trusted=event.mcp_identity_trusted,
                )
            )
        finally:
            handler._linked_approvals.pop("C_LINK:1781300000.0001", None)
        assert ts == "1781300000.0001"
        blocks = slack.post_blocks.call_args.args[1]
        assert "MCP tool: *aws-outlook-mcp* / *email_send*" in json.dumps(blocks), blocks
