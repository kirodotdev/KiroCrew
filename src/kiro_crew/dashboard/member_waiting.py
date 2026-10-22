"""What a crewmate is waiting on the user for: its open questions, newest first.

The gateway records this list into the crewmate's crew log whenever it changes
(``dashboard.crewmate_feed``), as the ``waiting`` fold its own Dashboard page binds
for "Waiting on you". A crewmate that parks a
decision ends its message with an ``[OPTIONS: ...]`` line, or raises an
``ask_question`` card; until the person answers, the decision sits in the chat,
and a crewmate whose later wakes end quietly leaves it scrolled out of view.

Nothing is stored. The items are read from the thread itself, the same rows and
the same parser the sidebar uses for its own "asks a question" badge
(``slot.sidebar_messages``, the bounded tail a restored thread keeps before its
window loads, and ``slot_transcript._parse_options``):

* the crewmate's newest speech row, when it ends in an ``[OPTIONS: ...]`` line
  and the person has not answered since, is one item (an older ask is never
  brought back: the newest reply alone carries it);
* every ``ask_question`` card still waiting in that thread is one item;
* every change card (milestones, tools, check-ins...) still pending in that
  thread is one item.

The person answering clears them by construction. A row a human send path
marked (``history.HUMAN_TURN_META_KEY`` -- a typed message, an ``[OPTIONS]``
chip pressed in the chat, a steer) ends the walk, and so does a change card the
person pressed in that thread (applied, partly applied, failed on apply, or
declined): a crewmate question asked before that press is answered by it, the
way a typed reply answers every question above it. An answered card leaves the
pending set. A wake the gateway starts writes no row and presses no card, so a
heartbeat in between keeps the question listed; a card that only expired is no
answer either. Every string is redacted like the sidebar's preview and bounded.

Blocking: reads in-process slot state and the change-card store, which is read
from disk once per gateway and in memory after that. A caller on the serving loop
warms the store first (``CardStore.warm``, off the loop) or passes ``cards=False``.
"""

from __future__ import annotations

import logging
from typing import Any

from kiro_crew.crew_log.entry_types import (
    CREWMATE_OPTION_LIMIT,
    CREWMATE_TEXT_LIMIT,
    CREWMATE_WAITING_LIMIT,
)
from kiro_crew.history import HUMAN_TURN_META_KEY

logger = logging.getLogger(__name__)

#: How many items the list keeps: the ``waiting`` fold's own row bound, so the
#: producer and the fold cut at one place and ``total`` counts the rest.
MAX_ITEMS = CREWMATE_WAITING_LIMIT
#: How much of a question's text one item keeps, and how many choices: the fold's
#: own bounds, so the producer and the fold cut at one place.
TEXT_MAX_CHARS = CREWMATE_TEXT_LIMIT
MAX_OPTIONS = CREWMATE_OPTION_LIMIT
#: How long each choice may be.
OPTION_MAX_CHARS = 80


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _option_items(rows: list[dict[str, Any]], answered_at: float = 0.0) -> list[dict[str, Any]]:
    from kiro_crew.dashboard.state import (
        _OPTIONS_RE,
        _parse_options,
        _redact,
    )
    from kiro_crew.dashboard.system_notices import is_system_notice
    from kiro_crew.preview_text import strip_markdown_preview

    for row in reversed(rows):
        if not isinstance(row, dict):
            continue
        meta = row.get("meta")
        if isinstance(meta, dict) and meta.get(HUMAN_TURN_META_KEY) is True:
            return []
        if answered_at and _seconds(row.get("ts")) <= answered_at:
            return []
        role = row.get("role")
        content = row.get("content")
        # The newest conversation row decides, picked as the sidebar's own
        # ``has_options`` picks it (``slot_projection``): a user or assistant row
        # with text that is not a system notice. The newest reply alone carries the
        # ask (the "options are read from the newest reply only" entry in
        # docs/decisions), so any later reply, a quiet one too, retires it.
        if role not in ("user", "assistant") or is_system_notice(role, meta or {}):
            continue
        if not content:
            continue
        if role != "assistant":
            return []
        text = str(content)
        options = _parse_options(text)
        if not options:
            return []
        question = _redact(strip_markdown_preview(_OPTIONS_RE.sub("", text).strip()))
        return [
            {
                "kind": "options",
                "text": _clip(question, TEXT_MAX_CHARS),
                "options": [_clip(_redact(o), OPTION_MAX_CHARS) for o in options][:MAX_OPTIONS],
                "options_total": len(options),
                "asked_at": str(row.get("ts") or ""),
            }
        ]
    return []


