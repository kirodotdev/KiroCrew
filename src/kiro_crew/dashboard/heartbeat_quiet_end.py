"""A hidden crewmate turn shows the user its final reply, not its narration.

A heartbeat wake (:mod:`kiro_crew.dashboard.goal_reminder`), the other goal
check-ins and the welcome are hidden gateway turns
(``mate_welcome.dispatch_hidden_turn``): turns no person typed. The runner
already drops text that comes AFTER a ``nothing_to_do``, but a model that
writes a sentence and THEN calls a tool has had the sentence cut into an
assistant row at the tool call, so a quiet wake still showed as a message, and
a wake that did reply also showed the model's own narration ("Let me record
that.") above the reply.

Only for a hidden turn (``_run_chat(_hidden_turn=True)``, set by
``dispatch_hidden_turn`` and nothing else), the runner cuts each pre-tool text
segment with ``quiet_persist`` (no ``chat_message`` frame, no roster row, no
card event) and keeps the row here (:class:`HeldRows`, on the turn as
``_held_rows``). At turn completion :func:`settle_held_rows` settles them once:

* the quiet end applied (and no person steered into the turn): each held row's
  text moves into the applied ``nothing_to_do`` tool row's ``meta.output`` and
  ``meta.folded_reply``, so it stays inspectable in the folded steps, and the
  row becomes the say-nothing U+200B row (``is_speech_row`` False,
  ``isHiddenInvisibleAssistantRow`` True) -- the same not-speech shape a quiet
  patrol already ends on, kept in place so the transcript's order and disk
  counters are untouched;
* the turn ended with a final reply (text after its last tool call) and no
  person steered in: each held row folds the same way onto the tool row that
  followed it, and the final reply is shown as usual;
* otherwise (no final reply, a person steered in, a held row with no tool row
  after it, or text longer than one tool row keeps, :data:`MAX_FOLDED_CHARS`):
  each held row gets the deliveries ``slot.append`` withheld, in order, so text
  that was the turn's only words is still said and no character is dropped.

A turn that raises or is cancelled before it settles delivers its held rows the
same way from ``_run_chat``'s ``finally`` (:func:`release_held_rows`).

Every other turn type never constructs a :class:`HeldRows` and is unchanged.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: The say-nothing reply body (see ``system_notices.is_speech_row``).
SAY_NOTHING = "\u200b"

#: Where the folded text is kept on the quiet-end tool row.
FOLDED_REPLY_KEY = "folded_reply"

#: How much folded text one tool row keeps.
MAX_FOLDED_CHARS = 16_000

#: Meta that described the withdrawn text and would mislead on the U+200B row.
_TEXT_META_KEYS = ("blocked_links", "redactions", "variants", "variant_idx")


class HeldRows:
    """The pre-tool assistant rows a hidden turn cut without delivering."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def hold(self, row: dict[str, Any] | None) -> None:
        if isinstance(row, dict) and row.get("role") == "assistant":
            self.rows.append(row)

    def settle(
        self,
        state: Any,
        slot: Any,
        *,
        quiet_end_applied: bool,
        quiet_end_tcids: set[str],
        final_reply: bool = False,
    ) -> None:
        """Fold the held rows into the steps, or deliver them; once per turn.

        A quiet end takes them all onto its ``nothing_to_do`` row. A turn that
        ends with a *final_reply* (text after its last tool call, no person
        steering in) folds each one onto the tool row that followed it, so the
        reply is what the user reads and the narration stays in the steps.
        """
        rows, self.rows = self.rows, []
        if not rows:
            return
        # A row outside the window (trimmed) cannot be rewritten in place, so a
        # turn holding one delivers rather than folds.
        live = {id(m) for m in getattr(slot, "messages", []) or []}
        foldable = all(id(r) in live for r in rows)
        if quiet_end_applied and foldable:
            target = _quiet_end_row(slot, quiet_end_tcids)
            if target is not None and _fits(rows):
                _fold(state, slot, rows, target)
                return
        if final_reply and foldable and _fold_into_steps(state, slot, rows):
            return
        for row in rows:
            _deliver(slot, row)


def settle_held_rows(turn: Any, *, final_reply: bool) -> None:
    """Settle *turn*'s held rows at its end; a no-op on a turn that holds none.

    A person's steer into the turn turns both folds off, since text after it
    may be the answer to that person. *final_reply* is whether the turn has
    text after its last tool call.
    """
    held = turn._held_rows
    if held is None:
        return
    steered = bool(turn._person_steered_turn)
    held.settle(
        turn.state,
        turn.slot,
        quiet_end_applied=bool(turn._terminal_directive_applied) and not steered,
        quiet_end_tcids=turn._terminal_directive_tcids,
        final_reply=final_reply and not steered,
    )


