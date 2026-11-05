"""Pair a cron notification's read state with the acked item it added.

``POST /api/crons/<id>/ack`` appends the note's summary to the job's
``acked_items`` (the "already reported, do not repeat" list the next run
reads) and marks the note read. ``POST /api/notifications/unack`` must take
back that item and nothing else: a note marked read any other way (the bell,
ack-all) added nothing, and removing an item for it would delete an
acknowledgement someone else recorded, for example from Slack.

The ack gives its entry a fresh id (``CronJob.acked_item_ids``) and the note
records only the job and that id, never the summary text, so the unack
removes exactly that entry: not the job's newest, and not a later entry with
an equal summary. An id the job does not hold (aged out of its last 20)
removes nothing. The record is cleared only once the unack has run, so a
store that was busy or unreadable leaves it for the next one. It is an
internal underscore key that only the cron ack route writes, so a note
starts with no record.

One note's ack and unack run one at a time (:func:`cron_note_transition`):
the "already recorded" check, the awaited store write and the record update
are one step, so a double click cannot add two entries for one record.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

#: Note key holding ``{"job_id", "ack_id"}``: the entry this note's cron ack added.
CRON_ACK_ITEM_KEY = "_cron_ack_item"

#: State attribute holding ``{note ts: [lock, holders]}`` for notes mid-transition.
_LOCKS_ATTR = "_cron_note_ack_locks"


def new_ack_id() -> str:
    """A fresh id for one acked entry."""
    return uuid.uuid4().hex


@asynccontextmanager
async def cron_note_transition(state: Any, ts: str) -> AsyncIterator[None]:
    """Run one ack or unack of note *ts* at a time.

    The entry is dropped once no request holds or waits on it, so the table
    holds only notes with a transition in flight.
    """
    if not isinstance(ts, str):
        # No note has a non-string ts, so there is no record to guard; the
        # routes keep answering such a request as they always have.
        yield
        return
    locks: dict[str, list[Any]] | None = getattr(state, _LOCKS_ATTR, None)
    if locks is None:
        locks = {}
        setattr(state, _LOCKS_ATTR, locks)
    entry = locks.get(ts)
    if entry is None:
        entry = locks[ts] = [asyncio.Lock(), 0]
    entry[1] += 1
    try:
        async with entry[0]:
            yield
    finally:
        entry[1] -= 1
        if entry[1] == 0 and locks.get(ts) is entry:
            del locks[ts]


def _cron_note(state: Any, ts: str) -> dict[str, Any] | None:
    for note in getattr(state, "_notification_log", ()):
        if note.get("ts") == ts:
            return note if note.get("kind") == "cron" else None
    return None


def cron_item_already_recorded(state: Any, ts: str, job_id: str) -> bool:
    """Whether note *ts* already carries an entry the cron ack added to *job_id*."""
    return recorded_cron_item(state, ts, job_id) is not None


def mark_cron_item_recorded(state: Any, ts: str, job_id: str, ack_id: str) -> None:
    """Record on note *ts* that the cron ack added entry *ack_id* to *job_id*.

    Persisted by the ``ack_notification`` rewrite that follows it.
    """
    note = _cron_note(state, ts)
    if note is not None and note.get("job_id") == job_id:
        note[CRON_ACK_ITEM_KEY] = {"job_id": job_id, "ack_id": ack_id}


def recorded_cron_item(state: Any, ts: str, job_id: str | None = None) -> tuple[str, str] | None:
    """``(job_id, ack_id)`` of the entry note *ts*'s cron ack added, else ``None``."""
    note = _cron_note(state, ts)
    if note is None:
        return None
    record = note.get(CRON_ACK_ITEM_KEY)
    if not isinstance(record, dict):
        return None
    recorded_job, ack_id = record.get("job_id"), record.get("ack_id")
    if not isinstance(recorded_job, str) or not isinstance(ack_id, str) or not ack_id:
        return None
    if recorded_job != note.get("job_id") or (job_id is not None and recorded_job != job_id):
        return None
    return recorded_job, ack_id


def clear_cron_item_recorded(state: Any, ts: str) -> None:
    """Drop note *ts*'s record once its unack has run.

    Persisted by the ``unack_notification`` rewrite that follows it.
    """
    note = _cron_note(state, ts)
    if note is not None:
        note.pop(CRON_ACK_ITEM_KEY, None)
