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

from kiro_crew.dashboard.slot_persistence.write_guards import (
    AppliedSwap,
    finish_swap,
    publish_swap,
)

if TYPE_CHECKING:
    import asyncio

    from kiro_crew.dashboard.chat_runner import (
        _MAX_SLOT_MESSAGES,
        logger,
        save_slot_off_loop,
        variant_from_row,
    )
    from kiro_crew.dashboard.chat_turn.segment_flush import _adopt_pending_variants
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot, row_mid


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

    ``recovery_qid`` is set when the turn queued a recovery before it had any
    reply (a session-not-found replay, a refusal fallback, a continuation): that
    recovery is the same Regenerate's next attempt, so the target waits for it instead of settling the
    empty first attempt, and ``carried_variants`` holds the stashed variants
    meanwhile so no other turn's flush adopts them.
    """

    generation: int
    rows: list[dict[str, Any]]
    history_key: str
    slot_name: str
    reply_mids: list[str] = field(default_factory=list)
    recovery_qid: str | None = None
    carried_variants: list[dict[str, Any]] = field(default_factory=list)
    #: Backend control notices the Regenerate's turns streamed as assistant
    #: text (the Claude adapter's "Compacting..."). A row made of nothing but
    #: these is not a reply (``is_regenerate_answer``).
    notice_chunks: list[str] = field(default_factory=list)


#: How many control-notice chunks one Regenerate remembers. A turn streams a
#: handful at most; the bound keeps a misbehaving backend from growing it.
_MAX_NOTICE_CHUNKS = 64


def owned_regenerate_target(slot: "_ChatSlot") -> RegenerateTarget | None:
    """The slot's armed target when the turn now running owns it, else ``None``."""
    target = getattr(slot, "_regenerate_target", None)
    if not isinstance(target, RegenerateTarget):
        return None
    if target.generation != getattr(slot, "_turn_generation", None):
        return None
    return target


def carry_regenerate_target(slot: "_ChatSlot", recovery_qid: str) -> None:
    """Hand the owning Regenerate's target to the recovery its turn queued.

    Called from ``_queue_recovery``. A recovery queued before the turn has any
    reply (a replay of its message, or a continuation after a tool ran) is the
    same Regenerate's next attempt, so its reply must replace the previous one.
    A turn that already has a reply settles with it as usual.
    """
    target = owned_regenerate_target(slot)
    if target is not None and recovery_qid and not target.reply_mids:
        target.recovery_qid = recovery_qid


def _queued(slot: "_ChatSlot", qid: str) -> bool:
    return any(entry.get("id") == qid for entry in getattr(slot, "_queue", ()))


def adopt_regenerate_target(
    slot: "_ChatSlot", consumed: list[dict[str, Any]], task: "asyncio.Task[Any]"
) -> None:
    """Give a waiting target to the turn the drain just started, if it is the replay.

    Called right after the drain assigned ``slot.task``, so
    ``_turn_generation`` is the new turn's. The replay must run alone: merged
    with another queued message it is not the Regenerate's own message.
    A target whose replay left the queue without running is dropped, which
    leaves the previous reply as it is.
    """
    target = getattr(slot, "_regenerate_target", None)
    if not isinstance(target, RegenerateTarget) or not target.recovery_qid:
        return
    qid = target.recovery_qid
    if len(consumed) == 1 and consumed[0].get("id") == qid:
        target.generation = slot._turn_generation
        target.recovery_qid = None
        slot._pending_variants = target.carried_variants
        target.carried_variants = []
        task.add_done_callback(release_regenerate_on_done(slot, target))
    elif not _queued(slot, qid):
        slot._regenerate_target = None


def release_regenerate_on_done(slot: "_ChatSlot", target: RegenerateTarget):
    """A done-callback for the turn owning *target*: one that exited without its
    turn end settling it leaves the previous reply as it was. A target handed
    to a recovery replay (waiting, or adopted under a newer generation) is the
    replay's to settle, so it and its variants are left alone."""
    generation = target.generation

    def _release(task: "asyncio.Task[Any]") -> None:
        if target.recovery_qid or target.generation != generation:
            return
        if slot._regenerate_target is target:
            slot._regenerate_target = None
        if slot._pending_variants:
            if not task.cancelled() and task.exception() is None:
                logger.warning("Regenerate: pending variants not consumed by flush, discarding")
            slot._pending_variants = []

    return _release


def note_regenerate_reply(slot: "_ChatSlot", row: dict[str, Any]) -> None:
    """Record ``row`` as a reply of the owning Regenerate turn, if one is running.

    A row whose text is only backend control notices is not recorded: it
    answers nothing, so it must neither replace the previous reply nor stop a
    queued continuation from taking the target over.
    """
    target = owned_regenerate_target(slot)
    mid = row_mid(row)
    if target is None or not mid or mid in target.reply_mids:
        return
    if not is_regenerate_answer(slot, str(row.get("content") or "")):
        return
    if len(target.reply_mids) < _MAX_SLOT_MESSAGES:
        target.reply_mids.append(mid)


