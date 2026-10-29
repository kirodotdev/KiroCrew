"""Chat message reaction handlers — CRUD for per-message emoji reactions.

A fixed, small set of one-click emoji reactions on individual messages (both
user and assistant), prioritised for speed over expressiveness — the same
scope ruling ``chat_pins.py`` makes for pins. Reaction identity is
``(slot_key, mid, emoji)``: one caller can hold at most one reaction record
per message per emoji, and toggling the same emoji again removes it. Unlike
pins, there is no preview text to redact and no ordering to preserve, so the
list route returns every reaction for a slot with no pagination.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone

from aiohttp import web

# Reuse the pins authorization helper verbatim rather than copy it: the two
# routes need identical app-slot isolation, and a second definition would be a
# place the two could drift. Its default ``missing_code=None`` yields exactly
# the ``slot_not_found()`` behaviour reactions needs.
from kiro_crew.dashboard.chat_pins import _authorize_app_slot
from kiro_crew.dashboard.handlers._shared import read_bounded_json
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

# slot_key is retained in every record, so its length is capped like every
# other retained field; with the mid cap and the fixed emoji set, that bounds
# the size of each record. It does not bound how many records the store holds:
# the per-slot cap limits records per slot_key, but a dashboard caller can
# react under any slot_key and nothing removes a deleted session's reactions,
# so the number of distinct slot_keys can still grow (chat pins share this).
_MAX_SLOT_KEY_CHARS = 256
_MAX_MID_CHARS = 128
_MAX_REACTIONS_PER_SLOT = 500
# Fixed set, prioritised for speed over expressiveness: a full emoji picker
# is out of scope.
_VALID_EMOJI = frozenset({"👍", "✅", "👀", "⭐"})


async def api_chat_reactions_list(request: web.Request) -> web.Response:
    """GET /api/chat/reactions?slot=<slot_key> — list reactions for a slot."""
    state: DashboardState = request.app["state"]
    slot_key = request.query.get("slot", "")
    if not slot_key:
        return web.json_response(
            {"error": "slot query param required", "code": "missing_query_params"},
            status=400,
        )
    denied = _authorize_app_slot(request, state, slot_key, "chat.reactions_list")
    if denied is not None:
        return denied
    await state.ensure_chat_reactions_loaded()
    request_app = request.get("app", "")
    # Record-level app ownership mirrors chat_pins, and visibility is scoped to
    # the caller's OWN origin in both directions: an app caller sees only the
    # reactions it created, and a dashboard caller (origin_app="") sees only
    # dashboard records — never an app's. Create, quota and delete all key off
    # the same (slot_key, mid, emoji, origin_app) identity; if the list instead
    # showed a dashboard caller an app-owned duplicate of the same emoji, the
    # dashboard's single toggle would DELETE only its own record and the app's
    # copy would survive and reappear on the next fetch.
    reactions = [
        r
        for r in state._chat_reactions
        if r["slot_key"] == slot_key and r.get("origin_app", "") == request_app
    ]
    reactions.sort(key=lambda r: r.get("reacted_at", ""))
    return web.json_response({"reactions": reactions})


async def api_chat_reactions_create(request: web.Request) -> web.Response:
    """POST /api/chat/reactions — add a reaction to a chat message.

    Idempotent on ``(slot_key, mid, emoji, origin_app)``: reacting again with
    the same emoji returns the existing record rather than creating a
    duplicate. Toggling off is a separate DELETE call (see
    ``api_chat_reactions_delete_by_query``) — the client decides add vs.
    remove from its own optimistic state, same split as pin/unpin.
    """
    state: DashboardState = request.app["state"]
    body, body_error = await read_bounded_json(request)
    if body_error is not None:
        if body_error.status == 400 and body_error.text:
            try:
                error_body = json.loads(body_error.text)
            except ValueError:
                error_body = {}
            if error_body.get("code") == "body_not_object":
                return web.json_response(
                    {"error": "request body must be a JSON object", "code": "invalid_json"},
                    status=400,
                )
        return body_error
    assert body is not None

    def _str_field(key: str, default: str = "") -> str:
        value = body.get(key, default)
        return value.strip() if isinstance(value, str) else ""

    slot_key = _str_field("slot_key")
    mid = _str_field("mid")
    emoji = _str_field("emoji")
    if not slot_key or not mid:
        return web.json_response(
            {"error": "slot_key and mid are required", "code": "missing_required_fields"},
            status=400,
        )
    if len(slot_key) > _MAX_SLOT_KEY_CHARS:
        return web.json_response(
            {
                "error": f"slot_key exceeds {_MAX_SLOT_KEY_CHARS} characters",
                "code": "slot_key_too_large",
            },
            status=400,
        )
    if len(mid) > _MAX_MID_CHARS:
        return web.json_response(
            {"error": f"mid exceeds {_MAX_MID_CHARS} characters", "code": "mid_too_large"},
            status=400,
        )
    if emoji not in _VALID_EMOJI:
        return web.json_response(
            {"error": "emoji must be one of the fixed reaction set", "code": "invalid_emoji"},
            status=400,
        )
    denied = _authorize_app_slot(request, state, slot_key, "chat.reactions_create")
    if denied is not None:
        return denied

    await state.ensure_chat_reactions_loaded()
    request_app = request.get("app", "")

    async with state._chat_reactions_lock:
        denied_inner = _authorize_app_slot(request, state, slot_key, "chat.reactions_create")
        if denied_inner is not None:
            return denied_inner

        existing = next(
            (
                r
                for r in state._chat_reactions
                if r["slot_key"] == slot_key
                and r.get("mid") == mid
                and r.get("emoji") == emoji
                and r.get("origin_app", "") == request_app
            ),
            None,
        )
        if existing:
            return web.json_response(existing, status=200)

        slot_reaction_count = sum(
            1
            for r in state._chat_reactions
            if r["slot_key"] == slot_key and r.get("origin_app", "") == request_app
        )
        if slot_reaction_count >= _MAX_REACTIONS_PER_SLOT:
            return web.json_response(
                {
                    "error": f"slot already has {_MAX_REACTIONS_PER_SLOT} reactions",
                    "code": "reaction_limit_reached",
                    # The client states the number in its notice, so it reads
                    # it from here rather than keeping its own copy of the cap.
                    "limit": _MAX_REACTIONS_PER_SLOT,
                },
                status=409,
            )

        reaction = {
            "id": uuid.uuid4().hex[:12],
            "slot_key": slot_key,
            "mid": mid,
            "emoji": emoji,
            "reacted_at": datetime.now(timezone.utc).isoformat(),
            "origin_app": request_app,
        }
        # Persist before publish: build the next list, write it, and only then
        # swap it into the shared state. A reader holding no lock never sees the
        # appended record before its write has landed, and a failed write leaves
        # the live list untouched rather than rolling back a visible mutation.
        staged = [*state._chat_reactions, reaction]
        try:
            await asyncio.to_thread(state.save_chat_reactions, staged)
        except Exception:
            logger.warning("chat_reactions: failed to persist reaction", exc_info=True)
            return web.json_response(
                {"error": "failed to persist reaction", "code": "persist_failed"}, status=500
            )
        state._chat_reactions = staged
    # Broadcast after the lock is released: slot_key only, no reaction content,
    # same minimal-payload rule as pins_changed.
    state.broadcast_ws_owners("reactions_changed", {"slot_key": slot_key})
    return web.json_response(reaction, status=201)


async def api_chat_reactions_delete_by_query(request: web.Request) -> web.Response:
    """DELETE /api/chat/reactions?slot=<slot_key>&mid=<mid>&emoji=<emoji> — remove a reaction."""
    state: DashboardState = request.app["state"]
    slot_key = request.query.get("slot", "")
    mid = request.query.get("mid", "")
    emoji = request.query.get("emoji", "")
    if not slot_key or not mid or not emoji:
        return web.json_response(
            {
                "error": "slot, mid and emoji query params required",
                "code": "missing_query_params",
            },
            status=400,
        )
    denied = _authorize_app_slot(request, state, slot_key, "chat.reactions_delete")
    if denied is not None:
        return denied
    await state.ensure_chat_reactions_loaded()
    request_app = request.get("app", "")

    def _matches_identity(reaction: dict) -> bool:
        return (
            reaction["slot_key"] == slot_key
            and reaction.get("mid") == mid
            and reaction.get("emoji") == emoji
        )

    async with state._chat_reactions_lock:
        # Scope the match to the caller's OWN origin, same identity the list and
        # create routes use. A dashboard caller (origin_app="") matches only
        # dashboard records, not an app's copy of the same emoji — the list
        # never showed the dashboard that copy, so the toggle must not delete
        # it. This keeps visibility and deletion on one consistent identity, so
        # a removed reaction cannot reappear from a surviving foreign-origin
        # duplicate on the next fetch.
        idx = next(
            (
                i
                for i, reaction in enumerate(state._chat_reactions)
                if _matches_identity(reaction) and reaction.get("origin_app", "") == request_app
            ),
            None,
        )
        if idx is None:
            if request_app and any(_matches_identity(r) for r in state._chat_reactions):
                sel().log_api_access(
                    caller=request_app,
                    operation="chat.reactions_delete",
                    outcome="denied",
                    source="reaction_record_ownership",
                    resources=f"slot={slot_key},mid={mid},emoji={emoji}",
                    error="app does not own this reaction record",
                )
            return web.json_response(
                {"error": "not found", "code": "reaction_not_found"}, status=404
            )
        # Persist before publish: stage the list without the removed record,
        # write it, then swap. A failed write leaves the live list intact, and
        # an unlocked reader never sees the row vanish before it is gone on disk.
        staged = [r for i, r in enumerate(state._chat_reactions) if i != idx]
        try:
            await asyncio.to_thread(state.save_chat_reactions, staged)
        except Exception:
            logger.warning("chat_reactions: failed to persist removal", exc_info=True)
            return web.json_response(
                {"error": "failed to persist reaction", "code": "persist_failed"}, status=500
            )
        state._chat_reactions = staged
    state.broadcast_ws_owners("reactions_changed", {"slot_key": slot_key})
    return web.json_response({"ok": True})
