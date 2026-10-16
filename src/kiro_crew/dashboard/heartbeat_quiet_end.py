"""The quiet end of a crewmate HEARTBEAT turn leaves no chat message behind.

A heartbeat wake (:mod:`kiro_crew.dashboard.goal_reminder`) is a hidden gateway
turn that normally ends with ``nothing_to_do``. The runner already drops text
that comes AFTER that call, but a model that writes a sentence and THEN calls it
has had the sentence cut into an assistant row at the tool call, so the quiet
wake still showed as a message, badged the thread and moved the roster preview.

Only for a heartbeat turn (``_run_chat(_heartbeat_turn=True)``, set by
``mate_welcome.dispatch_hidden_turn(heartbeat=True)`` and nothing else), the
runner cuts each pre-tool text segment with ``quiet_persist`` (no
``chat_message`` frame, no roster row, no card event) and keeps the row here
(:class:`HeldRows`). At turn completion it settles them once:

* the quiet end applied (and no person steered into the turn): each held row's
  text moves into the applied ``nothing_to_do`` tool row's ``meta.output`` and
  ``meta.folded_reply``, so it stays inspectable in the folded steps, and the
  row becomes the say-nothing U+200B row (``is_speech_row`` False,
  ``isHiddenInvisibleAssistantRow`` True) -- the same not-speech shape a quiet
  patrol already ends on, kept in place so the transcript's order and disk
  counters are untouched;
* otherwise: each held row gets the deliveries ``slot.append`` withheld, in
  order, so a heartbeat that has something to say says it as before.

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
    """The pre-tool assistant rows a heartbeat turn cut without delivering."""

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
    ) -> None:
        """Fold the held rows into the quiet end, or deliver them; once per turn."""
        rows, self.rows = self.rows, []
        if not rows:
            return
        # A row outside the window (trimmed) cannot be rewritten in place, so a
        # turn holding one delivers rather than folds.
        live = {id(m) for m in getattr(slot, "messages", []) or []}
        foldable = all(id(r) in live for r in rows)
        if quiet_end_applied and foldable and _fold(state, slot, rows, quiet_end_tcids):
            return
        for row in rows:
            _deliver(slot, row)


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


def _fold(state: Any, slot: Any, rows: list[dict[str, Any]], tcids: set[str]) -> bool:
    target = _quiet_end_row(slot, tcids)
    if target is None:
        # No applied row to keep the text on: delivering beats losing it.
        return False
    texts = [str(r.get("content") or "").strip() for r in rows]
    folded = "\n\n".join(t for t in texts if t and t != SAY_NOTHING)[:MAX_FOLDED_CHARS]
    for row in rows:
        row["content"] = SAY_NOTHING
        meta = row.get("meta")
        if isinstance(meta, dict):
            for key in _TEXT_META_KEYS:
                meta.pop(key, None)
        for key in _TEXT_META_KEYS:
            row.pop(key, None)
    if folded:
        meta = target.setdefault("meta", {})
        meta[FOLDED_REPLY_KEY] = folded
        output = str(meta.get("output") or "").rstrip()
        meta["output"] = f"{output}\n\n{folded}" if output else folded
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
                logger.debug("heartbeat fold broadcast failed", exc_info=True)
    slot._dirty = True
    return True


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
            logger.debug("held heartbeat row delivery failed", exc_info=True)
