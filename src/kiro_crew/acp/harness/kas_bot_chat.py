"""The KAS projection steps for a Bot's own chat and the app servers its owner turned on.

Called from :class:`~kiro_crew.acp.harness.kas.KasHarness`: one settles the
app-server record from what a conductor projection really declares, the other moves
the app snapshot to the native session id once ``session/new`` has returned. The app
state itself lives in ``kiro_crew.bot_chat_app_tools``.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def bind_bot_chat_snapshot(handle: Any) -> None:
    """Move the app snapshot ``record_mounted`` wrote from the conversation key to the
    native KAS session id on *handle*."""
    # This must happen after session/new returns and the handle (with its native
    # session_id) is available, so note_unconfirmed_close can find the snapshot
    # under the native id even when a swap_conversation has reused the same
    # conversation key for a new session. A handle with no native id binds nothing:
    # the snapshot stays under the conversation key, where note_unconfirmed_close
    # also looks.
    session_key: str = getattr(handle, "_session_key", "") or ""
    native_sid = getattr(handle, "session_id", None)
    if not (session_key and native_sid):
        return
    try:
        from kiro_crew.bot_chat_app_tools import bind_snapshot_to_native_sid

        bind_snapshot_to_native_sid(session_key, native_sid, getattr(handle, "_runtime", None))
    except Exception:  # noqa: BLE001
        logger.debug(
            "KasHarness.record_session_projection: bind_snapshot_to_native_sid failed "
            "for %s / %s",
            session_key,
            native_sid,
            exc_info=True,
        )


def settle_bot_chat_projection(session_key: str, custom_agents: list) -> list:
    """Settle a Bot chat's app-server record from what *custom_agents* really
    declares, after every withholding, and return the agents with any stripped or
    voided app entries removed."""
    from kiro_crew.bot_chat_app_tools import (
        check_and_filter_projection,
        mounted_names,
        record_mounted,
    )

    own = custom_agents[0] if custom_agents else {}
    mcp_servers: dict = dict(own.get("mcpServers") or {})
    # Pass codex_name as name_transform so the sidecar lookup can match
    # whitespace-folded aliases ("notes:my_server") to the original
    # ("notes:my server") that conductor_session_spec stored.
    from kiro_crew.providers.mirrors.codex import codex_name as _cn

    mcp_servers = check_and_filter_projection(session_key, mcp_servers, name_transform=_cn)
    if mcp_servers != own.get("mcpServers"):
        # Some entries were stripped; rebuild the custom agent dict.
        own = dict(own)
        own["mcpServers"] = mcp_servers
        if custom_agents:
            custom_agents = [own, *custom_agents[1:]]
    # The projection may carry an alias as offered or whitespace-folded.
    names = set(mounted_names(mcp_servers))

    def spelled(a: str) -> str:
        return a if a in names else _cn(a)

    voided_apps = record_mounted(session_key, mounted_names(mcp_servers), spelled)
    if voided_apps:
        mcp_servers = {
            a: e
            for a, e in mcp_servers.items()
            if ":" not in a or a.split(":")[0] not in voided_apps
        }
        own = dict(own)
        own["mcpServers"] = mcp_servers
        if custom_agents:
            custom_agents = [own, *custom_agents[1:]]
    return custom_agents
