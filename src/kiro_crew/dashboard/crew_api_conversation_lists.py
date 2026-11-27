"""``GET /v1/conversations`` and ``GET /v1/conversations/{conversation_id}/items``: the
two paged reads of the conversations ``crew_api_conversations`` keeps.

What these routes serve:

* The machine owner alone, as the other conversation routes do. A Shared Crew
  teammate, an app credential and an internal caller get ``403 owner_only`` before a
  record is read.
* ``GET /v1/conversations``: every kept conversation of the caller, the ones a turn
  started for itself included, ordered by ``created_at`` and then by id.
* ``GET /v1/conversations/{conversation_id}/items``: what was said in the
  conversation, turn after turn: each kept turn's input items, then its reply. A
  turn that did not complete is listed too, and each of its items reads
  ``status: incomplete``. A turn still running is not listed until it ends. Every
  item is a ``message``: no other kind of item is kept.

Both page by ``after`` (an id of the list), ``limit`` (1 to 100, default 20) and
``order`` (``desc`` by default: newest first). ``after`` is looked up only in the
caller's own list, so an id that is not in it is ``400 invalid_request`` whether or
not it exists elsewhere.

Every text is redacted again here before it is served, as a turn read back is
(``crew_api_response_reads``). ``metadata`` goes back as the owner sent it.
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
    served,
    unavailable,
)
from kiro_crew.dashboard.crew_api_grant_requests import _host_owner_refusal
from kiro_crew.dashboard.crew_api_owner import owner_route
from kiro_crew.dashboard.crew_api_response_reads import (
    LIMIT_DEFAULT,
    LIMIT_MAX,
    _bad,
    _Invalid,
    _texts_redacted,
    _whole_number,
)
from kiro_crew.dashboard.crew_api_response_records import (
    STORE_KEY,
    ResponseStore,
    is_conversation_id,
)

logger = logging.getLogger(__name__)

LIST_PATH: Final = "/v1/conversations"
ITEMS_PATH: Final = "/v1/conversations/{conversation_id}/items"
#: Not a ``.list`` or a ``.read``: a read removes a record whose retention is over and
#: moves a damaged file aside.
LIST_OPERATION: Final = "crew_api.conversations.list.resolve"
ITEMS_OPERATION: Final = OWNER_ONLY_PUBLIC_ROUTES[ITEMS_PATH]


def _paging(request: web.Request) -> tuple[int, bool, str | None]:
    """The request's ``limit``, whether its ``order`` is newest first, and ``after``."""
    limit = _whole_number(request, "limit", 1, LIMIT_MAX) or LIMIT_DEFAULT
    order = request.query.get("order", "desc")
    if order not in ("asc", "desc"):
        raise _Invalid("order must be asc or desc")
    return limit, order == "desc", request.query.get("after")


def _page(
    rows: list[dict[str, Any]], limit: int, newest_first: bool, after: str | None
) -> dict[str, Any]:
    """One page of *rows*, which are oldest first, as the contract's list envelope."""
    if newest_first:
        rows = rows[::-1]
    if after is not None:
        ids = [row["id"] for row in rows]
        if after not in ids:
            raise _Invalid("after is not an id of this list")
        rows = rows[ids.index(after) + 1 :]
    page = rows[:limit]
    return {
        "object": "list",
        "data": page,
        "first_id": page[0]["id"] if page else None,
        "last_id": page[-1]["id"] if page else None,
        "has_more": len(rows) > len(page),
    }


async def _list(request: web.Request) -> web.Response:
    denied = await _host_owner_refusal(request, LIST_OPERATION)
    if denied is not None:
        return denied
    try:
        limit, newest_first, after = _paging(request)
    except _Invalid as invalid:
        return _bad(request, str(invalid))
    store: ConversationStore = request.app[CONVERSATIONS_KEY]
    try:
        records = await asyncio.to_thread(store.listed, OWNER)
    except OSError:
        return unavailable(request)
    await audit(request, LIST_OPERATION, f"conversations={len(records)}")
    rows = [served(record, store.is_busy(record["id"])) for record in records]
    try:
        return web.json_response(_page(rows, limit, newest_first, after))
    except _Invalid as invalid:
        return _bad(request, str(invalid))


def _items(
    conversations: ConversationStore, responses: ResponseStore, conversation_id: str
) -> list[dict[str, Any]] | None:
    """The items of the owner's conversation, oldest first; ``None`` when it names
    none. Blocks: it reads the conversation's record and each listed turn's.

    Only the turns the conversation's own record lists are read, and each one's
    record must name this conversation, as for a turn that continues it. A listed
    turn whose record is gone, or names another conversation, is left out and
    logged: what is kept of the others is still served.
    """
    record = conversations.load(conversation_id, OWNER)
    if record is None:
        return None
    items: list[dict[str, Any]] = []
    # shortcut: every listed turn's record is read for each page; read only up to the
    # page's end when conversations grow to hundreds of turns.
    for response_id in record["turns"]:
        kept = responses.load(response_id)
        if kept is None or kept["response"].get("conversation") != {"id": conversation_id}:
            logger.warning(
                "Crew API conversation %s lists response %s, which cannot be read as its turn",
                conversation_id,
                response_id,
            )
            continue
        completed = kept["response"]["status"] == "completed"
        for item in [*kept["input"], *kept["response"]["output"]]:
            # The contract has no field for the turn's own status on an item: an item
            # of a turn that did not complete reads incomplete.
            items.append(item if completed else {**item, "status": "incomplete"})
    redacted: list[dict[str, Any]] = _texts_redacted(items)
    return redacted


async def _list_items(request: web.Request) -> web.Response:
    denied = await _host_owner_refusal(request, ITEMS_OPERATION)
    if denied is not None:
        return denied
    try:
        limit, newest_first, after = _paging(request)
    except _Invalid as invalid:
        return _bad(request, str(invalid))
    conversations: ConversationStore = request.app[CONVERSATIONS_KEY]
    responses: ResponseStore = request.app[STORE_KEY]
    conversation_id = request.match_info["conversation_id"]
    try:
        # Off the loop: the disk reads and the redaction of every text.
        items = await asyncio.to_thread(_items, conversations, responses, conversation_id)
    except OSError:
        return unavailable(request)
    named = conversation_id if is_conversation_id(conversation_id) else "invalid"
    found = "no" if items is None else "yes"
    await audit(request, ITEMS_OPERATION, f"conversation={named} found={found}")
    if items is None:
        return missing(request)
    try:
        return web.json_response(_page(items, limit, newest_first, after))
    except _Invalid as invalid:
        return _bad(request, str(invalid))


#: GET /v1/conversations. Served by the owner gate, then by the machine-owner check in
#: the handler.
api_conversation_list = owner_route(_list, LIST_OPERATION)

#: GET /v1/conversations/{conversation_id}/items. The same two checks.
api_conversation_items = owner_route(_list_items, ITEMS_OPERATION)
