"""What a warm task brief reads: the owner's pending ask, what changed while
they were away, the turns since their last message, and the open work items.

Every section has its own budget and drops whole entries, so the model always
sees the ask and the changes first and never a cut JSON document (the old
40,000-character slice of the whole payload cut the transcript off). Each fact
carries its source and time, so the brief can say "not checked since HH:MM"
instead of repeating a stale claim as current.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from kiro_crew.history import HUMAN_TURN_META_KEY
from kiro_crew.history_schema import transcript_sort_key
from kiro_crew.session_summary import TranscriptTurn

# Per-section budgets, in characters of each entry's compact JSON. A section
# keeps whole entries, most important first, until the next would not fit.
CHANGES_CHARS = 5_000
TRANSCRIPT_CHARS = 16_000
OPEN_ITEMS_CHARS = 9_000
# One entry larger than its whole section keeps both ends of its text with a
# visible omission marker: the text field is cut, never the JSON around it.
_ENTRY_TEXT_CHARS = 4_000
_ITEM_TEXT_CHARS = 400
_RECENT_OWNER_TURNS = 3
_OWNER_SAMPLE_CHARS = 300


def parse_epoch(raw: object) -> float | None:
    """Epoch of an ISO-8601 stamp; a naive one reads as UTC."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        value = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def clock(epoch: float | None) -> str:
    """``HH:MM`` in the owner's configured time zone (with the date when it is
    not today), or "" for no time."""
    if epoch is None:
        return ""
    try:
        # Deferred: the cron package imports the gateway's config stack, which
        # this module (loaded with the dashboard) must not pull in at import.
        from kiro_crew.cron_service.schedule import get_local_tz

        zone = get_local_tz()[1]
    except Exception:
        zone = timezone.utc
    when = datetime.fromtimestamp(epoch, zone)
    # Another day gets its date, so "since 14:05" never reads as today.
    same_day = when.date() == datetime.now(zone).date()
    return when.strftime("%H:%M" if same_day else "%Y-%m-%d %H:%M")


