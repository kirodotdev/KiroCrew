"""WeChat (WeCom AI-bot) channel startup -- wired into the gateway boot.

``maybe_start_wecom`` is the single guarded entry point. When the channel is
enabled + credentialed it builds the :class:`WeComDispatcher` +
:class:`WeComTransport` + the low-level :class:`WeComClient`, wires the client's
inbound WS frames into ``transport.receive`` (authorize + normalize ->
dispatcher), then opens the outbound WebSocket via ``transport.connect()``.
Failures are logged and swallowed so a WeChat problem never takes down the
gateway.

The turn itself runs on the shared ``TurnDriver`` (credential/exfil redaction +
tool-approval ladder + SEL audit) via the dispatcher -- no hand-rolled loop.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from kiro_crew.messaging.driver import APPROVAL_AUTO, APPROVAL_INTERACTIVE
from kiro_crew.wechat.client import WeComClient
from kiro_crew.wechat.transport import WeComTransport
from kiro_crew.wechat.transport_dispatch import WeComDispatcher

if TYPE_CHECKING:
    from kiro_crew.slack.gateway import GatewayOrchestrator

logger = logging.getLogger(__name__)


def _resolve_approval_mode(orch: "GatewayOrchestrator") -> str:
    """Resolve the transport approval mode (mirrors the Slack/Telegram path).

    YOLO -> auto-approve; otherwise the CLI ``--approval`` override or the
    configured ``agent.approval_mode`` decides, collapsing anything that isn't
    ``auto`` to interactive (deny-by-default on WeCom, which has no buttons).
    """
    if getattr(orch, "_approval_mode", None) == "yolo":
        return APPROVAL_AUTO
    mode = getattr(orch, "_approval_mode", None) or orch._cfg.agent.approval_mode
    return APPROVAL_AUTO if mode == APPROVAL_AUTO else APPROVAL_INTERACTIVE


def _allowed_userids(orch: "GatewayOrchestrator") -> list[str]:
    """Extract the configured WeCom allow-list userids (filtered)."""
    out: list[str] = []
    for u in orch._cfg.wechat.allowed_users:
        uid = u.get("userid") if isinstance(u, dict) else None
        if uid:
            out.append(uid)
    return out


async def maybe_start_wecom(orch: "GatewayOrchestrator") -> "WeComClient | None":
    """Start the WeChat channel if enabled + credentialed; else no-op.

    Returns the running client (so the gateway can ``close()`` it on shutdown)
    or None. The transport + dispatcher stay alive via the client's handler
    references.
    """
    if not getattr(orch, "_wecom_enabled", False):
        return None
    try:
        assert orch.sessions is not None and orch.ctx_builder is not None
        proxy = (
            os.environ.get("HTTPS_PROXY")
            or os.environ.get("HTTP_PROXY")
            or os.environ.get("ALL_PROXY")
        )
        dispatcher = WeComDispatcher(
            sessions=orch.sessions,
            ctx_builder=orch.ctx_builder,
            cfg=orch._cfg,
            owner_id=orch._owner_id,
            agent=None,
            conv_log=getattr(orch, "conv_log", None),
            approval_mode=_resolve_approval_mode(orch),
        )
        client = WeComClient(
            bot_id=orch._wecom_bot_id,
            secret=orch._wecom_secret,
            ws_url=orch._cfg.wechat.ws_url,
            proxy=proxy,
        )
        transport = WeComTransport(
            client,
            allowed_users=_allowed_userids(orch),
            allow_all=bool(orch._cfg.wechat.allow_all_users),
            owner_id=orch._owner_id,
            dispatch=dispatcher.handle_message,
        )
        # Inbound: client WS frame -> transport.receive (authorize + normalize)
        # -> dispatcher.handle_message (drive the turn on the shared TurnDriver).
        # set_message_handler avoids the client<->transport construction cycle.
        client.set_message_handler(transport.receive)
        dispatcher.client = client

        # Keep the settings badge truthful: connect() only SCHEDULES the WS
        # loop, so "started" proves nothing about the credentials. The client
        # reports live connection transitions (connected + subscribed /
        # connect failure / immediate close on bad creds / server kick)
        # through on_status; start not-connected and let the first transition
        # flip it. This is the compensating control for skipping save-time
        # credential verification in the config API. Wired BEFORE connect()
        # so the very first transition cannot fire into a missing callback
        # (the dedupe would then swallow the re-report forever).
        if orch.dashboard_state is not None:
            state = orch.dashboard_state
            state.wecom_connected = False
            state.wecom_connect_error = ""

            def _on_status(healthy: bool, reason: str) -> None:
                state.wecom_connected = healthy
                state.wecom_connect_error = "" if healthy else reason[:120]

            client.on_status = _on_status

        await transport.connect()  # opens the outbound WS connect/serve loop
        if orch.dashboard_state is not None:
            orch.dashboard_state.register_channel_transport(transport)
        logger.info("WeChat (WeCom AI-bot) channel started (transport path).")
        return client
    except Exception as exc:
        if orch.dashboard_state is not None:
            orch.dashboard_state.wecom_connected = False
            orch.dashboard_state.wecom_connect_error = (
                f"{type(exc).__name__}: {exc}"[:120]
            )
        logger.exception("Failed to start WeChat channel; continuing without it.")
        return None
