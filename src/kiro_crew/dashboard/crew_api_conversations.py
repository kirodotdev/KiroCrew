"""``POST /v1/conversations`` and the ``GET``, ``POST`` and ``DELETE`` of
``/v1/conversations/{conversation_id}``, and what the gateway keeps about each
conversation. The two paged reads are in ``crew_api_conversation_lists``.

A conversation is the turns ``POST /v1/responses`` ran one after another under one
``conv_`` id. It is a record, not a session: one JSON file per conversation under
``member-records/crew-api-conversations/``, which no sandbox can read or write. The
record holds the conversation's ``metadata``, the ids of its turns whose records were
kept, the ``model`` its latest turn asked for, and who made it. ``POST
/v1/responses`` reads those turns' own records to give a later turn what came before
(``crew_api_responses``). A dashboard chat is not a conversation, and no conversation
is a dashboard chat.

What these routes serve:

* The machine owner alone, as ``POST /v1/responses`` does. A Shared Crew teammate, an
  app credential and an internal caller get ``403 owner_only`` before a record is read.
* ``POST /v1/conversations``: a conversation with no turn in it. ``items`` must be
  empty; the ``Idempotency-Key`` header is not read.
* ``GET /v1/conversations/{conversation_id}``: the conversation, with ``crew.status``
  ``running`` while a turn runs in it. An id that names no conversation, an id of
  another shape and a conversation whose retention is over are one ``404 not_found``.
* ``POST /v1/conversations/{conversation_id}``: replaces the conversation's
  ``metadata`` whole. The body holds ``metadata`` and nothing else. The
  conversation's retention is not renewed, and the ``Idempotency-Key`` header is
  not read.
* ``DELETE /v1/conversations/{conversation_id}``: removes the conversation and the
  records of the turns it lists. ``409 response_in_progress`` while a turn runs in
  it; the delete holds the conversation as a turn does, so a turn asked for during
  it is ``409`` too.

Who a record belongs to is its ``created_by_grant``, written here from the caller the
route verified: ``None`` for the machine owner, the grant's id for an app credential
once a route serves one. Every read and every change names its caller
(:class:`Caller`), and a record of another caller reads as absent. ``model`` is a
label for ``GET`` to show. It is never what a turn is allowed by: each turn's own
``model`` is judged when that turn is asked for.

A conversation is kept :data:`RETENTION_SECS` after its latest turn ended, or after
it was made while it holds none. Admitting a continuation renews that period before
its history is read; completion renews it again. The turns it lists are kept as long
as it is (``ResponseStore.keep_with``), with their file times set before the
conversation's record is written. That a turn is running in a conversation is held in
memory only, so a restart leaves no conversation busy.

Every method of the store that touches the disk blocks; the routes call them off the
event loop.
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

from aiohttp import web

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write, fsync_dir, read_bytes_with_retry
from kiro_crew.dashboard.crew_api_approval_records import set_aside
from kiro_crew.dashboard.crew_api_auth import OWNER_ONLY_PUBLIC_ROUTES
from kiro_crew.dashboard.crew_api_errors import contract_error, request_id_for
from kiro_crew.dashboard.crew_api_grant_requests import _host_owner_refusal
from kiro_crew.dashboard.crew_api_owner import owner_route
from kiro_crew.dashboard.crew_api_response_records import (
    NAMESPACE,
    RETENTION_LOCK,
    RETENTION_SECS,
    STORE_KEY,
    SWEEP_EVERY_SECS,
    ResponseStore,
    is_conversation_id,
    is_response_id,
)
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: The record kind under ``member-records/``; one of ``member_records.RECORD_KINDS``.
CONVERSATIONS_KIND: Final = "crew-api-conversations"
#: Where the routes find the gateway's store on the application.
CONVERSATIONS_KEY: Final = "crew_api_conversation_store"

CREATE_PATH: Final = "/v1/conversations"
READ_PATH: Final = "/v1/conversations/{conversation_id}"
CREATE_OPERATION: Final = OWNER_ONLY_PUBLIC_ROUTES[CREATE_PATH]
READ_OPERATION: Final = OWNER_ONLY_PUBLIC_ROUTES[READ_PATH]
#: The other methods of the two paths. ``OWNER_ONLY_PUBLIC_ROUTES`` names a path once,
#: and every method of a path it names is behind the owner gate.
UPDATE_OPERATION: Final = "crew_api.conversations.update"
DELETE_OPERATION: Final = "crew_api.conversations.delete"

#: The most of a record file that is read, and so the most that is written. A record
#: holds 16 metadata pairs and one 37-character id for each turn that was kept. The
#: turns that completed fit one prompt together; a write past this size is refused.
MAX_FILE_BYTES: Final = 8 * 1024 * 1024
#: The contract's limits for ``metadata`` ("as in OpenAI").
METADATA_MAX_PAIRS: Final = 16
METADATA_KEY_MAX: Final = 64
METADATA_VALUE_MAX: Final = 512

FIELDS: Final = (
    "id",
    "created_at",
    "updated_at",
    "metadata",
    "turns",
    "model",
    "created_by_grant",
    "namespace",
)
_SUFFIX: Final = ".json"
_ASIDE: Final = ".damaged-"


@dataclass(frozen=True)
class Caller:
    """Who asks for a conversation: the machine owner, or the grant of an app credential."""

    #: The grant's id, as the gateway verified it; ``None`` for the machine owner.
    grant: str | None


#: The machine owner. No route serves another caller yet.
OWNER: Final = Caller(None)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def metadata_refusal(given: Any) -> str | None:
    """Why *given* is not a ``metadata`` object the contract allows, or ``None``."""
    if not isinstance(given, dict) or len(given) > METADATA_MAX_PAIRS:
        return f"metadata must be an object of at most {METADATA_MAX_PAIRS} pairs"
    for key, value in given.items():
        if (
            len(key) > METADATA_KEY_MAX
            or not isinstance(value, str)
            or len(value) > METADATA_VALUE_MAX
        ):
            return (
                f"a metadata key holds at most {METADATA_KEY_MAX} characters and its value is a "
                f"string of at most {METADATA_VALUE_MAX}"
            )
    return None


def _checked(raw: Any, conversation_id: str) -> dict[str, Any]:
    """A stored record, or ``ValueError``: a record is never half believed."""
    if not isinstance(raw, dict) or set(raw) != set(FIELDS):
        raise ValueError("conversation record has the wrong fields")
    grant, model, turns = raw["created_by_grant"], raw["model"], raw["turns"]
    if not (
        raw["id"] == conversation_id
        and _is_int(raw["created_at"])
        and _is_int(raw["updated_at"])
        and metadata_refusal(raw["metadata"]) is None
        and isinstance(turns, list)
        and all(isinstance(turn, str) and is_response_id(turn) for turn in turns)
        and len(set(turns)) == len(turns)
        and (model is None or isinstance(model, str))
        and (grant is None or isinstance(grant, str))
        and isinstance(raw["namespace"], str)
    ):
        raise ValueError("conversation record has a wrong value")
    return raw


@dataclass
class ConversationStore:
    """One gateway's conversation records, and the conversations a turn runs in now.

    *directory* is given by a test; the gateway's own is the ``crew-api-conversations``
    member record directory, found on first use.
    """

    directory: Path | None = None
    clock: Callable[[], float] = time.time
    #: The conversations a turn is running in. Memory only: a restart empties it.
    _busy: set[str] = field(default_factory=set)
    _swept_at: float | None = None

    def _dir(self) -> Path:
        if self.directory is None:
            from kiro_crew import member_records

            self.directory = member_records.records_dir(CONVERSATIONS_KIND)
        return self.directory

    def _path(self, conversation_id: str) -> Path:
        return self._dir() / (conversation_id + _SUFFIX)

    def _expired(self, updated_at: float) -> bool:
        return updated_at + RETENTION_SECS <= self.clock()

    # ── the turn that runs in a conversation ────────────────────────────────

    def claim(self, conversation_id: str) -> bool:
        """Take *conversation_id* for one turn; ``False`` when a turn already runs in it.

        Call it on the event loop, and :meth:`release` in a ``finally``.
        """
        if conversation_id in self._busy:
            return False
        self._busy.add(conversation_id)
        return True

    def release(self, conversation_id: str) -> None:
        self._busy.discard(conversation_id)

    def is_busy(self, conversation_id: str) -> bool:
        return conversation_id in self._busy

    # ── the records ─────────────────────────────────────────────────────────

    def _write(self, record: dict[str, Any]) -> None:
        serialized = json.dumps({name: record[name] for name in FIELDS}) + "\n"
        if len(serialized) > MAX_FILE_BYTES:
            # ASCII, so characters are bytes. Never a file that could not be read back.
            raise ValueError("the conversation record is too large to keep")
        path = self._path(record["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Defence in depth only: the sandbox mask is the fence.
            platform_compat.restrict_dir_to_owner(path.parent)
        except OSError:
            pass
        atomic_write(path, serialized, fsync=True, restrict_to_owner=True)
        try:
            # The sweep reads a record's age from its file, without opening it.
            os.utime(path, (record["updated_at"], record["updated_at"]))
            fsync_dir(path.parent)
            fsync_dir(path.parent.parent)
        except OSError as exc:
            # The record reads back; only its survival of a power loss is unconfirmed.
            logger.warning(
                "Crew API conversation %s was recorded, and it is not confirmed that the "
                "record survives a power loss: %s",
                record["id"],
                type(exc).__name__,
            )

    def create(
        self, metadata: dict[str, str], caller: Caller, *, conversation_id: str | None = None
    ) -> dict[str, Any]:
        """Make and keep a conversation of *caller* with no turn in it."""
        now = int(self.clock())
        record = {
            "id": conversation_id if conversation_id is not None else "conv_" + uuid.uuid4().hex,
            "created_at": now,
            "updated_at": now,
            "metadata": dict(metadata),
            "turns": [],
            "model": None,
            "created_by_grant": caller.grant,
            "namespace": NAMESPACE,
        }
        self._write(record)
        self.sweep()
        return record

    def load(self, conversation_id: str, caller: Caller) -> dict[str, Any] | None:
        """*caller*'s conversation *conversation_id*, or ``None``.

        ``None`` for an id of another shape, an id that names nothing, a conversation
        whose retention is over and one another caller made: the four read alike. A
        file that was read and cannot be believed is moved aside and reads as absent.
        A file that cannot be read raises its ``OSError`` and stays where it is.
        """
        if not is_conversation_id(conversation_id):
            return None
        path = self._path(conversation_id)
        with RETENTION_LOCK:
            try:
                raw = read_bytes_with_retry(path, max_bytes=MAX_FILE_BYTES)
            except FileNotFoundError:
                return None
            try:
                record = _checked(json.loads(raw.decode("utf-8")), conversation_id)
            except (ValueError, RecursionError):
                self._set_aside(path)
                return None
            if self._expired(record["updated_at"]):
                path.unlink(missing_ok=True)
                return None
            if record["created_by_grant"] != caller.grant:
                return None
            return record

    def turn_started(
        self,
        conversation_id: str,
        caller: Caller,
        *,
        keep_turns: Callable[[list[str], float], None],
    ) -> dict[str, Any] | None:
        """Admit a claimed conversation before its time is up, and renew its retention.

        Keep its listed turns first, then its record, as :meth:`turn_ended` does.
        Reads and sweeps keep both through a turn shorter than the retention period.
        """
        with RETENTION_LOCK:
            record = self.load(conversation_id, caller)
            if record is None:
                return None
            record["updated_at"] = max(int(self.clock()), record["updated_at"])
            # shortcut: one retention period; hold records if turns can outlast that period.
            keep_turns(record["turns"], record["updated_at"])
            self._write(record)
            return record

    def turn_ended(
        self,
        conversation_id: str,
        caller: Caller,
        *,
        model: str,
        kept: str | None,
        keep_turns: Callable[[list[str], float], None] | None = None,
    ) -> dict[str, Any] | None:
        """Note that a turn of *caller*'s conversation ended; the record as it is
        now, ``None`` when the conversation is not the caller's to change.

        *model* is what the turn asked for. *kept* is the turn's response id when
        its record was kept, however the turn ended; ``None`` for a turn that was
        not kept. The caller holds the conversation (:meth:`claim`), so no other
        turn changes the record meanwhile. The conversation's retention counts from
        this moment.

        *keep_turns* is given the turns the record will list and that moment, before
        the record is written, and keeps the turns' own records until then
        (``ResponseStore.keep_with``). In that order a gateway that stops between
        the two leaves turns kept longer than their conversation, never shorter.
        When it raises, the record is not written.
        """
        with RETENTION_LOCK:
            record = self.load(conversation_id, caller)
            if record is None:
                return None
            if kept is not None and kept not in record["turns"]:
                record["turns"].append(kept)
            record["model"] = model
            record["updated_at"] = max(int(self.clock()), record["updated_at"])
            if keep_turns is not None:
                keep_turns(record["turns"], record["updated_at"])
            self._write(record)
            return record

    def kept_turn(
        self, responses: ResponseStore, response_id: str, caller: Caller
    ) -> dict[str, Any] | None:
        """The kept record of the turn *response_id*, or ``None``. Blocks: it reads
        the turn's record and its conversation's.

        The conversation's record is what says a turn is one of its turns. A turn's
        own record that names a conversation which is gone, or which does not list
        it, reads as absent: the gateway stopped before it listed the turn, or could
        neither list it nor remove it. A record that names no conversation, kept by
        a build that knew none, is served as it is.
        """
        kept = responses.load(response_id)
        if kept is None:
            return None
        named = kept["response"].get("conversation")
        if named is None:
            return kept
        record = self.load(str(named["id"]), caller)
        if record is None or response_id not in record["turns"]:
            return None
        return kept

    def listed(self, caller: Caller) -> list[dict[str, Any]]:
        """Every kept conversation of *caller*, oldest first; equal ages by id.

        The order is by ``created_at``, which never changes, so a conversation a turn
        runs in between two pages keeps its place. A record that cannot be read
        raises its ``OSError``.
        """
        # shortcut: every record in the directory is read on each call; keep an index
        # of (created_at, id) per caller when one data home holds many thousands.
        try:
            names = [entry.name for entry in os.scandir(self._dir())]
        except FileNotFoundError:
            return []
        records = []
        for name in names:
            if not name.endswith(_SUFFIX):
                continue
            record = self.load(name.removesuffix(_SUFFIX), caller)
            if record is not None:
                records.append(record)
        return sorted(records, key=lambda record: (record["created_at"], record["id"]))

    def set_metadata(
        self, conversation_id: str, caller: Caller, metadata: dict[str, str]
    ) -> dict[str, Any] | None:
        """Replace the ``metadata`` of *caller*'s conversation; the record as it is
        now, ``None`` when the conversation is not the caller's to change.

        Nothing else changes: ``updated_at`` stays, so the conversation's retention
        still counts from its latest turn. Held under the lock a turn's end writes
        the record under, so neither write loses the other's.
        """
        with RETENTION_LOCK:
            record = self.load(conversation_id, caller)
            if record is None:
                return None
            record["metadata"] = dict(metadata)
            self._write(record)
            return record

    def delete(self, conversation_id: str, caller: Caller, responses: ResponseStore) -> bool:
        """Remove *caller*'s conversation and the records of the turns it lists;
        ``False`` when the conversation is not the caller's to remove.

        The caller holds the conversation (:meth:`claim`), so no turn runs in it.
        The conversation's record goes first: from then on every turn it listed
        reads as absent (:meth:`kept_turn`), whether or not its own record is gone
        yet. A turn's record that cannot be removed is logged and goes with the
        sweep when its time is up.
        """
        with RETENTION_LOCK:
            record = self.load(conversation_id, caller)
            if record is None:
                return False
            path = self._path(conversation_id)
            path.unlink()
            for response_id in record["turns"]:
                try:
                    responses.discard(response_id)
                except OSError as exc:
                    logger.warning(
                        "Crew API response %s of the deleted conversation %s is still on "
                        "disk: %s",
                        response_id,
                        conversation_id,
                        type(exc).__name__,
                    )
            try:
                fsync_dir(path.parent)
            except OSError as exc:
                # Deleted; only its survival of a power loss is unconfirmed.
                logger.warning(
                    "Crew API conversation %s was deleted, and it is not confirmed that "
                    "the deletion survives a power loss: %s",
                    conversation_id,
                    type(exc).__name__,
                )
            return True

    def _set_aside(self, path: Path) -> None:
        try:
            target = set_aside(path, self.clock())
        except OSError:
            logger.warning("Crew API conversation record %s is unreadable", path.name)
            return
        logger.warning(
            "Crew API conversation record %s is unreadable; kept as %s", path.name, target
        )

    def sweep(self) -> int:
        """Remove the records whose time is up; how many went. At most once an hour.

        A record's age is its file's modification time, which every write sets to the
        record's ``updated_at``. A file that was moved aside keeps that time.
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
            if not name.endswith(_SUFFIX) or not is_conversation_id(name.removesuffix(_SUFFIX)):
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


