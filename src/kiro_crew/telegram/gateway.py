"""Telegram channel startup -- wired into the gateway boot.

``maybe_start_telegram`` is the single guarded entry point. When the channel is
enabled + credentialed it builds the :class:`TelegramDispatcher` +
:class:`TelegramTransport` + the low-level :class:`TelegramClient`, wires the
client's inbound long-poll into ``transport.receive`` (authorize + normalize)
and its inline-button presses into ``dispatcher.on_callback``, then starts
polling via ``transport.connect()``. Failures are logged and swallowed so a
Telegram problem never takes down the gateway.

The turn itself runs on the shared ``TurnDriver`` (credential/exfil redaction +
tool-approval ladder + SEL audit) via the dispatcher -- no hand-rolled loop.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kiro_crew.messaging.driver import APPROVAL_AUTO, APPROVAL_INTERACTIVE
from kiro_crew.telegram.client import TelegramClient
from kiro_crew.telegram.transport import TelegramTransport
from kiro_crew.telegram.transport_dispatch import TelegramDispatcher

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


async def maybe_start_telegram(orch: "GatewayOrchestrator") -> "TelegramClient | None":
    """Start the Telegram channel if enabled + credentialed; else no-op.

    Returns the running client (so the gateway can ``close()`` it on shutdown)
    or None. The transport + dispatcher stay alive via the client's handler
    references. Token / enabled / allowed_user_ids are read once in the
    orchestrator's constructor and consumed off ``orch`` here.
    """
    if not getattr(orch, "_telegram_enabled", False):
        return None
    bot_token = getattr(orch, "_telegram_bot_token", "")
    if not bot_token:
        return None

    try:
        assert orch.sessions is not None and orch.ctx_builder is not None

        allowed_ids: set[int] = set(getattr(orch, "_telegram_allowed_user_ids", []) or [])
        if not allowed_ids:
            logger.warning(
                "Telegram: allowed_user_ids is empty — the bot is globally "
                "reachable but will REJECT all messages (fail closed). Add your "
                "numeric Telegram user_id to telegram.allowed_user_ids to enable."
            )

        dispatcher = TelegramDispatcher(
            sessions=orch.sessions,
            ctx_builder=orch.ctx_builder,
            cfg=orch._cfg,
            allowed_user_ids=allowed_ids,
            agent=None,
            conv_log=getattr(orch, "conv_log", None),
            approval_mode=_resolve_approval_mode(orch),
        )
        client = TelegramClient(token=bot_token, on_callback=dispatcher.on_callback)
        transport = TelegramTransport(
            client, allowed_user_ids=allowed_ids, dispatch=dispatcher.handle_message
        )
        # Inbound: client long-poll -> transport.receive (authorize + normalize)
        # -> dispatcher.handle_message (drive the turn on the shared TurnDriver).
        # set_message_handler avoids the client<->transport construction cycle.
        client.set_message_handler(transport.receive)
        dispatcher.client = client

        await transport.connect()  # starts the long-polling loop
        if orch.dashboard_state is not None:
            orch.dashboard_state.register_channel_transport(transport)
        logger.info("Telegram channel started (transport path, long-polling).")
        return client
    except Exception:
        logger.exception("Failed to start Telegram channel; continuing without it.")
        return None
