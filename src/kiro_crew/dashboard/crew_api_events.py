"""``GET /v1/crew/events``: the event stream of one conversation, as paged JSON.

What this route serves so far:

* The machine owner alone, as the conversation routes do. A Shared Crew teammate, an
  app credential and an internal caller get ``403 owner_only`` before anything is
  read and before any wait begins.
* The conversation stream: ``?conversation=conv_...`` is required. The instance
  stream (no ``conversation``) and the SSE form (``Accept: text/event-stream``) are
  ``422 capability_unavailable``. The ``bot`` parameter is not read.
* The events ``turn.started``, ``message.created`` and ``turn.completed`` of the
  turns ``POST /v1/responses`` runs in the conversation (``crew_api_responses``). An
  event holds ids and a result word, never a message's text: the texts are read from
  ``GET /v1/conversations/{conversation_id}/items``.
* ``since=c``: the events with ``seq > c``, at most ``limit`` of them (0 to
  :data:`LIMIT_MAX`, default :data:`LIMIT_DEFAULT`). Without ``since``: only events
  from now on. ``410 resync_required`` for a cursor past the newest event, for one
  below an event that could not be written, and for any cursor while the stream is
  broken (``crew_api_event_records``).
* ``wait=n`` (0 to :data:`WAIT_MAX_SECS`): when there is nothing to answer yet, the
  request stays open until the conversation's next event or for ``n`` seconds, and
  then answers what there is. At most :data:`MAX_WAITING` requests wait at once; one
  more is ``429 concurrency_limit`` at once. Closing the gateway ends every wait.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Final

from aiohttp import web

from kiro_crew.dashboard.crew_api_auth import OWNER_ONLY_PUBLIC_ROUTES
from kiro_crew.dashboard.crew_api_conversations import (
    CONVERSATIONS_KEY,
    OWNER,
    ConversationStore,
    audit,
    missing,
    unavailable,
)
from kiro_crew.dashboard.crew_api_errors import contract_error, request_id_for
from kiro_crew.dashboard.crew_api_event_records import Resync
from kiro_crew.dashboard.crew_api_grant_requests import _host_owner_refusal
from kiro_crew.dashboard.crew_api_owner import owner_route
from kiro_crew.dashboard.crew_api_response_reads import _Invalid, _whole_number
from kiro_crew.dashboard.crew_api_response_records import is_conversation_id

logger = logging.getLogger(__name__)

PATH: Final = "/v1/crew/events"
#: Not a ``.read``: a read removes a conversation whose retention is over and moves
#: a damaged file aside.
OPERATION: Final = OWNER_ONLY_PUBLIC_ROUTES[PATH]

#: The contract's page sizes and its longest wait.
LIMIT_MAX: Final = 1000
LIMIT_DEFAULT: Final = 100
WAIT_MAX_SECS: Final = 30
#: The most requests that wait for an event at once. A proposal: the contract's own
#: proposed ceiling of event streams for one user, 10. The owner is the one caller.
MAX_WAITING: Final = 10


def _unavailable_part(request: web.Request, what: str) -> web.Response:
    return contract_error(
        422,
        "capability_unavailable",
        f"{what} is not available on this Kiro Crew yet",
        request_id_for(request),
    )


def _read(
    store: ConversationStore, conversation_id: str, since: int | None, limit: int
) -> dict[str, Any] | None:
    """The page of the owner's conversation, or ``None`` when it names none. Blocks."""
    if store.load(conversation_id, OWNER) is None:
        return None
    return store.events.events_after(conversation_id, since, limit)


async def _events(request: web.Request) -> web.Response:
    denied = await _host_owner_refusal(request, OPERATION)
    if denied is not None:
        return denied
    if "text/event-stream" in request.headers.get("Accept", ""):
        return _unavailable_part(request, "the event stream as server-sent events")
    conversation_id = request.query.get("conversation")
    if conversation_id is None:
        return _unavailable_part(request, "the instance stream")
    try:
        since = _whole_number(request, "since", 0, None)
        limit = _whole_number(request, "limit", 0, LIMIT_MAX)
        wait = _whole_number(request, "wait", 0, WAIT_MAX_SECS) or 0
    except _Invalid as invalid:
        return contract_error(400, "invalid_request", str(invalid), request_id_for(request))
    limit = LIMIT_DEFAULT if limit is None else limit
    store: ConversationStore = request.app[CONVERSATIONS_KEY]
    if not is_conversation_id(conversation_id):
        await audit(request, OPERATION, "conversation=invalid found=no")
        return missing(request)
    log = store.events
    # Listening before the read: an event written between the two ends the wait.
    heard = log.listen(conversation_id)
    try:
        page = await asyncio.to_thread(_read, store, conversation_id, since, limit)
        await audit(
            request, OPERATION, f"conversation={conversation_id} found={'yes' if page else 'no'}"
        )
        if page is None:
            return missing(request)
        if wait and not page["events"] and limit:
            if log.waits >= MAX_WAITING:
                return contract_error(
                    429,
                    "concurrency_limit",
                    "too many requests are waiting for an event",
                    request_id_for(request),
                    headers={"Retry-After": "1"},
                    extra={"limit": "event_streams"},
                )
            # A request with no cursor waits for what comes after this moment.
            cursor = page["latest_seq"] if since is None else since
            log.waits += 1
            try:
                await asyncio.wait_for(asyncio.shield(heard), wait)
            except asyncio.TimeoutError:
                pass
            finally:
                log.waits -= 1
            page = await asyncio.to_thread(_read, store, conversation_id, cursor, limit)
            if page is None:
                return missing(request)
    except Resync as resync:
        return contract_error(
            410,
            "resync_required",
            "the cursor cannot be continued from; read the current state, then "
            "continue from latest_seq",
            request_id_for(request),
            extra={"oldest_seq": resync.oldest_seq, "latest_seq": resync.latest_seq},
        )
    except OSError as exc:
        logger.warning("Crew API event stream was not read: %s", type(exc).__name__)
        return unavailable(request)
    finally:
        log.unlisten(conversation_id, heard)
    return web.json_response(page)


async def close_event_waits(app: web.Application) -> None:
    """An ``on_shutdown`` step: end every wait, so no request holds the gateway open."""
    store: ConversationStore = app[CONVERSATIONS_KEY]
    store.events.close()


#: GET /v1/crew/events. Served by the owner gate, then by the machine-owner check in
#: the handler.
api_events = owner_route(_events, OPERATION)