def note_regenerate_notice(slot: "_ChatSlot", chunk: str) -> None:
    """Record a backend control-notice chunk the owning Regenerate turn streamed."""
    target = owned_regenerate_target(slot)
    if target is None or not chunk or len(target.notice_chunks) >= _MAX_NOTICE_CHUNKS:
        return
    target.notice_chunks.append(chunk)


def is_regenerate_answer(slot: "_ChatSlot", text: str) -> bool:
    """Whether *text* answers the running Regenerate: anything left once its
    recorded control notices are removed. Outside a Regenerate, any text is."""
    target = owned_regenerate_target(slot)
    if target is None:
        return True
    answer = text
    for chunk in target.notice_chunks:
        answer = answer.replace(chunk, "", 1)
    return bool(answer.strip())


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


def regenerate_replaced_rows(slot: "_ChatSlot") -> list[dict[str, Any]]:
    """The previous-reply rows an owning Regenerate turn is replacing, or ``[]``.

    They stay in the window and on disk until the turn settles, so every
    history the turn hands the model (the cold-start replay, the
    interrupted-turn preamble) leaves them out: the question is asked again,
    and its old answer must not precede it as an answer to nothing.
    """
    target = owned_regenerate_target(slot)
    return list(target.rows) if target is not None else []


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
        if target.recovery_qid and _queued(slot, target.recovery_qid):
            # The turn queued a replay of its own message: that replay is this
            # Regenerate's second attempt and settles it. Keep the target, and
            # hold the variants on it so no other turn's flush adopts them.
            target.carried_variants = slot._pending_variants
            slot._pending_variants = []
            slot._regenerate_target = target
            return
        # Empty turn: the previous reply was never removed, so there is nothing
        # to restore. The stashed variants belong to no reply.
        target.recovery_qid = None
        slot._pending_variants = []
        return
    old = {id(r) for r in target.rows}
    live = {id(r) for r in slot.messages}
    if not old <= live:
        # Part of the previous reply was trimmed off the window's front into
        # the frozen prefix, where a rewrite cannot retire it. Keep both
        # replies rather than report a swap the transcript would not show.
        logger.warning(
            "Regenerate: kept the previous reply of %s; part of it left the window",
            slot.key,
        )
        slot._pending_variants = []
        return
    variants = reply.get("variants")
    if slot._pending_variants:
        # The segment flush leaves them stashed during a Regenerate turn, so
        # they reach the reply only in the swapped row the pinned write commits.
        variants = _adopt_pending_variants(slot, variant_from_row(reply))
    swapped = dict(reply)
    if isinstance(variants, list):
        swapped["variants"] = variants
        swapped["variant_idx"] = len(variants) - 1
    # No front trim from here until memory shows the swap: a trim would credit a
    # retired row to the frozen prefix after the write removed it from disk.
    slot._swap_in_flight = True
    try:
        await _replace_previous_reply(state, slot, target, reply, swapped, variants, old)
    finally:
        slot._swap_in_flight = False


async def _replace_previous_reply(
    state: "DashboardState",
    slot: "_ChatSlot",
    target: RegenerateTarget,
    reply: dict[str, Any],
    swapped: dict[str, Any],
    variants: Any,
    old: set[int],
) -> None:
    """The pinned replace write and, once it commits, the same swap in memory."""
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
            # Name the rows this rewrite removes, so it drops only them: a row
            # another writer persisted after the snapshot (a workflow result
            # landing during this await) is kept rather than rewritten away.
            retired_rows=list(target.rows),
            # Publish the commit inside the same lock: a save that copied the
            # window before it (a tag edit, an in-flight dirty flush) re-takes
            # its copy and writes the swapped window, variants included.
            after_commit_under_lock=lambda: publish_swap(
                slot, AppliedSwap(retired=tuple(target.rows), reply=reply, swapped=swapped)
            ),
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
    # Memory now shows the swap, so a window copied from here on needs no fix.
    finish_swap(slot)
    slot._resumed_count = 0
    slot.invalidate_source_links()
    state.broadcast_ws(
        "chat_variant_switch",
        {
            "slot": slot.key,
            "index": reply.get("variant_idx", 0),
            "content": reply.get("content"),
            "retired": retired_row_identities(target.rows),
        },
    )


def retired_row_identities(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    """The ids a client needs to drop the rows a committed swap retired.

    The client's refresh re-seats decided rows (tool permissions, elicitations)
    from its own copies, so a decided card of the replaced turn would come back
    beside the new reply unless the client drops it first. ``mids`` names every
    retired row that has one; ``approval_ids`` names the decided rows, which the
    client keys by approval id.
    """
    mids: list[str] = []
    approval_ids: list[str] = []
    for row in rows:
        mid = row_mid(row)
        if mid:
            mids.append(mid)
        meta = row.get("meta")
        approval_id = meta.get("approval_id") if isinstance(meta, dict) else None
        if isinstance(approval_id, str) and approval_id:
            approval_ids.append(approval_id)
    return {"mids": mids, "approval_ids": approval_ids}
