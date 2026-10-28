"""The question card of ONE slot, for that slot's owner app or the dashboard owner.

``GET /api/chat/slots/{slot}/question`` and ``POST .../question/answer`` let a
host that embeds a chat (the app-sdk ``ChatEmbed``) show and answer the blocking
``ask_question`` card its own session raised.
The owner-wide ``/api/ask-question/*`` routes stay closed to every app token
(``ask_question._deny_app_token``); these routes are the ownership-scoped way in.

Who may call:

* The per-slot checkpoint (``slot_ownership``) decides an app caller first, with
  the default OWNER policy: only the slot's owner app, on the slot's own session
  and transcript. Every other slot is the uniform 404 ``slot_not_found``. There is
  no session-grant reach onto a user session here, unlike ``/approve``.
* :func:`deny_session_approval_caller` then requires the app to hold
  ``permissions.sessionApproval``, the same grant ``/approve`` needs. The dashboard
  owner passes, and every bare internal-secret caller is refused.
* A write re-reads the grant after its body upload and refuses a slot replaced
  under the same name meanwhile.

An answer names the card it answers (its ``ask_id``), and the card must be
pending on THIS slot: a card on any other slot, answered or not, reads as
the same 404 ``question_not_found``, so these routes say nothing about another
slot's asks.
"""

from __future__ import annotations

from typing import Any

from aiohttp import web

from kiro_crew.dashboard.handlers._shared import read_bounded_json
from kiro_crew.dashboard.handlers.ask_question import (
    _parse_answer_body,
    _resolve_or_not_found,
    pending_question_cards,
)
from kiro_crew.dashboard.slot_ownership import (
    _record_isolation_allow,
    app_owns_slot_session,
    audit_app_slot_denial,
    checkpoint_slot_replaced,
    session_grant,
    slot_not_found,
)


def _question_not_found() -> web.Response:
    return web.json_response(
        {"error": "no pending question with that id on this slot", "code": "question_not_found"},
        status=404,
    )


async def _caller_refusal(request: web.Request, operation: str) -> web.Response | None:
    # Imported here: chat_handlers wires the approval module's names at import.
    from kiro_crew.dashboard.chat_handlers import deny_session_approval_caller

    return await deny_session_approval_caller(request, operation)


async def _read_body(request: web.Request) -> dict | web.Response:
    body, err = await read_bounded_json(request)
    if err is not None:
        return err
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object", "code": "invalid_body"}, status=400
        )
    return body


async def _after_body_refusal(
    request: web.Request, slot: Any, name: str, operation: str
) -> web.Response | None:
    """The checks a write repeats once its body has arrived.

    The upload can outlast a grant removal or a close and same-name create, so the
    grant is read again and the slot must still be the one the checkpoint judged.
    """
    request_app = str(request.get("app") or "")
    if not request_app:
        return None
    slots = request.app["state"]._slots
    if checkpoint_slot_replaced(request, slot) or slots.get(name) is not slot:
        audit_app_slot_denial(request_app, operation, name, "slot replaced during the body read")
        return slot_not_found()
    if not await session_grant(request, request_app, fresh=True):
        audit_app_slot_denial(request_app, operation, name, "session approval permission removed")
        return web.json_response(
            {
                "error": "app cannot manage session approvals",
                "code": "session_approval_not_granted",
            },
            status=403,
        )
    if slots.get(name) is not slot or not app_owns_slot_session(request_app, slot):
        audit_app_slot_denial(request_app, operation, name, "slot replaced during the grant read")
        return slot_not_found()
    return None


async def _read_caller_refusal(request: web.Request, operation: str) -> web.Response | None:
    """The caller check for the polled GET, with a windowed allow audit.

    The same decision as :func:`deny_session_approval_caller`, which refuses and
    audits every non-app, internal and ungranted caller. A granted app passes
    without its per-call allow row: an embed polls this route every 1.5 s, so
    the allow is recorded through the checkpoint's window
    (``_record_isolation_allow``, one row per app, route and slot per 5 min).
    """
    request_app = str(request.get("app") or "")
    if request_app and request.get("internal_auth") is not True:
        if await session_grant(request, request_app):
            _record_isolation_allow(request_app, operation, request.match_info.get("slot", ""))
            return None
    # Every other case, refusals included, takes the shared check. The grant
    # verdict is cached on the request, so an ungranted app is not read twice.
    return await _caller_refusal(request, operation)


async def api_chat_slot_question(request: web.Request) -> web.Response:
    """GET /api/chat/slots/{slot}/question — this slot's pending question cards."""
    denied = await _read_caller_refusal(request, "chat_slot_question")
    if denied is not None:
        return denied
    state = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if slot is None:
        return slot_not_found()
    request_app = str(request.get("app") or "")
    # The caller check awaited the grant read: a close and same-name create
    # inside it leaves a slot no ownership decision covered. Judged here with
    # no await before the listing, so the cards read are the judged slot's.
    if request_app and (
        checkpoint_slot_replaced(request, slot) or not app_owns_slot_session(request_app, slot)
    ):
        audit_app_slot_denial(
            request_app, "chat_slot_question", name, "slot replaced during the grant read"
        )
        return slot_not_found()
    return web.json_response({"pending": pending_question_cards(state, name)})


async def api_chat_slot_question_answer(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/question/answer — answer or decline a blocking card.

    Body: ``{ask_id, answers: {question: answer}}``, or ``{ask_id, dismissed: true}``
    (with an optional ``reason`` of ``composer`` or ``queued``), bounded exactly as
    ``POST /api/ask-question/{ask_id}/answer`` bounds it.
    """
    operation = "chat_slot_question_answer"
    denied = await _caller_refusal(request, operation)
    if denied is not None:
        return denied
    state = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if slot is None:
        return slot_not_found()
    body = await _read_body(request)
    if isinstance(body, web.Response):
        return body
    denied = await _after_body_refusal(request, slot, name, operation)
    if denied is not None:
        return denied
    ask_id = body.get("ask_id")
    if not isinstance(ask_id, str) or not ask_id:
        return web.json_response(
            {"error": "ask_id is required", "code": "missing_ask_id"}, status=400
        )
    parsed = _parse_answer_body(body)
    if isinstance(parsed, web.Response):
        return parsed
    answers, reason = parsed
    # No await from here to the resolve: the ask is judged and settled together.
    pending = state._pending_questions.get(ask_id)
    if not isinstance(pending, dict) or pending.get("slot", "") != name:
        return _question_not_found()
    return _resolve_or_not_found(state, ask_id, answers, reason)
