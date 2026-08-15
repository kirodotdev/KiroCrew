"""WhatsApp channel startup, wired into the gateway boot."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kiro_crew.config.paths import data_home
from kiro_crew.messaging.driver import APPROVAL_AUTO, APPROVAL_INTERACTIVE
from kiro_crew.whatsapp.client import WhatsAppClient, default_db_path, neonize_available
from kiro_crew.whatsapp.transport import WhatsAppTransport
from kiro_crew.whatsapp.transport_dispatch import WhatsAppDispatcher

if TYPE_CHECKING:
    from kiro_crew.slack.gateway import GatewayOrchestrator

logger = logging.getLogger(__name__)


def _resolve_approval_mode(orch: "GatewayOrchestrator") -> str:
    if getattr(orch, "_approval_mode", None) == "yolo":
        return APPROVAL_AUTO
    mode = getattr(orch, "_approval_mode", None) or orch._cfg.agent.approval_mode
    return APPROVAL_AUTO if mode == APPROVAL_AUTO else APPROVAL_INTERACTIVE


async def maybe_start_whatsapp(orch: "GatewayOrchestrator") -> "WhatsAppClient | None":
    """Start the WhatsApp channel if enabled; else no-op."""
    if not getattr(orch, "_whatsapp_enabled", False):
        return None
    state = orch.dashboard_state
    if not neonize_available():
        from kiro_crew.whatsapp.client import MISSING_EXTRA_HINT

        logger.warning("whatsapp: %s", MISSING_EXTRA_HINT)
        if state is not None:
            state.whatsapp_connected = False
            state.whatsapp_connect_error = MISSING_EXTRA_HINT[:120]
        return None

    try:
        assert orch.sessions is not None and orch.ctx_builder is not None
        cfg = orch._cfg.whatsapp
        db_path = cfg.db_path or str(default_db_path(data_home()))
        client = WhatsAppClient(db_path)
        dispatcher = WhatsAppDispatcher(
            orch._cfg,
            orch.sessions,
            orch.ctx_builder,
            approval_mode=_resolve_approval_mode(orch),
        )
        dispatcher.client = client
        dispatcher.conv_log = getattr(orch, "conv_log", None)
        transport = WhatsAppTransport(
            client,
            dispatcher.handle_message,
            dm_policy=cfg.dm_policy,
            allowed_wa_ids=list(cfg.allowed_wa_ids),
            groups=list(cfg.groups),
        )
        dispatcher.transport = transport
        if state is not None:

            def _on_state(new_state: str, detail: str) -> None:
                state.whatsapp_connected = new_state == "connected"
                state.whatsapp_connect_error = (
                    "" if new_state == "connected" else f"{new_state}: {detail}"[:120]
                )

            client.on_state_change = _on_state
            state.register_channel_transport(transport)
        await transport.connect()
        logger.info("WhatsApp channel started (state=%s).", client.state)
        return client
    except Exception as exc:
        logger.exception("Failed to start WhatsApp channel; continuing without it.")
        if state is not None:
            state.whatsapp_connected = False
            state.whatsapp_connect_error = str(exc)[:120]
        return None
