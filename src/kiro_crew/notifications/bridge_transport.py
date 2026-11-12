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

import asyncio
from collections.abc import Mapping
from typing import Any

from kiro_crew.constants import CHANNEL_OWNER_DM_NAMESPACES
from kiro_crew.messaging.transport import sole_direct_target
from kiro_crew.notifications.bridge import BridgeTransportUnavailable
from kiro_crew.notifications.bridge_approval_note import (
    ACTION_TOKEN_KEY,
    APPROVAL_DETAIL_KEY,
    is_pending_approval_note,
)

#: The bridge transports this sink serves; ``DashboardState`` registers one each.
TRANSPORT_SINKS: tuple[str, ...] = ("discord", "telegram", "webex")

#: The transports whose owner may answer a bridged approval by replying ``approve``
#: or ``deny`` to it (RFC bridge actions, B4d). Each needs an inbound reader that
#: knows which message a reply quotes; the others stay read-only until they get one.
REPLY_TRANSPORTS: tuple[str, ...] = ("telegram",)

#: The marker a critical note's first line carries.
_CRITICAL_MARKER = "🚨"

#: The ``user:`` prefix of a configured direct target.
_DM_PREFIX = "user:"


def reply_owner(state: Any, transport: str) -> str:
    """The owner's user id on reply transport *transport*, or ``""``. Never raises.

    The one owner rule the messaging layer already owns: the transport must be in
    ``constants.CHANNEL_OWNER_DM_NAMESPACES`` and advertise exactly one available,
    non-learned direct target (``messaging.transport.sole_direct_target``). Read
    live, so an owner dropped from the allow-list cannot answer.
    """
    try:
        if transport not in REPLY_TRANSPORTS or transport not in CHANNEL_OWNER_DM_NAMESPACES:
            return ""
        live = state.get_channel_transport(transport)
        if live is None:
            return ""
        target = sole_direct_target(list(live.configured_targets()))
        return target.removeprefix(_DM_PREFIX) if target.startswith(_DM_PREFIX) else ""
    except Exception:
        return ""


#: The most a reply note may render to, in characters: below Telegram's per-message
#: cap with room for the delivery leg's display pass, so the note is one message and
#: a reply to it can only quote the message that carries the prompt.
_ONE_MESSAGE = 3500

#: The fewest characters of a cut detail still shown with a prompt: below it, what
#: an Approve would run is not readable enough to consent to, so no prompt is sent.
_MIN_SHOWN = 100


def render_reply_text(head: str, detail: str) -> str:
    """*head* (the note) plus what will run and how to answer, as one message, or ``""``.

    *detail* is scrubbed for display FIRST, so the cut can never sever a credential
    the scan would have caught, then collapsed to one line and cut the way
    Telegram's own approval card shows a call's input; every backtick is followed
    by a zero-width space, so untrusted input can neither close the code fence nor
    render as markup. The cut shrinks until the whole text renders to one
    Telegram message; ``""`` when it would have to fall below :data:`_MIN_SHOWN`,
    and then the note carries no prompt. MAY BLOCK: run it off the loop.
    """
    # Lazy: the Telegram renderer is not needed until a note is bound.
    from kiro_crew.telegram.renderer import md_to_telegram_html_safe
    from kiro_crew.telegram.renderer_approval import _APPROVAL_INPUT_CHARS
    from kiro_crew.telegram.renderer_display import _display_safe

    shown = " ".join(_display_safe(" ".join(detail.split())).split())
    cut = _APPROVAL_INPUT_CHARS
    while cut >= _MIN_SHOWN:
        part = shown if len(shown) <= cut else shown[: cut - 1].rstrip() + "…"
        part = part.replace("`", "`\u200b")
        text = f"{head}\n\n```\n{part}\n```\nReply **approve** or **deny** to this message."
        if len(text) <= _ONE_MESSAGE and len(md_to_telegram_html_safe(text)) <= _ONE_MESSAGE:
            return text
        cut //= 2
    return ""


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
    one of ``bridge.BRIDGE_TRANSPORTS``. On a :data:`REPLY_TRANSPORTS` transport a
    bound pending approval also shows what it will run and how to answer, and each
    delivered message is linked to its token by ``<chat id>:<message id>``.
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

        token, action_detail = self._reply_action(note)
        text = render_transport_text(note)
        if token:
            # Off the loop: the display scan reads the whole, model-authored detail.
            reply_text = await asyncio.to_thread(render_reply_text, text, action_detail)
            text, token = (reply_text, token) if reply_text else (text, "")
        posted: list[tuple[str, str]] = []
        sent, code, detail = await messaging._deliver_channel_dm(
            self._state,
            self.transport,
            text,
            caller_session="",
            markdown=True,
            posted=posted,
        )
        registry = getattr(self._state, "bridge_actions", None)
        if token and registry is not None:
            # Every delivered part, keyed by its chat and message id: a reply quotes
            # one message, and a message id alone repeats across chats.
            for conversation_id, message_id in posted:
                registry.link_message(self.transport, f"{conversation_id}:{message_id}", token)
        if sent:
            return
        if not code:
            # The leg's soft miss: no single configured owner DM to address.
            raise BridgeTransportUnavailable(f"{self.transport} names no owner DM")
        raise RuntimeError(f"{self.transport} delivery refused: {code}")

    def _reply_action(self, note: Mapping[str, Any]) -> tuple[str, str]:
        """The note's action token and detail when its owner may answer by reply here.

        ``("", "")`` unless this is a reply transport whose owner DM is known, and
        the note is a pending approval carrying both a token and something to show:
        a reply the owner cannot read the call for is not consent.
        """
        if not reply_owner(self._state, self.transport) or not is_pending_approval_note(note):
            return "", ""
        token, detail = note.get(ACTION_TOKEN_KEY), note.get(APPROVAL_DETAIL_KEY)
        if not isinstance(token, str) or not isinstance(detail, str) or not detail.strip():
            return "", ""
        return token, detail
