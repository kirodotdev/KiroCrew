"""A crewmate's Profile Permissions, applied to its conductor chat and its workers.

The mode is ``agents.<name>.approval_mode``. Slot trust is in memory only, so it
is seeded at each turn start (:func:`seed_for_turn`) and on a Profile change
(:func:`apply_to_live_sessions`). The crewmate comes from the session's attested
execution record. POLICY ∩ PROFILE still decides every call.
"""

from __future__ import annotations

import logging
import weakref
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


#: The state attribute mapping a worker's session key to the start on the conducting
#: kit that was checked: a weak reference to the live provider and that provider's
#: native session id (``LLMProvider.session_id``). A new start publishes a new
#: provider, a conversation swapped in place on the same provider
#: (``SessionLifecycleService.swap_conversation``, behind Edit & Resend) has a new
#: native id, and a claim of the running session keeps both, so each start is checked
#: once. (``SessionManager.session_generation`` cannot say this: every claim advances
#: it.) An empty id never matches: a session that reports none (one not started yet, or
#: a direct client whose process ended between turns) is checked at every turn until
#: its start reports one, so the check needs nothing from the order in which other
#: modules set the id, and it only ever lowers. Kept on the state, not the slot:
#: ``_ChatSlot`` declares ``__slots__``.
KIT_CHECKED_ATTR = "_crewmate_kit_checked"

_PROFILE_CALLER = "dashboard:member_profile"


async def predicted_start_mounts(state: Any, slot: Any, config: Any) -> bool:
    """Whether the session start a turn is about to make for *slot* mounts session control.

    The start's own predicate (``mate_worker_conducts.start_mounts_session_control``)
    on what a cold start of *slot* uses: the backend it selects under *config*
    (``chat_handlers.configured_backend_for_session``), the slot's agent and folder (the
    worktree admission picks first), its key for the spec its Bot's file shapes, and
    *config* for its Bot's switch, read off the loop. False with no read for any slot
    but a worker its Bot's thread opened, and for one whose session is live, which the
    check after the claim reads instead.
    """
    import asyncio

    from kiro_crew import members
    from kiro_crew.dashboard.chat_utils import effective_session_key, slot_history_key

    if not members.is_thread_born_crewmate_worker(slot_history_key(slot)):
        return False
    if state.sessions.get_provider(effective_session_key(slot)) is not None:
        return False
    return await asyncio.to_thread(_cold_start_mounts, slot, config)


def _cold_start_mounts(slot: Any, config: Any) -> bool:
    """The blocking half of :func:`predicted_start_mounts`."""
    from kiro_crew.crewmate_harness import CrewmateHarnessUnavailable
    from kiro_crew.dashboard.chat_handlers import configured_backend_for_session
    from kiro_crew.dashboard.chat_utils import effective_session_key, slot_history_key
    from kiro_crew.mate_worker_conducts import start_mounts_session_control

    try:
        backend = configured_backend_for_session(effective_session_key(slot), config)
    except CrewmateHarnessUnavailable:
        # A pin this gateway cannot start refuses the start itself; nothing mounts.
        return False
    agent = getattr(slot, "agent", "") or None
    return start_mounts_session_control(
        slot_history_key(slot), backend, agent, getattr(slot, "project", "") or None, config
    )


