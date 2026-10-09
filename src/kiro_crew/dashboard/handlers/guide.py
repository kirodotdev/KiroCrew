"""HTTP routes behind the ``kirocrew-guide`` MCP server.

The agent half (``/api/guide/agent/*``) is MCP-only and strict-internal: the
whole prefix sits in ``server._STRICT_INTERNAL_API_PATHS``, and every handler
re-asserts ``internal_auth`` itself because a ``local_only=False`` deployment
reclassifies strict paths as mixed. The caller's slot is derived SOLELY from the
verified ``X-Session-Key`` by walking the LIVE slot table
(``session_control.caller_slot_key``) -- a surface-registry hit is not proof a
tab is open, and no request field can name a slot. App callers, subagents,
unattended (cron/workflow) tabs, channel turns and a key with no live slot are
refused (:func:`_resolve_agent_caller`).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from kiro_crew.sel import sel

logger = logging.getLogger(__name__)


class GuideError(Exception):
    """A refusal with an HTTP status and a stable machine code."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _audit(caller: str, operation: str, outcome: str, error: str = "") -> None:
    try:
        sel().log_api_access(
            caller=caller or "unknown",
            operation=operation,
            outcome=outcome,
            source="dashboard",
            resources="/api/guide",
            error=error,
        )
    except Exception:  # pragma: no cover - an audit must never change the outcome
        logger.debug("SEL audit for %s failed", operation, exc_info=True)


def _refusal(exc: GuideError) -> web.Response:
    status = exc.status if exc.status in (400, 403, 404, 409, 429) else 400
    return web.json_response({"error": exc.message, "code": exc.code}, status=status)


def _deny(status: int, code: str, message: str) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=status)


async def _json_body(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        raise GuideError(400, "invalid_json", "invalid JSON body") from None
    if not isinstance(body, dict):
        raise GuideError(400, "invalid_body", "body must be a JSON object")
    return body


#: Operations that change what the person sees on their word: only a turn they
#: sent may. ``member.rename_self`` is a crewmate taking the name the user gave
#: it, so a wake or the hidden welcome kickoff cannot pick one.
_USER_TURN_OPERATIONS = frozenset({"member.rename_self"})


def _resolve_agent_caller(request: web.Request, operation: str) -> tuple[str, str]:
    """The verified caller's ``(slot_key, session_key)``. Raises :class:`GuideError`.

    Admits only a tool call made by a turn running in the caller's dashboard
    slot that did not come from a messaging channel. A channel-born
    conversation is mirrored into a dashboard slot, so finding a live slot
    proves nothing about who is asking; the turn's own opener provenance
    (``_turn_channel_origin``) and any channel steer admitted into it
    (``_turn_channel_narrowed``) are what the gateway recorded when the turn
    started. An operation in :data:`_USER_TURN_OPERATIONS` also needs the turn
    to be one the person sent (``_turn_user_sent``): a loop wake, a cron or app
    injection, a ``session_send`` and a sub-agent completion are refused with
    403 ``not_user_turn``.
    """
    from kiro_crew.dashboard.handlers._shared import _read_session_key
    from kiro_crew.dashboard.session_control import (
        UNATTENDED_SLOT_PREFIXES,
        caller_slot_key,
    )

    sk = _read_session_key(request)
    if request.get("internal_auth") is not True:
        _audit(sk, operation, "denied", "internal secret required")
        raise GuideError(403, "internal_secret_required", "forbidden")
    app_name = request.get("app", "")
    if app_name:
        _audit(str(app_name), operation, "denied", "app caller")
        raise GuideError(403, "app_caller", "apps cannot act for the dashboard user")
    if not sk:
        _audit("anonymous", operation, "denied", "missing session key")
        raise GuideError(400, "missing_session_key", "missing X-Session-Key")
    if sk.startswith("subagent:"):
        _audit(sk, operation, "denied", "subagent caller")
        raise GuideError(
            403,
            "subagent_caller",
            "a subagent has no dashboard tab of its own; ask from the parent session",
        )
    state = request.app["state"]
    slot_key = caller_slot_key(state, sk)
    slot = state.get_slot(slot_key) if slot_key else None
    if not slot_key or slot is None:
        _audit(sk, operation, "denied", "no live slot")
        raise GuideError(409, "no_live_slot", "this session is not open in a dashboard tab")
    if getattr(slot, "_app", ""):
        _audit(sk, operation, "denied", "app-scoped slot")
        raise GuideError(403, "app_scoped_caller", "app-scoped sessions cannot do this")
    if slot_key.startswith(UNATTENDED_SLOT_PREFIXES):
        _audit(sk, operation, "denied", "unattended slot")
        raise GuideError(403, "unattended_caller", "scheduled runs cannot do this")
    if not slot.turn_running:
        # The call came from a turn that is not this slot's: a channel's own
        # session answering a message the dashboard merely mirrors.
        _audit(sk, operation, "denied", "no dashboard turn")
        raise GuideError(
            409,
            "no_dashboard_turn",
            "this request did not come from a message sent in the dashboard",
        )
    if getattr(slot, "_turn_channel_origin", False) or getattr(
        slot, "_turn_channel_narrowed", False
    ):
        _audit(sk, operation, "denied", "channel turn")
        raise GuideError(
            403,
            "channel_caller",
            "this message came from a messaging channel, not the dashboard",
        )
    if operation in _USER_TURN_OPERATIONS and getattr(slot, "_turn_user_sent", False) is not True:
        _audit(sk, operation, "denied", "not a user turn")
        raise GuideError(
            403,
            "not_user_turn",
            "only a message the user sent in the dashboard can do this",
        )
    return slot_key, sk


async def api_guide_agent_rename_self(request: web.Request) -> web.Response:
    """POST /api/guide/agent/rename — the calling crewmate renames itself.

    The member is the one whose pinned thread the verified caller's slot is,
    never a name from the body: the body carries only the new display name. Like
    every agent-half route it admits only a turn the user sent from the
    dashboard (:func:`_resolve_agent_caller`), so a channel message, a schedule,
    a subagent or an app cannot rename anyone.
    """
    from kiro_crew import members as members_mod

    try:
        slot_key, sk = _resolve_agent_caller(request, "member.rename_self")
        body = await _json_body(request)
    except GuideError as exc:
        return _refusal(exc)
    slot = request.app["state"].get_slot(slot_key)
    member = getattr(slot, "agent", "") if slot is not None else ""
    if (
        slot is None
        or getattr(slot, "mode", "") != members_mod.DM_SLOT_MODE
        or not isinstance(member, str)
        or not member
    ):
        _audit(sk, "member.rename_self", "denied", "not a crewmate thread")
        return _deny(403, "not_a_crewmate", "only a crewmate, in its own chat, can rename itself")
    try:
        name = await asyncio.to_thread(members_mod.rename_member_display, member, body.get("name"))
    except members_mod.SelfRenameError as exc:
        _audit(sk, "member.rename_self", "denied", exc.code)
        status = 403 if exc.code == "not_a_crewmate" else 400
        return _deny(status, exc.code, str(exc))
    _audit(sk, "member.rename_self", "ok")
    return web.json_response({"ok": True, "member": member, "display_name": name})


def register_guide_routes(app: web.Application) -> None:
    """Mount the agent half. Kept in step with ``mcp_routes`` (a test pins both)."""
    app.router.add_post("/api/guide/agent/rename", api_guide_agent_rename_self)
