"""What a crewmate's worker can use, told to the conductor when it hands work off.

When a crewmate's conductor opens its own worker (``session_create``) or sends it
work (``session_send``), the route adds a short line to that tool's result: the
worker's MCP servers and the servers this gateway has loaded that the worker
lacks (:func:`kiro_crew.mate_capability_catalog.dispatch_summary`, the data
``find_capabilities`` steps 1 and 2 return; the registry stays on demand). So
the conductor learns, at the moment it hands off, whether the worker can reach
what the work needs, and raises the capability card when the gateway has it.

Only that path: the caller's attested execution record must be a crewmate's
conductor, and the target's must be a worker of the same crewmate. Every other
caller and target gets nothing, and any failure answers ``""``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _note(app: Any, caller_history_key: str, target_history_key: str) -> str:
    """The summary when caller and target are a conductor and its own worker. Blocking."""
    from kiro_crew import mate_capabilities
    from kiro_crew import mate_capability_catalog as catalog_mod
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard.change_cards import _capability_service
    from kiro_crew.dashboard.crewmate_approval import crewmate_name_for
    from kiro_crew.execution_context import read_session_execution
    from kiro_crew.mate_worker import MATE_WORKER_AGENT, is_conductor_execution

    caller = read_session_execution(caller_history_key)
    target = read_session_execution(target_history_key)
    if caller is None or target is None or not is_conductor_execution(caller):
        return ""
    if (
        getattr(target, "selection_kind", "") != "template"
        or getattr(target, "template_id", "") != MATE_WORKER_AGENT
        or not caller.member_id
        or target.member_id != caller.member_id
    ):
        return ""
    from kiro_crew import crewmate_dynamic_capabilities

    config = KiroCrewConfig.load()
    member = crewmate_name_for(config, caller)
    if not member or not mate_capabilities.binding_is_mate(member):
        return ""
    if not crewmate_dynamic_capabilities.enabled_for(member, config):
        # This crewmate's Dynamic capabilities preview is off: no card to invite.
        return ""
    catalog = _capability_service(app)._mate_catalog(member)
    spec = catalog_mod.worker_spec(member)
    return catalog_mod.dispatch_summary(spec, catalog)


async def own_worker_note(app: Any, state: Any, caller_session_key: str, target_key: str) -> str:
    """The capability line for this hand-off, or ``""`` when it is not conductor to own worker."""
    # The summary invites a capability card, so it is the crewmate-driven path:
    # the crewmate's own preview decides, in ``_note``.
    from kiro_crew.dashboard.chat_utils import slot_history_key
    from kiro_crew.dashboard.session_control_parts.caller_access import caller_slot_key

    try:
        caller_slot = state.get_slot(caller_slot_key(state, caller_session_key))
        target_slot = state.get_slot(target_key) if target_key else None
        if caller_slot is None or target_slot is None:
            return ""
        return await asyncio.to_thread(
            _note, app, slot_history_key(caller_slot), slot_history_key(target_slot)
        )
    except Exception:  # noqa: BLE001 - the hand-off already happened; the note is extra
        logger.warning("worker capability note failed", exc_info=True)
        return ""
