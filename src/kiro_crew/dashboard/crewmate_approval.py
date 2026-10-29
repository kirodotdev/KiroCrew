"""A crewmate's Profile Permissions, applied to its conductor chat and its workers.

The mode is ``agents.<name>.approval_mode``. Slot trust is in memory only, so it
is seeded at each turn start (:func:`seed_for_turn`) and on a Profile change
(:func:`apply_to_live_sessions`). The crewmate comes from the session's attested
execution record. POLICY ∩ PROFILE still decides every call.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def crewmate_name_for(config: Any, execution: Any) -> str:
    """The ``config.agents`` name of the crewmate *execution* runs for, else ``""``.

    A conductor chat (``mate_worker.is_conductor_execution``) or a worker on
    ``kirocrew-mate-worker`` with the crewmate's ``member_id``. Any other record,
    a missing one, or an id the config does not hold answers ``""``.
    """
    from kiro_crew.execution_context import member_config_for_id
    from kiro_crew.mate_worker import MATE_WORKER_AGENT, is_conductor_execution

    if execution is None:
        return ""
    is_worker = (
        getattr(execution, "selection_kind", "") == "template"
        and getattr(execution, "template_id", "") == MATE_WORKER_AGENT
    )
    if not (is_worker or is_conductor_execution(execution, config)):
        return ""
    member_id = getattr(execution, "member_id", None)
    if member_id:
        try:
            return member_config_for_id(config, member_id)[0]
        except Exception:  # noqa: BLE001 - an unknown or ambiguous id is no crewmate
            return ""
    name = getattr(execution, "selection_name", "") or ""
    return name if not is_worker and name in getattr(config, "agents", {}) else ""


def seed_for_turn(state: Any, slot: Any, config: Any, execution: Any) -> bool:
    """Give *slot* its crewmate's mode before this turn claims a provider.

    Once per live slot (``slot._member_approval_seeded``), through the same seed a
    crewmate's thread takes when it is opened (``handlers.members._seed_member_approval``):
    a slot that already holds a grant keeps it, ``normal`` adds none. Returns
    whether the slot belongs to a crewmate. Pure in-memory work.
    """
    if getattr(slot, "_member_approval_seeded", True):
        return False
    name = crewmate_name_for(config, execution)
    if not name:
        return False
    from kiro_crew.dashboard.handlers.members import _seed_member_approval

    record = config.agents.get(name)
    _seed_member_approval(state, slot, getattr(record, "approval_mode", "") or "")
    return True


async def seed_new_worker(state: Any, slot: Any, execution: Any) -> bool:
    """Seed a just-born worker from the crewmate's Profile as it stands now.

    Called once the worker's record is on disk, so a Profile change from here on
    reaches it through :func:`apply_to_live_sessions`; one that landed while the
    worker was being created is read here. Returns whether it was seeded.
    """
    import asyncio

    from kiro_crew.config.loader import KiroCrewConfig

    config = await asyncio.to_thread(KiroCrewConfig.load)
    # A Profile save whose apply runs while this read is in its thread leaves
    # `config` stale. The once-per-slot `_member_approval_seeded` flag covers it:
    # that apply sets the flag, so the seed below skips and the saved mode stands.
    if state._slots.get(slot.key) is not slot:
        return False
    return seed_for_turn(state, slot, config, execution)


async def apply_to_live_sessions(state: Any, name: str, mode: str) -> int:
    """Put every live worker session of crewmate *name* on *mode*. Returns how many.

    The crewmate's own thread is handled by its caller
    (``agent_admin.crew_update._apply_member_approval_to_live_thread``). Each slot's
    record is read off the loop; the mode goes through ``apply_approval_mode``,
    the mutation half of ``POST /api/chat/mode``.
    """
    import asyncio

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard.chat_handlers import apply_approval_mode
    from kiro_crew.dashboard.chat_utils import slot_history_key
    from kiro_crew.execution_context import read_session_execution
    from kiro_crew.mate_worker import MATE_WORKER_AGENT

    slots = list(state._slots.values())

    def _workers() -> list[Any]:
        config = KiroCrewConfig.load()
        out = []
        for slot in slots:
            try:
                execution = read_session_execution(slot_history_key(slot))
            except Exception:  # noqa: BLE001 - an unreadable record is not this crewmate's
                continue
            if (
                getattr(execution, "template_id", "") == MATE_WORKER_AGENT
                and crewmate_name_for(config, execution) == name
            ):
                out.append(slot)
        return out

    from kiro_crew.config.sections import MEMBER_APPROVAL_MODE_UNSET_EFFECTIVE

    mode = mode or MEMBER_APPROVAL_MODE_UNSET_EFFECTIVE
    workers = await asyncio.to_thread(_workers)
    for slot in workers:
        if state._slots.get(slot.key) is not slot:
            continue
        await apply_approval_mode(
            state,
            mode=mode,
            slot=slot,
            slot_key=slot.key,
            request_app="",
            cron_creator="",
            audit_caller=lambda label: "dashboard:member_profile",
        )
        slot._member_approval_seeded = True
    return len(workers)
