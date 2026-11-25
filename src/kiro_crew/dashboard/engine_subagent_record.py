"""The lasting record of a turn's KAS engine sub-agents, kept on the reply row it wrote.

A ``spawn_run`` child leaves rows in the transcript (its tool row and its completion
event). A KAS engine child (``dashboard.engine_subagent_cards``) leaves none: it ends
inside the turn, and its card lives only in the slot's in-memory tracker. So once the
turn ends, the reply row the turn wrote carries one more meta key,
:data:`RECORD_META_KEY`, beside ``turn_stats`` and on the same row at the same moment
(``chat_turn.turn_stats``), and a reopened chat draws it under the reply.

The record holds what the engine reported, as the card already shows it, after the
card's own redaction: the agent the dispatch named, its task, how it ended and the
titles of its tool calls. Beside those it keeps two figures Kiro Crew itself has: when
the card opened and closed on the gateway's clock (``started_at`` and ``ended_at``,
the arrival of the child's first and last frame), and the turn's credits
(``turn_credits``), the same total the reply's footer shows, and the turn's key
(``turn``, ``engine_subagent_cards.engine_turn_key``) its live cards carried. The engine reports no
credits, model or time for one child (its frames carry none), so the record holds none
of those per child. No output either. Only a card marked ``engine`` is recorded, so a
chat whose sub-agents are not the engine's own stores nothing new.
"""

from __future__ import annotations

import math
import time
from typing import Any

from kiro_crew.dashboard.engine_subagent_cards import ENGINE_TURN_FIELD, cut_unsplit

#: The reply row's meta key the record is kept under.
RECORD_META_KEY = "engine_subagents"
#: The most children one record lists; ``more`` counts the rest.
RECORD_CHILDREN_MAX = 32
#: The longest task one entry keeps.
RECORD_TASK_MAX = 600
#: The most tool titles one entry keeps, the newest; ``tool_count`` is how many the
#: engine reported.
RECORD_TOOLS_MAX = 12
#: How an entry says it ended: the engine's own terminal status, or ``not_reported``
#: when the turn ended before the engine reported one and Kiro Crew closed the card.
END_COMPLETED = "completed"
END_FAILED = "failed"
END_NOT_REPORTED = "not_reported"


def _end_of(info: dict[str, Any]) -> str:
    stage = str(info.get("engine_end") or "").lower()
    if stage == "completed":
        return END_COMPLETED
    if stage in ("failed", "error"):
        return END_FAILED
    return END_NOT_REPORTED


def _entry(info: dict[str, Any]) -> dict[str, Any]:
    end = _end_of(info)
    entry: dict[str, Any] = {
        "agent": str(info.get("agent") or ""),
        # The card's task, already as the chat shows it: cut shorter, and masked
        # where that cut would split a credential.
        "task": cut_unsplit(str(info.get("task") or ""), RECORD_TASK_MAX),
        "end": end,
    }
    reason = info.get("error")
    if end == END_FAILED and isinstance(reason, str) and reason:
        entry["reason"] = reason
    calls = info.get("tool_calls")
    titles = [str(t) for t in calls.values()] if isinstance(calls, dict) else []
    # The newest titles, as the card's own timeline keeps them: the page says how
    # many earlier calls it does not list.
    entry["tools"] = titles[-RECORD_TOOLS_MAX:]
    entry["tool_count"] = max(int(info.get("tool_count") or 0), len(titles))
    started, ended = _clock(info.get("started")), _clock(info.get("done_at"))
    if started is not None and ended is not None and ended >= started:
        entry["started_at"] = started
        entry["ended_at"] = ended
    return entry


def _clock(value: object) -> float | None:
    """A tracker time (epoch seconds), rounded to the millisecond, or ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return round(float(value), 3)


def engine_record(tracker: object, now: float | None = None) -> dict[str, Any] | None:
    """The record of *tracker*'s engine cards, or ``None`` when it holds none.

    *tracker* is the turn's native card tracker (``chat_turn.native_cards``). Every
    value is one its card already shows; this only cuts the task shorter
    (``engine_subagent_cards.cut_unsplit``).

    ``ends`` counts how every engine card ended and ``started_at`` / ``ended_at`` span
    all of them, the unlisted ones too, so the card's totals and duration are the whole
    batch's even when ``more`` children are left out of the list. A card still open
    when the record is made (a turn that died first) counts to *now*, the moment the
    record was made, so the span is not cut to the children that ended.
    """
    children: list[dict[str, Any]] = []
    more = 0
    turn = ""
    ends = {END_COMPLETED: 0, END_FAILED: 0, END_NOT_REPORTED: 0}
    first: float | None = None
    last: float | None = None
    for info in tracker.values() if isinstance(tracker, dict) else ():
        if not isinstance(info, dict) or not info.get("engine") or not info.get("id"):
            continue
        key = info.get(ENGINE_TURN_FIELD)
        if not turn and isinstance(key, str):
            turn = key
        ends[_end_of(info)] += 1
        started = _clock(info.get("started"))
        if info.get("done"):
            ended = _clock(info.get("done_at"))
        else:
            ended = _clock(time.time() if now is None else now)
        if started is not None and ended is not None and ended >= started:
            first = started if first is None else min(first, started)
            last = ended if last is None else max(last, ended)
        if len(children) >= RECORD_CHILDREN_MAX:
            more += 1
            continue
        children.append(_entry(info))
    if not children:
        return None
    record: dict[str, Any] = {"children": children, "ends": ends}
    if more:
        record["more"] = more
    if first is not None and last is not None:
        record["started_at"] = first
        record["ended_at"] = last
    if turn:
        # The key the turn's live cards carried, so the page keeps the live card and
        # this record as one element.
        record["turn"] = turn
    return record


def attach_engine_record(slot: Any, row: dict[str, Any]) -> bool:
    """Put the record of *slot*'s current turn on *row*, the reply its footer went on.

    Returns whether a record was put on. Never raises: a record is a display nicety
    and must not disturb the turn's end.

    The row's footer (``meta.turn_stats``) is written first, so its ``credits`` is the
    turn's total, which the record keeps as ``turn_credits``: the whole turn's,
    sub-agents included, because the engine reports no share of it per child.
    """
    try:
        record = engine_record(getattr(slot, "_native_subagent_tracker", None))
        if record is None:
            return False
        meta = row.setdefault("meta", {})
        stats = meta.get("turn_stats")
        credits = stats.get("credits") if isinstance(stats, dict) else None
        if isinstance(credits, (int, float)) and not isinstance(credits, bool):
            if math.isfinite(credits) and credits > 0:
                record["turn_credits"] = credits
        meta[RECORD_META_KEY] = record
        return True
    except Exception:  # noqa: BLE001 - see the docstring
        return False


__all__ = [
    "END_COMPLETED",
    "END_FAILED",
    "END_NOT_REPORTED",
    "RECORD_CHILDREN_MAX",
    "RECORD_META_KEY",
    "RECORD_TASK_MAX",
    "RECORD_TOOLS_MAX",
    "attach_engine_record",
    "engine_record",
]
