"""Slack sink for the notification bridge (RFC phase B1).

The bridge is transport-generic; this is its first concrete leg. Slack is
deliberately absent from ``DashboardState.channel_transports`` -- it keeps its
dedicated ``slack_client`` for the rich streaming mirror -- so the sink is
built from that client plus the owner id rather than from the shared transport
registry, and DM resolution reuses ``slack.retry.open_dm_with_retry`` so a
bridged send inherits the same rate-limit and 5xx handling every other
proactive Slack path has.
"""

from __future__ import annotations

import logging
from typing import Any

from kiro_crew.slack.format import render_for_slack
from kiro_crew.slack.retry import open_dm_with_retry

logger = logging.getLogger(__name__)

# Slack's own per-message text ceiling is 40k; this is the bridge's rendering
# floor, matching what the cron path uses for a proactive owner DM.
_BRIDGE_MSG_LIMIT = 3900


class SlackBridgeSink:
    """Deliver a bridged notification to the owner's Slack DM."""

    transport_id = "slack"

    def __init__(self, client: Any, owner_id: str) -> None:
        self._client = client
        self._owner_id = owner_id

    async def send(self, text: str) -> str:
        """Open the owner DM and post *text*. Returns the last message id.

        ``render_for_slack`` is the redaction boundary on this path as it is on
        every other Slack egress: it normalises ANSI before converting, so a
        credential split by escape sequences cannot be reassembled by the strip
        inside the mrkdwn conversion. The bridge redacts before calling this
        too -- that is not redundant bookkeeping, it is the difference between
        the guarantee holding in this module and holding only in the pair.
        """
        channel = await open_dm_with_retry(
            self._client, self._owner_id, context="Notification bridge"
        )
        if not channel:
            raise RuntimeError("could not open owner DM")
        message_id = ""
        for part in render_for_slack(text, limit=_BRIDGE_MSG_LIMIT):
            message_id = await self._client.post_message(channel, part, None) or ""
        return str(message_id)


def slack_sink_for(state: Any) -> SlackBridgeSink | None:
    """Build the Slack sink for *state*, or ``None`` when Slack cannot receive.

    Resolved per delivery rather than cached at boot: ``None`` is the honest
    answer for a Slack install that is configured but whose socket is down, and
    the bridge turns that into an audited skip. A sink cached at boot would keep
    answering for a connection that has since dropped.
    """
    client = getattr(state, "slack_client", None)
    owner_id = getattr(state, "owner_id", "") or ""
    if client is None or not owner_id:
        return None
    if not getattr(state, "slack_socket_connected", False):
        return None
    return SlackBridgeSink(client, owner_id)


__all__ = ["SlackBridgeSink", "slack_sink_for"]
