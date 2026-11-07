"""The notification bridge's sink for a registered ``MessagingTransport``.

Slack has its own client and sink (``bridge_slack.py``). Every other transport
the bridge serves is a ``MessagingTransport`` in ``DashboardState.channel_transports``,
and one sink class covers them all: it renders the note as compact markdown and
hands it to the proactive owner-DM leg ``send_message`` already uses
(``dashboard.messaging_api.channel_delivery._deliver_channel_dm``). That leg
looks the transport up by name, takes the owner from the transport's own
configured targets, re-runs the ``channels`` gate on the shared send ladder,
checks the recipient, applies the display-safe floor, and chunks at the
transport's own limit, so a long note degrades per transport.

The dispatcher has already vetted, rate-limited and redacted the note
(``bridge.py``). A transport that is not connected, cannot send proactively, or
names no single owner DM is an audited skip, and so is one outside
``constants.CHANNEL_OWNER_DM_NAMESPACES``, the channels whose configured targets
hold no learned peers and so can name the owner (the same roster ``send_message``
enforces for its owner DM). A refused or failed send is an audited error.

Loop safety: the send is the bot's own message, which each transport's inbound
path ignores, so a bridged note never comes back as a turn or a note.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from kiro_crew.constants import CHANNEL_OWNER_DM_NAMESPACES
from kiro_crew.notifications.bridge import BridgeTransportUnavailable

#: The bridge transports this sink serves; ``DashboardState`` registers one each.
TRANSPORT_SINKS: tuple[str, ...] = ("discord", "telegram")

#: The marker a critical note's first line carries.
_CRITICAL_MARKER = "🚨"


def _text(note: Mapping[str, Any], field: str) -> str:
    value = note.get(field)
    return value if isinstance(value, str) else ""


def render_transport_text(note: Mapping[str, Any]) -> str:
    """The note as markdown: bold title, body, then the deep-link path as text."""
    title, body, url = (_text(note, field) for field in ("title", "body", "url"))
    head = f"**{title}**" if title else ""
    if note.get("priority") == "critical":
        head = f"{_CRITICAL_MARKER} {head}".rstrip()
    return "\n".join(line for line in (head, body, url) if line)


class TransportBridgeSink:
    """Deliver bridged notes to the owner DM on one ``MessagingTransport``.

    *state* is the gateway's ``DashboardState``; *transport* is the channel type,
    one of ``bridge.BRIDGE_TRANSPORTS``.
    """

    def __init__(self, state: Any, transport: str) -> None:
        self._state = state
        self.transport = transport

    async def deliver(self, note: Mapping[str, Any]) -> None:
        live = self._state.get_channel_transport(self.transport)
        if live is None:
            raise BridgeTransportUnavailable(f"{self.transport} is not connected")
        if not live.capabilities.supports_proactive_send:
            raise BridgeTransportUnavailable(f"{self.transport} cannot send proactively")
        if self.transport not in CHANNEL_OWNER_DM_NAMESPACES:
            # The owner-DM leg infers the owner from configured targets; a channel
            # that mixes learned peers into them could name a stranger instead.
            raise BridgeTransportUnavailable(f"{self.transport} cannot name its owner")
        # Lazy: the dashboard handlers import the notification settings, which
        # import the bridge.
        from kiro_crew.dashboard.handlers import messaging

        sent, code, detail = await messaging._deliver_channel_dm(
            self._state,
            self.transport,
            render_transport_text(note),
            caller_session="",
            markdown=True,
        )
        if sent:
            return
        if not code:
            # The leg's soft miss: no single configured owner DM to address.
            raise BridgeTransportUnavailable(f"{self.transport} names no owner DM")
        raise RuntimeError(f"{self.transport} delivery refused: {code}")