def release_held_rows(turn: Any) -> None:
    """Deliver whatever *turn* still holds, unfolded; run on every turn exit.

    :func:`settle_held_rows` runs where a turn completes. A turn that raises or is
    cancelled after a pre-tool cut leaves through ``_run_chat``'s ``finally``
    instead, and its held rows would stay saved but never reach the roster, the
    card events or the reader. Here they get the deliveries ``slot.append``
    withheld. A no-op once settled, and on a turn that never built its holder.
    """
    held = vars(turn).get("_held_rows")
    if held is None or not held.rows:
        return
    rows, held.rows = held.rows, []
    for row in rows:
        _deliver(turn.slot, row)


def _fits(rows: list[dict[str, Any]]) -> bool:
    """Whether *rows*' joined text can move onto one tool row with nothing lost.

    A fold blanks every row it takes, so it may only take text it keeps in full;
    a longer join is delivered as its own rows instead. Nor may it take a row that
    names an image (``![...](...)``): the history save preserves a local image
    only from a row's own content (``chat_attachments.persist_inline_images``),
    never from tool metadata, so a folded reference to a scratch file would lose
    its picture when the scratch is swept.
    """
    if any("![" in str(r.get("content") or "") for r in rows):
        return False
    return len(_joined(rows)) <= MAX_FOLDED_CHARS


def _joined(rows: list[dict[str, Any]]) -> str:
    texts = [str(r.get("content") or "").strip() for r in rows]
    return "\n\n".join(t for t in texts if t and t != SAY_NOTHING)


def _quiet_end_row(slot: Any, tcids: set[str]) -> dict[str, Any] | None:
    """The newest applied ``nothing_to_do`` tool row this turn wrote."""
    from kiro_crew.dashboard.state import is_quiet_end_row

    for m in reversed(getattr(slot, "messages", []) or []):
        if m.get("role") != "tool":
            continue
        meta = m.get("meta") or {}
        if meta.get("tool_call_id") in tcids and is_quiet_end_row(m):
            return m
    return None


def _fold_into_steps(state: Any, slot: Any, rows: list[dict[str, Any]]) -> bool:
    """Fold each held row onto the tool row right after it; False when one has none.

    Every target is found before anything is rewritten, so a row with no tool
    row after it (nothing to keep its text on) leaves every row to be delivered.
    """
    messages = list(getattr(slot, "messages", []) or [])
    index = {id(m): i for i, m in enumerate(messages)}
    groups: dict[int, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
    for row in rows:
        target = next(
            (m for m in messages[index[id(row)] + 1 :] if m.get("role") == "tool"),
            None,
        )
        if target is None:
            return False
        groups.setdefault(id(target), (target, []))[1].append(row)
    if not all(_fits(held) for _target, held in groups.values()):
        return False
    for target, held in groups.values():
        _fold(state, slot, held, target)
    return True


def _fold(state: Any, slot: Any, rows: list[dict[str, Any]], target: dict[str, Any]) -> None:
    # Callers check _fits first: the text is kept whole, never cut.
    folded = _joined(rows)
    # The target gets its copy BEFORE any source row is blanked: a history flush
    # serializes these same dicts from an executor thread, so at every point between
    # the two steps at least one copy of the text is in the transcript.
    if folded:
        meta = target.setdefault("meta", {})
        output = str(meta.get("output") or "").rstrip()
        meta["output"] = f"{output}\n\n{folded}" if output else folded
        meta[FOLDED_REPLY_KEY] = folded
    for row in rows:
        row["content"] = SAY_NOTHING
        meta = row.get("meta")
        if isinstance(meta, dict):
            for key in _TEXT_META_KEYS:
                meta.pop(key, None)
        for key in _TEXT_META_KEYS:
            row.pop(key, None)
    if folded:
        meta = target["meta"]
        ts = str(target.get("ts") or "")
        if ts:
            try:
                state.broadcast_ws(
                    "chat_message_update",
                    {
                        "slot": slot.key,
                        "ts": ts,
                        "meta": {FOLDED_REPLY_KEY: folded, "output": meta["output"]},
                    },
                )
            except Exception:
                logger.debug("hidden turn fold broadcast failed", exc_info=True)
    slot._dirty = True


def _deliver(slot: Any, row: dict[str, Any]) -> None:
    """The deliveries ``slot.append`` runs for a live assistant row, run now."""
    key = getattr(slot, "key", "")
    hooks = (
        (getattr(slot, "_on_card_event", None), (slot, "assistant")),
        (
            None if getattr(slot, "_has_reader", False) else getattr(slot, "_on_message", None),
            (key, row),
        ),
        (getattr(slot, "_on_row", None), (key, row)),
    )
    for hook, args in hooks:
        if hook is None:
            continue
        try:
            hook(*args)
        except Exception:
            logger.debug("held hidden-turn row delivery failed", exc_info=True)
