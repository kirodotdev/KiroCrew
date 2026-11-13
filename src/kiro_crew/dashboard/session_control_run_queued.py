"""``session_run_queued``: run a message the caller queued on a session it manages now.

The queue card's Run now for an agent caller. The gates (``authorize_target``,
the manager fence, the audit row) stay in :mod:`kiro_crew.dashboard.session_control`
and are reached through the module, as :mod:`session_control_queue` reaches them,
and the entry rule is that module's :func:`_queue_entry_from`. The stop and the
idle dispatch are the route's own (``chat_api/turn_stop.py``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils_injected_rows import teardown_hold_active
from kiro_crew.dashboard.queue_edit_hold import edit_hold_blocks_user_turn
from kiro_crew.dashboard.relay_archive import (
    RELAY_ARCHIVE_CODE,
    RELAY_ARCHIVE_ERROR,
    is_relay_archive,
)
from kiro_crew.dashboard.session_control_queue import QUEUE_FROM_CALLER, _queue_entry_from
from kiro_crew.dashboard.slot_queue_repository import RUN_NOW_CHOSEN_KEY, clear_run_now_choice
from kiro_crew.sel import sel

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)


async def run_queued_target(
    state: "DashboardState",
    *,
    caller_session_key: str,
    target: str,
    queue_id: str,
    caller_fenced: bool | None = None,
) -> dict[str, Any]:
    """Run queued entry *queue_id* on *target* now, through the card's Run now path.

    Gates, all before anything changes: :func:`authorize_target`; the manager
    fence ``session_queue`` uses (:func:`_outside_reach`, so a handoff moves this
    verb with the queue); a relay archive is refused as the card's route refuses
    it; and the entry must be one the caller queued (:func:`_queue_entry_from`).

    * Running target: claim the cooperative stop, promote the entry, mark it
      :data:`RUN_NOW_CHOSEN_KEY` and stop the turn with the queue kept. The turn's
      end drains exactly that entry, past the child-work hold. Nothing suspends
      from the gate to the claim, so the route's supersede guards have no window
      to cover here. A stop already pending answers ``noop`` and changes nothing.
      A compaction holding the session is declined before the claim; one that
      commits after the probe leaves the entry at the front, as the route does,
      and answers ``compacting``.
    * Idle target: the route's dispatch, re-running every gate under the lock.
      An open queue editor or a pending conversation reset refuses it
      ``queue_held``: the dispatch passes both only for a person's own Run now.

    Same prewarm order as :func:`stop_target`: both awaits come before the gate.
    """
    try:
        await asyncio.to_thread(sel)
    except Exception:  # noqa: BLE001 - a prewarm failure must not fail the call
        logger.warning("session-control SEL prewarm failed", exc_info=True)
    await sc.prewarm_enabled_check()

    deny = sc._deny_factory(
        caller_session_key=caller_session_key, operation="run_queued", target=target
    )
    caller_key = sc.caller_slot_key(state, caller_session_key)
    if caller_fenced is None:
        caller_fenced = bool(caller_key) and sc._caller_is_ownership_fenced(state, caller_key)

    def _gate(name: str, *, recheck: bool = False) -> Any:
        slot = sc.authorize_target(
            state,
            caller_session_key=caller_session_key,
            target=name,
            operation="run_queued",
            skip_enabled_check=recheck,
            precomputed_ownership_fenced=caller_fenced,
        )
        if sc._outside_reach(state, slot, caller_key, "run_queued"):
            raise deny(
                "session_run_queued reaches only sessions this session manages", "not_creator"
            )
        if is_relay_archive(slot):
            raise deny(RELAY_ARCHIVE_ERROR, RELAY_ARCHIVE_CODE, status=409)
        entry = next((item for item in slot._queue if item.get("id") == queue_id), None)
        if entry is None:
            raise deny(
                f"no queued entry {queue_id!r} on that session", "entry_not_found", status=404
            )
        caller_tab = str(getattr(state._slots.get(caller_key), "_tab_id", "") or "")
        if _queue_entry_from(entry, caller_key, caller_tab) != QUEUE_FROM_CALLER:
            raise deny(
                "that entry was not queued by this session; only the messages you "
                "queued with session_send can be run now",
                "not_your_entry",
            )
        return slot

    slot = _gate(target)
    # Deferred for the import cycle `stop_target` documents.
    from kiro_crew.dashboard import chat_handlers as ch

    metadata = {"slot": slot.key, "queue_id": queue_id, "via": "session_control"}
    result: dict[str, Any]
    if slot.running:
        if slot._stop_state != "idle":
            ch._log_interrupt(slot, "noop", {**metadata, "reason": "stop already in progress"})
            result = {"outcome": "noop", "info": "stop already in progress"}
        else:
            cancel_key = ch._cancel_target(slot)
            if ch._compaction_in_flight(state, cancel_key):
                ch._decline_compacting_interrupt(state, slot, metadata)
                result = {"outcome": "compacting"}
            else:
                slot._stop_state = "soft_pending"
                claim_generation = slot._stop_generation
                clear_run_now_choice(slot)
                slot.queue_promote_by_id(queue_id)
                slot._queue[0][RUN_NOW_CHOSEN_KEY] = True
                stop = await ch._interrupt_claimed_turn(
                    state, slot, cancel_key, claim_generation, metadata
                )
                if stop == "compacting":
                    # Nothing was stopped: the entry keeps the front, not the mark.
                    clear_run_now_choice(slot)
                    result = {"outcome": "compacting", "stop": stop}
                else:
                    # ``idle``: the provider had no turn to cancel; the turn's end
                    # still drains the marked entry.
                    outcome = "stopping" if stop in ("soft", "hard") else stop
                    result = {"outcome": outcome, "stop": stop}
    else:

        def _replaced() -> None:
            if state._slots.get(slot.key) is not slot:
                raise deny(
                    "the target session was replaced before its message could run",
                    "target_replaced",
                    status=409,
                )

        def _reauthorize() -> None:
            # The lock await suspends: the target can have gained a channel link,
            # changed hands, or lost or reshaped the entry meanwhile.
            _gate(slot.key, recheck=True)
            # The dispatch passes the edit and teardown holds as a person's Run now
            # does; that pass is the person's to take, so the caller waits instead.
            if edit_hold_blocks_user_turn(slot) or teardown_hold_active(state, slot):
                raise deny(
                    "a person is editing a queued message on that session, or a "
                    "conversation reset is pending there; try again shortly",
                    "queue_held",
                    status=409,
                )

        def _refuse(error: str, code: str) -> None:
            raise deny(error, code, status=409)

        await ch._dispatch_run_now_idle(
            state,
            slot,
            queue_id,
            metadata,
            replaced=_replaced,
            reauthorize=_reauthorize,
            refuse=_refuse,
        )
        result = {"outcome": "started"}

    sc._audit(
        caller_session_key=caller_session_key,
        operation="run_queued",
        slot_key=slot.key,
        outcome="allowed",
        detail={"queue_id": queue_id, "outcome": result["outcome"]},
    )
    return {"ok": True, "target": slot.key, "queue_id": queue_id, **result}
