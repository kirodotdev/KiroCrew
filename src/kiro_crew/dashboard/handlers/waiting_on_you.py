"""Waiting-on-you routes: the agent declares and clears asks, the owner answers and dismisses.

``POST /api/agent-waiting/set`` / ``/api/agent-waiting/clear``
    Machine routes for the ``waiting_on_you`` and ``waiting_on_you_clear`` tools. Strict
    internal paths whose caller is the attested session key, exactly as
    ``/api/agent-ask/*``: the ask goes to that session's own slot, and only a
    dashboard-born session may pin one.

``POST /api/chat/slots/{slot}/waiting-on-you/{key}/answer``
    The owner picks an option, or presses Done on an action card. The answer is sent as
    one ordinary person turn on that slot through the normal chat send (now or queued),
    carrying only what the owner chose or typed, with ``meta.waitingOnYou`` naming the
    ask. The ask is retired only once that send is accepted; a refused send leaves it
    pinned and passes the refusal through.

``POST /api/chat/slots/{slot}/waiting-on-you/{key}/dismiss``
    The owner's ✕.

Both owner routes refuse app tokens and every caller but the owner.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from aiohttp import web
from yarl import URL

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
    ANSWER_ID_RE,
    ANSWER_TEXT_MAX,
    ASK_ID_RE,
    CODE_INVALID_ANSWER,
    EMPTY_ACTION_ANSWER,
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


def _incognito(state: Any, slot_key: str) -> bool:
    from kiro_crew.history_derivation import is_incognito_transcript

    slot = state._slots.get(slot_key)
    return is_incognito_transcript(getattr(slot, "memory_mode", ""))


async def api_agent_waiting_set(request: web.Request) -> web.Response:
    """POST /api/agent-waiting/set — pin or replace one ask on the caller's own slot.

    Body: ``{key, title, detail?, options?}``. Responds ``{ok, ask, changed, keys}``.
    """
    state = request.app["state"]
    caller = await _agent_slot(request, "agent_waiting_set")
    if isinstance(caller, web.Response):
        return caller
    session_key, slot_key = caller
    body = await _json_object(request)
    if isinstance(body, web.Response):
        return body
    try:
        ask = _redacted(validate_declaration(body))
        public, changed, keys = await store_mod.store_for(state).declare(
            slot_key, ask, persist=not _incognito(state, slot_key)
        )
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


def _answer_text(body: dict[str, Any]) -> tuple[str | None, str | None] | web.Response:
    """``(label, text)`` from an answer body: exactly one is a string."""
    label, text = body.get("label"), body.get("text")
    if (label is None) == (text is None):
        return _invalid("send label for a decision card or text for an action card")
    if label is not None and not isinstance(label, str):
        return _invalid("label must be a string")
    if text is not None:
        if not isinstance(text, str):
            return _invalid("text must be a string")
        text = text.strip()
        if len(text) > ANSWER_TEXT_MAX:
            return _invalid(f"text is longer than {ANSWER_TEXT_MAX} characters")
    return label, text


def _checker(label: str | None, text: str | None) -> Any:
    def _check(ask: dict[str, Any]) -> str:
        options = ask.get("options") or []
        if options:
            if label is None or label not in {o["label"] for o in options}:
                raise AskError(400, CODE_INVALID_ANSWER, "that is not one of this ask's options")
            return label
        if label is not None:
            raise AskError(400, CODE_INVALID_ANSWER, "this ask takes text, not an option")
        return text or EMPTY_ACTION_ANSWER

    return _check


def _send_request(request: web.Request) -> web.Request:
    """A copy of *request* aimed at ``POST /api/chat?ws=1``, made before its body is read.

    The answer goes through the chat send itself, with the caller's own identity and
    every check that send makes. aiohttp clones a request only while its body is
    unread, so the copy is taken first and given its own body afterwards.
    """
    return request.clone(rel_url=URL("/api/chat").with_query(ws="1"))  # type: ignore[return-value]


async def _send(inner: web.Request, slot_key: str, text: str, answer_id: str) -> web.StreamResponse:
    from kiro_crew.dashboard import chat_handlers

    body = {"slot": slot_key, "message": text, "meta": {"sendId": answer_id}}
    inner._read_bytes = json.dumps(body).encode("utf-8")
    return await chat_handlers.api_chat(inner)


def _receipt(resp: web.StreamResponse) -> dict[str, Any]:
    if isinstance(resp, web.Response) and resp.text:
        try:
            parsed = json.loads(resp.text)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


#: Receipt keys that name a row the chat send already appended.
_LANDED_ROW_RECEIPT_KEYS = ("mid", "message_id", "row_id")


def _answer_row_landed(receipt: dict[str, Any], slots: list[Any], answer_id: str) -> bool:
    """Whether a refused chat send had already appended this answer's row.

    The send can refuse after its append (a tab closed mid-send answers 409
    ``slot_closed`` with the row in place), so the receipt is not the only witness:
    a row whose ``meta.waitingOnYou`` names *answer_id* is one too.
    """
    if any(isinstance(receipt.get(k), str) and receipt[k] for k in _LANDED_ROW_RECEIPT_KEYS):
        return True
    for slot in slots:
        for row in reversed(getattr(slot, "messages", None) or []):
            meta = row.get("meta") if isinstance(row, dict) else None
            claim = meta.get(events.WAITING_ON_YOU_META_KEY) if isinstance(meta, dict) else None
            if isinstance(claim, dict) and claim.get("answer_id") == answer_id:
                return True
    return False


async def api_waiting_on_you_answer(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/waiting-on-you/{key}/answer — answer one ask.

    Body: ``{ask_id, answer_id, label}`` for a decision card, ``{ask_id, answer_id,
    text}`` for an action card. Responds ``{ok, outcome: "sent" | "queued",
    answer_id}``; a retry of the same ``answer_id`` gets that answer back and sends
    nothing.
    """
    state = request.app["state"]
    refused = _owner_refusal(request, "waiting_on_you_answer")
    if refused is not None:
        return refused
    inner = _send_request(request)
    body = await _owner_body(request)
    if isinstance(body, web.Response):
        return body
    slot_key = request.match_info.get("slot", "")
    key = request.match_info.get("key", "")
    ask_id, answer_id = body.get("ask_id"), body.get("answer_id")
    if not isinstance(ask_id, str) or not ASK_ID_RE.fullmatch(ask_id):
        return _invalid("ask_id is required")
    if not isinstance(answer_id, str) or not ANSWER_ID_RE.fullmatch(answer_id):
        return _invalid("answer_id must be 8-64 characters of A-Z, a-z, 0-9, '_' and '-'")
    if not valid_key(key):
        return _refusal(AskError(409, "cleared", "this ask is no longer pinned"))
    parsed = _answer_text(body)
    if isinstance(parsed, web.Response):
        return parsed
    store = store_mod.store_for(state)
    try:
        verdict, payload = await store.reserve(slot_key, key, ask_id, answer_id, _checker(*parsed))
    except AskError as exc:
        return _refusal(exc)
    if verdict == "replay":
        return web.json_response(payload)
    assert payload is not None
    inner[events.ANSWER_REQUEST_KEY] = {"key": key, "ask_id": ask_id, "answer_id": answer_id}
    # Held before the send: a close during it can detach the slot from the state.
    slot_before_send = state._slots.get(slot_key)
    try:
        resp = await _send(inner, slot_key, payload["text"], answer_id)
    except BaseException:
        # Cancelled or failed mid-send: whether the row landed is unknown.
        logger.warning("waiting-on-you answer send for %s did not finish", slot_key, exc_info=True)
        await asyncio.shield(store.mark_uncertain(slot_key, ask_id, answer_id))
        raise
    receipt = _receipt(resp)
    if resp.status >= 400:
        slots = [s for s in (slot_before_send, state._slots.get(slot_key)) if s is not None]
        if _answer_row_landed(receipt, slots, answer_id):
            # The row is in the chat, so a retry of this answer_id must not send it again.
            await store.mark_uncertain(slot_key, ask_id, answer_id)
        else:
            await store.release(slot_key, ask_id, answer_id)
        return web.json_response(
            {
                "ok": False,
                "code": str(receipt.get("code") or "send_refused"),
                "error": str(receipt.get("error") or "the message was not sent"),
            },
            status=resp.status,
        )
    queued = resp.status == 202 or receipt.get("queued") is True
    outcome = {"ok": True, "outcome": "queued" if queued else "sent", "answer_id": answer_id}
    retired = await store.settle(slot_key, key, ask_id, answer_id, outcome)
    await events.publish(state, slot_key, events.ROW_ANSWERED, [retired] if retired else [])
    return web.json_response(outcome)


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
    """The two agent routes and the two owner routes, plus the store's lifecycle."""
    app.on_startup.append(events.startup)
    app.on_cleanup.append(events.cleanup)
    app.router.add_post("/api/agent-waiting/set", api_agent_waiting_set)
    app.router.add_post("/api/agent-waiting/clear", api_agent_waiting_clear)
    app.router.add_post(
        "/api/chat/slots/{slot}/waiting-on-you/{key}/answer", api_waiting_on_you_answer
    )
    app.router.add_post(
        "/api/chat/slots/{slot}/waiting-on-you/{key}/dismiss", api_waiting_on_you_dismiss
    )


__all__ = [
    "api_agent_waiting_clear",
    "api_agent_waiting_set",
    "api_waiting_on_you_answer",
    "api_waiting_on_you_dismiss",
    "register_routes",
]
