"""Durable pointer events for creator-owned dashboard session turns.

The event contains no child response text.  It points the creator at the child
transcript and remains untrusted until the crew-log lineage and completed turn
are revalidated by :mod:`kiro_crew.dashboard.peer_completion_delivery`.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Final, Mapping

PEER_TURN_COMPLETION_KIND: Final[str] = "peer_session_turn_completed"
PEER_TURN_COMPLETION_META_KEY: Final[str] = "peer_session_turn_completed"
PEER_TURN_COMPLETION_ATTESTED_KEY: Final[str] = "_peer_completion_attested"
PEER_TURN_COMPLETION_PREPARING_KEY: Final[str] = "_peer_completion_preparing"
PEER_TURN_COMPLETION_HELD_KEY: Final[str] = "_peer_completion_held"
PEER_TURN_COMPLETION_INJECT_KIND: Final[str] = "peer_session_completion"
PEER_TURN_COMPLETION_PREFIX: Final[str] = "[Peer session turn completed]"

_MAX_SESSION_KEY_CHARS: Final[int] = 512
_MAX_TIMESTAMP_CHARS: Final[int] = 64


def event_id(child_session_id: str, child_turn_id: int) -> str:
    """Stable idempotency key for one protected child session turn."""
    return hashlib.sha256(
        f"{child_session_id}\0{int(child_turn_id)}".encode("utf-8", "strict")
    ).hexdigest()


def _timestamp_is_valid(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value) > _MAX_TIMESTAMP_CHARS:
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def normalize_event(raw: object) -> dict[str, Any] | None:
    """Return a bounded canonical event, or ``None`` for an invalid record.

    Persisted session metadata is editable by the local agent, so shape checking
    is not attestation.  The delivery layer still proves the creator and completed
    turn from the protected crew log before a restored event may open a turn.
    """
    if not isinstance(raw, Mapping):
        return None
    creator = raw.get("creator_session")
    child = raw.get("child_session")
    child_session_id = raw.get("child_session_id")
    turn = raw.get("child_turn_id")
    cursor = raw.get("transcript_index")
    ended_at = raw.get("ended_at")
    pending_approval = raw.get("pending_approval")
    child_state = raw.get("child_state")
    identity = raw.get("event_id")
    if not isinstance(creator, str) or not creator or len(creator) > _MAX_SESSION_KEY_CHARS:
        return None
    if not isinstance(child, str) or not child or len(child) > _MAX_SESSION_KEY_CHARS:
        return None
    if creator == child:
        return None
    if (
        not isinstance(child_session_id, str)
        or not child_session_id
        or len(child_session_id) > _MAX_SESSION_KEY_CHARS
    ):
        return None
    if not isinstance(turn, int) or isinstance(turn, bool) or turn < 1:
        return None
    if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
        return None
    if cursor != turn:
        return None
    if not _timestamp_is_valid(ended_at):
        return None
    if pending_approval is not False or child_state != "idle":
        return None
    expected = event_id(child_session_id, turn)
    if not isinstance(identity, str) or identity != expected:
        return None
    return {
        "event_id": expected,
        "creator_session": creator,
        "child_session": child,
        "child_session_id": child_session_id,
        "child_turn_id": turn,
        "transcript_index": cursor,
        "ended_at": ended_at,
        "pending_approval": False,
        "child_state": "idle",
    }


def build_event(
    *,
    creator_session: str,
    child_session: str,
    child_session_id: str,
    child_turn_id: int,
    transcript_index: int,
    ended_at: str,
) -> dict[str, Any]:
    """Build one canonical event; raise when the caller violates the contract."""
    raw = {
        "event_id": event_id(child_session_id, child_turn_id),
        "creator_session": creator_session,
        "child_session": child_session,
        "child_session_id": child_session_id,
        "child_turn_id": child_turn_id,
        "transcript_index": transcript_index,
        "ended_at": ended_at,
        "pending_approval": False,
        "child_state": "idle",
    }
    event = normalize_event(raw)
    if event is None:
        raise ValueError("invalid peer session turn completion event")
    return event


def prompt_for_event(event: Mapping[str, Any]) -> str:
    """Render the pointer-only machine envelope delivered to the creator."""
    child = event["child_session"]
    turn = event["child_turn_id"]
    cursor = event["transcript_index"]
    ended_at = event["ended_at"]
    return (
        f"{PEER_TURN_COMPLETION_PREFIX}\n"
        f"Child session: `{child}`\n"
        f"Child turn: {turn}\n"
        f"Transcript cursor: {cursor}\n"
        f"Ended at: {ended_at}\n"
        "State: idle; pending approval: false\n\n"
        f'Read the result with session_read_message(target="{child}", since={cursor}). '
        "This event proves only that the turn ended. Verify its revision, artifacts, "
        "and acceptance criteria before recording PASS, DONE, BLOCKED, or any restart."
    )


def event_from_entry(entry: object) -> dict[str, Any] | None:
    """Read a canonical peer event from one queue or transcript row."""
    if not isinstance(entry, Mapping):
        return None
    meta = entry.get("meta")
    if not isinstance(meta, Mapping):
        return None
    return normalize_event(meta.get(PEER_TURN_COMPLETION_META_KEY))


def entry_is_attested(entry: object) -> bool:
    return bool(
        isinstance(entry, Mapping)
        and entry.get("kind") == PEER_TURN_COMPLETION_KIND
        and entry.get(PEER_TURN_COMPLETION_ATTESTED_KEY) is True
        and event_from_entry(entry) is not None
    )
