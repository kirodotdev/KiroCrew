"""A requeued steer row that its own drained turn supersedes: the match, the carried meta
and the retire (``_requeued_steer_row``, ``_carry_superseded_steer_meta``,
``_retire_superseded_steer_row``)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from kiro_crew.dashboard.chat_delivery import STEER_STATE_REQUEUED, STEER_STATE_SUPERSEDED
from kiro_crew.dashboard.chat_utils_injected_rows import REQUEUED_STEER_ROW_MID_META
from kiro_crew.shared_crew.contracts import MESSAGE_AUTHOR_META_KEY

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import DashboardState, _ChatSlot, logger


#: Meta the persisting tail stamps only on a steer row. The drain keeps its
#: decision receipt, quote, and server-stamped author when it supersedes that
#: row. Without the author, hiding the old row also hides the sender's byline.
#: The queue_pop frame and the row that stays drawn both read this tuple.
_SUPERSEDED_STEER_CARRIED_KEYS = (
    "decisions_strip",
    "quote",
    MESSAGE_AUTHOR_META_KEY,
)


def _requeued_steer_row(
    slot: _ChatSlot, consumed: list[dict], next_msg: str, role: str
) -> dict | None:
    """Return the requeued steer row a drained entry supersedes, or None.

    ``_requeue_unconsumed_steers`` names the row on the entry
    (``REQUEUED_STEER_ROW_MID_META``) when the steer was already persisted. It is
    superseded only when it still says exactly what this turn runs: the entry
    drains alone, the row is a steer row in the ``requeued`` state with the
    *role* the drain writes, and its text equals ``next_msg``. The drain passes
    ``"user"`` for the person's or a peer's steer and ``"nudge"`` for an
    automation steer, so the row drawn in the old row's place is the same kind
    of row. Anything else returns None and both rows stay visible, which is how
    every edited or merged send already renders.
    """
    if len(consumed) != 1:
        return None
    meta = consumed[0].get("meta")
    if not isinstance(meta, dict):
        return None
    mid = meta.get(REQUEUED_STEER_ROW_MID_META)
    if not (isinstance(mid, str) and mid):
        return None
    for row in slot.messages:
        row_meta = row.get("meta")
        if not isinstance(row_meta, dict) or row_meta.get("mid") != mid:
            continue
        if (
            row.get("role") == role
            and row_meta.get("steer") is True
            and row_meta.get("steerState") == STEER_STATE_REQUEUED
            and row.get("content") == next_msg
        ):
            return row
        return None
    return None


def _carry_superseded_steer_meta(row: dict, meta: dict) -> None:
    """Copy the superseded *row*'s carried meta onto *meta*, where *meta* lacks it.

    The superseded row is the only row that holds the steer's decision receipt,
    quote, and server-stamped author, and the requeued entry carries none of them.
    Both readers take them before the drain hides the old row: the ``queue_pop``
    frame and the drained row that stays drawn. The author is copied as a new
    dict, and only when it is one.
    """
    old_meta = row.get("meta") or {}
    for key in _SUPERSEDED_STEER_CARRIED_KEYS:
        if key not in old_meta or key in meta:
            continue
        value = old_meta[key]
        if key == MESSAGE_AUTHOR_META_KEY:
            if isinstance(value, dict):
                meta[key] = dict(value)
        else:
            meta[key] = value


def _retire_superseded_steer_row(
    state: DashboardState, slot: _ChatSlot, row: dict, successor: dict | None
) -> None:
    """Mark a requeued steer row ``superseded`` once its own turn's row has landed.

    The drained turn wrote a fresh row carrying the same text, so the transcript
    keeps both rows for the model and for history while the client stops drawing
    the old one. Best-effort like ``_mark_steer_row_state``: a missed patch leaves
    the pre-fix rendering (the text twice), never a lost message.

    ``supersededBy`` names the successor's ``mid``, so a pin, quote or share link
    saved against the old row lands on the row that is drawn in its place.
    """
    meta = dict(row.get("meta") or {})
    mid = meta.get("mid")
    ts = str(row.get("ts") or "")
    if not (isinstance(mid, str) and mid):
        return
    meta["steerState"] = STEER_STATE_SUPERSEDED
    successor_mid = ((successor or {}).get("meta") or {}).get("mid")
    if isinstance(successor_mid, str) and successor_mid and successor_mid != mid:
        meta["supersededBy"] = successor_mid
    if slot.update_message(ts, meta=meta, mid=mid) is None:
        return
    try:
        state.broadcast_ws(
            "chat_message_update", {"slot": slot.key, "ts": ts, "mid": mid, "meta": meta}
        )
    except Exception:
        logger.warning("superseded steer broadcast failed for slot %s", slot.key, exc_info=True)
