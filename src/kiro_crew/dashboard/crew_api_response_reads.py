"""``GET /v1/responses/{response_id}`` and ``GET /v1/responses/{response_id}/input_items``:
read back a turn ``POST /v1/responses`` ran.

Both read the turn's record (``crew_api_response_records``), or, for a turn still
running, what the route holds in memory. An id that names neither is ``404
not_found``, and so is one whose retention period is over.

What these routes serve:

* The machine owner alone, as ``POST /v1/responses`` does: the turn is the owner's.
  A Shared Crew teammate, an app credential and an internal caller get ``403
  owner_only`` before anything is read.
* ``GET /v1/responses/{response_id}``: the ``Response`` the turn was answered with.
  With ``stream=true`` it sends the turn's stream events again and closes;
  ``starting_after=N`` leaves out the events up to sequence number N. No event is
  kept: they are made again from the ``Response`` and, for a turn that was answered
  as a stream, the text of each delta that stream sent, so they come out the same,
  number for number. A turn answered as JSON gets the events of its ``Response``.
  A turn still running reads ``in_progress`` with no output, and its stream is its
  two opening events: the rest of a running stream cannot be followed from here.
* ``GET /v1/responses/{response_id}/input_items``: the turn's input items, paged by
  ``after``, ``limit`` and ``order``. ``instructions`` is not an input item.

A query value outside what the contract allows is ``400 invalid_request``.

Every reply, failure and input text is redacted again here before it is served
(:func:`_redacted`): the file may not be what this build wrote. A file that is not
the one shape the store writes reads as absent. ``metadata`` goes back as the owner
sent it.

A read removes a record whose retention is over and moves a damaged file aside, so
the two actions are ``.resolve``, not ``.read``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any, Final, TypeVar

from aiohttp import web

from kiro_crew.dashboard.chat_handlers import write_sse_or_abort
from kiro_crew.dashboard.crew_api_auth import OWNER_ONLY_PUBLIC_ROUTES
from kiro_crew.dashboard.crew_api_errors import (
    CREW_API_VERSION,
    REQUEST_ID_HEADER,
    VERSION_HEADER,
    contract_error,
    request_id_for,
)
from kiro_crew.dashboard.crew_api_grant_requests import _host_owner_refusal
from kiro_crew.dashboard.crew_api_owner import owner_route
from kiro_crew.dashboard.crew_api_response_records import (
    STORE_KEY,
    ResponseStore,
    is_response_id,
)
from kiro_crew.dashboard.crew_api_response_stream import encode, opening, rebuilt
from kiro_crew.dashboard.openai_compat import _redact
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

READ_PATH: Final = "/v1/responses/{response_id}"
ITEMS_PATH: Final = "/v1/responses/{response_id}/input_items"

_Built = TypeVar("_Built")

#: The contract's ``Limit`` parameter.
LIMIT_DEFAULT: Final = 20
LIMIT_MAX: Final = 100


class _Invalid(Exception):
    """A query value the contract does not allow."""


class _Unreadable(Exception):
    """The record store could not be read."""


def _unreadable(request: web.Request) -> web.Response:
    return contract_error(
        503,
        "instance_unavailable",
        "the response records cannot be read right now",
        request_id_for(request),
    )


def _bad(request: web.Request, message: str) -> web.Response:
    return contract_error(400, "invalid_request", message, request_id_for(request))


def _missing(request: web.Request) -> web.Response:
    # One answer for an id that never existed and one whose time is up.
    return contract_error(
        404, "not_found", "no such response, or it is no longer kept", request_id_for(request)
    )


def _whole_number(request: web.Request, name: str, low: int, high: int | None) -> int | None:
    raw = request.query.get(name)
    if raw is None:
        return None
    if not raw.isascii() or not raw.isdigit() or len(raw) > 9:
        raise _Invalid(f"{name} must be a whole number")
    value = int(raw)
    if value < low or (high is not None and value > high):
        raise _Invalid(f"{name} is out of range")
    return value


def _flag(request: web.Request, name: str) -> bool:
    raw = request.query.get(name)
    if raw is None or raw == "false":
        return False
    if raw != "true":
        raise _Invalid(f"{name} must be true or false")
    return True


async def _audit(request: web.Request, operation: str, response_id: str, found: bool) -> None:
    caller = str(request.get("user") or "unknown")
    # The id is the caller's text until it has this gateway's shape.
    named = response_id if is_response_id(response_id) else "invalid"

    def write() -> None:
        sel().log_api_access(
            caller=caller,
            operation=operation,
            outcome="allowed",
            source="crew_api",
            resources=f"response={named} found={'yes' if found else 'no'}",
        )

    try:
        await asyncio.to_thread(write)
    except Exception:  # the audit row must never change the outcome
        logger.warning("No audit row written for %s", operation)


async def _find(
    request: web.Request, operation: str, build: Callable[[dict[str, Any]], _Built]
) -> _Built | None:
    """What *build* makes of the turn the path names, or ``None`` when it names none.

    The turn is its record, else the running turn. The record wins: a turn's record
    is written before the turn stops being a running one. *build* receives the turn
    with its texts redacted (:func:`_redacted`) and returns the whole answer, so
    nothing about the record is read after this returns.

    A record no answer can be built from is not served in part: it is moved aside,
    like any file that cannot be believed, and the id reads as absent. Only *build*'s
    own :class:`_Invalid` passes through.
    """
    store: ResponseStore = request.app[STORE_KEY]
    response_id = request.match_info["response_id"]
    # Read before the disk is: a turn that ends during the disk read has left the
    # running turns by the time it returns, and its record was not there yet.
    running = store.live.get(response_id)

    def served() -> _Built | None:
        found = kept = store.load(response_id)
        if found is None and running is not None:
            found = {**running, "stream": None, "running": True}
        if found is None:
            return None
        try:
            return build(_redacted(found))
        except _Invalid:
            raise
        except Exception as exc:
            logger.warning(
                "Crew API response %s could not be served: %s", response_id, type(exc).__name__
            )
            if kept is not None:
                store.set_aside(response_id)
            return None

    try:
        # Off the loop: the disk read, the redaction and the building of an answer of
        # any size.
        built = await asyncio.to_thread(served)
    except _Invalid:
        # The turn was found and read; only the request's own value was wrong.
        await _audit(request, operation, response_id, True)
        raise
    except OSError as exc:
        # Unreadable is not damaged and not absent: the file stays where it is, as
        # another process may only be holding it for a moment, and the next call
        # reads it again. Nothing of it was served.
        logger.warning(
            "Crew API response record could not be read: %s",
            exc.filename or "the records directory",
        )
        raise _Unreadable from None
    await _audit(request, operation, response_id, built is not None)
    return built


def _texts_redacted(node: Any) -> Any:
    """*node* with every ``text`` and ``message`` string in it redacted.

    ``metadata`` is left as it is: the owner's own pairs go back as they were sent,
    as in the turn's answer.
    """
    if isinstance(node, dict):
        return {
            key: (
                value
                if key == "metadata"
                else (
                    _redact(value)
                    if key in ("text", "message") and isinstance(value, str)
                    else _texts_redacted(value)
                )
            )
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_texts_redacted(item) for item in node]
    return node


def _redacted(found: dict[str, Any]) -> dict[str, Any]:
    """What is served of a kept or running turn: every reply, failure and input text
    passes the route's redaction again on its way out.

    The file is not trusted to hold only what this build wrote. The store hands over
    one shape only (``crew_api_response_records._checked``), whose texts are its
    ``text`` and ``message`` fields and its stream's ``deltas``; a file of another
    shape reads as absent. Text the redaction already passed comes out of it
    unchanged, so a turn reads back as it was answered.

    A stream's deltas are judged joined, as the text they carry, since a credential
    can lie across two of them. They are used only when redaction changes nothing in
    them and the reply they carry; otherwise the stream is made from the redacted
    ``Response`` alone.
    """
    response = _texts_redacted(found["response"])
    stream = found["stream"]
    if stream is not None:
        carried = "".join(stream["deltas"])
        if _redact(carried) != carried or response != found["response"]:
            stream = None
    return {
        "response": response,
        "input": _texts_redacted(found["input"]),
        "stream": stream,
        "running": found.get("running", False),
    }


async def _replay(request: web.Request, frames: list[bytes]) -> web.StreamResponse:
    resp = web.StreamResponse()
    resp.content_type = "text/event-stream"
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    # The owner gate adds these after the handler returns, when a stream's headers have left.
    resp.headers[REQUEST_ID_HEADER] = request_id_for(request)
    resp.headers[VERSION_HEADER] = CREW_API_VERSION
    try:
        await resp.prepare(request)
        for frame in frames:
            await write_sse_or_abort(request, resp, frame)
    except ConnectionResetError:
        pass  # the reader left; nothing runs for it
    return resp


async def _read(request: web.Request) -> web.StreamResponse:
    operation = OWNER_ONLY_PUBLIC_ROUTES[READ_PATH]
    denied = await _host_owner_refusal(request, operation)
    if denied is not None:
        return denied
    try:
        stream = _flag(request, "stream")
        after = _whole_number(request, "starting_after", 0, None)
    except _Invalid as invalid:
        return _bad(request, str(invalid))

    def answer(found: dict[str, Any]) -> dict[str, Any] | list[bytes]:
        if not stream:
            return found["response"]
        if found["running"]:
            events = opening(found["response"])
        else:
            events = rebuilt(found["response"], found["stream"])
        # Every frame is made before the stream's headers leave.
        return [
            encode(event) for event in events if after is None or event["sequence_number"] > after
        ]

    try:
        built = await _find(request, operation, answer)
    except _Unreadable:
        return _unreadable(request)
    if built is None:
        return _missing(request)
    if isinstance(built, list):
        return await _replay(request, built)
    return web.json_response(built)


async def _input_items(request: web.Request) -> web.Response:
    operation = OWNER_ONLY_PUBLIC_ROUTES[ITEMS_PATH]
    denied = await _host_owner_refusal(request, operation)
    if denied is not None:
        return denied
    try:
        limit = _whole_number(request, "limit", 1, LIMIT_MAX) or LIMIT_DEFAULT
        order = request.query.get("order", "desc")
        if order not in ("asc", "desc"):
            raise _Invalid("order must be asc or desc")
    except _Invalid as invalid:
        return _bad(request, str(invalid))
    after = request.query.get("after")

    def page_of(found: dict[str, Any]) -> dict[str, Any]:
        items: list[dict[str, Any]] = found["input"]
        if order == "desc":
            items = items[::-1]
        if after is not None:
            ids = [item["id"] for item in items]
            if after not in ids:
                raise _Invalid("after names no input item of this response")
            items = items[ids.index(after) + 1 :]
        page = items[:limit]
        return {
            "object": "list",
            "data": page,
            "first_id": page[0]["id"] if page else None,
            "last_id": page[-1]["id"] if page else None,
            "has_more": len(items) > len(page),
        }

    try:
        built = await _find(request, operation, page_of)
    except _Unreadable:
        return _unreadable(request)
    except _Invalid as invalid:
        return _bad(request, str(invalid))
    if built is None:
        return _missing(request)
    return web.json_response(built)


#: GET /v1/responses/{response_id}. Served by the owner gate, then by the machine-owner
#: check in the handler.
api_response_read = owner_route(_read, OWNER_ONLY_PUBLIC_ROUTES[READ_PATH])

#: GET /v1/responses/{response_id}/input_items. The same two checks.
api_response_input_items = owner_route(_input_items, OWNER_ONLY_PUBLIC_ROUTES[ITEMS_PATH])
