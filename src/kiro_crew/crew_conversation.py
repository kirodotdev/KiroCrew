"""Crew conversation index — the thin per-(human × member) conversation entity.

A crew member's DM thread on the Crew Members page is a *conversation* between
one human and one member. Its lifetime is longer than any single session: the
DM slot can be rebuilt, rotated, or re-bound, and a worker session the member
dispatched may hand a result back into it. The conversation therefore needs an
identity of its own — but it must NOT become a second transcript.

This module keeps that identity **thin**:

* it stores **pointers**, never bodies — an entry is either a
  ``(session_key, mid)`` reference into a session's JSONL transcript, or a
  native *escalation* record whose text still lives on the transcript row;
* the human-facing projection (what the chat view shows) is computed from the
  referenced transcripts, so a conversation can never disagree with the
  sessions it points at;
* ``needs_you`` is **derived** from the pending escalations on the index, not
  stored on the slot — the slot is a process, the conversation is the thing the
  human is in.

The key is ``dm:<slug>`` today (one human, one member). The record already
carries a ``participants`` list and a ``sessions`` list rather than a single
member/session field, so a later ``goal:<id>`` conversation (one goal, several
members plus the human) is a new key shape, not a schema migration.

Same placement discipline as the activity log (:mod:`kiro_crew.members`):
the file sits in the member's own directory, beside ``activity.jsonl``, and is
NOT the trust binding — the binding is the identity authority and stays
strict-shape; this is mutable UI state and stays out of the keystone-gated
subtree.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.members import member_dir, validate_slug

logger = logging.getLogger(__name__)

#: File name inside ``member_dir(slug)``.
CONVERSATION_FILE_NAME = "conversation.json"

SCHEMA_VERSION = 1

#: Cap on SETTLED entries per conversation — pointers are ~200 bytes, so the
#: settled history stays around 100 KiB. Eviction is oldest-first over settled
#: entries only: a pending escalation is never dropped by the cap (a badge that
#: vanished without an answer, a deadline or a default would be a lost
#: decision, not a trimmed log), so a record with more than this many OPEN
#: decisions is allowed to exceed the cap.
_MAX_ENTRIES = 500

#: Cap on option labels an escalation may offer (mirrors ``ask_question``).
MAX_ESCALATION_OPTIONS = 6

# One lock per slug around every read-modify-write. All writers live in one
# gateway process (the dashboard's own executor threads: the escalation path,
# the reply hook, a future ref writer), but they run on different threads, so
# without this two concurrent mutations would each load the file, each append
# their entry and the second `atomic_write` would silently drop the first.
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()

# In-memory pending view for the hot read path (`needs_you` runs inside the
# slot projection, ON the event loop, on every sidebar push): index file path
# -> (id, deadline) of the records stored as pending. Never read from disk on
# the read path; the writers refresh it after every write and `prime` loads it
# once per member.
_PENDING_CACHE: dict[str, list[tuple[str, str | None]]] = {}


def _cache_key(slug: str) -> str:
    """The pending view is keyed by the index FILE, not the slug: the data home
    can change under one process (tests, a profile switch), and a view that
    outlived its file would report another install's escalations."""
    return str(conversation_path(slug))


