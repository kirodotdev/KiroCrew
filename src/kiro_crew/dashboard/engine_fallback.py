"""A dashboard turn's view of a fallback the ENGINE made (``acp/engine_fallback.py``).

The session handle has already moved the session's resolved model by the time
an ``EVENT_MODEL_FALLBACK`` reaches the turn. What is left for the dashboard is
the slot: a promotion makes the target the conversation's model, so the slot's
pin and chip follow it and Crew's own restore paths stop treating the model
that refused as the one to return to. And the transcript: when the turn ends,
one notice says what the engine did, in kiro-cli's wording.

Crew's own fallback (``chat_turn/model_fallback.py``) is not consulted or
changed here; it still runs on a turn that ends in an error or a refusal.
"""

from __future__ import annotations

import logging
from typing import Any

from kiro_crew.agent_sdk.drivers.acp_vocab import (
    EngineFallbackWalk,
    ModelFallbackNotice,
    is_unpinned_pick,
    walk_outcome,
)
from kiro_crew.llm_helpers import pick_epoch_host

logger = logging.getLogger(__name__)


def record_engine_fallback(
    state: Any, slot: Any, client: Any, walk: EngineFallbackWalk, notice: ModelFallbackNotice
) -> None:
    """Record one engine notice on the turn's walk; follow a promotion on the slot."""
    walk.record(notice)
    to = notice.to_model
    if not notice.promoted or to is None or to == notice.from_model:
        return
    # The engine already made `to` the session's model. A pinned slot follows
    # it, as kiro-cli's picker does; otherwise the next re-apply of the pin
    # would move the session back onto the model that refused. An unpinned
    # slot stays unpinned, and its chip names the served model instead.
    if not is_unpinned_pick(getattr(slot, "model", "")):
        slot.model = to
    # Counted as an explicit pick, which is what it is from the slot's side: the
    # throttle restore probe and the refusal-fallback restore both drop a record
    # whose pick generation or client epoch moved, rather than restoring a
    # primary over the engine's choice.
    slot._model_pick_gen = getattr(slot, "_model_pick_gen", 0) + 1
    try:
        host = pick_epoch_host(client)
        host._explicit_pick_epoch = getattr(host, "_explicit_pick_epoch", 0) + 1
    except Exception:  # noqa: BLE001 - a frozen/slotted stub client
        logger.debug("engine fallback: could not stamp the pick epoch", exc_info=True)
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
