"""The session-start notice naming the agent spec's remote MCP servers whose ``oauth``
block a harness other than KAS drops (``_notice_dropped_mcp_oauth``), in the wording
and ``meta.kind`` of the KAS spec-keys row."""

from __future__ import annotations

import asyncio
import functools
from typing import TYPE_CHECKING, Any

from kiro_crew.agent_sdk.spec_hooks import session_agent, spec_project_dir
from kiro_crew.agent_sdk.spec_mcp_oauth import spec_mcp_oauth_dropped
from kiro_crew.dashboard.chat_utils_injected_rows import MCP_SIGN_IN_UNSUPPORTED_NOTICE_KIND
from kiro_crew.dashboard.session_notice_marks import _session_notice_is_new

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        ACP_BACKEND_KAS,
        DashboardState,
        _ChatSlot,
        append_and_surface,
        capabilities_of,
        logger,
    )
    from kiro_crew.dashboard.chat_turn.deny_chokepoints import _redact_display_text
    from kiro_crew.dashboard.chat_turn.tool_approval import _spec_keys_notice


async def _notice_dropped_mcp_oauth(
    state: "DashboardState", slot: "_ChatSlot", client: Any, agent: str
) -> None:
    """One notice row naming the spec's MCP servers whose ``oauth`` block *client*'s
    session never receives, in the KAS row's wording and ``meta.kind``.

    For a known backend other than KAS outside ``ACP_BACKENDS_REMOTE_MCP_OAUTH``.
    Notice only. Best-effort: a spec that cannot be read renders no row.
    """
    backend = capabilities_of(client).backend
    if not backend or backend == ACP_BACKEND_KAS or capabilities_of(client).honors_remote_mcp_oauth:
        return
    agent = session_agent(client, agent)
    if not agent:
        return
    held = getattr(slot, "_active_turn_session_key", "")  # a crewmate's merged spec
    try:
        lost = await asyncio.to_thread(
            functools.partial(
                spec_mcp_oauth_dropped,
                agent,
                spec_project_dir(client),
                backend=backend,
                session_key=held if isinstance(held, str) else "",
            )
        )
    except Exception:  # noqa: BLE001 - an informational row must never fail a turn
        logger.debug("agent spec MCP servers for %r could not be read", agent, exc_info=True)
        return
    if not lost:
        return
    text = _redact_display_text(_spec_keys_notice(agent, lost))
    if _session_notice_is_new(slot, text):
        meta = {"kind": MCP_SIGN_IN_UNSUPPORTED_NOTICE_KIND}
        append_and_surface(state, slot, "notice", text, "msg msg-info", meta=meta)
