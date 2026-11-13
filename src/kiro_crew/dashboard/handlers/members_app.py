"""What an app token sees of the Bots: a trimmed roster, the goals, and a wake.

An app reaches these only through ``members_access._app_bot_gate`` (the scope it
declared and the owner approved, and the Bots the owner ticked for it; see
``apps.bot_grants``). Each answer is built here from its own allowlist, never by
narrowing the owner's response: the owner's roster row carries the DM slot key, the
last line spoken and the memory store, and none of that is the app's.

The shapes follow the Crew API draft's ``Bot``, ``BotList`` and ``BotGoals``
(``/v1/crew/bots``), so the SDK hooks keep their surface when they move onto that
route. Where the draft has no field for what the gateway knows (``status``) the
field is added here; where the gateway has a different name (``readings``) the
contract's name is used.

Routes, all behind the gate:

- ``GET /api/members`` with ``bots: read``: the ticked Bots, in the owner's order.
- ``GET /api/members/{slug}/goals`` with ``bots: read``: that Bot's goals and key results.
- ``POST /api/members/{slug}/wake`` with ``bots: wake``: ``dashboard.app_bot_wake``.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from aiohttp import web

from kiro_crew import member_goals
from kiro_crew import members as members_mod
from kiro_crew.apps.bot_grants import BOT_SCOPE_READ, BOT_SCOPE_WAKE
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers.members_access import (
    _app_bot_gate,
    _member_name_is_addressable,
    _member_names_for_slug,
    _not_found,
)
from kiro_crew.members import MemberSlugError

#: Bot ``status``: a turn is running in its thread.
STATUS_BUSY = "busy"
#: Bot ``status``: no turn is running.
STATUS_IDLE = "idle"

#: What a wake's ``ref`` may hold: an opaque id the app minted, never prose. Its
#: charset keeps the wake line the gateway writes free of anything an app could
#: phrase as an instruction.
WAKE_REF_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}\Z")


def _bot_row(slug: str, name: str, agent_cfg: Any, has_goal: bool) -> dict:
    """One ``Bot``: an explicit allowlist, every free-text value masked like the roster's."""
    # The roster's redaction chokepoint, through the agents handlers' facade.
    from kiro_crew.dashboard.handlers.agents import _roster_mask

    display = _roster_mask(getattr(agent_cfg, "display_name", ""))
    return {
        "id": slug,
        "object": "bot",
        "name": display or name,
        "description": _roster_mask(getattr(agent_cfg, "description", "")),
        "avatar_url": None,
        "status": STATUS_IDLE,
        "has_goal": has_goal,
    }


def _resolve(cfg: KiroCrewConfig, slug: str) -> str | None:
    """The one addressable Bot name *slug* belongs to, or None (absent or ambiguous)."""
    names = _member_names_for_slug(cfg, slug)
    return names[0] if len(names) == 1 else None


def _thread_key(slug: str, name: str) -> str:
    """*name*'s DM thread slot key, or ``""`` when it has none. File IO: off the loop.

    The binding check of ``members_roster.read_dm_thread_message_state``, which this
    owner cannot import: members_roster loads after it and imports it.
    """
    binding = members_mod.read_dm_binding(slug)
    if not binding or binding.get("member") != name:
        return ""
    return str(binding.get("slot_key", "") or "")


def _roster(ticked: tuple[str, ...]) -> list[tuple[dict, str]]:
    """Each ticked Bot's row (status still idle) and its thread key. File IO: off the loop."""
    cfg = KiroCrewConfig.load()
    by_slug: dict[str, tuple[str, Any]] = {}
    for name, agent_cfg in cfg.agents.items():
        if not _member_name_is_addressable(name):
            continue
        try:
            slug = members_mod.member_slug(name, cfg)
        except MemberSlugError:
            continue
        if slug in ticked and slug not in by_slug and _resolve(cfg, slug) == name:
            by_slug[slug] = (name, agent_cfg)
    rows: list[tuple[dict, str]] = []
    for slug in ticked:
        if slug not in by_slug:
            continue
        name, agent_cfg = by_slug[slug]
        try:
            has_goal = member_goals.has_goal(member_goals.read_goals(slug, name))
        except member_goals.GoalsUnreadable:
            has_goal = False
        rows.append((_bot_row(slug, name, agent_cfg, has_goal), _thread_key(slug, name)))
    return rows


async def app_bot_list(request: web.Request) -> web.Response:
    """``GET /api/members`` for an app token: the ``BotList`` of the ticked Bots."""
    gate = await _app_bot_gate(request, BOT_SCOPE_READ, "members.list")
    if isinstance(gate, web.Response):
        return gate
    _app, ticked = gate
    state = request.app.get("state")
    rows: list[dict] = []
    for row, slot_key in await asyncio.to_thread(_roster, ticked):
        # Live slot state is read on the loop, like the owner's roster does.
        slot = state._slots.get(slot_key) if (state is not None and slot_key) else None
        if slot is not None and slot.running:
            row["status"] = STATUS_BUSY
        rows.append(row)
    return web.json_response(
        {
            "object": "list",
            "data": rows,
            "first_id": rows[0]["id"] if rows else None,
            "last_id": rows[-1]["id"] if rows else None,
            "has_more": False,
        }
    )


