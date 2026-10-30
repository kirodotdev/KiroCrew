"""``session_handoff``: give a session to another session to manage.

The ownership fence admits an agent caller to the sessions it CREATED. A lead that
opens a conductor and its workers cannot put the workers under that conductor, because
the conductor created none of them. A handoff names the conductor as the workers'
manager (:mod:`kiro_crew.dashboard.session_managers`), and the verbs that run a session
read the manager from then on (``HANDOFF_WIDENED_OPERATIONS``). ``_created_by`` is never
rewritten, so create, fork and the memory a session runs against still follow the
creator.

The reach MOVES rather than being shared: the giver is refused afterwards unless the
manager chain switch is on and the giver manages the new manager.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from kiro_crew.dashboard import session_managers
from kiro_crew.dashboard.session_control_parts.caller_access import (
    MAX_MANAGER_CHAIN,
    SessionControlError,
    _audit,
    _audit_denials,
    _outside_reach,
    caller_slot_key,
    manager_chain_enabled,
    prewarm_enabled_check,
)
from kiro_crew.dashboard.session_control_parts.session_tree import (
    _live_sid_of,
    _note_pending_edge,
    _reaches_through_pending,
    _recorded_sid_of,
    _slot_tree_parent,
    _tree_append_landed,
    _tree_append_waiter,
    _tree_mutation_lock,
)
from kiro_crew.dashboard.session_control_parts.target_authorization import authorize_target
from kiro_crew.dashboard.state import (
    MAX_SLOTS_PER_CREATOR,
    append_and_surface,
    load_window_off_loop,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

logger = logging.getLogger("kiro_crew.dashboard.session_control")

#: Held by :func:`handoff_target` from its first read of the records through its write,
#: and by :func:`hand_back_on_close`, so each one decides on the records the one before
#: it saved. Without it, A to B and B to A at the same time both pass the loop check,
#: and a recipient that closes during the write is never handed back.
_HANDOFF_LOCK = session_managers.HANDOFF_LOCK


def _manages_up_the_line(state: "DashboardState", start: "_ChatSlot", target_key: str) -> bool:
    """Whether *target_key* is *start*'s manager, or its manager's manager, and so on."""
    slot: Any = start
    for _ in range(MAX_MANAGER_CHAIN):
        holder = session_managers.holder(slot)
        if not holder:
            return False
        if holder == target_key:
            return True
        slot = state.get_slot(holder)
        if slot is None:
            return False
    return False


async def _move_tree_edge(state: "DashboardState", slot: "_ChatSlot", parent: str) -> bool:
    """Hang *slot* under *parent* in the session tree. Best-effort; ``True`` when moved.

    The same ``session/adopted`` entry ``session_adopt`` writes, so the sidebar shows the
    session under the manager it was handed to. Skipped, with the handoff still standing,
    when the tree is not recorded or not readable, when the edge is already there, or
    when the move would make a loop.
    """
    from kiro_crew.crew_log import emit as crew_log_emit

    target_sid = _live_sid_of(state, slot.key)
    if not crew_log_emit.enabled() or not target_sid:
        return False
    async with _tree_mutation_lock():
        known, previous_parent, nodes = _slot_tree_parent(slot.key)
        if not known or previous_parent == parent:
            return False
        if _reaches_through_pending(slot.key, parent, nodes):
            return False
        parent_sid = await _recorded_sid_of(state, parent)
        previous_sid = await _recorded_sid_of(state, previous_parent) if previous_parent else ""
        settled, landed = _tree_append_waiter(slot.key)
        _note_pending_edge(slot.key, parent, landed)
        crew_log_emit.on_session_adopted(
            target_sid,
            slot=slot.key,
            parent_slot=parent,
            parent_sid=parent_sid,
            previous_parent_slot=previous_parent,
            previous_parent_sid=previous_sid,
            on_settled=settled,
        )
        try:
            await _tree_append_landed(landed, operation="handoff", target=slot.key)
        except SessionControlError as exc:
            logger.warning("session_handoff: tree edge for %s not moved: %s", slot.key, exc)
            return False
    return True


async def _note_in_transcript(
    state: "DashboardState", slot: "_ChatSlot", manager: str, previous: str
) -> None:
    """One display-only row in the handed-over session's transcript. Best-effort.

    A ``notice`` row is never part of a prompt, so the text states what happened for a
    person reading the transcript rather than telling the agent anything.
    """
    text = f"This session was handed over: {manager} manages it now"
    text += f", in place of {previous}." if previous else "."
    try:
        await load_window_off_loop(slot)
        if state.get_slot(slot.key) is slot:
            append_and_surface(state, slot, "notice", text, "msg msg-info")
    except Exception as exc:  # the row must never undo a committed handoff
        logger.warning("session_handoff: transcript note for %s not written: %s", slot.key, exc)


async def handoff_target(
    state: "DashboardState",
    *,
    caller_session_key: str,
    target: str,
    to: str,
    caller_fenced: bool | None = None,
) -> "dict[str, Any]":
    """Make *to* the manager of *target*.

    Both sessions are authorized through :func:`authorize_target` with the ``handoff``
    operation, and then held to the manager rule for EVERY caller, fenced or not: the
    caller must manage the target (only the current manager can hand it on) and the
    recipient, or be the recipient. Without that second check an unfenced agent in one
    of the person's own tabs could hand another of the person's tabs, which has no
    manager, to a fenced session. Refused when the recipient is the target, already
    manages it, or is managed by it (a loop in the manager line).

    The new manager is written to disk before anything else changes. The tree edge and
    the transcript row follow, and neither can undo the handoff.
    """
    async with _HANDOFF_LOCK:
        await asyncio.to_thread(session_managers.load)
        await prewarm_enabled_check()
        slot = authorize_target(
            state,
            caller_session_key=caller_session_key,
            target=target,
            operation="handoff",
            precomputed_ownership_fenced=caller_fenced,
        )
        receiver = authorize_target(
            state,
            caller_session_key=caller_session_key,
            target=to,
            operation="handoff",
            precomputed_ownership_fenced=caller_fenced,
            allow_self=True,
        )
        with _audit_denials(
            caller_session_key=caller_session_key, operation="handoff", slot_key=slot.key
        ):
            caller_key = caller_slot_key(state, caller_session_key)
            for held in (slot, receiver):
                if held.key != caller_key and _outside_reach(state, held, caller_key, "handoff"):
                    raise SessionControlError(
                        f"{held.key!r} is not a session this one manages, so it cannot be "
                        "handed over or handed to",
                        status=403,
                        code="not_manager",
                    )
            if receiver.key == slot.key:
                raise SessionControlError(
                    "a session cannot be handed to itself", status=409, code="self_manager"
                )
            previous = session_managers.holder(slot)
            if receiver.key == previous:
                raise SessionControlError(
                    f"{receiver.key!r} already manages {slot.key!r}",
                    status=409,
                    code="already_manager",
                )
            if _manages_up_the_line(state, receiver, slot.key):
                raise SessionControlError(
                    f"{slot.key!r} manages {receiver.key!r}, so handing it over would make a loop",
                    status=409,
                    code="would_cycle",
                )
            # The cap charges the manager as well as the creator
            # (``creator_slot_count``), so the receiver takes on this slot's charge
            # and must have room for it unless it is charged for it already.
            if (
                getattr(slot, "_revived_by", "") != receiver.key
                and getattr(slot, "_created_by", "") != receiver.key
                and state.creator_slot_count(receiver.key) >= MAX_SLOTS_PER_CREATOR
            ):
                raise SessionControlError(
                    f"{receiver.key!r} already holds the per-caller slot cap "
                    f"({MAX_SLOTS_PER_CREATOR})",
                    code="creator_slot_cap_reached",
                    status=429,
                )
            tab = str(getattr(slot, "_tab_id", "") or "")
            # ``authorize_target`` found the receiver open, and nothing suspends between
            # it and this write. A close that lands during the write pops the receiver,
            # then waits here in ``hand_back_on_close`` and hands back what this saved.
            try:
                await asyncio.to_thread(session_managers.write, {slot.key: (receiver.key, tab)})
            except OSError as exc:
                raise SessionControlError(
                    f"the handoff of {slot.key!r} could not be saved, so nothing changed",
                    status=500,
                    code="handoff_write_failed",
                ) from exc
    _audit(
        caller_session_key=caller_session_key,
        operation="handoff",
        slot_key=slot.key,
        outcome="allowed",
        detail={"manager": receiver.key, "previous_manager": previous or "none"},
    )
    tree_moved = await _move_tree_edge(state, slot, receiver.key)
    await _note_in_transcript(state, slot, receiver.key, previous)
    return {
        "target": slot.key,
        "manager": receiver.key,
        "previous_manager": previous,
        "tree_moved": tree_moved,
    }


async def hand_back_on_close(state: "DashboardState", departed: "_ChatSlot") -> "list[str]":
    """Move the sessions *departed* managed to *departed*'s own manager, chain switch on.

    Called once a close has committed. The sessions it managed are the ones handed to
    it and the open ones it created that nobody handed elsewhere. With the switch off
    nothing moves: they keep naming a manager that is gone, so only the person reaches
    them, which is the cost of leaving the switch off. With it on, each goes to the
    manager above, or back to its creator when *departed* had none. Never raises.
    """
    try:
        if not await asyncio.to_thread(manager_chain_enabled):
            return []
        async with _HANDOFF_LOCK:
            records = await asyncio.to_thread(session_managers.load)
            reports = session_managers.managed_by(departed.key)
            for slot in list(state._slots.values()):
                if slot.key not in records and getattr(slot, "_created_by", "") == departed.key:
                    reports[slot.key] = str(getattr(slot, "_tab_id", "") or "")
            if not reports:
                return []
            up = session_managers.holder(departed)
            changes: dict[str, tuple[str, str] | None] = {
                key: ((up, tab) if up and up != key else None) for key, tab in reports.items()
            }
            # A created session with no record already falls back to its creator, so it
            # needs a record only when there is a manager above to name.
            changes = {k: v for k, v in changes.items() if v is not None or k in records}
            await asyncio.to_thread(session_managers.write, changes)
    except Exception as exc:
        logger.warning("session_handoff: hand-back from %s failed: %s", departed.key, exc)
        return []
    logger.info("session_handoff: %s closed; %d session(s) handed up", departed.key, len(changes))
    return sorted(changes)
