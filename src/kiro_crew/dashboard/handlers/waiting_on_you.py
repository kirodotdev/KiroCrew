"""Waiting-on-you routes: the agent declares and clears asks, the owner dismisses them.

``POST /api/agent-waiting/set`` / ``/api/agent-waiting/clear``
    Machine routes for the ``waiting_on_you`` and ``waiting_on_you_clear`` tools. Strict
    internal paths whose caller is the attested session key, exactly as
    ``/api/agent-ask/*``: the ask goes to that session's own slot, and only a
    dashboard-born session may pin one.

``POST /api/chat/slots/{slot}/waiting-on-you/{key}/dismiss``
    The owner's ✕. Refuses app tokens and every caller but the owner.

An answer has no route of its own: the card sends it through ``POST /api/chat`` with a
claim naming its ask, checked when its row is written (``waiting_on_you_events``).
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from kiro_crew.dashboard import waiting_on_you as store_mod
from kiro_crew.dashboard import waiting_on_you_events as events
from kiro_crew.dashboard.handlers._shared import read_bounded_json
from kiro_crew.dashboard.handlers.ask_question import (
    _attested_session,
    _crew_worker_refusal,
    _deny_app_token,
    _deny_non_owner,
    _slot_key_from_session,
)
from kiro_crew.sel import sel
from kiro_crew.waiting_on_you_contract import (
    ASK_ID_RE,
    CODE_INVALID_ANSWER,
    AskError,
    valid_key,
    validate_declaration,
)

logger = logging.getLogger(__name__)


def _refusal(exc: AskError) -> web.Response:
    """The refusal body: ``ok``, the contract ``code`` and a sentence; the pinned keys too."""
    if "keys" in exc.extra:
        return web.json_response(
            {"ok": False, "code": exc.code, "error": exc.message, "keys": exc.extra["keys"]},
            status=exc.status,
        )
    return web.json_response(
        {"ok": False, "code": exc.code, "error": exc.message}, status=exc.status
    )


def _audit(request: web.Request, operation: str, outcome: str, error: str = "", **who: str) -> None:
    try:
        sel().log_api_access(
            caller=who.get("session_key") or str(request.get("user") or "anonymous"),
            operation=operation,
            outcome=outcome,
            source="agent_waiting",
            resources="/api/agent-waiting",
            error=error,
        )
    except Exception:
        logger.warning("SEL audit failed for a waiting-on-you route", exc_info=True)


def _denied(
    request: web.Request,
    operation: str,
    status: int,
    code: str,
    message: str,
    session_key: str = "",
) -> web.Response:
    _audit(request, operation, "denied", message, session_key=session_key)
    return web.json_response({"ok": False, "error": message, "code": code}, status=status)


async def _agent_slot(request: web.Request, operation: str) -> tuple[str, str] | web.Response:
    """``(session_key, slot_key)`` for an attested dashboard caller, else the refusal."""
    state = request.app["state"]
    session_key = await _attested_session(request)
    if session_key is None:
        return _denied(
            request, operation, 403, "session_unattested", "caller session is not attested"
        )
    if not session_key.startswith("dashboard:"):
        return _denied(
            request,
            operation,
            403,
            "not_dashboard_session",
            "only dashboard sessions can pin an ask for the owner",
            session_key,
        )
    slot_key = _slot_key_from_session(session_key)
    if not slot_key or slot_key not in state._slots:
        return web.json_response(
            {"ok": False, "error": "no dashboard tab for this session", "code": "slot_not_found"},
            status=404,
        )
    refused = _crew_worker_refusal(state, slot_key, session_key)
    if refused is not None:
        return refused
    return session_key, slot_key


async def _json_object(request: web.Request) -> dict[str, Any] | web.Response:
    body, err = await read_bounded_json(request)
    if err is not None:
        return err
    assert body is not None
    return body


def _redacted(ask: dict[str, Any]) -> dict[str, Any]:
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls

    def _clean(text: str) -> str:
        out, _ = redact_exfiltration_urls(text)
        out, _ = redact_credentials(out)
        return out

    out = {
        "key": ask["key"],
        "title": _clean(ask["title"]),
        "detail": _clean(ask["detail"]),
        "options": [
            {"label": _clean(o["label"]), "description": _clean(o["description"])}
            for o in ask["options"]
        ],
    }
    # Redaction could fold two labels together, and an answer names one exactly.
    return validate_declaration(out)


def _incognito(slot: Any) -> bool:
    from kiro_crew.history_derivation import is_incognito_transcript

    return is_incognito_transcript(getattr(slot, "memory_mode", ""))


def _tab_gone(state: Any, slot_key: str, slot: Any) -> web.Response | None:
    """The refusal when *slot* stopped being the tab on *slot_key* during the request.

    A route reads the caller's tab once, at its start, and decides by that object:
    a tab closed or replaced while the route awaited is not the one that asked.
    """
    if state._slots.get(slot_key) is slot:
        return None
    return web.json_response(
        {"ok": False, "error": "the tab closed during the request", "code": "slot_not_found"},
        status=404,
    )


async def api_agent_waiting_set(request: web.Request) -> web.Response:
    """POST /api/agent-waiting/set — pin or replace one ask on the caller's own slot.

    Body: ``{key, title, detail?, options?}``. Responds ``{ok, ask, changed, keys}``.
    """
    state = request.app["state"]
    caller = await _agent_slot(request, "agent_waiting_set")
    if isinstance(caller, web.Response):
        return caller
    session_key, slot_key = caller
    slot = state._slots.get(slot_key)
    private = _incognito(slot)
    body = await _json_object(request)
    if isinstance(body, web.Response):
        return body
    store = store_mod.store_for(state)
    try:
        ask = _redacted(validate_declaration(body))
        expired = await store.expire_due(slot_key)
        if expired:
            await events.publish(state, slot_key, events.ROW_EXPIRED, expired)
        gone = _tab_gone(state, slot_key, slot)
        if gone is not None:
            return gone
        public, changed, keys = await store.declare(slot_key, ask, persist=not private)
    except AskError as exc:
        return _refusal(exc)
    _audit(request, "agent_waiting_set", "allowed", session_key=session_key)
    if changed:
        await events.publish(state, slot_key, events.ROW_PINNED, [public])
    return web.json_response({"ok": True, "ask": public, "changed": changed, "keys": keys})


async def api_agent_waiting_clear(request: web.Request) -> web.Response:
    """POST /api/agent-waiting/clear — remove asks from the caller's own slot.

    Body: ``{key}`` or ``{all: true}``. Responds ``{ok, cleared, keys}``.
    """
    state = request.app["state"]
    caller = await _agent_slot(request, "agent_waiting_clear")
    if isinstance(caller, web.Response):
        return caller
    session_key, slot_key = caller
    slot = state._slots.get(slot_key)
    body = await _json_object(request)
    if isinstance(body, web.Response):
        return body
    every = body.get("all") is True
    key = body.get("key")
    if every == (key is not None) or (key is not None and not valid_key(key)):
        return web.json_response(
            {"ok": False, "error": "pass a pinned key, or all: true", "code": "invalid_ask"},
            status=400,
        )
    store = store_mod.store_for(state)
    closed = _tab_gone(state, slot_key, slot)
    if closed is not None:
        return closed
    try:
        gone = await store.clear(slot_key, None if every else [str(key)])
    except AskError as exc:
        return _refusal(exc)
    _audit(request, "agent_waiting_clear", "allowed", session_key=session_key)
    if gone:
        await events.publish(state, slot_key, events.ROW_CLEARED, gone)
    return web.json_response(
        {
            "ok": True,
            "cleared": [a["key"] for a in gone],
            "keys": [a["key"] for a in store.asks(slot_key)],
        }
    )


# ── the owner's routes ──


def _owner_refusal(request: web.Request, operation: str) -> web.Response | None:
    """Refuse every caller but the machine owner.

    ``_deny_non_owner`` admits a Shared Crew teammate on most routes; an ask is the
    owner's, and a teammate sees the card read-only, so a teammate is refused too.
    """
    from kiro_crew.dashboard.handlers.source_providers import is_host_owner_dashboard_request

    refused = _deny_app_token(request, operation) or _deny_non_owner(request, operation)
    if refused is not None:
        return refused
    if is_host_owner_dashboard_request(request):
        return None
    _audit(request, operation, "denied", "waiting-on-you answers are the owner's")
    return web.json_response(
        {"ok": False, "code": "owner_only", "error": "only the owner answers these asks"},
        status=403,
    )


async def _owner_body(request: web.Request) -> dict[str, Any] | web.Response:
    """The owner route's JSON object body, or a 400 ``invalid_answer`` the card can show."""
    body = await _json_object(request)
    if isinstance(body, web.Response):
        return _invalid("the request body must be a JSON object")
    return body


