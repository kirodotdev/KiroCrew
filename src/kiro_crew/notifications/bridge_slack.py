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

from collections.abc import Mapping
from typing import Any

from kiro_crew.notifications.bridge import BridgeTransportUnavailable
from kiro_crew.notifications.bridge_approval_note import SLACK_CARD_KEY, is_pending_approval_note
from kiro_crew.slack.format import SLACK_MAX_TEXT, escape_mrkdwn
from kiro_crew.slack.retry import open_dm_with_retry

#: The marker a critical note's first line carries.
_CRITICAL_MARKER = ":rotating_light:"


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

    *state* is the gateway's ``DashboardState``; only its ``slack_client`` and
    ``owner_id`` are read.
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
        # In order; a failed part raises, so the delivery is audited as an error
        # rather than completed with its tail missing.
        for part in render_slack_parts(note):
            await client.post_message(channel, part)
