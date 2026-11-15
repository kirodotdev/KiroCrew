"""The people who have written in a session, for its session row and Bot row.

A slot keeps a short list on :data:`SLOT_PARTICIPANTS_ATTR`, one entry per person,
noted when a live person-authored user row is appended
(``state_parts.slot_transcript.append``), plus the people the cap dropped on
:data:`SLOT_DROPPED_ATTR` (by id, so a person who writes again is not counted
twice), so a row's "+N" still counts everyone past the cap. The list is what a
session list draws initials from, so no list ever reads a transcript to draw
them. Persisted by the metadata codec's ``participants`` row and read back on
every restore.

Only a ``HUMAN`` author counts: an Unknown author (a channel sender, a row
written before authors) and Crew automation (a system principal) are not
participants. The stored shape is the public principal (id and label, no issuer
or subject), the same shape every message row already carries.

The metadata line is agent-editable, so a read-back entry is a display claim,
exactly like a message row's ``author``: nothing authorizes on it, nothing but
the row's display reads it, and its labels are redacted on read-back.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from typing import Any

from kiro_crew.shared_crew.attribution import is_person, message_author, slot_attribution
from kiro_crew.shared_crew.contracts import _PRINCIPAL_ID_RE, Principal, principal_or_unknown

SLOT_PARTICIPANTS_ATTR = "_participants"
SLOT_DROPPED_ATTR = "_participants_dropped"

#: Entries kept per session. The row draws a few initials and a "+N" for the
#: rest, so the cap bounds the line and the payload; the people it drops are
#: kept as ids (:data:`MAX_DROPPED_IDS`) and counted into "+N" (:func:`dropped`).
MAX_PARTICIPANTS = 32

#: Dropped people remembered by id, so one who writes again leaves the dropped
#: set instead of being counted twice. Past this many, a drop only adds to a
#: plain count.
#: shortcut: a person counted only in that plain count who writes again is
#: counted twice; raise the cap if sessions ever have hundreds of writers.
MAX_DROPPED_IDS = 256


def _entries(slot: Any) -> list[dict[str, Any]]:
    value = getattr(slot, SLOT_PARTICIPANTS_ATTR, None)
    return value if isinstance(value, list) else []


def _count(raw: object) -> int:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else 0


def _is_id(raw: object) -> bool:
    """Whether *raw* is a principal id: the contract's bounded grammar."""
    return isinstance(raw, str) and bool(_PRINCIPAL_ID_RE.match(raw))


def _dropped(slot: Any) -> tuple[list[str], int]:
    value = getattr(slot, SLOT_DROPPED_ATTR, None)
    if not isinstance(value, Mapping):
        return [], 0
    ids = value.get("ids")
    kept = [i for i in ids if _is_id(i)] if isinstance(ids, list) else []
    return kept[:MAX_DROPPED_IDS], _count(value.get("more"))


def dropped(slot: Any) -> int:
    """How many people the cap has dropped from the slot's list."""
    ids, more = _dropped(slot)
    return len(ids) + more


def _set(slot: Any, entries: list[dict[str, Any]], ids: list[str], more: int) -> None:
    try:
        setattr(slot, SLOT_PARTICIPANTS_ATTR, entries)
        setattr(slot, SLOT_DROPPED_ATTR, {"ids": ids, "more": more})
    except AttributeError:
        # A slot class that does not declare the attributes draws no initials.
        pass


def _creator_id(slot: Any) -> str:
    value = slot_attribution(slot)
    return value.creator.id if value is not None and is_person(value.creator) else ""


def _store(slot: Any, entries: list[dict[str, Any]], ids: list[str], more: int) -> None:
    """Cut *entries* to the cap (least recent first, never the creator) and record who went."""
    keep = _creator_id(slot)
    if len(entries) > MAX_PARTICIPANTS:
        others = sorted(
            (e for e in entries if e["principal"]["id"] != keep), key=lambda e: e["last_at"]
        )
        gone = others[: len(entries) - MAX_PARTICIPANTS]
        drop = {id(e) for e in gone}
        entries = [e for e in entries if id(e) not in drop]
        for e in gone:
            pid = e["principal"]["id"]
            if pid in ids:
                continue
            if len(ids) < MAX_DROPPED_IDS:
                ids = [*ids, pid]
            else:
                more += 1
    _set(slot, entries, ids, more)


def note_author(slot: Any, meta: Mapping[str, Any] | None, *, now: float | None = None) -> None:
    """Record the person who wrote a user row with *meta* as a participant."""
    author = message_author(meta)
    if not is_person(author):
        return
    at = time.time() if now is None else now
    entries = [e for e in _entries(slot) if e["principal"]["id"] != author.id]
    entries.append({"principal": author.to_public_dict(), "last_at": at})
    ids, more = _dropped(slot)
    # A dropped person who writes again is back in the list, not past the cap.
    _store(slot, entries, [i for i in ids if i != author.id], more)


def ordered(slot: Any) -> list[dict[str, Any]]:
    """The public principals, the session's creator first, then most recent first."""
    creator = _creator_id(slot)
    entries = sorted(_entries(slot), key=lambda e: e["last_at"], reverse=True)
    first = [e for e in entries if e["principal"]["id"] == creator]
    rest = [e for e in entries if e["principal"]["id"] != creator]
    return [dict(e["principal"]) for e in first + rest]


def metadata_value(slot: Any) -> dict[str, Any] | None:
    """The ``participants`` value a save writes, or ``None`` to write nothing."""
    entries = _entries(slot)
    if not entries:
        return None
    ids, more = _dropped(slot)
    return {"entries": [dict(e) for e in entries], "dropped_ids": list(ids), "dropped_more": more}


def _redacted(public: dict[str, Any]) -> dict[str, Any]:
    """*public* with its labels through the dashboard's credential redaction."""
    # Function-local: keeps this module's import closure within shared_crew.
    from kiro_crew.external_text import redact_external_text

    display = dict(public["display"])
    display["name"] = redact_external_text(display.get("name") or "")
    display["login"] = redact_external_text(display.get("login") or "")
    return {**public, "display": display}


def _read_entry(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    principal: Principal = principal_or_unknown(raw.get("principal"))
    if not is_person(principal):
        return None
    return {
        "principal": _redacted(principal.to_public_dict()),
        "last_at": _finite(raw.get("last_at")),
    }


def _finite(raw: object) -> float:
    """*raw* as a finite float, else ``0.0`` (the line is agent-editable)."""
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        return 0.0
    try:
        value = float(raw)
    except OverflowError:
        return 0.0
    return value if math.isfinite(value) else 0.0


def restore_from_metadata(slot: Any, meta: Mapping[str, Any]) -> None:
    """Read the stored participants onto a slot hydrated from *meta*.

    An absent or unreadable value reads as nobody: a session written before this
    field draws no initials until someone writes in it again.
    """
    raw = meta.get("participants")
    stored = raw if isinstance(raw, Mapping) else {}
    items = stored.get("entries")
    entries: dict[str, dict[str, Any]] = {}
    for item in items if isinstance(items, list) else ():
        entry = _read_entry(item)
        if entry is not None:
            entries[entry["principal"]["id"]] = entry
    raw_ids = stored.get("dropped_ids")
    candidates = raw_ids if isinstance(raw_ids, list) else []
    ids = list(dict.fromkeys(i for i in candidates if _is_id(i) and i not in entries))[
        :MAX_DROPPED_IDS
    ]
    _store(slot, list(entries.values()), ids, _count(stored.get("dropped_more")))