def _goals(slug: str) -> dict | None:
    cfg = KiroCrewConfig.load()
    name = _resolve(cfg, slug)
    if name is None:
        return None
    rec = member_goals.read_goals(slug, name)
    view = member_goals.public_view(rec)
    return {
        "bot": slug,
        "goals": [{"id": g["id"], "number": g["number"], "text": g["text"]} for g in view["goals"]],
        "key_results": [
            {
                "id": k["id"],
                "text": k["text"],
                "target": k["target"],
                "current": k.get("current") or None,
                "updated_at": int(k["updated_at"]) if k.get("updated_at") else None,
                "goal_ids": list(k.get("goal_ids") or []),
                "history": [
                    {"at": int(r["at"]), "value": r["value"]} for r in k.get("readings") or []
                ],
            }
            for k in view["key_results"]
        ],
    }


async def app_bot_goals(request: web.Request) -> web.Response:
    """``GET /api/members/{slug}/goals`` for an app token: the Bot's ``BotGoals``."""
    slug = request.match_info.get("slug", "")
    gate = await _app_bot_gate(request, BOT_SCOPE_READ, "members.goals", slug)
    if isinstance(gate, web.Response):
        return gate
    try:
        members_mod.validate_slug(slug)
        body = await asyncio.to_thread(_goals, slug)
    except MemberSlugError:
        return _not_found()
    except member_goals.GoalsUnreadable:
        return web.json_response(
            {"error": "this Bot's goal record cannot be read", "code": "goals_unreadable"},
            status=500,
        )
    return _not_found() if body is None else web.json_response(body)


def _bad(code: str, message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=status)


async def api_member_wake(request: web.Request) -> web.Response:
    """``POST /api/members/{slug}/wake`` -- body ``{"ref": "<id>"}`` and nothing else.

    App tokens only: a dashboard user talks to the Bot in its thread. Any other body
    key is refused, so no text an app writes reaches the Bot; the line it reads is
    the gateway's (``app_bot_wake.wake_prompt``).
    """
    from kiro_crew.dashboard import app_bot_wake, create_rate_limit

    retry_after = {"Retry-After": str(int(create_rate_limit.WINDOW_SECS))}
    slug = request.match_info.get("slug", "")
    if not request.get("app", ""):
        # A dashboard session, the owner's included, never wakes a Bot here: it
        # talks to the Bot in its thread. Refused like every gated write.
        return _bad("app_token_required", "only an app token may wake a Bot here", status=403)
    gate = await _app_bot_gate(request, BOT_SCOPE_WAKE, "members.wake", slug)
    if isinstance(gate, web.Response):
        return gate
    app_name, _ticked = gate
    try:
        body = await request.json()
    except Exception:
        return _bad("invalid_json", "invalid JSON body")
    if not isinstance(body, dict):
        return _bad("invalid_json", "invalid JSON body")
    extra = sorted(k for k in body if k != "ref")
    if extra:
        return _bad("unexpected_field", "a wake carries only 'ref'; refused: " + ", ".join(extra))
    ref = body.get("ref")
    if not isinstance(ref, str) or not WAKE_REF_RE.match(ref):
        return _bad("invalid_ref", "ref must be 1-64 characters of A-Z a-z 0-9 _ . : -")
    try:
        members_mod.validate_slug(slug)
    except MemberSlugError:
        return _not_found()
    state = request.app.get("state")
    outcome = await app_bot_wake.request_wake(state, app_name, slug, ref)
    if outcome == app_bot_wake.RATE_LIMITED:
        return web.json_response(
            {"error": "too many wakes for this Bot; try again later", "code": "rate_limited"},
            status=429,
            headers=retry_after,
        )
    if outcome == app_bot_wake.QUEUE_FULL:
        return web.json_response(
            {
                "error": "this Bot already holds the most wakes it keeps from this app",
                "code": "wake_queue_full",
            },
            status=429,
            headers=retry_after,
        )
    if outcome == app_bot_wake.NO_BOT:
        return _not_found()
    if outcome == app_bot_wake.NO_THREAD:
        return _bad("bot_unavailable", "this Bot cannot be woken right now", status=409)
    return web.json_response({"status": outcome}, status=202)


__all__ = [
    "STATUS_BUSY",
    "STATUS_IDLE",
    "WAKE_REF_RE",
    "api_member_wake",
    "app_bot_goals",
    "app_bot_list",
]
