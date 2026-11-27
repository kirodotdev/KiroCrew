"""HTTP routes behind a bot's own memory tools on ``kirocrew-mate``.

* ``POST /api/member/self/memory/list`` (``memory_list``): one page of the calling
  bot's own facts or experiences, with ids, dates, when recall last used an
  experience, and confidence or importance. A read, admitted on any turn in the
  bot's own dashboard thread, like ``goal_read``.
* ``propose_memory_changes`` goes through ``POST /api/member/self/propose``
  (:mod:`handlers.member_self`) with request ``memory``: :func:`memory_params` turns
  the bot's changes into a ``crewmate.memory`` card (:mod:`kiro_crew.change_card_memory`)
  for its own store. Only a turn the user sent may raise it, and nothing is written
  until the user presses the card.

Both act on the store the calling bot's configuration binds, and only while that is
the store its thread runs on: a bot deleted and created again under the same name
has a new store, and this turn's is not it. Both sit under the strict
``/api/member/self`` prefix, which only the gateway's own MCP servers can reach.
"""

from __future__ import annotations

import asyncio
from typing import Any

from aiohttp import web

from kiro_crew.change_card_catalog_kinds import CardCatalogError
from kiro_crew.dashboard.guide_runs import GuideError

#: ``memory_list`` operation name. Not in ``handlers.guide._USER_TURN_OPERATIONS``:
#: a read of the bot's own memory, like ``memory_recall``.
OP_MEMORY_LIST = "member.memory_list"


def _deny(status: int, code: str, message: str) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=status)


def _refusal_for(exc: CardCatalogError) -> web.Response:
    status = 403 if exc.code in ("not_own_store", "shared_memory") else 400
    if exc.code == "store_unavailable":
        status = 503
    return _deny(status, exc.code, exc.message)


async def _store_for_thread(member: str, thread_store: str) -> str:
    """*member*'s own store, checked to be the one this thread runs on."""
    from kiro_crew.dashboard.member_memory_records import own_store

    store = await asyncio.to_thread(own_store, member)
    if store != thread_store:
        raise CardCatalogError("not_own_store", "this chat is not the bot's own thread")
    return store


async def memory_params(
    sk: str, member: str, thread_store: str, raw: dict[str, Any]
) -> tuple[web.Response | None, dict[str, Any]]:
    """``(refusal, params)`` for a ``crewmate.memory`` card the bot asked for."""
    from kiro_crew.dashboard.handlers.guide import _audit
    from kiro_crew.dashboard.handlers.member_self import OP_PROPOSE_SELF

    if set(raw) != {"changes"}:
        _audit(sk, OP_PROPOSE_SELF, "denied", "invalid_params")
        return _deny(400, "invalid_params", "'memory' takes only changes"), {}
    try:
        store = await _store_for_thread(member, thread_store)
    except CardCatalogError as exc:
        _audit(sk, OP_PROPOSE_SELF, "denied", exc.code)
        return _refusal_for(exc), {}
    return None, {"member": member, "store": store, "changes": raw.get("changes")}


def _int(body: dict[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    value = body.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise CardCatalogError("invalid_param", f"'{key}' must be an integer from {lo} to {hi}")
    return value


async def api_member_self_memory_list(request: web.Request) -> web.Response:
    """POST /api/member/self/memory/list — ``{kind, query?, sort?, offset?, limit?}``."""
    from kiro_crew import memory_edit
    from kiro_crew._sqlite_compat import sqlite3
    from kiro_crew.dashboard.handlers.guide import (
        _audit,
        _json_body,
        _refusal,
        _resolve_agent_caller,
    )
    from kiro_crew.dashboard.handlers.member_self import _caller_member
    from kiro_crew.dashboard.member_memory_records import LIST_PAGE_MAX, list_own
    from kiro_crew.validation import LESSON_LIST_OFFSET_MAX

    try:
        slot_key, sk = await _resolve_agent_caller(request, OP_MEMORY_LIST)
        body = await _json_body(request)
    except GuideError as exc:
        return _refusal(exc)
    who = _caller_member(request, slot_key)
    if who is None:
        _audit(sk, OP_MEMORY_LIST, "denied", "not a Bot thread")
        return _deny(403, "not_a_crewmate", "only a bot, in its own chat, can list its memory")
    member, thread_store = who
    query = body.get("query", "")
    try:
        if not isinstance(query, str) or len(query) > 200:
            raise CardCatalogError("invalid_param", "'query' is at most 200 characters")
        await _store_for_thread(member, thread_store)
        result = await list_own(
            request.app["state"],
            member,
            kind=str(body.get("kind") or ""),
            query=query,
            sort=str(body.get("sort") or "updated"),
            offset=_int(body, "offset", 0, 0, LESSON_LIST_OFFSET_MAX),
            limit=_int(body, "limit", 25, 1, LIST_PAGE_MAX),
        )
    except CardCatalogError as exc:
        _audit(sk, OP_MEMORY_LIST, "denied", exc.code)
        return _refusal_for(exc)
    except memory_edit.MemoryEditError as exc:
        _audit(sk, OP_MEMORY_LIST, "denied", exc.code)
        return _deny(exc.status, exc.code, str(exc))
    except (OSError, sqlite3.Error):
        return _deny(503, "store_unavailable", "this bot's memory store is unavailable")
    _audit(sk, OP_MEMORY_LIST, "ok", str(len(result["entries"])))
    return web.json_response({"ok": True, **result})
