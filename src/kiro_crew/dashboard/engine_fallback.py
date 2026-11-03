"""A dashboard turn's view of a fallback the ENGINE made (``acp/engine_fallback.py``).

Crew reflects the model kiro-cli is on. The handle has already moved the
resolved model; this module moves the slot: a promotion becomes the pinned
slot's model, and the turn ends with one notice in kiro-cli's wording.

While Crew's own fallback (``chat_turn/model_fallback.py``) serves the slot, the
pin is the user's primary: a promotion moves Crew's record instead, so Crew's
restore still returns to the primary.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

from kiro_crew.agent_sdk.drivers.acp_vocab import (
    EngineFallbackWalk,
    ModelFallbackNotice,
    is_unpinned_pick,
    same_model,
    walk_outcome,
)
from kiro_crew.llm_helpers import TURN_FALLBACK_ATTR, pick_epoch_host

logger = logging.getLogger(__name__)


def record_engine_fallback(
    state: Any, slot: Any, client: Any, walk: EngineFallbackWalk, notice: ModelFallbackNotice
) -> None:
    """Record one engine notice on the turn's walk; follow a promotion on the slot."""
    to = notice.to_model
    if not notice.promoted or to is None or to == notice.from_model:
        walk.record(notice)
        return
    if _crew_fallback_active(slot):
        # The engine moved off Crew's fallback, not the user's pin. Crew's record
        # follows; the pin stays, so the restore still returns to the primary.
        # Crew moves the session off `to`, so the line says "for this turn".
        _retarget_crew_fallback(slot, client, notice.from_model, to)
        if not _crew_fallback_active(slot):
            walk.record(notice)  # back on the primary: it is the conversation's model
        elif not any(hop.to_model == to for hop in walk.hops):
            walk.record(dataclasses.replace(notice, promoted=False))
        _sync_slot(state, slot, client)
        return
    deferred = getattr(slot, "_deferred_model_switch_key", "")
    if isinstance(deferred, str) and deferred:
        # The user picked a model during this turn; the turn boundary applies it,
        # so the engine's target answers this turn only and the pick stays.
        if not any(hop.to_model == to for hop in walk.hops):
            walk.record(dataclasses.replace(notice, promoted=False))
        _sync_slot(state, slot, client)
        return
    walk.record(notice)
    # Crew reflects the model kiro-cli is on: a pinned slot takes `to`, so a
    # re-apply does not undo it. An unpinned slot stays unpinned, and so does a
    # pin the session withheld, which the engine never ran.
    withheld = getattr(client, "model_pin_refused", "")
    if is_unpinned_pick(getattr(slot, "model", "")) or (isinstance(withheld, str) and withheld):
        _sync_slot(state, slot, client)
        return
    slot.model = to
    # Counted as a pick, so a pending Crew restore reads as stale.
    slot._model_pick_gen = getattr(slot, "_model_pick_gen", 0) + 1
    try:
        host = pick_epoch_host(client)
        host._explicit_pick_epoch = getattr(host, "_explicit_pick_epoch", 0) + 1
    except Exception:  # noqa: BLE001 - a frozen/slotted stub client
        logger.debug("engine fallback: could not stamp the pick epoch", exc_info=True)
    _sync_slot(state, slot, client)


def _crew_fallback_active(slot: Any) -> bool:
    """Whether Crew's own throttle or refusal fallback is serving *slot* now."""
    return any(
        isinstance(value, str) and bool(value.strip())
        for value in (
            getattr(slot, "_active_fallback_model", ""),
            getattr(slot, "_refusal_fallback_primary", ""),
        )
    )


def _retarget_crew_fallback(slot: Any, client: Any, frm: str, to: str) -> None:
    """Move Crew's fallback record from *frm* to the engine's target *to*.

    The restore checks that the session still runs Crew's candidate before it
    puts the primary back; left on *frm*, it would read the engine's move as the
    session leaving the fallback and drop the record without restoring.
    """
    active = getattr(slot, "_active_fallback_model", "")
    if isinstance(active, str) and active and same_model(active, frm):
        if same_model(to, getattr(slot, "_fallback_primary_model", "")):
            # The engine moved back onto the primary: the fallback is over.
            from kiro_crew.dashboard import chat_runner  # chat_runner imports this module

            chat_runner._clear_fallback_sticky_state(slot, client)
        else:
            slot._active_fallback_model = to
            marker = getattr(client, TURN_FALLBACK_ATTR, None)
            if (
                isinstance(marker, (tuple, list))
                and len(marker) >= 2
                and same_model(marker[1], frm)
            ):
                try:
                    setattr(client, TURN_FALLBACK_ATTR, (marker[0], to))
                except Exception:  # noqa: BLE001 - a frozen/slotted stub client
                    logger.debug("engine fallback: could not move the marker", exc_info=True)
    primary = getattr(slot, "_refusal_fallback_primary", "")
    if (
        isinstance(primary, str)
        and primary
        and same_model(getattr(slot, "_refusal_fallback_candidate", ""), frm)
    ):
        if same_model(to, primary):
            slot._refusal_fallback_primary = slot._refusal_fallback_candidate = ""
        else:
            slot._refusal_fallback_candidate = to


def _sync_slot(state: Any, slot: Any, client: Any) -> None:
    """Let the slot's chip name the model now serving, and push the change."""
    record = getattr(slot, "record_served_model", None)
    if record is not None:
        record(str(getattr(client, "served_model", "") or ""))
    push = getattr(state, "push_slots_update", None)
    if push is not None:
        push()


def finish_engine_fallback(
    slot: Any, walk: EngineFallbackWalk, *, stop_reason: str, cancelled: bool
) -> None:
    """Append the walk's line to the transcript, once, when the turn ends.

    A persisted notice card, like the other model notices, so the account of
    which model answered survives a reload. Never raises: it runs in the turn's
    teardown, where an exception would mask the one that ended the turn.
    """
    try:
        outcome = walk_outcome(stop_reason, cancelled=cancelled)
        line = walk.line(outcome)
        walk.hops.clear()
        walk.promoted_to = None
        if line:
            slot.append("notice", line, "msg msg-info")
    except Exception:  # noqa: BLE001 - teardown must not raise
        logger.warning("engine fallback notice failed", exc_info=True)
