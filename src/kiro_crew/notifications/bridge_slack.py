"""The notification bridge's Slack sink: a bridged note as a DM to the owner.

The dispatcher has already vetted, rate-limited and redacted the note
(``bridge.py``); this sink renders it as escaped mrkdwn and posts it to the
owner's DM with the bot, opened through ``slack.retry.open_dm_with_retry``.

The client and the owner id are read off the gateway state at delivery time, so
a Slack connection that comes up after boot is picked up, and one that is down
makes the delivery an audited skip rather than an error.

Loop safety: the post is a bot message, which the Slack inbound path already
ignores, so a bridged note can never come back as a new turn or a new note.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from kiro_crew.notifications.bridge import BridgeTransportUnavailable
from kiro_crew.notifications.bridge_actions import QUESTION_CHOICES, SLACK_ACTION_PREFIX
from kiro_crew.notifications.bridge_approval_note import (
    ACTION_TOKEN_KEY,
    APPROVAL_DETAIL_KEY,
    SLACK_CARD_KEY,
    is_pending_approval_note,
)
from kiro_crew.notifications.bridge_question_note import OPTIONS_KEY, is_pending_question_note
from kiro_crew.slack.format import SLACK_MAX_TEXT, escape_mrkdwn
from kiro_crew.slack.retry import open_dm_with_retry

#: The marker a critical note's first line carries.
_CRITICAL_MARKER = ":rotating_light:"

#: Slack's limit on a section block's text. A longer last part is posted as the
#: message text with the buttons alone in its blocks.
_SECTION_MAX = 3000


def _redact(text: str) -> str:
    from kiro_crew.security.exfil import redact_exfiltration_urls
    from kiro_crew.security.redaction import redact_credentials

    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


def _detail_blocks(detail: str) -> list[dict[str, Any]]:
    """What the call will run, drawn by the native approval card's own renderer.

    Redacted first (``redact_note`` covers only the title and body), then
    ``approval_card.approval_detail_blocks`` escapes it for Slack's mrkdwn parser,
    breaks backtick runs so nothing closes the code fence, and cuts it to the
    section limit. Lazy: that module imports the Slack handler facade.
    """
    from kiro_crew.slack.approval_card import approval_detail_blocks, bounded_scan

    return approval_detail_blocks("", bounded_scan(detail, _redact))


def action_blocks(text: str, token: str, detail: str) -> list[dict[str, Any]]:
    """The Block Kit for a pending approval: its text, what will run, then Approve and Deny.

    *detail* is the call's input (or its title when it has none), redacted and
    shown above the buttons so the owner sees what an Approve runs. Each button's
    ``action_id`` names its answer and its ``value`` is the token, so the press
    handler reads both from what Slack sends back. The token appears in no text
    block.
    """
    buttons = [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": label},
            "style": style,
            "action_id": f"{SLACK_ACTION_PREFIX}{choice}",
            "value": token,
        }
        for choice, label, style in (
            ("approve", "Approve", "primary"),
            ("deny", "Deny", "danger"),
        )
    ]
    blocks: list[dict[str, Any]] = []
    if text and len(text) <= _SECTION_MAX:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
    blocks += _detail_blocks(detail)
    blocks.append({"type": "actions", "elements": buttons})
    return blocks


#: Slack's limit on a button's label.
_BUTTON_LABEL_MAX = 75


def button_label(label: str) -> str:
    """*label* as a Slack button shows it: redacted, on one line, cut to the limit."""
    shown = " ".join(_redact(label).split())
    if len(shown) > _BUTTON_LABEL_MAX:
        shown = shown[: _BUTTON_LABEL_MAX - 1].rstrip() + "…"
    return shown


def question_blocks(text: str, token: str, labels: list[str]) -> list[dict[str, Any]]:
    """The Block Kit for a pending question: its text, then one button per option.

    Each button's ``action_id`` names its option by position (``bridge_action_
    option_0``) and its ``value`` is the token. A label is agent-authored, so it is
    redacted and cut to Slack's button limit; a button label is ``plain_text``,
    which Slack never parses as markup. The token appears in no text block.
    """
    buttons = []
    for i, label in enumerate(labels):
        buttons.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": button_label(label)},
                "action_id": f"{SLACK_ACTION_PREFIX}option_{i}",
                "value": token,
            }
        )
    blocks: list[dict[str, Any]] = []
    if text and len(text) <= _SECTION_MAX:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
    blocks.append({"type": "actions", "elements": buttons})
    return blocks


def _actions_for(
    note: Mapping[str, Any],
) -> tuple[str, Callable[[str], list[dict[str, Any]]] | None]:
    """The note's action token and its block builder, or ``("", None)`` for none.

    A pending approval needs something to show of the call (an Approve the owner
    cannot read is not consent); a pending question needs every option it binds.
    """
    token = note.get(ACTION_TOKEN_KEY)
    if not isinstance(token, str) or not token:
        return "", None
    if is_pending_approval_note(note):
        detail = note.get(APPROVAL_DETAIL_KEY)
        if isinstance(detail, str) and detail.strip():
            return token, lambda last: action_blocks(last, token, detail)
    elif is_pending_question_note(note):
        labels = note.get(OPTIONS_KEY)
        if (
            isinstance(labels, list)
            and labels
            and len(labels) <= len(QUESTION_CHOICES)
            and all(isinstance(label, str) and label for label in labels)
        ):
            shown = [button_label(label) for label in labels]
            # Two buttons that read the same would send different answers the owner
            # cannot tell apart; such a card is answered in the dashboard instead.
            if all(shown) and len({s.casefold() for s in shown}) == len(shown):
                return token, lambda last: question_blocks(last, token, labels)
    return "", None


def _text(note: Mapping[str, Any], field: str) -> str:
    value = note.get(field)
    return value if isinstance(value, str) else ""


#: Raw characters of body and link per message. Escaping grows a character at most
#: five times (``&`` -> ``&amp;``), so a slice this long stays under
#: ``SLACK_MAX_TEXT`` with room for the escaped title, which the bus caps at 500.
_RAW_BODY_CHUNK = (SLACK_MAX_TEXT - 5 * 500 - 100) // 5


def render_slack_parts(note: Mapping[str, Any]) -> list[str]:
    """The note as Slack mrkdwn messages: bold title, body, then the deep link.

    Every producer-written field is escaped, so ``<!channel>`` or ``<@U...>`` in a
    note can never ping anyone. The body is sliced BEFORE escaping, so no slice
    cuts an entity in half, and each message fits Slack's text limit however much
    the escaping grows it. The deep link is a dashboard-internal path (the bus
    validates it), shown as text after the body.
    """
    title, body, url = (_text(note, field) for field in ("title", "body", "url"))
    head = f"*{escape_mrkdwn(title)}*" if title else ""
    if note.get("priority") == "critical":
        head = f"{_CRITICAL_MARKER} {head}".rstrip()
    rest = "\n".join(line for line in (body, url) if line)
    parts = [
        escape_mrkdwn(rest[i : i + _RAW_BODY_CHUNK]) for i in range(0, len(rest), _RAW_BODY_CHUNK)
    ] or [""]
    parts[0] = "\n".join(line for line in (head, parts[0]) if line)
    return [part for part in parts if part]


class SlackBridgeSink:
    """Deliver bridged notes to the owner's Slack DM.

    *state* is the gateway's ``DashboardState``; only its ``slack_client``,
    ``owner_id`` and ``bridge_actions`` are read. A pending-approval note carrying
    an action token gets Approve and Deny buttons under its last part, a
    pending-question note one button per option, and the posted message is linked
    to the token.
    """

    transport = "slack"

    def __init__(self, state: Any) -> None:
        self._state = state

    async def deliver(self, note: Mapping[str, Any]) -> None:
        if is_pending_approval_note(note) and note.get(SLACK_CARD_KEY) is True:
            # The approval card is already in a linked Slack thread; that card is
            # the one to answer, so no second copy is posted. Raised as a skip, so
            # the dispatcher audits it as skipped and keeps the rate-limit token.
            raise BridgeTransportUnavailable("the approval card is already in a Slack thread")
        client = getattr(self._state, "slack_client", None)
        owner = getattr(self._state, "owner_id", "")
        if client is None or not isinstance(owner, str) or not owner:
            raise BridgeTransportUnavailable("slack is not connected")
        channel = await open_dm_with_retry(client, owner, context="Notification bridge")
        if not channel:
            raise BridgeTransportUnavailable("slack returned no DM channel for the owner")
        parts = render_slack_parts(note)
        token, build = _actions_for(note)
        # The buttons ride on the last part, so they sit under the whole note.
        last = parts.pop() if token and parts else ""
        # In order; a failed part raises, so the delivery is audited as an error
        # rather than completed with its tail missing.
        for part in parts:
            await client.post_message(channel, part)
        if token and build is not None:
            fallback = "Question waiting" if is_pending_question_note(note) else "Approval needed"
            ts = await client.post_blocks(channel, build(last), last or fallback)
            registry = getattr(self._state, "bridge_actions", None)
            if registry is not None and ts:
                registry.link_message(self.transport, f"{channel}:{ts}", token)