# ── the routes ──────────────────────────────────────────────────────────────


def served(record: dict[str, Any], busy: bool) -> dict[str, Any]:
    """The ``Conversation`` object of *record*. ``metadata`` goes back as it was sent."""
    crew: dict[str, Any] = {"title": None, "status": "running" if busy else "idle"}
    if record["model"] is not None:
        crew["model"] = record["model"]
    return {
        "id": record["id"],
        "object": "conversation",
        "created_at": record["created_at"],
        "metadata": record["metadata"],
        "crew": crew,
    }


def _bad(request: web.Request, message: str) -> web.Response:
    return contract_error(400, "invalid_request", message, request_id_for(request))


def unavailable(request: web.Request) -> web.Response:
    return contract_error(
        503,
        "instance_unavailable",
        "the conversation records cannot be used right now",
        request_id_for(request),
    )


def missing(request: web.Request) -> web.Response:
    """One answer for an id that never existed, one of another shape, one whose time
    is up and a conversation of another caller."""
    return contract_error(
        404, "not_found", "no such conversation, or it is no longer kept", request_id_for(request)
    )


async def audit(request: web.Request, operation: str, resources: str) -> None:
    caller = str(request.get("user") or "unknown")

    def write() -> None:
        sel().log_api_access(
            caller=caller,
            operation=operation,
            outcome="allowed",
            source="crew_api",
            resources=resources,
        )

    try:
        await asyncio.to_thread(write)
    except Exception:  # the audit row must never change the outcome
        logger.warning("No audit row written for %s", operation)


