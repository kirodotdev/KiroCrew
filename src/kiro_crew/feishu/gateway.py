"""Feishu (Lark/飞书) channel startup -- wired into the gateway boot.

``maybe_start_feishu`` is the single guarded entry point.  When the channel
is enabled + credentialed it builds the :class:`FeishuDispatcher` +
:class:`FeishuTransport` + the low-level :class:`LarkClient`, wires the
client's inbound WS frames into ``transport.receive`` (authorise + normalise
-> dispatcher), then opens the WebSocket via ``transport.connect()``.
Failures are logged and swallowed so a Feishu problem never takes down the
gateway.

The turn itself runs on the shared ``TurnDriver`` (credential/exfil redaction
+ tool-approval ladder + SEL audit) via the dispatcher -- no hand-rolled loop.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kiro_crew.feishu.client import LarkClient
from kiro_crew.feishu.transport import FeishuTransport
from kiro_crew.feishu.transport_dispatch import FeishuDispatcher
from kiro_crew.messaging.driver import APPROVAL_AUTO, APPROVAL_INTERACTIVE

if TYPE_CHECKING:
    from kiro_crew.slack.gateway import GatewayOrchestrator

logger = logging.getLogger(__name__)


def _resolve_approval_mode(orch: "GatewayOrchestrator") -> str:
    """Resolve the transport approval mode (mirrors the WeCom / Telegram path).

    YOLO -> auto-approve; otherwise the CLI ``--approval`` override or the
    configured ``agent.approval_mode`` decides.  Feishu has no interactive
    buttons, so anything other than ``auto`` collapses to interactive
    (deny-by-default unless a hook approves).
    """
    if getattr(orch, "_approval_mode", None) == "yolo":
        return APPROVAL_AUTO
    mode = getattr(orch, "_approval_mode", None) or orch._cfg.agent.approval_mode
    return APPROVAL_AUTO if mode == APPROVAL_AUTO else APPROVAL_INTERACTIVE


async def maybe_start_feishu(orch: "GatewayOrchestrator") -> "LarkClient | None":
    """Start the Feishu channel if enabled + credentialed; else no-op.

    Returns the running client (so the gateway can ``close()`` it on shutdown)
    or None.  The transport + dispatcher stay alive via the client's handler
    references.  ``app_id`` / ``app_secret`` are read from ``orch`` (set in
    ``GatewayOrchestrator.__init__`` from env / credentials store).
    """
    if not getattr(orch, "_feishu_enabled", False):
        return None
    try:
        assert orch.sessions is not None and orch.ctx_builder is not None

        allowed_open_ids: list[str] = list(getattr(orch, "_feishu_allowed_open_ids", []) or [])
        if not allowed_open_ids:
            logger.warning(
                "Feishu: feishu.allowed_open_ids is empty — the bot is globally "
                "reachable but will REJECT all messages (fail closed). Add your "
                "Feishu open_id to feishu.allowed_open_ids to enable."
            )

        dispatcher = FeishuDispatcher(
            sessions=orch.sessions,
            ctx_builder=orch.ctx_builder,
            cfg=orch._cfg,
            agent=None,
            conv_log=getattr(orch, "conv_log", None),
            approval_mode=_resolve_approval_mode(orch),
        )
        client = LarkClient(
            app_id=orch._feishu_app_id,
            app_secret=orch._feishu_app_secret,
        )
        transport = FeishuTransport(
            client,
            allowed_open_ids=allowed_open_ids,
            allow_group=bool(getattr(orch, "_feishu_allow_group", False)),
            allowed_group_ids=list(getattr(orch, "_feishu_allowed_group_ids", []) or []),
            dispatch=dispatcher.handle_message,
        )
        # Inbound: client WS frame -> transport.receive (authorise + normalise)
        # -> dispatcher.handle_message (drive the turn on the shared TurnDriver).
        # set_message_handler avoids the client<->transport construction cycle.
        client.set_message_handler(transport.receive)
        dispatcher.client = client

        await transport.connect()  # spawns the WS daemon thread
        if orch.dashboard_state is not None:
            orch.dashboard_state.register_channel_transport(transport)
        logger.info("Feishu (飞书/Lark) channel started (app_id=%s).", orch._feishu_app_id)
        return client
    except Exception:
        logger.exception("Failed to start Feishu channel; continuing without it.")
        return None
