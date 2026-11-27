"""What the gateway keeps about each ``POST /v1/responses`` turn, so it can be read back.

One JSON file per response under ``member-records/crew-api-responses/``, which no
sandbox can read or write. A record holds the ``Response`` the turn was answered
with, the turn's input items, and, when the turn was answered as a stream, the text of
each delta that stream sent: enough to make its events again, and no event is kept. The reply, the failure reason and each input text passed the route's
redaction before they reached the file. ``metadata``, the model name and the names of
ignored fields are in it as the owner sent them: only the machine owner makes a
record and reads it, and redacting a pair would break an id that looks like a key.

A record is written once, when its turn has ended. A turn still running is held in
memory only (:attr:`ResponseStore.live`), so a turn the gateway did not see end
leaves nothing behind.

Records are kept :data:`RETENTION_SECS`: from the turn's own start, or, for a turn
of a conversation, from when that conversation's latest turn was admitted or ended
(:meth:`ResponseStore.keep_with`), so a conversation's turns are kept as long as the
conversation is. A read treats an older one as absent and
removes it; :meth:`ResponseStore.sweep` removes the others, at most once an hour,
when a record is written.

Every method here that touches the disk blocks; the routes call them off the event
loop.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any, Final

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write, fsync_dir, read_bytes_with_retry
from kiro_crew.dashboard.crew_api_approval_records import set_aside

logger = logging.getLogger(__name__)

# Serialize both stores' retention decisions and renewals in worker threads.
# shortcut: one process; use a process-shared lock if gateways share a data home.
RETENTION_LOCK: Final = RLock()

#: The record kind under ``member-records/``; one of ``member_records.RECORD_KINDS``.
RESPONSES_KIND: Final = "crew-api-responses"
#: Where the routes find the gateway's store on the application.
STORE_KEY: Final = "crew_api_response_store"

#: How long a response can be read back: the contract's default of 30 days.
#: shortcut: fixed; the contract's ``crew_api.retention_days`` setting is not built.
RETENTION_SECS: Final = 30 * 24 * 60 * 60
#: The longest gap between two sweeps while records are being written.
SWEEP_EVERY_SECS: Final = 60 * 60
#: The largest reply a record keeps, in UTF-8 bytes. A turn whose reply is larger is
#: answered as ever and not kept. The reply is the turn's own text, so the line is
#: the same for a turn answered as JSON and one answered as a stream, whatever its
#: input and whatever the file's encoding makes of its characters.
MAX_REPLY_BYTES: Final = 1024 * 1024
#: The largest request a record is kept for, in UTF-8 bytes as the caller sent it:
#: the input items' texts and the names of the ignored fields together. A turn that
#: sent more is answered as ever and not kept. The prompt limit does not bound
#: these: an input can shrink on its way into the prompt and is kept as it was sent.
#: The route applies this line before the turn, to the text as sent; the redacted
#: text a record holds can be longer and is bounded by the file limit alone.
MAX_REQUEST_BYTES: Final = 1024 * 1024
#: The most of a record file that is read, and so the most :meth:`ResponseStore.save`
#: writes: it refuses a larger file, so no record is written that cannot be read
#: back. A file holds the reply at most twice (the ``Response`` and the stream's
#: deltas) and the redacted request once, and spells a character in at most six
#: bytes for each of its UTF-8 bytes (a control character), so a record within the
#: two limits above reaches this only when redaction made its input several times
#: longer.
MAX_FILE_BYTES: Final = 32 * 1024 * 1024

#: The fields of a record, and nothing else, reach the file.
#: ``created_by_grant`` and ``namespace`` have no reader yet. They say whose turn a
#: record is: the contract lets a grant with only ``responses:write`` read the
#: responses that grant created, and a record written without them could not be
#: given an owner afterwards.
FIELDS: Final = (
    "id",
    "created_at",
    "response",
    "input",
    "stream",
    "created_by_grant",
    "namespace",
)
#: A field an earlier build wrote and nothing read. A file that carries it is still
#: a record; the field is left out of what a read is given.
_FORMER_FIELDS: Final = ("slot",)
#: Every record's namespace, as on a grant of local authorization.
NAMESPACE: Final = "default"


class RecordNotDurable(OSError):
    """The record file was written, and a step after it failed (setting the file's
    time, or syncing its directory): a read finds the record, and it is not confirmed
    that it survives a power loss."""


_ID_RE: Final = re.compile(r"resp_[0-9a-f]{32}")
_CONVERSATION_ID_RE: Final = re.compile(r"conv_[0-9a-f]{32}")
_ITEM_ID_RE: Final = re.compile(r"msg_[0-9a-f]{32}")
_SUFFIX: Final = ".json"
#: What follows a record's file name once ``crew_api_approval_records.set_aside``
#: moved it aside; the sweep finds the copies by it.
_ASIDE: Final = ".damaged-"


def is_response_id(value: str) -> bool:
    """Whether *value* is an id this gateway gives a response."""
    return _ID_RE.fullmatch(value) is not None


def is_conversation_id(value: Any) -> bool:
    """Whether *value* is an id this gateway gives a conversation."""
    return isinstance(value, str) and _CONVERSATION_ID_RE.fullmatch(value) is not None


def _need(ok: bool, what: str) -> None:
    if not ok:
        raise ValueError(f"response record has a wrong {what}")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_item_id(value: Any) -> bool:
    return isinstance(value, str) and _ITEM_ID_RE.fullmatch(value) is not None


# What follows holds a record to the one shape this module's writer gives it. Every
# field a read serves is one of three kinds, and a read route relies on that:
#   fixed    an id of this gateway's shape (the turn's, an item's, the conversation's),
#            a number, or one of the values named here;
#   text     ``text``, ``message`` and the stream's ``deltas``: free text, which a
#            read redacts;
#   as sent  ``metadata``, ``model`` and ``crew.ignored_parameters``: the owner's own.
# ``namespace`` and ``created_by_grant`` are kept and never served. A field of no
# kind does not exist: the key sets are exact.

_ROLES_SENT: Final = ("user", "assistant", "developer", "system")
_ITEM_STATUSES: Final = ("in_progress", "completed", "incomplete")
#: How a turn can have ended, and so every status a kept ``Response`` can have.
_ENDED: Final = ("completed", "failed", "cancelled", "incomplete")
#: The one error code and the one cut-short reason ``POST /v1/responses`` gives.
_ERROR_CODE: Final = "server_error"
_CUT_REASONS: Final = ("turn_time_limit",)
_RESPONSE_KEYS: Final = frozenset(
    {
        "id",
        "object",
        "created_at",
        "status",
        "model",
        "output",
        "error",
        "incomplete_details",
        "metadata",
        "usage",
        "crew",
    }
)
#: The conversation a turn ran in, as ``{"id": "conv_..."}``. A ``Response`` kept by
#: a build that knew no conversations has no such field.
_CONVERSATION_KEY: Final = "conversation"


def _check_part(part: Any, kind: str) -> None:
    """One content part: its type and its text, and for a reply its empty annotations.

    A reply part holds some text: a turn with no reply text has no output item.
    """
    keys = {"type", "text"} | ({"annotations"} if kind == "output_text" else set())
    _need(isinstance(part, dict) and set(part) == keys, "content part")
    _need(part["type"] == kind and isinstance(part["text"], str), "content part")
    _need(kind != "output_text" or (part["annotations"] == [] and part["text"]), "content part")


def _check_item(item: Any, roles: tuple[str, ...], kind: str) -> None:
    """One ``message`` item whose content is exactly one *kind* part."""
    _need(
        isinstance(item, dict) and set(item) == {"type", "id", "role", "status", "content"},
        "item",
    )
    content = item["content"]
    _need(
        item["type"] == "message"
        and _is_item_id(item["id"])
        and isinstance(item["role"], str)
        and item["role"] in roles
        and isinstance(item["status"], str)
        and item["status"] in _ITEM_STATUSES
        and isinstance(content, list)
        and len(content) == 1,
        "item",
    )
    for part in content:
        _check_part(part, kind)


def _check_response(response: Any, response_id: str) -> None:
    """A ``Response`` as ``POST /v1/responses`` builds it for a turn that ended."""
    _need(
        isinstance(response, dict) and set(response) - {_CONVERSATION_KEY} == _RESPONSE_KEYS,
        "response",
    )
    if _CONVERSATION_KEY in response:
        conversation = response[_CONVERSATION_KEY]
        _need(
            isinstance(conversation, dict)
            and set(conversation) == {"id"}
            and is_conversation_id(conversation["id"]),
            "response conversation",
        )
    error, cut, crew = response["error"], response["incomplete_details"], response["crew"]
    metadata = response["metadata"]
    _need(
        response["id"] == response_id
        and response["object"] == "response"
        and _is_int(response["created_at"])
        and isinstance(response["status"], str)
        and response["status"] in _ENDED
        and isinstance(response["model"], str)
        and isinstance(response["output"], list)
        and len(response["output"]) <= 1
        and response["usage"] is None,
        "response",
    )
    _need(
        isinstance(metadata, dict) and all(isinstance(v, str) for v in metadata.values()),
        "response metadata",
    )
    for item in response["output"]:
        _check_item(item, ("assistant",), "output_text")
    _need(
        error is None
        or (
            isinstance(error, dict)
            and set(error) == {"code", "message"}
            and error["code"] == _ERROR_CODE
            and isinstance(error["message"], str)
        ),
        "response error",
    )
    _need(
        cut is None
        or (
            isinstance(cut, dict)
            and set(cut) == {"reason"}
            and isinstance(cut["reason"], str)
            and cut["reason"] in _CUT_REASONS
        ),
        "response incomplete_details",
    )
    _need(
        isinstance(crew, dict)
        and set(crew) == {"ignored_parameters"}
        and isinstance(crew["ignored_parameters"], list)
        and all(isinstance(name, str) for name in crew["ignored_parameters"]),
        "response crew field",
    )


def _check_stream(stream: Any, response: dict[str, Any]) -> None:
    """What is kept of a stream, and that it agrees with the ``Response`` it ended on.

    The events themselves are not kept: ``crew_api_response_stream.rebuilt`` makes
    them from the ``Response`` and these texts, so no event can be malformed.
    """
    _need(isinstance(stream, dict) and set(stream) == {"deltas", "item_id", "error"}, "stream")
    deltas, output = stream["deltas"], response["output"]
    _need(
        isinstance(deltas, list)
        and all(isinstance(text, str) and text for text in deltas)
        and _is_item_id(stream["item_id"])
        and isinstance(stream["error"], bool),
        "stream",
    )
    if stream["error"]:
        # Closed on an ``error`` event: the turn could not be answered.
        _need(
            response["status"] == "failed" and response["error"] is not None and not output,
            "stream ending",
        )
        return
    # Closed on its Response: the deltas carried exactly that Response's text.
    text = output[0]["content"][0]["text"] if output else ""
    _need("".join(deltas) == text, "stream text")
    _need(not output or output[0]["id"] == stream["item_id"], "stream item")


def _checked(raw: Any, response_id: str) -> dict[str, Any]:
    """A stored record, or ``ValueError``: a record is never half believed."""
    if not isinstance(raw, dict) or not set(FIELDS) <= set(raw) <= {*FIELDS, *_FORMER_FIELDS}:
        raise ValueError("response record has the wrong fields")
    _need(all(isinstance(raw[name], str) for name in _FORMER_FIELDS if name in raw), "value")
    raw = {name: raw[name] for name in FIELDS}
    grant = raw["created_by_grant"]
    _need(
        raw["id"] == response_id
        and _is_int(raw["created_at"])
        and isinstance(raw["input"], list)
        and (grant is None or isinstance(grant, str))
        and isinstance(raw["namespace"], str),
        "value",
    )
    _check_response(raw["response"], response_id)
    for item in raw["input"]:
        _check_item(item, _ROLES_SENT, "input_text")
    # A page is found by its last item's id, so no two items share one.
    _need(len({item["id"] for item in raw["input"]}) == len(raw["input"]), "input item ids")
    if raw["stream"] is not None:
        _check_stream(raw["stream"], raw["response"])
    return raw


def reply_bytes(record: dict[str, Any]) -> int:
    """The size of the reply a record would keep, in UTF-8 bytes.

    The text the turn's ``Response`` holds, or, for a stream closed on an ``error``
    event, the text its deltas carried.
    """
    output = record["response"]["output"]
    text = output[0]["content"][0]["text"] if output else ""
    stream = record["stream"]
    carried = "".join(stream["deltas"]) if stream is not None else ""
    return max(_size(text), _size(carried))


def _size(text: str) -> int:
    # surrogatepass: JSON can carry half of a surrogate pair, which has a size too.
    return len(text.encode("utf-8", "surrogatepass"))


def request_bytes(texts: list[str], ignored: list[str]) -> int:
    """The size of a request as it was sent, in UTF-8 bytes: the input items'
    *texts* and the names of the *ignored* fields. The route measures this before
    the turn; the record's own redacted texts can be longer and are not measured
    again, so a request within the limit is never dropped for what redaction added."""
    return sum(_size(text) for text in [*texts, *ignored])


@dataclass
class ResponseStore:
    """One gateway's response records, and the turns it is running now.

    *directory* is given by a test; the gateway's own is the ``crew-api-responses``
    member record directory, found on first use.
    """

    directory: Path | None = None
    clock: Callable[[], float] = time.time
    #: The turns still running, by response id. The value is the route's own.
    live: dict[str, Any] = field(default_factory=dict)
    _swept_at: float | None = None

    def _dir(self) -> Path:
        if self.directory is None:
            from kiro_crew import member_records

            self.directory = member_records.records_dir(RESPONSES_KIND)
        return self.directory

    def _path(self, response_id: str) -> Path:
        return self._dir() / (response_id + _SUFFIX)

    def _expired(self, created_at: float) -> bool:
        return created_at + RETENTION_SECS <= self.clock()

    def save(self, record: dict[str, Any]) -> None:
        """Write *record* durably. Raises when it cannot be written or is too large,
        and :class:`RecordNotDurable` when it was written and a later step failed."""
        response_id = record["id"]
        if not is_response_id(response_id):
            raise ValueError("not a response id")
        kept = {name: record[name] for name in FIELDS}
        if reply_bytes(kept) > MAX_REPLY_BYTES:
            raise ValueError("the reply is too large to keep")
        serialized = json.dumps(kept) + "\n"
        if len(serialized) > MAX_FILE_BYTES:
            # ASCII, so characters are bytes. Never a file that could not be read back.
            raise ValueError("the record is too large to keep")
        path = self._path(response_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Defence in depth only: the sandbox mask is the fence.
            platform_compat.restrict_dir_to_owner(path.parent)
        except OSError:
            pass
        atomic_write(path, serialized, fsync=True, restrict_to_owner=True)
        # From here the record reads back, so a failure is not "nothing was written".
        try:
            # The sweep reads a record's age from its file, without opening it.
            os.utime(path, (kept["created_at"], kept["created_at"]))
            fsync_dir(path.parent)
            # Every save: an earlier failed save may have made the directory without
            # syncing its parent. An existing directory is not proof it is durable.
            fsync_dir(path.parent.parent)
        except OSError as exc:
            raise RecordNotDurable(*exc.args) from exc
        self.sweep()

    def load(self, response_id: str) -> dict[str, Any] | None:
        """The record of *response_id*, or ``None``: no such id, or its time is up.

        A file that was read and cannot be believed is moved aside and reads as
        absent. A file that cannot be read raises its ``OSError`` and stays where it
        is: unreadable is not damaged.
        """
        if not is_response_id(response_id):
            return None
        path = self._path(response_id)
        try:
            raw = read_bytes_with_retry(path, max_bytes=MAX_FILE_BYTES)
        except FileNotFoundError:
            return None
        try:
            record = _checked(json.loads(raw.decode("utf-8")), response_id)
        except (ValueError, RecursionError):
            # RecursionError: a file nested deeper than the parser goes.
            self._set_aside(path)
            return None
        with RETENTION_LOCK:
            kept_from = record["created_at"]
            if "conversation" in record["response"]:
                # Read the file's current retention time while renewal is excluded.
                try:
                    kept_from = max(kept_from, path.stat().st_mtime)
                except FileNotFoundError:
                    return None
            if self._expired(kept_from):
                path.unlink(missing_ok=True)
                return None
        return record

    def discard(self, response_id: str) -> None:
        """Remove the record of *response_id*, so the turn reads as one not kept."""
        if is_response_id(response_id):
            with RETENTION_LOCK:
                self._path(response_id).unlink(missing_ok=True)

    def keep_with(self, response_ids: list[str], latest_turn_at: float) -> None:
        """Keep the records of a conversation's turns as long as the conversation.

        *latest_turn_at* is when the conversation's latest turn was admitted or ended,
        the moment its own retention counts from. It becomes each file's time, which :meth:`load`
        and :meth:`sweep` read a conversation turn's age from. A file's time is only
        ever moved later. A record that is gone is skipped. A time that cannot be set
        raises its ``OSError``: the caller then does not move the conversation's own
        time later, so no turn's record goes before its conversation does.
        """
        with RETENTION_LOCK:
            for response_id in response_ids:
                if not is_response_id(response_id):
                    continue
                path = self._path(response_id)
                try:
                    if path.stat().st_mtime < latest_turn_at:
                        os.utime(path, (latest_turn_at, latest_turn_at))
                except FileNotFoundError:
                    continue

    def set_aside(self, response_id: str) -> None:
        """Move the record of *response_id* out of the way: no answer could be built
        from it. The copy goes with the sweep, when the record's retention is over."""
        if is_response_id(response_id):
            self._set_aside(self._path(response_id))

    def _set_aside(self, path: Path) -> None:
        try:
            # The approval records' own move: a name nothing holds, so nothing is
            # overwritten. Called off the event loop, with no lock held, so a retry
            # on Windows blocks no one.
            target = set_aside(path, self.clock())
        except FileNotFoundError:
            # Another read moved it first, or its time was up and it was removed.
            logger.warning("Crew API response record %s is unreadable and already gone", path.name)
            return
        except OSError:
            # The move is a rename and then a sync of the directory: either can fail.
            if os.path.lexists(path):
                logger.warning("Crew API response record %s is unreadable and stays", path.name)
            else:
                logger.warning(
                    "Crew API response record %s is unreadable and was moved aside; "
                    "it is not confirmed that the move survives a power loss",
                    path.name,
                )
            return
        logger.warning("Crew API response record %s is unreadable; kept as %s", path.name, target)

    def sweep(self) -> int:
        """Remove the records whose time is up; how many went. At most once an hour.

        A record's age is its file's modification time, which :meth:`save` sets to the
        record's ``created_at``. A file that was moved aside keeps that time and goes
        the same way, so it does not outlive the retention either.
        """
        now = self.clock()
        if self._swept_at is not None and now - self._swept_at < SWEEP_EVERY_SECS:
            return 0
        self._swept_at = now
        removed = 0
        try:
            entries = list(os.scandir(self._dir()))
        except OSError:
            return 0
        for entry in entries:
            name, _, aside = entry.name.partition(_ASIDE)
            if not name.endswith(_SUFFIX) or not is_response_id(name.removesuffix(_SUFFIX)):
                continue
            if entry.name != name and not aside:
                continue
            try:
                with RETENTION_LOCK:
                    if self._expired(os.stat(entry.path, follow_symlinks=False).st_mtime):
                        os.unlink(entry.path)
                        removed += 1
            except OSError:
                continue
        return removed