def tighten_for_conducting(
    state: Any, slot: Any, config: Any, execution: Any, *, start_mounts: bool = False
) -> bool:
    """Hold a Bot's worker to the stricter of its posture and its Bot's Profile mode
    at its first turn on the conducting kit. Never raises a posture.

    The kit comes with a session start that mounts session control while the Bot's
    ``worker_conducts`` switch is on, the reload a switch change brings included. By
    then the worker may hold a posture its Profile never gave it: a trust a fork copied
    from the thread while the switch was off, or a mode a person set while it was
    plain. The walk that carries the thread's later mode change passes over a Bot's
    workers, so such a posture would outlive the thread's.

    Turn admission calls this twice. Before the claim, a start the turn is about to
    make is judged by *start_mounts* (:func:`predicted_start_mounts`, the start's own
    predicate on *config*, the turn's load), so a session that will mount starts on
    the lowered posture and one that cannot (a backend that carries no mount, a
    ``kirocrew-dashboard`` switched off, by its Bot's file too) keeps the mode a
    person set; that judgement only lowers and is not recorded. After the claim, the live provider says
    what its start mounted (``mate_worker_conducts.holds_conducting_kit``), and the
    outcome is recorded
    against that provider and its native session id (:data:`KIT_CHECKED_ATTR`): a mode
    a person sets while the worker conducts stands until its next start on the kit,
    a conversation swapped in place included. A session that reports no native id is
    checked at every turn, only ever lowering. A pending policy verdict (a
    ceiling install, while trust reads as denied) leaves it to the next turn.
    Sessions that inherit the worker's mode follow the lowering and keep following
    it. Returns whether a posture was lowered. Pure in-memory work, safe on the loop.
    """
    from kiro_crew import members
    from kiro_crew.approval_mode_policy import clamp_session_mode, verdict_pending
    from kiro_crew.config.sections import MEMBER_APPROVAL_MODE_UNSET_EFFECTIVE
    from kiro_crew.dashboard.chat_handlers import _apply_inherited_postures
    from kiro_crew.dashboard.chat_utils import effective_session_key, slot_history_key
    from kiro_crew.dashboard.slot_approval_seed import sync_session_policy, tighten_slot_approval
    from kiro_crew.mate_worker_conducts import holds_conducting_kit

    checked = getattr(state, KIT_CHECKED_ATTR, None)
    if not isinstance(checked, dict):
        checked = {}
        setattr(state, KIT_CHECKED_ATTR, checked)
    key = effective_session_key(slot)
    provider = state.sessions.get_provider(key)
    native_sid = ""
    if provider is not None:
        if not holds_conducting_kit(state, slot):
            checked.pop(key, None)
            return False
        native_sid = str(getattr(provider, "session_id", "") or "")
        judged = checked.get(key)
        if (
            judged is not None
            and native_sid
            and judged[0]() is provider
            and judged[1] == native_sid
        ):
            return False
    elif not (start_mounts and members.conducting_worker_session(slot_history_key(slot), config)):
        # A cold start that will not mount session control keeps the mode it holds.
        # shortcut: *start_mounts* is read before the start, so it cannot see a withhold
        # the composing client makes only once it holds the session: a per-tool
        # switch-off on ``kirocrew-dashboard`` the host cannot put in force (pi has no
        # per-tool deny, so its client withholds the server), or a claude permission
        # file Crew does not govern. Such a start, like one a save changes after this
        # read, is still lowered below; it only ever lowers. Hand the composer's answer
        # to admission if that lowering must ever be exact.
        return False
    if verdict_pending():
        return False
    name = crewmate_name_for(config, execution)
    record = config.agents.get(name) if name else None
    if record is None:
        return False
    profile = clamp_session_mode(
        getattr(record, "approval_mode", "") or MEMBER_APPROVAL_MODE_UNSET_EFFECTIVE
    )
    lowered = tighten_slot_approval(state, slot, profile, caller=_PROFILE_CALLER)
    if lowered:
        before = {id(s): (s._trust, s._trust_reads) for s in list(state._slots.values())}
        _apply_inherited_postures(
            state, slot, may_raise=False, audit_caller=lambda _label: _PROFILE_CALLER
        )
        for followed in list(state._slots.values()):
            if before.get(id(followed)) != (followed._trust, followed._trust_reads):
                sync_session_policy(state, followed)
    if provider is not None:
        try:
            checked[key] = (weakref.ref(provider), native_sid)
        except TypeError:
            # shortcut: a provider that takes no weak reference records nothing and is
            # checked at every turn (only ever lowering); record it another way if one
            # ever conducts.
            checked.pop(key, None)
    return lowered


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

    from kiro_crew.approval_mode_policy import clamp_session_mode, ensure_resolved
    from kiro_crew.config.sections import MEMBER_APPROVAL_MODE_UNSET_EFFECTIVE

    # A final policy answer before the clamp: a passing deny is not a Profile choice.
    await asyncio.to_thread(ensure_resolved)
    workers = await asyncio.to_thread(_workers)
    mode = clamp_session_mode(mode or MEMBER_APPROVAL_MODE_UNSET_EFFECTIVE)
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