def _invalid(message: str) -> web.Response:
    return web.json_response(
        {"ok": False, "code": CODE_INVALID_ANSWER, "error": message}, status=400
    )


async def api_waiting_on_you_dismiss(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/waiting-on-you/{key}/dismiss — the owner's ✕."""
    state = request.app["state"]
    refused = _owner_refusal(request, "waiting_on_you_dismiss")
    if refused is not None:
        return refused
    body = await _owner_body(request)
    if isinstance(body, web.Response):
        return body
    slot_key = request.match_info.get("slot", "")
    key = request.match_info.get("key", "")
    ask_id = body.get("ask_id")
    if not isinstance(ask_id, str) or not ASK_ID_RE.fullmatch(ask_id):
        return _invalid("ask_id is required")
    if not valid_key(key):
        return _refusal(AskError(409, "cleared", "this ask is no longer pinned"))
    try:
        gone = await store_mod.store_for(state).dismiss(slot_key, key, ask_id)
    except AskError as exc:
        return _refusal(exc)
    await events.publish(state, slot_key, events.ROW_DISMISSED, [gone])
    return web.json_response({"ok": True})


def register_routes(app: web.Application) -> None:
    """The two agent routes and the owner's dismiss route, plus the store's lifecycle."""
    app.on_startup.append(events.startup)
    app.on_cleanup.append(events.cleanup)
    app.router.add_post("/api/agent-waiting/set", api_agent_waiting_set)
    app.router.add_post("/api/agent-waiting/clear", api_agent_waiting_clear)
    app.router.add_post(
        "/api/chat/slots/{slot}/waiting-on-you/{key}/dismiss", api_waiting_on_you_dismiss
    )


__all__ = [
    "api_agent_waiting_clear",
    "api_agent_waiting_set",
    "api_waiting_on_you_dismiss",
    "register_routes",
]
