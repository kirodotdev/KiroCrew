"""Regenerate's replace step: the previous reply leaves only once its replacement exists.

A Regenerate re-runs the last user message to get a new reply. The endpoint
(``chat_regenerate.api_chat_slot_regenerate``) leaves the previous reply in
the window and on disk while the new turn runs, and arms a
:class:`RegenerateTarget` naming its rows. The turn then runs as any turn does,
appending its reply after the old one. At the turn's end (``_end_turn_tail``,
before the queue hands off to a successor) :func:`settle_regenerate` decides:

* the turn appended a reply -> the old rows are removed and the new reply keeps
  the old one as a variant, through one pinned rewrite of the transcript;
* the turn appended nothing (refused, cancelled before output, failed) -> the
  target is dropped and the previous reply stays where it was.

Settling at the turn's end keeps every per-turn reader on the window it was
built for: the turn-stats and file-change boundaries are indices captured at
turn start, and the window only shrinks after they have run. The rewrite is
pinned to the transcript and slot the Regenerate was authorized against, so a
close-and-recreate or a rebind during the turn refuses it and nothing of the
replacement's transcript is touched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        _MAX_SLOT_MESSAGES,
        logger,
        row_mid,
        save_slot_off_loop,
        variant_from_row,
    )
    from kiro_crew.dashboard.chat_turn.segment_flush import _adopt_pending_variants
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot


@dataclass(eq=False)
class RegenerateTarget:
    """The previous reply one Regenerate turn will replace.

    ``generation`` is the slot's ``_turn_generation`` of the Regenerate turn, so
    a later turn (a queued successor) never acts on a target it does not own.
    ``rows`` are the window rows after the regenerated user row at dispatch,
    matched by identity. ``history_key`` and ``slot_name`` pin the rewrite.
    ``reply_mids`` holds the ids of the reply rows this turn appends: model
    segments and partial replies (``_note_reply_row``) and command replies
    (``_append_command_reply``). Only the first surviving one is ever used, so
    the list is bounded at ``_MAX_SLOT_MESSAGES`` like ``_turn_reply_mids``.
    """

    generation: int
    rows: list[dict[str, Any]]
    history_key: str
    slot_name: str
    reply_mids: list[str] = field(default_factory=list)


def owned_regenerate_target(slot: "_ChatSlot") -> RegenerateTarget | None:
    """The slot's armed target when the turn now running owns it, else ``None``."""
    target = getattr(slot, "_regenerate_target", None)
    if not isinstance(target, RegenerateTarget):
        return None
    if target.generation != getattr(slot, "_turn_generation", None):
        return None
    return target


def note_regenerate_reply(slot: "_ChatSlot", row: dict[str, Any]) -> None:
    """Record ``row`` as a reply of the owning Regenerate turn, if one is running."""
    target = owned_regenerate_target(slot)
    mid = row_mid(row)
    if target is None or not mid or mid in target.reply_mids:
        return
    if len(target.reply_mids) < _MAX_SLOT_MESSAGES:
        target.reply_mids.append(mid)


def rows_before_regenerate_target(
    slot: "_ChatSlot", rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """``rows`` without the previous reply an owning Regenerate turn is replacing.

    The crew log numbers a turn by the durable rows before it. A Regenerate is
    the same turn asked again, so its number must not count the reply it is
    replacing: with that reply excluded the turn draws the ordinal of the turn
    it re-runs, and the emitter records it as a retry.
    """
    target = owned_regenerate_target(slot)
    if target is None:
        return rows
    old = {id(r) for r in target.rows}
    return [r for r in rows if id(r) not in old]


def _first_reply(slot: "_ChatSlot", target: RegenerateTarget) -> dict[str, Any] | None:
    """The first reply row the Regenerate turn left in the window, or ``None``."""
    mids = set(target.reply_mids)
    for row in slot.messages:
        if row_mid(row) in mids:
            return row
    return None


async def settle_regenerate(state: "DashboardState", slot: "_ChatSlot") -> None:
    """Replace the previous reply with the new one, or leave it when there is none.

    Runs once at the end of the Regenerate turn. The target is consumed first,
    so a second call is a no-op. Nothing changes in memory unless the pinned
    rewrite committed: a refused or failed write leaves both replies in place,
    which loses nothing.
    """
    target = owned_regenerate_target(slot)
    if target is None:
        return
    slot._regenerate_target = None
    reply = _first_reply(slot, target)
    if reply is None:
        # Empty turn: the previous reply was never removed, so there is nothing
        # to restore. The stashed variants belong to no reply.
        slot._pending_variants = []
        return
    old = {id(r) for r in target.rows}
    variants = reply.get("variants")
    if slot._pending_variants:
        # No flush adopted them (a partial reply or a local command reply).
        variants = _adopt_pending_variants(slot, variant_from_row(reply))
    swapped = dict(reply)
    if isinstance(variants, list):
        swapped["variants"] = variants
        swapped["variant_idx"] = len(variants) - 1
    # Read in the same synchronous stretch as the snapshot: an append at the
    # window cap during the write's await trims the front, and the save refuses
    # the drift instead of writing trimmed rows twice.
    snapshot = [swapped if r is reply else r for r in slot.messages if id(r) not in old]
    disk_older = slot._disk_older_count
    try:
        committed = await save_slot_off_loop(
            state,
            slot,
            snapshot,
            best_effort=False,
            expected_history_key=target.history_key,
            expected_slot_name=target.slot_name,
            expected_disk_older_count=disk_older,
        )
    except Exception:
        logger.warning("Regenerate: replacing the previous reply failed", exc_info=True)
        committed = False
    if not committed:
        logger.warning(
            "Regenerate: kept the previous reply of %s; the replace write was refused",
            slot.key,
        )
        return
    # Apply the committed swap to the live window. Rows may have been added
    # during the write's await; removal by identity keeps them.
    for index in range(len(slot.messages) - 1, -1, -1):
        if id(slot.messages[index]) in old:
            del slot.messages[index]
    if isinstance(variants, list):
        reply["variants"] = variants
        reply["variant_idx"] = len(variants) - 1
    slot._resumed_count = 0
    slot.invalidate_source_links()
    state.broadcast_ws(
        "chat_variant_switch",
        {"slot": slot.key, "index": reply.get("variant_idx", 0), "content": reply.get("content")},
    )
