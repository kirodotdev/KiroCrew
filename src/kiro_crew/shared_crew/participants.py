"""The people who have written in a session, for its session row and Bot row.

A slot keeps a short list on :data:`SLOT_PARTICIPANTS_ATTR`, one entry per person,
noted when a live person-authored user row is appended
(``state_parts.slot_transcript.append``). The list is what a session list draws
initials from, so no list ever reads a transcript to draw them. Persisted by the
metadata codec's ``participants`` row and read back on every restore.

Only a ``HUMAN`` author counts: an Unknown author (a channel sender, a row
written before authors) and Crew automation (a system principal) are not
participants. The stored shape is the public principal (id and label, no issuer
or subject), the same shape every message row already carries.

The metadata line is agent-editable, so a read-back entry is a display claim,
exactly like a message row's ``author``: nothing authorizes on it.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from kiro_crew.shared_crew.attribution import is_person, message_author, slot_attribution
from kiro_crew.shared_crew.contracts import Principal, principal_or_unknown

SLOT_PARTICIPANTS_ATTR = "_participants"

#: Entries kept per session. The row draws a few initials and a "+N" for the
#: rest, so the cap bounds the line and the payload, not what the row says.
#: shortcut: past this many people "+N" undercounts; raise it if a Crew ever
#: has sessions with more writers than this.
MAX_PARTICIPANTS = 32


def _entries(slot: Any) -> list[dict[str, Any]]:
    value = getattr(slot, SLOT_PARTICIPANTS_ATTR, None)
    return value if isinstance(value, list) else []


def _set(slot: Any, entries: list[dict[str, Any]]) -> None:
    try:
        setattr(slot, SLOT_PARTICIPANTS_ATTR, entries)
    except AttributeError:
        # A slot class that does not declare the attribute draws no initials.
        pass


def _creator_id(slot: Any) -> str:
    value = slot_attribution(slot)
    return value.creator.id if value is not None and is_person(value.creator) else ""


def _trim(entries: list[dict[str, Any]], keep: str) -> list[dict[str, Any]]:
    """*entries* cut to the cap by dropping the least recent, never *keep*."""
    if len(entries) <= MAX_PARTICIPANTS:
        return entries
    others = sorted(
        (e for e in entries if e["principal"]["id"] != keep), key=lambda e: e["last_at"]
    )
    drop = {id(e) for e in others[: len(entries) - MAX_PARTICIPANTS]}
    return [e for e in entries if id(e) not in drop]


def note_author(slot: Any, meta: Mapping[str, Any] | None, *, now: float | None = None) -> None:
    """Record the person who wrote a user row with *meta* as a participant."""
    author = message_author(meta)
    if not is_person(author):
        return
    at = time.time() if now is None else now
    public = author.to_public_dict()
    entries = [e for e in _entries(slot) if e["principal"]["id"] != author.id]
    entries.append({"principal": public, "last_at": at})
    _set(slot, _trim(entries, _creator_id(slot)))


def ordered(slot: Any) -> list[dict[str, Any]]:
    """The public principals, the session's creator first, then most recent first."""
    creator = _creator_id(slot)
    entries = sorted(_entries(slot), key=lambda e: e["last_at"], reverse=True)
    first = [e for e in entries if e["principal"]["id"] == creator]
    rest = [e for e in entries if e["principal"]["id"] != creator]
    return [dict(e["principal"]) for e in first + rest]


def metadata_value(slot: Any) -> list[dict[str, Any]] | None:
    """The ``participants`` value a save writes, or ``None`` to write nothing."""
    entries = _entries(slot)
    return [dict(e) for e in entries] if entries else None


def _read_entry(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    principal: Principal = principal_or_unknown(raw.get("principal"))
    if not is_person(principal):
        return None
    last_at = raw.get("last_at")
    if not isinstance(last_at, (int, float)) or isinstance(last_at, bool):
        last_at = 0.0
    return {"principal": principal.to_public_dict(), "last_at": float(last_at)}


def restore_from_metadata(slot: Any, meta: Mapping[str, Any]) -> None:
    """Read the stored participants onto a slot hydrated from *meta*.

    An absent or unreadable value reads as nobody: a session written before this
    field draws no initials until someone writes in it again.
    """
    raw = meta.get("participants")
    entries: dict[str, dict[str, Any]] = {}
    for item in raw if isinstance(raw, list) else ():
        entry = _read_entry(item)
        if entry is not None:
            entries[entry["principal"]["id"]] = entry
    _set(slot, _trim(list(entries.values()), _creator_id(slot)))
