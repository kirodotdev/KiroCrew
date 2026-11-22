"""The words on the transport Slack approval card: which tool, where, why, and with what.

:func:`kiro_crew.slack.renderer.build_approval_blocks` owns the card's buttons and
the token each one carries; this module owns only the text drawn above them, so
nothing a click is routed by depends on it.

Every string reaching here but the MCP identity is model-authored and must already
be display-safe (redacted against what Slack renders); :func:`bounded_scan` runs
that scan over only as much of each string as the card can show. The identity is
the trusted ``_meta.kiro`` server and tool, drawn by the native card's own
:func:`kiro_crew.slack.handler.mcp_identity_text`, so the two cards name a server
the same way.

What this module adds is safety against Slack's mrkdwn PARSER, a separate
hazard: ``<!channel>`` in a tool argument notifies a whole channel the moment the
card posts, and three backticks inside the arguments close the code fence early
and hand the rest to the parser as markup. It also keeps each text under Slack's
per-block limit, since a card over it is refused whole and the approver sees
nothing at all.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from kiro_crew.slack.format import escape_mrkdwn
from kiro_crew.slack.handler import (
    _SLACK_SECTION_TEXT_LIMIT,
    _TRUNCATION_MARKER,
    mcp_identity_text,
)

#: A backtick followed by another. Each gets a zero-width space after it, so no
#: two backticks stay adjacent inside the fence and none can close it. The space
#: is invisible, so the approver reads the arguments as they were sent.
_BACKTICK_PAIR_RE = re.compile(r"`(?=`)")

#: How much of one model-authored string the card's display scan reads. The card
#: shows at most ``_SLACK_SECTION_TEXT_LIMIT`` characters of each, so a 500 KB
#: write costs the scan what a 16 KB one does, on the event loop every turn shares.
_SCAN_CHARS = 16 * 1024


def bounded_scan(text: str, scan: Callable[[str], str]) -> str:
    """*scan* (the renderer's display-form redaction) of the part of *text* the card shows.

    A longer *text* is scanned as its first :data:`_SCAN_CHARS` characters, and the
    card shows only the start of those. The cut can split a key at the head's end,
    leaving its first part unredacted there; that end stays off the card while the
    scanned head is over half the bound. A head the scan shrank further (stripped
    escapes, a long secret's short tag) is answered by a scan of the whole text.
    """
    if len(text) <= _SCAN_CHARS:
        return scan(text)
    head = scan(text[:_SCAN_CHARS])
    return head if len(head) > _SCAN_CHARS // 2 else scan(text)


def _cut(text: str, marker: str) -> str:
    """*text* bounded to the section limit, ending in *marker* when it was cut.

    The cut backs off an entity it would split, so an escaped ``&amp;`` never
    shows as a stray ``&am``.
    """
    if len(text) <= _SLACK_SECTION_TEXT_LIMIT:
        return text
    head = text[: _SLACK_SECTION_TEXT_LIMIT - len(marker)]
    amp = head.rfind("&")
    if amp != -1 and ";" not in head[amp:]:
        head = head[:amp]
    return head + marker


def approval_header_text(title: str) -> str:
    """The card's first line, naming the tool *title* asks about."""
    return f"🔧 Approve tool *{_cut(escape_mrkdwn(title), '…')}*?"


def approval_detail_blocks(
    purpose: str, tool_input: str, mcp_identity: tuple[str, str] = ("", "")
) -> list[dict[str, Any]]:
    """The blocks between the card's header and its buttons.

    A context line naming the MCP server and tool when *mcp_identity* (the trusted
    ``(server, tool)`` pair) names a server, a context line with *purpose* when the
    tool declared one, then *tool_input* in a code block. An empty *tool_input* draws
    no block: ``""`` means the provider sent no detail, not that the tool takes no
    arguments, and an empty fence would claim the latter.
    """
    blocks: list[dict[str, Any]] = []
    identity = mcp_identity_text(*mcp_identity)
    if identity:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": identity}]})
    if purpose.strip():
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"_{_cut(escape_mrkdwn(purpose), '…')}_"}],
            }
        )
    if tool_input.strip():
        detail = _BACKTICK_PAIR_RE.sub("`\u200b", escape_mrkdwn(tool_input))
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"```\n{_cut(detail, _TRUNCATION_MARKER)}\n```"},
            }
        )
    return blocks