async def _create(request: web.Request) -> web.Response:
    denied = await _host_owner_refusal(request, CREATE_OPERATION)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except ValueError:
        return _bad(request, "the body must be JSON")
    if not isinstance(body, dict):
        return _bad(request, "the body must be a JSON object")
    if body.get("items") not in (None, []):
        return _bad(request, "items must be empty: a conversation starts with no turn in it")
    metadata = body.get("metadata")
    metadata = {} if metadata is None else metadata
    refusal = metadata_refusal(metadata)
    if refusal is not None:
        return _bad(request, refusal)
    store: ConversationStore = request.app[CONVERSATIONS_KEY]
    try:
        record = await asyncio.to_thread(store.create, metadata, OWNER)
    except (OSError, ValueError) as exc:
        logger.warning("Crew API conversation was not recorded: %s", type(exc).__name__)
        return unavailable(request)
    await audit(request, CREATE_OPERATION, f"conversation={record['id']}")
    return web.json_response(served(record, False))


async def _read(request: web.Request) -> web.Response:
    denied = await _host_owner_refusal(request, READ_OPERATION)
    if denied is not None:
        return denied
    store: ConversationStore = request.app[CONVERSATIONS_KEY]
    conversation_id = request.match_info["conversation_id"]
    try:
        record = await asyncio.to_thread(store.load, conversation_id, OWNER)
    except OSError:
        return unavailable(request)
    # The id is the caller's text until it has this gateway's shape.
    named = conversation_id if is_conversation_id(conversation_id) else "invalid"
    await audit(request, READ_OPERATION, f"conversation={named} found={'yes' if record else 'no'}")
    if record is None:
        return missing(request)
    return web.json_response(served(record, store.is_busy(conversation_id)))


