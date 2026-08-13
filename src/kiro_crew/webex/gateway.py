"""Webex channel startup -- wired into the gateway boot.

``maybe_start_webex`` is the single guarded entry point. When the channel is
enabled + credentialed it builds the :class:`WebexDispatcher` +
:class:`WebexTransport` + the low-level :class:`WebexClient`, wires the
client's inbound WS events into ``transport.receive`` (authorize + normalize
-> dispatcher), then connects via ``transport.connect()``. Failures are
logged and swallowed so a Webex problem never takes down the gateway.

The turn itself runs on the shared ``TurnDriver`` (credential/exfil redaction
+ tool-approval ladder + SEL audit) via the dispatcher -- no hand-rolled loop.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kiro_crew.messaging.driver import APPROVAL_AUTO, APPROVAL_INTERACTIVE
from kiro_crew.webex.client import WebexClient
from kiro_crew.webex.transport import WebexTransport
from kiro_crew.webex.transport_dispatch import WebexDispatcher

if TYPE_CHECKING:
    from kiro_crew.slack.gateway import GatewayOrchestrator

logger = logging.getLogger(__name__)


def _resolve_approval_mode(orch: "GatewayOrchestrator") -> str:
    """Resolve the transport approval mode (mirrors the Slack path, neutral).

    YOLO -> auto-approve; otherwise the CLI ``--approval`` override or the
    configured ``agent.approval_mode`` decides, collapsing anything that isn't
    ``auto`` to interactive (deny-by-default unless a decider/hook approves).
    """
    if getattr(orch, "_approval_mode", None) == "yolo":
        return APPROVAL_AUTO
    mode = getattr(orch, "_approval_mode", None) or orch._cfg.agent.approval_mode
    return APPROVAL_AUTO if mode == APPROVAL_AUTO else APPROVAL_INTERACTIVE


async def maybe_start_webex(orch: "GatewayOrchestrator") -> "WebexClient | None":
    """Start the Webex channel if enabled + credentialed; else no-op.

    Returns the running client (so the gateway can ``close()`` it on shutdown)
    or None. The transport + dispatcher stay alive via the client's handler
    references. Token / enabled / allowed_emails are read once in the
    orchestrator's constructor and consumed off ``orch`` here.
    """
    if not getattr(orch, "_webex_enabled", False):
        return None
    bot_token = getattr(orch, "_webex_bot_token", "")
    if not bot_token:
        return None

    try:
        assert orch.sessions is not None and orch.ctx_builder is not None

        allowed_emails: set[str] = {
            e.lower() for e in (getattr(orch, "_webex_allowed_emails", []) or []) if e
        }
        if not allowed_emails:
            logger.warning(
                "Webex: allowed_emails is empty — the bot is reachable by anyone "
                "in the org but will REJECT all messages (fail closed). Add your "
                "Webex account email to webex.allowed_emails to enable."
            )

        dispatcher = WebexDispatcher(
            sessions=orch.sessions,
            ctx_builder=orch.ctx_builder,
            cfg=orch._cfg,
            agent=None,
            conv_log=getattr(orch, "conv_log", None),
            approval_mode=_resolve_approval_mode(orch),
        )
        client = WebexClient(token=bot_token)
        transport = WebexTransport(
            client, allowed_emails=allowed_emails, dispatch=dispatcher.handle_message
        )
        # Inbound: client WS events -> transport.receive (authorize + normalize)
        # -> dispatcher.handle_message (drive the turn on the shared TurnDriver).
        # set_message_handler avoids the client<->transport construction cycle.
        client.set_message_handler(transport.receive)
        dispatcher.client = client

        await transport.connect()  # registers the device + opens the WS
        if orch.dashboard_state is not None:
            orch.dashboard_state.register_channel_transport(transport)
            state = orch.dashboard_state

            # Keep the status badge truthful across the channel's lifetime:
            # a successful connect+authorize marks connected; any disconnect
            # flips it back off with the reason. Mirrors the Discord channel.
            def _on_state(connected: bool, error: str) -> None:
                state.webex_connected = connected
                state.webex_connect_error = error[:120]

            client.on_state_change = _on_state
            # "Connected" only once the WS is actually up — connect() merely
            # schedules the serve loop, and reporting success before the
            # handshake would show a green badge on a bad token. On timeout
            # the badge stays off with an actionable reason.
            if await client.wait_ready(timeout=15.0):
                state.webex_connected = True
                state.webex_connect_error = ""
            else:
                state.webex_connected = False
                state.webex_connect_error = (
                    client.last_error
                    or "WebSocket not connected within 15s (check bot token/network)"
                )
        logger.info("Webex channel started (transport path, device WebSocket).")
        return client
    except Exception as exc:
        if orch.dashboard_state is not None:
            orch.dashboard_state.webex_connect_error = type(exc).__name__[:120]
        logger.exception("Failed to start Webex channel; continuing without it.")
        return None
