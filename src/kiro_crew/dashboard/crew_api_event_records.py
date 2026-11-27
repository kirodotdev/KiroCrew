"""What the gateway keeps of each conversation's event stream, for ``GET /v1/crew/events``.

One file per conversation under ``member-records/crew-api-events/``, which no sandbox
can read or write: one JSON line per event, in the order they were written. An event
holds ids and a result word, never a message's text. ``seq`` counts the lines of a
conversation's stream from 1.

An event is written before any reader can see it, and a turn is never changed by an
event that could not be written. When one cannot be written, or the stream cannot be
read to write it, the stream is marked broken: a marker file beside it
(``<conv>.gap``), which holds no number. While a stream is broken every read that
names a cursor is told to resync. The first read or write that finds the stream
whole again puts a gap line into the stream itself, numbered one above its newest
line, and removes the marker. A cursor below a gap line is told to resync, so no
reader passes a missing event without knowing. Every number a reader was ever given
is a line of the stream's own file, so a gap is never numbered from memory.

A process that dies between a write that failed and the marker would forget the
loss. So before the first event of a turn is written, a second marker
(``<conv>.open``) is put beside the stream, and it is removed when the last turn
this process has open in the conversation wrote its end. A process that finds an
open marker it did not write takes the stream as broken: the process that wrote it
died with a turn's events unwritten. That costs one more file per turn, not one
per event.

Every file written here is synced, and so is its directory and the directory above
(as ``ConversationStore._write`` does), before the event is given to a reader: a
power loss never brings back an older stream whose numbers would be given again. A
directory that could not be synced leaves the stream broken.

The limits: when neither marker could be written, the stream is broken in memory
only and a restart forgets it. A stream file that cannot be believed is
replaced by one gap line above the highest number the file names; a file that names
none is replaced by gap line 1.

The stream of a conversation is kept as long as the conversation and removed with
it (``ConversationStore``).

Every method that touches the disk blocks and takes the lock the conversation and
response records share; call them off the event loop. The methods that wait for an
event (:meth:`EventLog.listen` and the ones after it) are event-loop only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write, fsync_dir, read_bytes_with_retry
from kiro_crew.dashboard.crew_api_response_records import (
    RETENTION_LOCK,
    is_conversation_id,
    is_response_id,
)

logger = logging.getLogger(__name__)

#: The record kind under ``member-records/``; one of ``member_records.RECORD_KINDS``.
EVENTS_KIND: Final = "crew-api-events"

#: The event types a conversation's stream holds so far.
TURN_STARTED: Final = "turn.started"
MESSAGE_CREATED: Final = "message.created"
TURN_COMPLETED: Final = "turn.completed"
TYPES: Final = (TURN_STARTED, MESSAGE_CREATED, TURN_COMPLETED)

#: The most bytes a stream's file holds. An event is about 200 bytes, so this holds
#: some tens of thousands of them.
# shortcut: a stream at this size takes no more events and stays broken, so its
# readers are told to resync on every read; drop the oldest lines when one gets here.
MAX_FILE_BYTES: Final = 8 * 1024 * 1024
_SUFFIX: Final = ".jsonl"
_MARKER: Final = ".gap"
_OPEN: Final = ".open"
#: The most bytes one line holds; an event is ids and a word, some hundreds of bytes.
#: A line above this size is never written, so one names no number a reader was given.
_LINE_MAX: Final = 64 * 1024
_CHUNK: Final = 64 * 1024
_FIELDS: Final = frozenset(
    {"seq", "event_id", "type", "conversation", "response", "created_at", "data"}
)
#: The one key of a gap line: the number an event after it would have had.
_GAP: Final = "gap"


class Resync(Exception):
    """The cursor cannot be continued from: it is past the newest line, below a gap,
    or the stream is broken."""

    def __init__(self, oldest_seq: int, latest_seq: int):
        super().__init__("the cursor cannot be continued from")
        self.oldest_seq = oldest_seq
        self.latest_seq = latest_seq


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _number(line: dict[str, Any]) -> int:
    """The ``seq`` of an event line, or the number a gap line holds."""
    number: int = line[_GAP] if _GAP in line else line["seq"]
    return number


def _checked(raw: Any, conversation_id: str, after: int) -> dict[str, Any]:
    """A stored line, or ``ValueError``: a stream is never half believed."""
    if isinstance(raw, dict) and set(raw) == {_GAP}:
        if not (_is_int(raw[_GAP]) and raw[_GAP] > after):
            raise ValueError("gap line has a wrong value")
        return raw
    if not isinstance(raw, dict) or set(raw) != _FIELDS:
        raise ValueError("event has the wrong fields")
    response = raw["response"]
    if not (
        _is_int(raw["seq"])
        and raw["seq"] > after
        and isinstance(raw["event_id"], str)
        and raw["event_id"].startswith("evt_")
        and raw["type"] in TYPES
        and raw["conversation"] == conversation_id
        and isinstance(response, str)
        and is_response_id(response)
        and _is_int(raw["created_at"])
        and isinstance(raw["data"], dict)
    ):
        raise ValueError("event has a wrong value")
    return raw


def _number_named(text: bytes) -> int:
    try:
        line = json.loads(text)
        number = line[_GAP] if _GAP in line else line["seq"]
    except (ValueError, RecursionError, KeyError, TypeError):
        return 0
    return number if _is_int(number) else 0


def _highest_number_named(path: Path) -> int:
    """The highest number any line of a stream file names, whatever else is wrong
    with the file and however long it is; 0 when no line names one.

    The whole file is read, a piece at a time: no more than a line is held. A line
    longer than one is ever written is passed over.
    """
    highest = 0
    held = b""
    passing = False
    with open(path, "rb") as stream:
        while chunk := stream.read(_CHUNK):
            *ended, rest = chunk.split(b"\n")
            for index, piece in enumerate(ended):
                if index == 0:
                    piece, held = held + piece, b""
                    if passing:
                        passing = False
                        continue
                highest = max(highest, _number_named(piece))
            if not passing:
                held += rest
                if len(held) > _LINE_MAX:
                    held, passing = b"", True
    return highest if passing else max(highest, _number_named(held))


@dataclass
class EventLog:
    """One gateway's conversation event streams, and the requests waiting on them.

    *directory* is given by a test; the gateway's own is the ``crew-api-events``
    member record directory, found on first use.
    """

    directory: Path | None = None
    clock: Callable[[], float] = time.time
    #: The streams that are broken and whose marker file could not be written.
    _unmarked: set[str] = field(default_factory=set)
    #: The streams whose marker files are left over: one could not be removed.
    _stale: set[str] = field(default_factory=set)
    #: How many turns this process has open, by conversation.
    _turns: dict[str, int] = field(default_factory=dict)
    #: The streams whose open marker this process wrote and has not removed.
    _promised: set[str] = field(default_factory=set)
    #: How many requests are held open for an event now; the route counts them.
    waits: int = 0
    #: What ends at the next event of a conversation, by conversation. Event-loop only.
    _waiting: dict[str, set[asyncio.Future[None]]] = field(default_factory=dict)
    _loop: asyncio.AbstractEventLoop | None = None
    _closed: bool = False

    def _dir(self) -> Path:
        if self.directory is None:
            from kiro_crew import member_records

            self.directory = member_records.records_dir(EVENTS_KIND)
        return self.directory

    def _path(self, conversation_id: str) -> Path:
        return self._dir() / (conversation_id + _SUFFIX)

    def _marker_path(self, conversation_id: str) -> Path:
        return self._dir() / (conversation_id + _MARKER)

    def _open_path(self, conversation_id: str) -> Path:
        return self._dir() / (conversation_id + _OPEN)

    # ── the records ─────────────────────────────────────────────────────────

    def _write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Defence in depth only: the sandbox mask is the fence.
            platform_compat.restrict_dir_to_owner(path.parent)
        except OSError:
            pass
        # No newline translation: the file holds the bytes ``_store`` counted.
        atomic_write(path, text, fsync=True, restrict_to_owner=True, newline="\n")
        # The file's name is in its directory: until that is synced a power loss can
        # bring the file before it back. Quiet only where a directory cannot be
        # synced at all (``fsync_dir``); any other failure is the write's failure.
        fsync_dir(path.parent)
        fsync_dir(path.parent.parent)

    def _store(self, conversation_id: str, lines: list[dict[str, Any]]) -> None:
        """Replace the stream's file with *lines*. ``ValueError`` for more than the
        file may hold, ``OSError`` for a write that failed; the file is then as it was."""
        # shortcut: the whole stream is rewritten for each line, so a write is never
        # half on disk; append in place when streams grow to megabytes.
        texts = [json.dumps(line) + "\n" for line in lines]
        if any(len(one) > _LINE_MAX for one in texts):
            raise ValueError("an event is too long to keep")
        text = "".join(texts)
        if len(text.encode("utf-8")) > MAX_FILE_BYTES:
            raise ValueError("the event stream is too large to keep")
        self._write(self._path(conversation_id), text)

    def _lines(self, conversation_id: str) -> list[dict[str, Any]]:
        """The stream's lines, oldest first. Raises the ``OSError`` of a file that
        cannot be read.

        A file that was read and cannot be believed, one longer than a stream is
        ever written included, is replaced by one gap line above the highest number
        it names anywhere, read to its end. When the replacement cannot be written
        the file stays as it is and the ``OSError`` is raised.
        """
        path = self._path(conversation_id)
        try:
            raw = read_bytes_with_retry(path, max_bytes=MAX_FILE_BYTES + 1)
        except FileNotFoundError:
            return []
        lines: list[dict[str, Any]] = []
        try:
            if len(raw) > MAX_FILE_BYTES:
                raise ValueError("the stream is longer than one is written")
            for text in raw.decode("utf-8").splitlines():
                after = _number(lines[-1]) if lines else 0
                lines.append(_checked(json.loads(text), conversation_id, after))
        except (ValueError, RecursionError):
            lost = [{_GAP: _highest_number_named(path) + 1}]
            logger.warning(
                "Crew API event stream of %s cannot be believed; its events are replaced "
                "by a gap at %d",
                conversation_id,
                lost[0][_GAP],
            )
            self._store(conversation_id, lost)
            return lost
        return lines

    def _broken(self, conversation_id: str) -> bool:
        """Whether an event of the stream was lost since its newest line was written."""
        if conversation_id in self._unmarked:
            return True
        if conversation_id in self._stale:
            return False
        markers = [self._marker_path(conversation_id)]
        if conversation_id not in self._promised:
            # Another process wrote it, and died before its turn's end was written.
            markers.append(self._open_path(conversation_id))
        for marker in markers:
            try:
                os.stat(marker)
            except FileNotFoundError:
                continue
            except OSError:
                # A marker that cannot be looked at is taken as there.
                return True
            return True
        return False

    def _promise(self, conversation_id: str) -> None:
        """Put the open marker beside the stream before a write to it, so a process
        that dies with the write undone leaves the stream broken. Raises the
        ``OSError`` of a marker that could not be written: nothing is written then."""
        if conversation_id not in self._promised:
            self._write(self._open_path(conversation_id), "1\n")
            self._promised.add(conversation_id)

    def _turn_ended(self, conversation_id: str, written: bool) -> None:
        """A turn's end was handled. When it was *written* and no other turn of this
        process is open in the conversation, remove the open marker. Never raises."""
        turns = self._turns.get(conversation_id, 0) - 1
        if turns > 0:
            self._turns[conversation_id] = turns
            return
        self._turns.pop(conversation_id, None)
        if conversation_id not in self._promised:
            return
        # From here the marker counts as any process's: when the end was not
        # written the stream is broken, and mending it removes the marker.
        self._promised.discard(conversation_id)
        if not written:
            return
        try:
            # Not synced: a marker that a power loss brings back adds a gap, no more.
            self._open_path(conversation_id).unlink(missing_ok=True)
        except OSError as exc:
            self._stale.add(conversation_id)
            logger.warning(
                "Crew API event stream marker of %s could not be removed: %s",
                conversation_id,
                type(exc).__name__,
            )

    def _break(self, conversation_id: str) -> None:
        """Mark the stream broken. Never raises."""
        self._stale.discard(conversation_id)
        try:
            self._write(self._marker_path(conversation_id), "1\n")
        except OSError as exc:
            self._unmarked.add(conversation_id)
            logger.warning(
                "Crew API event stream of %s is missing an event, and that could not be "
                "recorded: a restart forgets it (%s)",
                conversation_id,
                type(exc).__name__,
            )

    def _mended(self, lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """*lines* of a broken stream with the gap line that stands for what was lost."""
        return [*lines, {_GAP: (_number(lines[-1]) if lines else 0) + 1}]

    def _unbreak(self, conversation_id: str) -> None:
        """The stream's file holds the gap line: remove the markers. Never raises."""
        self._unmarked.discard(conversation_id)
        try:
            self._marker_path(conversation_id).unlink(missing_ok=True)
            if conversation_id not in self._promised:
                self._open_path(conversation_id).unlink(missing_ok=True)
        except OSError as exc:
            # Left on disk, a later process reads it as one more lost event.
            self._stale.add(conversation_id)
            logger.warning(
                "Crew API event stream marker of %s could not be removed: %s",
                conversation_id,
                type(exc).__name__,
            )

    def append(self, conversation_id: str, kind: str, response_id: str, **data: Any) -> None:
        """Write one event to the conversation's stream, then wake its readers.

        Never raises: the turn that the event is about is never changed by it. An
        event that cannot be written leaves the stream broken (:meth:`_break`), and
        so does a process that dies before the turn's end is written (:meth:`_promise`).
        """
        with RETENTION_LOCK:
            written = False
            if kind == TURN_STARTED:
                self._turns[conversation_id] = self._turns.get(conversation_id, 0) + 1
            try:
                # Before the promise: an open marker of a process that died counts.
                broken = self._broken(conversation_id)
                self._promise(conversation_id)
                lines = self._lines(conversation_id)
                if broken:
                    lines = self._mended(lines)
                event = {
                    "seq": (_number(lines[-1]) if lines else 0) + 1,
                    "event_id": "evt_" + uuid.uuid4().hex,
                    "type": kind,
                    "conversation": conversation_id,
                    "response": response_id,
                    "created_at": int(self.clock()),
                    "data": data,
                }
                self._store(conversation_id, [*lines, event])
                written = True
                if broken:
                    self._unbreak(conversation_id)
            except (OSError, ValueError) as exc:
                logger.warning(
                    "Crew API event %s of %s was not written: %s",
                    kind,
                    conversation_id,
                    type(exc).__name__,
                )
                self._break(conversation_id)
            if kind == TURN_COMPLETED:
                self._turn_ended(conversation_id, written)
        self._wake(conversation_id)

    def events_after(self, conversation_id: str, since: int | None, limit: int) -> dict[str, Any]:
        """The ``EventPage`` after *since*; with no *since*, the newest ``seq`` alone.

        Raises :class:`Resync` for a cursor past the newest line, for one below a
        gap, and for any cursor while the stream is broken; ``OSError`` for a stream
        that cannot be read. A broken stream that reads whole is mended here.
        """
        with RETENTION_LOCK:
            lines = self._lines(conversation_id)
            broken = self._broken(conversation_id)
            if broken:
                mended = self._mended(lines)
                try:
                    self._store(conversation_id, mended)
                except (OSError, ValueError):
                    pass
                else:
                    self._unbreak(conversation_id)
                    lines, broken = mended, False
        latest = _number(lines[-1]) if lines else 0
        gap = max((line[_GAP] for line in lines if _GAP in line), default=0)
        kept = [line for line in lines if _GAP not in line and line["seq"] > gap]
        oldest = kept[0]["seq"] if kept else latest
        if since is None:
            return {"events": [], "latest_seq": latest, "oldest_seq": oldest, "has_more": False}
        if broken or since > latest or since < gap:
            raise Resync(oldest, latest)
        after = [event for event in kept if event["seq"] > since]
        return {
            "events": after[:limit],
            "latest_seq": latest,
            "oldest_seq": oldest,
            "has_more": len(after) > limit,
        }

    def remove(self, conversation_id: str) -> None:
        """Remove the conversation's stream and its marker, then wake its readers.
        Raises the ``OSError`` of a file that could not be removed."""
        with RETENTION_LOCK:
            self._unmarked.discard(conversation_id)
            self._stale.discard(conversation_id)
            self._promised.discard(conversation_id)
            self._path(conversation_id).unlink(missing_ok=True)
            self._marker_path(conversation_id).unlink(missing_ok=True)
            self._open_path(conversation_id).unlink(missing_ok=True)
        self._wake(conversation_id)

    def remove_without(self, kept: Callable[[str], bool]) -> int:
        """Remove every stream whose conversation *kept* does not hold; how many
        files went. A file that cannot be removed stays for the next call."""
        removed = 0
        try:
            names = [entry.name for entry in os.scandir(self._dir())]
        except OSError:
            return 0
        for name in names:
            stem, dot, _suffix = name.partition(".")
            if not dot or not is_conversation_id(stem):
                continue
            try:
                with RETENTION_LOCK:
                    if not kept(stem):
                        os.unlink(self._dir() / name)
                        self._promised.discard(stem)
                        removed += 1
            except OSError:
                continue
        return removed

    # ── waiting for an event (event-loop only) ──────────────────────────────

    def listen(self, conversation_id: str) -> asyncio.Future[None]:
        """A future that is done at the next event of the conversation, at its
        removal, or when the gateway closes. Give it back with :meth:`unlisten`."""
        loop = asyncio.get_running_loop()
        self._loop = loop
        future: asyncio.Future[None] = loop.create_future()
        if self._closed:
            future.set_result(None)
        self._waiting.setdefault(conversation_id, set()).add(future)
        return future

    def unlisten(self, conversation_id: str, future: asyncio.Future[None]) -> None:
        futures = self._waiting.get(conversation_id)
        if futures is not None:
            futures.discard(future)
            if not futures:
                del self._waiting[conversation_id]

    def _wake_now(self, conversation_id: str) -> None:
        for future in self._waiting.get(conversation_id, ()):
            if not future.done():
                future.set_result(None)

    def _wake(self, conversation_id: str) -> None:
        """From any thread: end the waits on the conversation."""
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._wake_now, conversation_id)
        except RuntimeError:
            # The loop is closed: nobody is waiting.
            pass

    def close(self) -> None:
        """End every wait, and let none begin: the gateway is stopping."""
        self._closed = True
        for conversation_id in list(self._waiting):
            self._wake_now(conversation_id)