async def _update(request: web.Request) -> web.Response:
    denied = await _host_owner_refusal(request, UPDATE_OPERATION)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except ValueError:
        return _bad(request, "the body must be JSON")
    if not isinstance(body, dict) or set(body) != {"metadata"}:
        return _bad(request, "the body must be a JSON object with metadata and nothing else")
    refusal = metadata_refusal(body["metadata"])
    if refusal is not None:
        return _bad(request, refusal)
    store: ConversationStore = request.app[CONVERSATIONS_KEY]
    conversation_id = request.match_info["conversation_id"]
    try:
        record = await asyncio.to_thread(
            store.set_metadata, conversation_id, OWNER, body["metadata"]
        )
    except (OSError, ValueError) as exc:
        logger.warning("Crew API conversation was not updated: %s", type(exc).__name__)
        return unavailable(request)
    named = conversation_id if is_conversation_id(conversation_id) else "invalid"
    await audit(
        request, UPDATE_OPERATION, f"conversation={named} found={'yes' if record else 'no'}"
    )
    if record is None:
        return missing(request)
    return web.json_response(served(record, store.is_busy(conversation_id)))


async def _delete(request: web.Request) -> web.Response:
    denied = await _host_owner_refusal(request, DELETE_OPERATION)
    if denied is not None:
        return denied
    store: ConversationStore = request.app[CONVERSATIONS_KEY]
    responses: ResponseStore = request.app[STORE_KEY]
    conversation_id = request.match_info["conversation_id"]
    if not is_conversation_id(conversation_id):
        await audit(request, DELETE_OPERATION, "conversation=invalid found=no")
        return missing(request)
    # Taken as a turn takes it: a turn that runs is not deleted under, and no turn
    # starts in a conversation that is being deleted.
    if not store.claim(conversation_id):
        return contract_error(
            409,
            "response_in_progress",
            "the conversation has a turn running",
            request_id_for(request),
        )
    try:
        try:
            deleted = await asyncio.to_thread(store.delete, conversation_id, OWNER, responses)
        except OSError as exc:
            logger.warning("Crew API conversation was not deleted: %s", type(exc).__name__)
            return unavailable(request)
    finally:
        store.release(conversation_id)
    await audit(
        request,
        DELETE_OPERATION,
        f"conversation={conversation_id} found={'yes' if deleted else 'no'}",
    )
    if not deleted:
        return missing(request)
    return web.json_response(
        {"id": conversation_id, "object": "conversation.deleted", "deleted": True}
    )


#: POST /v1/conversations/{conversation_id}. The same two checks as the routes below.
api_conversation_update = owner_route(_update, UPDATE_OPERATION)

#: DELETE /v1/conversations/{conversation_id}. The same two checks.
api_conversation_delete = owner_route(_delete, DELETE_OPERATION)

#: POST /v1/conversations. Served by the owner gate, then by the machine-owner check
#: in the handler.
api_conversation_create = owner_route(_create, CREATE_OPERATION)

#: GET /v1/conversations/{conversation_id}. The same two checks.
api_conversation_read = owner_route(_read, READ_OPERATION)
