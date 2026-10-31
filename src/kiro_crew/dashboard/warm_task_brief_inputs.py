"""What a warm task brief reads besides what waits on the owner (that list is
``member_waiting.waiting_items``, the "Waiting on you" list): what changed while
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
from datetime import datetime, timezone
from typing import Any

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


def waiting_entries(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The "Waiting on you" items for the prompt, with ``asked_at`` as HH:MM."""
    return [
        {
            "kind": item.get("kind"),
            "text": item.get("text"),
            "options": list(item.get("options") or []),
            "asked_at": clock(row_epoch(item.get("asked_at"))),
        }
        for item in items
    ]


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


# Who wrote each work-item stamp, for "Since you left" lines.
_CHANGE_SOURCES = {
    "last_report_at": "worker report",
    "decided_at": "conductor decision",
    "closed_at": "work ledger",
}


# Fields a ledger READ derives from the clock or from other fields. They are
# never part of a stored record, but a record passed through a read could
# carry them, and idle time alone must not count as a change.
_DIGEST_EXCLUDED = frozenset(
    {
        "stale",
        "orphaned",
        "unpatrolled",
        "pending_overdue",
        "report_undelivered",
        "acceptance_concrete",
        "read_at",
    }
)


def _stored(record: object) -> Any:
    """*record* as stored: a ledger record's own ``to_dict``, a mapping as is,
    with the derived fields dropped."""
    to_dict = getattr(record, "to_dict", None)
    raw = to_dict() if callable(to_dict) else record
    if isinstance(raw, Mapping):
        return {k: v for k, v in raw.items() if k not in _DIGEST_EXCLUDED}
    return raw


def ledger_digest(
    conductor: object,
    items: Sequence[object],
    session_state: Mapping[str, Any],
) -> str:
    """A digest of everything the work ledger and session ledger have stored.

    The whole conductor record, every item record and the session ledger are
    hashed as stored, minus only ``_DIGEST_EXCLUDED``, so any write changes it
    (a new stored field counts by default) and time passing alone does not.
    """
    import hashlib

    stored_items = sorted(
        (_stored(item) for item in items),
        key=lambda row: str(row.get("item_id") if isinstance(row, Mapping) else row),
    )
    source = {
        "conductor": _stored(conductor) if conductor is not None else None,
        "items": stored_items,
        "session": _stored(session_state),
    }
    blob = json.dumps(
        source, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


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