def _card_items(state: Any, slot: Any) -> list[dict[str, Any]]:
    from kiro_crew.dashboard.state import _redact

    records: list[tuple[Any, list[Any]]] = []
    for rec in (getattr(slot, "_question_pending", None) or {}).values():
        if isinstance(rec, dict) and not rec.get("blocking") and rec.get("questions"):
            records.append((rec.get("ts", 0), rec["questions"]))
    for rec in (getattr(state, "_pending_questions", None) or {}).values():
        if isinstance(rec, dict) and rec.get("slot") == slot.key and rec.get("questions"):
            records.append((rec.get("ts", 0), rec["questions"]))
    out: list[dict[str, Any]] = []
    for ts, questions in records:
        first = questions[0] if isinstance(questions, list) and questions else {}
        if not isinstance(first, dict):
            continue
        text = str(first.get("question") or first.get("header") or "")
        labels = [
            str(o.get("label") if isinstance(o, dict) else o)
            for o in (first.get("options") or [])
            if (o.get("label") if isinstance(o, dict) else o)
        ]
        if not text:
            continue
        out.append(
            {
                "kind": "question",
                "text": _clip(_redact(text), TEXT_MAX_CHARS),
                "options": [_clip(_redact(lb), OPTION_MAX_CHARS) for lb in labels][:MAX_OPTIONS],
                "options_total": len(labels),
                "asked_at": _stamp(ts),
            }
        )
    return out


#: Change-card outcomes that only a person's press produces.
_PRESSED_STATUSES = frozenset({"applied", "partial", "failed", "cancelled"})


def _seconds(stamp: Any) -> float:
    """A row's ISO stamp as epoch seconds; 0 for one that does not parse."""
    from kiro_crew.history import transcript_sort_key

    try:
        bucket, seconds = transcript_sort_key(str(stamp or ""))
    except (ValueError, OverflowError, OSError, TypeError):
        return 0.0
    return float(seconds) if stamp and bucket == 0 else 0.0


def _change_cards(state: Any, slot_key: str) -> tuple[list[dict[str, Any]], float]:
    """The thread's change cards still waiting on the person, and when they last pressed one.

    ``(items, answered_at)``: one item per pending card, and the epoch second of
    the newest card the person pressed in this thread (0 when none). A store
    that cannot be read counts as no card.
    """
    from kiro_crew.dashboard import change_cards
    from kiro_crew.dashboard.state import _redact

    try:
        store = change_cards.card_store_for(state)
        cards = store.pending(slot_key)
        # A press answers the questions before it even once its card is dismissed or
        # past the display TTL, which ``pending`` hides: read it from every card.
        answered_at = store.last_finished_at(slot_key, frozenset(_PRESSED_STATUSES))
    except Exception:
        logger.debug("waiting items: card store unreadable for %s", slot_key, exc_info=True)
        return [], 0.0
    items: list[dict[str, Any]] = []
    for card in cards:
        status = card.get("status")
        # A failed card still waits on the person: it offers Retry and Dismiss.
        if status in (change_cards.STATUS_PENDING, change_cards.STATUS_FAILED) and card.get(
            "title"
        ):
            items.append(
                {
                    "kind": "card",
                    "text": _clip(_redact(str(card["title"])), TEXT_MAX_CHARS),
                    "options": [],
                    "options_total": 0,
                    "asked_at": _stamp(card.get("created_at")),
                }
            )
    return items, answered_at


def _stamp(ts: Any) -> str:
    from datetime import datetime, timezone

    if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0:
        try:
            return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return ""
    return ts if isinstance(ts, str) else ""


def waiting_items(state: Any, slot_key: str, *, cards: bool = True) -> list[dict[str, Any]]:
    """:func:`waiting_list`'s items alone."""
    return waiting_list(state, slot_key, cards=cards)[0]


def waiting_list(
    state: Any, slot_key: str, *, cards: bool = True
) -> tuple[list[dict[str, Any]], int]:
    """The thread *slot_key*'s open questions to the user, newest first, at most
    :data:`MAX_ITEMS`, and how many there are in all.

    Each ``{kind, text, options, asked_at}``: ``kind`` is ``options`` (a message
    ending in an ``[OPTIONS:]`` line), ``question`` (an ``ask_question`` card) or
    ``card`` (a change card waiting on its buttons). Empty when the thread is not
    open in this gateway or cannot be read. *cards* False skips the change-card
    store (the caller could not warm it off the loop), so no ``card`` item is listed
    and no card press ends the walk.
    """
    slot = state.get_slot(slot_key) if state is not None and slot_key else None
    if slot is None:
        return [], 0
    try:
        rows = list(slot.sidebar_messages())
        pending, answered_at = _change_cards(state, slot_key) if cards else ([], 0.0)
        items = _card_items(state, slot) + pending + _option_items(rows, answered_at)
    except Exception:
        logger.debug("waiting items unreadable for %s", slot_key, exc_info=True)
        return [], 0
    from kiro_crew.history import transcript_sort_key

    def order(item: dict[str, Any]) -> tuple[int, float]:
        # A stamp that does not parse sorts last, after every dated item.
        try:
            bucket, seconds = transcript_sort_key(item["asked_at"])
        except (ValueError, OverflowError, OSError, TypeError):
            return (-1, 0.0)
        return (0, seconds) if item["asked_at"] and bucket == 0 else (-1, 0.0)

    items.sort(key=order, reverse=True)
    return items[:MAX_ITEMS], len(items)
