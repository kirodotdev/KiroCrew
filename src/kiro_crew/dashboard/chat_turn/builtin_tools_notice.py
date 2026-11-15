"""The session-start notice naming what of the agent spec's ``tools`` list and ``model``
pin a claude session does not apply (``_notice_unapplied_builtin_tools``)."""

from __future__ import annotations

import asyncio
import functools
from typing import TYPE_CHECKING, Any

from kiro_crew.agent_sdk.spec_builtin_tools import spec_builtin_tools_notice
from kiro_crew.agent_sdk.spec_hooks import session_agent, spec_project_dir
from kiro_crew.dashboard.session_notice_marks import _session_notice_is_new

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        DashboardState,
        _ChatSlot,
        append_and_surface,
        capabilities_of,
        logger,
    )
    from kiro_crew.dashboard.chat_turn.deny_chokepoints import _redact_display_text


#: How long a new session's turn waits for the spec read behind this row.
_SPEC_NOTICE_TIMEOUT_SECS = 10.0


async def _notice_unapplied_builtin_tools(
    state: "DashboardState", slot: "_ChatSlot", client: Any, agent: str
) -> None:
    """One notice row when *client*'s claude session runs the agent's spec without
    its ``tools`` limit on the built-ins or without its ``model`` pin
    (``agent_sdk.spec_builtin_tools``).

    Notice only. Best-effort: a spec that cannot be read in time renders no row.
    """
    agent = session_agent(client, agent)
    if not agent:
        return
    held = getattr(slot, "_active_turn_session_key", "")  # a crewmate's merged spec
    try:
        text = await asyncio.wait_for(
            asyncio.to_thread(
                functools.partial(
                    spec_builtin_tools_notice,
                    agent,
                    spec_project_dir(client),
                    backend=capabilities_of(client).backend,
                    namespace=capabilities_of(client).model_id_namespace,
                    session_key=held if isinstance(held, str) else "",
                )
            ),
            _SPEC_NOTICE_TIMEOUT_SECS,
        )
    except Exception:  # noqa: BLE001 - an informational row must never fail a turn
        logger.debug("agent spec tools for %r could not be read", agent, exc_info=True)
        return
    if not text:
        return
    text = _redact_display_text(text)
    if _session_notice_is_new(slot, text):
        append_and_surface(state, slot, "notice", text, "msg msg-info")