def _lock_for(slug: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(slug)
        if lock is None:
            lock = _LOCKS[slug] = threading.Lock()
        return lock


def conversation_id(slug: str) -> str:
    """The conversation key for a member's 1:1 DM with the human."""
    return f"dm:{validate_slug(slug)}"


def conversation_path(slug: str) -> Path:
    """Absolute path of one member's conversation index (not created)."""
    return member_dir(slug) / CONVERSATION_FILE_NAME


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp (``Z`` or offset) to an aware datetime."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
#: Bounds on a relative deadline: below a minute the veto window is not real;
#: above a week the escalation should have been a decision, not a window.
MIN_DEADLINE_SECS = 60
MAX_DEADLINE_SECS = 7 * 86400


def resolve_deadline(value: Any, *, now: datetime | None = None) -> str | None:
    """Normalise a caller-supplied deadline to an absolute ISO timestamp.

    Accepts an ISO-8601 timestamp, a bare number of seconds, or a duration
    such as ``30m`` / ``2h`` / ``900s`` / ``1d``. Returns ``None`` for an
    empty value. Raises :class:`ValueError` for anything unparseable or a
    window outside ``[MIN_DEADLINE_SECS, MAX_DEADLINE_SECS]`` from *now*.
    """
    if value is None:
        return None
    base = now or datetime.now(timezone.utc)
    if isinstance(value, bool):
        raise ValueError("deadline must be a timestamp or a duration")
    if isinstance(value, (int, float)):
        secs = float(value)
    else:
        text = str(value).strip()
        if not text:
            return None
        absolute = parse_ts(text)
        if absolute is not None:
            secs = (absolute - base).total_seconds()
        else:
            unit = text[-1].lower()
            number = text[:-1].strip()
            if unit in _DURATION_UNITS and number.replace(".", "", 1).isdigit():
                secs = float(number) * _DURATION_UNITS[unit]
            elif text.replace(".", "", 1).isdigit():
                secs = float(text)
            else:
                raise ValueError("deadline must be ISO-8601 or a duration like 30m / 2h / 900s")
    if secs < MIN_DEADLINE_SECS or secs > MAX_DEADLINE_SECS:
        raise ValueError(
            f"deadline must be between {MIN_DEADLINE_SECS}s and {MAX_DEADLINE_SECS // 86400}d from now"
        )
    return _now_iso(base + timedelta(seconds=secs))


#: The one spelling of an escalation id. Boundaries that accept an id from a
#: client (the chat send handler's ``meta.escalation_id``) validate against this
#: rather than trusting free text into a queue entry.
ESCALATION_ID_RE = re.compile(r"^esc-[0-9a-f]{16}$")


def new_escalation_id() -> str:
    """Mint one escalation id (``esc-<16 hex>``); random for the same reason
    ``history.mint_row_mid`` is — a counter rebased after restore can collide."""
    return f"esc-{uuid.uuid4().hex[:16]}"


def _scaffold(slug: str) -> dict[str, Any]:
    return {
        "version": SCHEMA_VERSION,
        "conversation_id": conversation_id(slug),
        "participants": [],
        "sessions": [],
        "entries": [],
    }


def _parse_file(path: Path, slug: str) -> dict[str, Any] | None:
    """Parse the index file, or ``None`` when missing/unreadable/misshapen."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    except ValueError:
        logger.warning("conversation index unreadable for %s", slug, exc_info=True)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        return None
    record = _scaffold(slug)
    # Type-check every field a writer later mutates: a hand-edited or torn file
    # with ``"participants": null`` must read as "no participants", not crash the
    # next ``record_escalation`` in ``_ensure_participants``.
    if isinstance(data.get("conversation_id"), str) and data["conversation_id"]:
        record["conversation_id"] = data["conversation_id"]
    if isinstance(data.get("version"), int):
        record["version"] = data["version"]
    record["participants"] = (
        [p for p in (data.get("participants") or []) if isinstance(p, dict)]
        if isinstance(data.get("participants"), list)
        else []
    )
    record["sessions"] = (
        [s for s in (data.get("sessions") or []) if isinstance(s, str)]
        if isinstance(data.get("sessions"), list)
        else []
    )
    record["entries"] = [e for e in data["entries"] if isinstance(e, dict)]
    return record


def read_conversation(slug: str) -> dict[str, Any]:
    """Load a member's conversation index; a missing or unreadable file reads
    as an empty scaffold (never raises — the index is derived state).

    Always parses: callers mutate what they get back, so no shared cached
    record is ever handed out. The hot path (:func:`needs_you`) has its own
    scalar cache.
    """
    return _parse_file(conversation_path(slug), slug) or _scaffold(slug)


def _settled(entry: dict[str, Any]) -> bool:
    return not (entry.get("type") == "escalation" and entry.get("state") == "pending")


def _write_conversation(slug: str, record: dict[str, Any]) -> None:
    """Persist *record*. Caller holds the slug lock."""
    path = conversation_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = record["entries"]
    overflow = len(entries) - _MAX_ENTRIES
    if overflow > 0:
        # Evict the oldest SETTLED entries only. A pending escalation is never
        # evicted: if every entry is an open decision the file simply exceeds
        # the cap, because a lost decision is worse than a large file (500
        # unanswered escalations is a member that needs stopping, not trimming).
        keep: list[dict[str, Any]] = []
        for entry in entries:
            if overflow > 0 and _settled(entry):
                overflow -= 1
                continue
            keep.append(entry)
        record["entries"] = keep
        # The sessions list is a pointer set over the entries; once an entry is
        # gone, a session nothing points at is dropped with it so the record's
        # size is bounded by the entries, not by history.
        referenced = {e.get("session_key") for e in keep} | {
            e.get("from_session") for e in keep if e.get("type") == "escalation"
        }
        record["sessions"] = [s for s in record.get("sessions", []) if s in referenced]
    atomic_write(path, json.dumps(record, ensure_ascii=False, indent=1), fsync=False)
    _PENDING_CACHE.pop(_cache_key(slug), None)
    _prime_pending_cache(slug)


def _ensure_participants(record: dict[str, Any], *, member: str, slug: str) -> None:
    parts = record.setdefault("participants", [])
    if not any(p.get("kind") == "human" for p in parts if isinstance(p, dict)):
        parts.append({"kind": "human", "id": "owner"})
    if not any(
        p.get("kind") == "member" and p.get("slug") == slug for p in parts if isinstance(p, dict)
    ):
        parts.append({"kind": "member", "slug": slug, "name": member})


def _ensure_session(record: dict[str, Any], session_key: str) -> None:
    sessions = record.setdefault("sessions", [])
    if session_key and session_key not in sessions:
        sessions.append(session_key)


def append_ref(
    slug: str,
    *,
    member: str,
    session_key: str,
    mid: str,
    role: str,
    ts: str = "",
) -> dict[str, Any]:
    """Point the conversation at one transcript row in *session_key*.

    Used when a row from a session OTHER than the DM slot belongs in the
    conversation (a worker session's final report, for instance). Rows in the
    DM slot itself need no ref — the projection reads that session whole.
    """
    entry = {
        "type": "ref",
        "session_key": session_key,
        "mid": mid,
        "role": role,
        "ts": ts or _now_iso(),
    }
    with _lock_for(slug):
        record = read_conversation(slug)
        _ensure_participants(record, member=member, slug=slug)
        _ensure_session(record, session_key)
        record["entries"].append(entry)
        _write_conversation(slug, record)
    return entry


def record_escalation(
    slug: str,
    *,
    member: str,
    session_key: str,
    mid: str,
    escalation_id: str,
    from_session: str,
    created_ts: str = "",
    deadline: str | None = None,
    default_action: str | None = None,
    goal: str | None = None,
    options: list[str] | None = None,
) -> dict[str, Any]:
    """Add one pending escalation record pointing at its transcript row."""
    entry = {
        "type": "escalation",
        "id": escalation_id,
        "session_key": session_key,
        "mid": mid,
        "from_session": from_session,
        "state": "pending",
        "created_ts": created_ts or _now_iso(),
        "deadline": deadline,
        "default_action": default_action,
        "goal": goal,
        "options": list(options or [])[:MAX_ESCALATION_OPTIONS],
        "answered_ts": None,
    }
    with _lock_for(slug):
        record = read_conversation(slug)
        _ensure_participants(record, member=member, slug=slug)
        _ensure_session(record, session_key)
        record["entries"].append(entry)
        _write_conversation(slug, record)
    return entry


def sweep_deadlines(record: dict[str, Any], *, now: datetime | None = None) -> bool:
    """Move pending records whose deadline has passed to ``defaulted`` /
    ``expired`` in place. Pure over *record*; returns whether anything moved."""
    current = now or datetime.now(timezone.utc)
    changed = False
    for entry in record.get("entries", []):
        if entry.get("type") != "escalation" or entry.get("state") != "pending":
            continue
        due = parse_ts(entry.get("deadline"))
        if due is not None and due <= current:
            entry["state"] = "defaulted" if entry.get("default_action") else "expired"
            changed = True
    return changed


def pending_escalations(record: dict[str, Any], *, now: datetime | None = None) -> list[dict]:
    """Escalations still awaiting the human, after a lazy deadline sweep."""
    sweep_deadlines(record, now=now)
    return [
        e
        for e in record.get("entries", [])
        if e.get("type") == "escalation" and e.get("state") == "pending"
    ]


def _pending_deadlines_from_disk(slug: str) -> list[tuple[str, str | None]]:
    """``(id, deadline)`` of the records stored as ``pending`` (unswept), read
    from disk. Blocking IO — callers run it off the event loop."""
    record = _parse_file(conversation_path(slug), slug)
    if record is None:
        return []
    return [
        (
            str(e.get("id") or ""),
            e.get("deadline") if isinstance(e.get("deadline"), str) else None,
        )
        for e in record["entries"]
        if e.get("type") == "escalation" and e.get("state") == "pending"
    ]


def pending_ids(slug: str, *, now: datetime | None = None) -> list[str]:
    """Ids of the escalations awaiting the human RIGHT NOW, from the in-memory
    view (no IO). The reply hook snapshots this on the event loop at the moment
    the human's row is appended, so the answer rule is evaluated against the
    pending set as it stood in transcript order — not as it stands a moment
    later on the executor, after a concurrent escalation may have landed."""
    current = now or datetime.now(timezone.utc)
    out: list[str] = []
    for eid, deadline in _PENDING_CACHE.get(_cache_key(slug), ()):
        due = parse_ts(deadline)
        if due is None or due > current:
            out.append(eid)
    return out


def _prime_pending_cache(slug: str) -> None:
    """Refresh the in-memory pending view for *slug* from disk (blocking IO)."""
    try:
        _PENDING_CACHE[_cache_key(slug)] = _pending_deadlines_from_disk(slug)
    except Exception:  # noqa: BLE001 - a cache refresh must never raise into a writer
        logger.debug("pending cache prime failed for %s", slug, exc_info=True)


def prime(slug: str) -> None:
    """Load a member's pending view into memory. Blocking IO — call it off the
    event loop (``asyncio.to_thread``) once per member at slot creation or
    restore; every later change is applied by the writer that made it."""
    _prime_pending_cache(slug)


def needs_you(slug: str, *, now: datetime | None = None) -> bool:
    """Whether the member has at least one escalation awaiting the human.

    Memory-only: this runs inside the slot projection on the event loop, on
    every sidebar push, so it must not stat or read a file. The view is kept
    current by the writers (``record_escalation`` / ``mark_answered`` /
    ``append_ref`` all refresh it after their write) and primed by
    :func:`prime` when a member slot is created or restored. A slug that was
    never primed reads as ``False`` until a writer or a prime touches it.

    A passed deadline clears it without a write (the file is updated the next
    time the record is written for another reason, or by :func:`mark_answered`).
    """
    try:
        return bool(pending_ids(slug, now=now))
    except Exception:  # noqa: BLE001 - a projection must never fail on derived state
        logger.debug("needs_you derivation failed for %s", slug, exc_info=True)
        return False


def mark_answered(
    slug: str,
    *,
    escalation_id: str | None = None,
    escalation_ids: list[str] | None = None,
    candidates: list[str] | None = None,
    answered_ts: str = "",
    now: datetime | None = None,
) -> int:
    """The human replied in the conversation. Which record that answers:

    * a reply carrying an ``escalation_id`` (an option chip) answers exactly
      that record, if it is still pending; a reply carrying several
      (``escalation_ids`` — chip replies merged into one row by the queue
      drain) answers each of them;
    * a reply without one (typed text) answers the pending record only when
      EXACTLY ONE is pending — with none or several it answers nothing, so an
      unrelated message cannot silently retire N open decisions;
    * a record whose deadline has already passed is swept to
      ``defaulted``/``expired`` first and is never answered late.

    ``candidates`` is the set of ids that were pending when the reply row was
    appended (:func:`pending_ids`, snapshotted on the event loop). It is what
    the free-text rule counts, so a record that landed on the executor between
    the append and this call is neither counted nor answered — the index then
    agrees with the transcript order the chat projection reads. Without a
    snapshot the rule falls back to the records pending now.

    The chat projection applies the same rule client-side, so the card and the
    index agree without a round trip. Also persists any deadline transitions
    found on the way. Returns the number of records moved to ``answered``; a
    conversation with nothing to change is left untouched (no write).
    """
    with _lock_for(slug):
        record = read_conversation(slug)
        swept = sweep_deadlines(record, now=now)
        pending = [
            e
            for e in record.get("entries", [])
            if e.get("type") == "escalation" and e.get("state") == "pending"
        ]
        if candidates is not None:
            allowed = set(candidates)
            pending = [e for e in pending if e.get("id") in allowed]
        targets: list[dict[str, Any]]
        named = [
            i for i in ([escalation_id] if escalation_id else []) + list(escalation_ids or []) if i
        ]
        if named:
            wanted = set(named)
            targets = [e for e in pending if e.get("id") in wanted]
        elif len(pending) == 1:
            targets = pending
        else:
            targets = []
        stamp = answered_ts or _now_iso(now)
        for entry in targets:
            entry["state"] = "answered"
            entry["answered_ts"] = stamp
        if targets or swept:
            _write_conversation(slug, record)
        return len(targets)


def retract_escalation(slug: str, escalation_id: str) -> bool:
    """Remove a record whose transcript row never materialised.

    The escalation path writes the index BEFORE it surfaces the card (so a fast
    reply cannot race the record); if the append then fails, this is the
    compensation — without it a no-deadline record would keep ``needs_you`` lit
    for a card nobody can see. Returns whether a record was removed.
    """
    with _lock_for(slug):
        record = read_conversation(slug)
        before = len(record["entries"])
        record["entries"] = [
            e
            for e in record["entries"]
            if not (e.get("type") == "escalation" and e.get("id") == escalation_id)
        ]
        if len(record["entries"]) == before:
            return False
        _write_conversation(slug, record)
        return True


def public_view(record: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """The index as the dashboard reads it: swept, with ``needs_you`` derived."""
    pending = pending_escalations(record, now=now)
    return {
        "conversation_id": record.get("conversation_id", ""),
        "participants": list(record.get("participants", [])),
        "sessions": list(record.get("sessions", [])),
        "entries": list(record.get("entries", [])),
        "needs_you": bool(pending),
        "pending_escalations": len(pending),
    }


def invalidate_cache(slug: str | None = None) -> None:
    """Test hook / explicit cache drop."""
    if slug is None:
        _PENDING_CACHE.clear()
    else:
        _PENDING_CACHE.pop(_cache_key(slug), None)


__all__ = [
    "CONVERSATION_FILE_NAME",
    "ESCALATION_ID_RE",
    "MAX_DEADLINE_SECS",
    "MAX_ESCALATION_OPTIONS",
    "MIN_DEADLINE_SECS",
    "append_ref",
    "conversation_id",
    "conversation_path",
    "mark_answered",
    "needs_you",
    "new_escalation_id",
    "parse_ts",
    "pending_escalations",
    "prime",
    "public_view",
    "read_conversation",
    "record_escalation",
    "resolve_deadline",
    "sweep_deadlines",
]
