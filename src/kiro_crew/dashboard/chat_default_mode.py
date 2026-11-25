"""The start mode of a plain chat the owner opens from the dashboard.

``dashboard.default_approval_mode`` (``normal`` or ``trust_reads``) is the mode the
owner chose ahead of time for the plain chats they start with New chat. The create
route (``chat_api/slot_create.py``) calls :func:`seed_new_chat` once, on a slot it
just minted, before the slot's first frame, and only when the create asks for it
(``start_mode_default: true``): the browser sends that from the person's own New
chat entry points (``createSlot``'s ``startModeDefault``, implied by
``crewModeDefault``), so an app page's create through the same client never gets it. The mode goes through
:func:`slot_approval_seed.seed_slot_approval`, the seed a crewmate's thread and a
Slack code channel take, never through ``apply_approval_mode``: that is a person's
press, and its Reads ends a declared YOLO grant, which opening a chat must not do.
So the seed clamps to the ``approval_modes`` policy, leaves YOLO alone, and writes
the ``mode_change:trust_reads`` SEL record under :data:`SEED_CALLER`.

Only the machine owner's own create gets it. A Shared Crew teammate, an app, a cron,
an agent's internal call, a crewmate's chat (its Profile seeds it at the first turn,
and a seed never replaces a posture the slot already holds) and every create the
route reports as not plain keep how they start today. See
``docs/request-for-change/rfc-default-approval-mode.md`` section 3.2.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: The SEL caller of a default's seed, so an audit tells a default from a pick.
SEED_CALLER = "chat_default_mode"


def configured_mode(cfg: Any) -> str:
    """The stored start mode; a missing, unknown or mistyped value reads as ``normal``."""
    from kiro_crew.config.sections import DEFAULT_APPROVAL_MODES

    dashboard = getattr(cfg, "dashboard", None)
    mode = getattr(dashboard, "default_approval_mode", "normal")
    return mode if isinstance(mode, str) and mode in DEFAULT_APPROVAL_MODES else "normal"


def _owner_browser_request(request: Any) -> bool:
    """The machine owner's own dashboard session: no teammate, app or internal caller."""
    from kiro_crew.dashboard.handlers.source_providers import is_host_owner_dashboard_request

    return (
        request.headers.get("X-Internal-Secret") is None
        and request.get("internal_auth") is not True
        and is_host_owner_dashboard_request(request)
    )


async def seed_new_chat(state: Any, request: Any, slot: Any, cfg: Any, execution: Any) -> str:
    """Put the owner's just-created plain chat *slot* on the stored start mode.

    *execution* is the selection the create just recorded. Returns ``"policy"`` when
    the ``approval_modes`` ceiling holds the chat below the stored mode, else ``""``.
    Never raises: a failed seed leaves the chat on Normal and the create stands.
    """
    mode = configured_mode(cfg)
    if mode == "normal" or not _owner_browser_request(request):
        return ""
    try:
        from kiro_crew.dashboard.crewmate_approval import crewmate_name_for

        if crewmate_name_for(cfg, execution):
            return ""
        from kiro_crew.approval_mode_policy import (
            clamp_session_mode,
            ensure_resolved,
            verdict_pending,
        )

        if verdict_pending():
            # A ceiling is being installed: its passing deny is not the answer.
            await asyncio.to_thread(ensure_resolved)
            if state._slots.get(slot.key) is not slot:
                return ""
        if clamp_session_mode(mode) != mode:
            return "policy"
        from kiro_crew.dashboard.slot_approval_seed import seed_slot_approval

        seed_slot_approval(state, slot, mode, caller=SEED_CALLER)
    except Exception:
        logger.warning("Could not seed the start mode of new chat %s", slot.key, exc_info=True)
    return ""
