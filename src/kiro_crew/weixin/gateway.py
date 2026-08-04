"""Weixin (iLink) channel startup -- wired into the gateway boot.

``maybe_start_weixin`` is the single guarded entry point. When the channel is
enabled + credentialed (bot token + account id, obtained via the Settings QR
flow) it builds the :class:`WeixinDispatcher` + :class:`WeixinTransport` +
low-level :class:`WeixinClient`, then starts the iLink long-poll via
``transport.connect()``. Failures are logged and swallowed so a Weixin problem
never takes down the gateway (mirrors the Telegram/WeCom paths).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kiro_crew.config.paths import data_home
from kiro_crew.messaging.driver import APPROVAL_AUTO, APPROVAL_INTERACTIVE
from kiro_crew.weixin.client import (
    ILINK_BASE_URL,
    ContextTokenStore,
    TypingTicketCache,
    WeixinClient,
)
from kiro_crew.weixin.transport import WeixinTransport
from kiro_crew.weixin.transport_dispatch import WeixinDispatcher

if TYPE_CHECKING:
    from kiro_crew.slack.gateway import GatewayOrchestrator

logger = logging.getLogger(__name__)


def _resolve_approval_mode(orch: "GatewayOrchestrator") -> str:
    if getattr(orch, "_approval_mode", None) == "yolo":
        return APPROVAL_AUTO
    mode = getattr(orch, "_approval_mode", None) or orch._cfg.agent.approval_mode
    return APPROVAL_AUTO if mode == APPROVAL_AUTO else APPROVAL_INTERACTIVE


async def maybe_start_weixin(orch: "GatewayOrchestrator") -> "WeixinClient | None":
    """Start the Weixin channel if enabled + credentialed; else no-op."""
    if not getattr(orch, "_weixin_enabled", False):
        return None
    token = getattr(orch, "_weixin_token", "")
    account_id = getattr(orch, "_weixin_account_id", "")
    if not token or not account_id:
        logger.info("weixin: enabled but not credentialed (no token/account_id) — skipping. Connect via Settings QR.")
        return None

    try:
        assert orch.sessions is not None and orch.ctx_builder is not None
        base_url = getattr(orch, "_weixin_base_url", "") or ILINK_BASE_URL
        dm_policy = getattr(orch, "_weixin_dm_policy", "allowlist")
        allowed = set(getattr(orch, "_weixin_allowed_user_ids", []) or [])
        # Resolve through data_home() so KIROCREW_HOME (isolated profiles, the
        # dev backend, tests) is honoured. A hardcoded expanduser() would write
        # peer context state into the DEFAULT home, letting separate profiles
        # overwrite each other's reply state.
        home = getattr(orch, "_weixin_home", "") or str(data_home())

        if dm_policy == "allowlist" and not allowed:
            logger.warning(
                "weixin: dm_policy=allowlist with an empty allow-list — the bot "
                "will REJECT all DMs (fail closed). Add user ids to authorize."
            )

        ctx_store = ContextTokenStore(home)
        typing_cache = TypingTicketCache()
        client = WeixinClient(token=token, base_url=base_url, account_id=account_id)
        dispatcher = WeixinDispatcher(
            sessions=orch.sessions,
            ctx_builder=orch.ctx_builder,
            cfg=orch._cfg,
            account_id=account_id,
            ctx_store=ctx_store,
            typing_cache=typing_cache,
            conv_log=getattr(orch, "conv_log", None),
            approval_mode=_resolve_approval_mode(orch),
        )
        dispatcher.client = client
        transport = WeixinTransport(
            client,
            account_id=account_id,
            ctx_store=ctx_store,
            allowed_user_ids=allowed,
            dm_policy=dm_policy,
            dispatch=dispatcher.handle_message,
        )
        await transport.connect()  # starts the iLink long-poll loop
        if orch.dashboard_state is not None:
            orch.dashboard_state.register_channel_transport(transport)
            # Surface live connection state to GET /api/weixin/config.
            orch.dashboard_state.weixin_connected = True
            orch.dashboard_state.weixin_connect_error = ""
        logger.info("Weixin channel started (transport path, iLink long-polling).")
        return client
    except Exception as exc:
        logger.exception("Failed to start Weixin channel; continuing without it.")
        if orch.dashboard_state is not None:
            orch.dashboard_state.weixin_connected = False
            orch.dashboard_state.weixin_connect_error = str(exc)[:120]
        return None