def _ends(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    half = max(1, (cap - 40) // 2)
    return f"{text[:half]}\n[... {len(text) - 2 * half} characters omitted ...]\n{text[-half:]}"


def _size(entry: object) -> int:
    return len(json.dumps(entry, ensure_ascii=False, separators=(",", ":")))


def fit(entries: Iterable[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
    """The leading *entries* whose compact JSON fits *budget*, whole."""
    kept: list[dict[str, Any]] = []
    used = 0
    for entry in entries:
        size = _size(entry)
        if used + size > budget:
            break
        kept.append(entry)
        used += size
    return kept


# ---------------------------------------------------------------- pending ask


@dataclass(frozen=True)
class PendingAsk:
    """The thread's newest reply, when it offers choices the owner has not answered."""

    text: str
    options: tuple[str, ...]
    epoch: float | None


def pending_ask(rows: Sequence[Mapping[str, Any]], answered_at: float = 0.0) -> PendingAsk | None:
    """The newest assistant reply ending in ``[OPTIONS: ...]``, unless answered.

    The same walk the "Waiting on you" list makes (``member_waiting``): the
    newest conversation row decides, and any later user or assistant row,
    a brief or a wake included, retires the ask. An older ask is never brought
    back: the newest reply alone carries it (see docs/decisions, the
    options-are-read-from-the-newest-reply-only entry). *answered_at* is when
    the person last pressed a change card in the thread; a row at or before it
    is answered.
    """
    # circular import: dashboard.state imports the warm brief through its
    # handlers, as member_waiting's same walk notes.
    from kiro_crew.dashboard.state import _OPTIONS_RE, _parse_options
    from kiro_crew.dashboard.system_notices import is_system_notice

    for row in reversed(rows):
        if not isinstance(row, Mapping):
            continue
        meta = row.get("meta")
        if isinstance(meta, Mapping) and meta.get(HUMAN_TURN_META_KEY) is True:
            return None
        # A change card the person pressed after this row answers it too.
        stamp = row_epoch(row.get("ts"))
        if answered_at and (stamp is None or stamp <= answered_at):
            return None
        role = row.get("role")
        content = row.get("content")
        if role not in ("user", "assistant") or is_system_notice(role, meta or {}):
            continue
        if not isinstance(content, str) or not content:
            continue
        if role == "user":
            return None
        options = _parse_options(content)
        if not options:
            return None
        text = _OPTIONS_RE.sub("", content).strip()
        return PendingAsk(
            text=_ends(text, _ENTRY_TEXT_CHARS),
            options=tuple(options),
            epoch=row_epoch(row.get("ts")),
        )
    return None


def row_epoch(raw: object) -> float | None:
    """Epoch of a transcript row's ``ts``; older builds wrote naive local time."""
    if not isinstance(raw, str) or not raw:
        return None
    bucket, epoch = transcript_sort_key(raw)
    return epoch if bucket == 0 else None


# ---------------------------------------------------------------- items


def _item_epochs(item: Mapping[str, Any]) -> list[float]:
    return [
        epoch
        for key in ("last_report_at", "decided_at", "closed_at")
        if (epoch := parse_epoch(item.get(key))) is not None
    ]


def changed_since(item: Mapping[str, Any], anchor: float | None) -> bool:
    """Whether a worker report, a decision or a close landed after *anchor*."""
    return anchor is not None and any(epoch > anchor for epoch in _item_epochs(item))


def is_open_ask(item: Mapping[str, Any], terminal: Callable[[object], bool]) -> bool:
    return not terminal(item.get("state")) and item.get("status") in ("question", "blocked")


def _item_entry(item: Mapping[str, Any], anchor: float | None) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "item_id": item.get("item_id"),
        "title": item.get("title"),
        "state": item.get("state"),
        "status": item.get("status"),
    }
    if item.get("reason"):
        entry["reason"] = item["reason"]
    # Spelled "#N", the form the brief writes and the PR-number post-check reads.
    if isinstance(item.get("pr"), int) and item["pr"] > 0:
        entry["pr"] = f"#{item['pr']}"
    for key in ("summary", "decision"):
        value = item.get(key)
        if isinstance(value, str) and value:
            entry[key] = _ends(value, _ITEM_TEXT_CHARS)
    reported = parse_epoch(item.get("last_report_at"))
    if reported is not None:
        entry["worker_report_at"] = clock(reported)
    if changed_since(item, anchor):
        entry["changed_since_away"] = True
    return entry


def open_item_entries(
    items: Sequence[Mapping[str, Any]],
    anchor: float | None,
    terminal: Callable[[object], bool],
) -> list[dict[str, Any]]:
    """Open items, and terminal ones that closed while the owner was away.

    Questions and blocked items first, then the ones that changed, then the
    rest by their newest report.
    """
    chosen = [
        item for item in items if not terminal(item.get("state")) or changed_since(item, anchor)
    ]

    def rank(item: Mapping[str, Any]) -> tuple[int, int, float]:
        epochs = _item_epochs(item)
        return (
            0 if is_open_ask(item, terminal) else 1,
            0 if changed_since(item, anchor) else 1,
            -max(epochs, default=0.0),
        )

    return fit((_item_entry(item, anchor) for item in sorted(chosen, key=rank)), OPEN_ITEMS_CHARS)


def session_changed_since(session_state: Mapping[str, Any], anchor: float | None) -> bool:
    """Whether the thread's session ledger recorded progress after *anchor*."""
    stamp = parse_epoch(session_state.get("last_progress_at"))
    return anchor is not None and stamp is not None and stamp > anchor


# Who wrote each work-item stamp, for "Since you left" lines.
_CHANGE_SOURCES = {
    "last_report_at": "worker report",
    "decided_at": "conductor decision",
    "closed_at": "work ledger",
}


def change_entries(
    items: Sequence[Mapping[str, Any]],
    anchor: float | None,
    session_state: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """What changed since *anchor*: item reports, decisions and closes, and
    session-ledger events. Newest first."""
    rows: list[tuple[float, dict[str, Any]]] = []
    for item in items:
        if not changed_since(item, anchor):
            continue
        stamps = {
            key: stamp
            for key in ("last_report_at", "decided_at", "closed_at")
            if (stamp := parse_epoch(item.get(key))) is not None
        }
        newest = max(stamps, key=lambda key: stamps[key])
        epoch = stamps[newest]
        source = _CHANGE_SOURCES[newest]
        entry = {
            "item": item.get("title"),
            "state": item.get("state"),
            "status": item.get("status"),
            "source": source,
            "at": clock(epoch),
        }
        rows.append((epoch, {k: v for k, v in entry.items() if v}))
    events = (session_state or {}).get("events")
    for event in events if isinstance(events, list) else []:
        stamp = parse_epoch(event.get("ts")) if isinstance(event, Mapping) else None
        if anchor is None or stamp is None or stamp <= anchor:
            continue
        rows.append(
            (
                stamp,
                {
                    "event": _ends(str(event.get("text") or ""), _ITEM_TEXT_CHARS),
                    "kind": event.get("kind"),
                    "source": "session ledger",
                    "at": clock(stamp),
                },
            )
        )
    rows.sort(key=lambda pair: -pair[0])
    return fit((entry for _, entry in rows), CHANGES_CHARS)


# ---------------------------------------------------------------- transcript


def turns_since_owner(turns: Sequence[TranscriptTurn]) -> list[dict[str, Any]]:
    """The owner's last genuine message and every turn after it, whole.

    Over budget, the oldest turns drop first; the owner's message is kept as
    long as anything is.
    """
    start = 0
    for index in range(len(turns) - 1, -1, -1):
        turn = turns[index]
        if turn.role == "user" and not turn.injected:
            start = index
            break
    entries = [
        {
            "role": "automation" if turn.injected else turn.role,
            "at": clock(row_epoch(turn.ts)),
            "text": _ends(turn.text, _ENTRY_TEXT_CHARS),
        }
        for turn in turns[start:]
        if turn.text.strip("\u200b \t\r\n")
    ]
    if not entries:
        return []
    head, rest = entries[0], entries[1:]
    budget = TRANSCRIPT_CHARS - _size(head)
    kept: list[dict[str, Any]] = []
    for entry in reversed(rest):
        size = _size(entry)
        if size > budget:
            break
        kept.append(entry)
        budget -= size
    kept.reverse()
    return ([head] if budget >= 0 else []) + kept


def assistant_spoke_since(turns: Sequence[TranscriptTurn], anchor: float | None) -> bool:
    """Whether the thread said anything visible after *anchor*."""
    if anchor is None:
        return False
    for turn in turns:
        epoch = row_epoch(turn.ts)
        if (
            turn.role == "assistant"
            and epoch is not None
            and epoch > anchor
            and turn.text.strip("\u200b \t\r\n")
        ):
            return True
    return False


def owner_samples(turns: Sequence[TranscriptTurn]) -> list[str]:
    """The owner's last three genuine messages, for the reply language."""
    genuine = [t.text for t in turns if t.role == "user" and not t.injected and t.text.strip()]
    return [text[:_OWNER_SAMPLE_CHARS] for text in genuine[-_RECENT_OWNER_TURNS:]]
