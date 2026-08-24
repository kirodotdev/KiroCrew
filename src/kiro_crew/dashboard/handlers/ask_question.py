"""Agent-question HTTP API — render a question card and block for the answer.

Two endpoints form one blocking round-trip:

``POST /api/ask-question``
    Called by the ``ask_question`` MCP tool. Validates the question payload,
    broadcasts a ``question_card`` to the owning slot's dashboard clients, and
    holds the request open until the user answers (or the window elapses).

``POST /api/ask-question/{ask_id}/answer``
    Called by the dashboard when the user submits (or dismisses) the card.
    Resolves the blocked request above.

This mirrors the tool-approval round-trip in
:meth:`kiro_crew.dashboard.state.DashboardState.request_approval` — the
difference is that the resolution value is the user's answer map rather than an
allow/deny boolean, and the card is addressed to a single slot.
"""

from __future__ import annotations

import logging
import uuid

from aiohttp import web

from kiro_crew.dashboard.chat_utils import dashboard_slot_key
from kiro_crew.dashboard.handlers.source_providers import (
    is_owner_dashboard_request,
    stale_owner_session_response,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.sel import sel
from kiro_crew.validation import (
    _ASK_MAX_ANSWER_LEN,
    _ASK_MAX_QUESTION_LEN,
    _ASK_MAX_QUESTIONS,
    ValidationError,
    validate_ask_user_question,
)

logger = logging.getLogger(__name__)


def _slot_key_from_session(session_key: str) -> str:
    """The slot key of the tab displaying *session_key*, or ``""`` if none.

    The question card is addressed by slot key — what the frontend compares
    against ``activeSlot`` — while MCP callers hold a session key. The slot name
    is looked up rather than derived by stripping a prefix: a channel-born
    conversation runs under its own channel key while its tab is open, so
    ``slack:<ts>`` must resolve to the ``slack_<ts>`` the frontend matches.
    """
    return dashboard_slot_key(session_key)


def _deny_app_token(request: web.Request, operation: str) -> web.Response | None:
    """Refuse app tokens on these MCP-only endpoints. Returns 403 or None.

    The middleware's ``_enforce_app_scope`` only checks that the *route* is in
    the calling app's manifest ``permissions.api`` allowlist — it does not check
    slot ownership. Without this gate, an app that lists ``/api/ask-question``
    in its manifest would pass scope enforcement and could then target ANY
    slot, including the owner's: broadcast a crafted question card and read the
    user's typed answer straight out of its own blocked HTTP response. That is
    cross-slot phishing plus answer exfiltration, so these endpoints are
    owner-only rather than ownership-scoped.

    Denying app tokens outright also removes the need to bind each pending
    ``ask_id`` to an originating app: with only dashboard-user tokens accepted,
    the sole party that can answer is the single dashboard owner — the actor the
    card is addressed to.

    Callers are the ``ask_question`` flow (the session directive posts a
    non-blocking question card to the owner's own slot) and the dashboard UI
    itself, so no legitimate caller is an app.
    """
    app_name = request.get("app", "")
    if not app_name:
        return None
    try:
        sel().log_api_access(
            caller=app_name,
            operation=operation,
            outcome="denied",
            source="app_isolation",
            resources="/api/ask-question",
            error="app tokens are not permitted on agent-question endpoints",
        )
    except Exception:
        logger.warning("SEL audit failed for app-token denial", exc_info=True)
    return web.json_response(
        {"error": "app token not permitted for this endpoint"}, status=403
    )


def _deny_non_owner(request: web.Request, operation: str) -> web.Response | None:
    """Require the dashboard owner on these endpoints. Returns 403 or None.

    Denying app tokens is not sufficient. A dashboard session token is also
    minted for every *allowed Slack user* (``!dashboard``), and that token has
    an empty app identity, so it clears ``_deny_app_token`` while belonging to
    someone who is not the owner. Such a caller could address a card at any
    slot — phishing the owner with crafted options and then reading the typed
    answer out of its own blocked response — or resolve a card the owner is
    still looking at, feeding the agent an answer the owner never gave.

    ``is_owner_dashboard_request`` is reused rather than re-derived so there is
    one definition of "owner" in the dashboard: an exact match against the
    configured ``owner_id``, or a signed local bootstrap subject when no owner
    is configured. That matches the identity the ``ask_question`` MCP tool
    itself carries, since its token is minted as ``owner_id or "local-app"``.
    """
    if is_owner_dashboard_request(request):
        return None
    try:
        sel().log_api_access(
            caller=str(request.get("user") or "anonymous"),
            operation=operation,
            outcome="denied",
            source="dashboard",
            resources="/api/ask-question",
            error="agent-question endpoints are owner-only",
        )
    except Exception:
        logger.warning("SEL audit failed for non-owner denial", exc_info=True)
    # Deny decision made above; only the response label changes for a signed
    # pre-owner bootstrap subject (see stale_owner_session_response).
    stale = stale_owner_session_response(request)
    if stale is not None:
        return stale
    return web.json_response({"error": "forbidden"}, status=403)


async def api_ask_question(request: web.Request) -> web.Response:
    """POST /api/ask-question — show a question card and block for the answer.

    Body: ``{session_key, questions: [...], timeout_secs?}``

    Responds ``{"status": "answered", "answers": {...}}`` once the user submits,
    or ``{"status": "timeout"}`` when the window elapses / the card is dismissed.
    """
    state: DashboardState = request.app["state"]
    deny = _deny_app_token(request, "ask_question")
    if deny is not None:
        return deny
    deny = _deny_non_owner(request, "ask_question")
    if deny is not None:
        return deny
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        # Valid JSON is not necessarily an object: `[]`, `null` and bare scalars
        # all parse, then blow up on `.get()` as a 500 instead of a 400.
        return web.json_response({"error": "body must be a JSON object"}, status=400)

    session_key = str(body.get("session_key") or "")
    if not session_key:
        return web.json_response({"error": "session_key is required"}, status=400)
    slot_key = _slot_key_from_session(session_key)
    # Refuse to address a slot that does not exist: otherwise the caller blocks
    # for the full window on a card no client will ever render. An empty slot key
    # is the same dead end — the conversation has no open tab to render into.
    if not slot_key or slot_key not in state._slots:
        return web.json_response(
            {"error": f"unknown slot for {session_key!r} — no dashboard session to ask"},
            status=404,
        )

    try:
        questions = validate_ask_user_question(body)
    except ValidationError as exc:
        return web.json_response({"error": str(exc)}, status=400)

    try:
        timeout_secs = int(body.get("timeout_secs") or state._QUESTION_TIMEOUT_DEFAULT)
    except (TypeError, ValueError):
        return web.json_response({"error": "timeout_secs must be an integer"}, status=400)

    ask_id = uuid.uuid4().hex
    try:
        sel().log_tool_invocation(
            session_key=session_key,
            source="dashboard",
            tool_name="ask_question",
            outcome="invoked",
            request_id=ask_id,
        )
    except Exception:
        logger.warning("SEL audit failed for ask_question", exc_info=True)

    try:
        answers = await state.request_question(
            ask_id=ask_id,
            slot_key=slot_key,
            questions=questions,
            timeout=timeout_secs,
        )
    except ValueError as exc:
        # Raised when redaction collapses two questions into the same key, which
        # is only detectable after the redaction pass — so it surfaces here as a
        # 400 rather than from validate_ask_user_question.
        return web.json_response({"error": str(exc)}, status=400)
    if answers is None:
        return web.json_response({"status": "timeout", "ask_id": ask_id})
    return web.json_response({"status": "answered", "ask_id": ask_id, "answers": answers})


async def api_ask_question_pending(request: web.Request) -> web.Response:
    """GET /api/ask-question/pending — question cards still awaiting an answer.

    ``question_card`` is a one-shot broadcast, so a client that reloads or
    reconnects after it fired has no card on screen while the agent is still
    waiting — the question is invisible. This is the rehydration source,
    mirroring ``GET /api/approvals`` for tool approvals (the frontend re-syncs
    both on websocket open).

    Both kinds are listed, distinguished by which identity they carry:

    * a BLOCKING ask carries ``ask_id`` — its payload lives in
      ``_pending_questions`` for as long as the parked wait does;
    * a STATELESS card carries ``card_id`` — its redacted payload is kept on the
      slot's needs-input record. Without it a reloaded tab would show the
      session's "needs your answer" status with no card to answer, and no way to
      dismiss it (the client no longer knows the ``card_id``) — a stuck state
      that only sending a message could clear.

    Owner-only on the same grounds as the other endpoints: the payload is the
    question text addressed to the owner.
    """
    state: DashboardState = request.app["state"]
    deny = _deny_app_token(request, "ask_question_pending")
    if deny is not None:
        return deny
    deny = _deny_non_owner(request, "ask_question_pending")
    if deny is not None:
        return deny
    out: list[dict] = [
        {
            "ask_id": ask_id,
            "slot": p.get("slot", ""),
            "questions": p.get("questions", []),
            "ts": p.get("ts", 0),
        }
        for ask_id, p in state._pending_questions.items()
    ]
    for slot_key, slot in list((getattr(state, "_slots", None) or {}).items()):
        for card_id, rec in list((getattr(slot, "_question_pending", None) or {}).items()):
            # Blocking entries are already listed above, from the authoritative
            # wait registry; a record with no stored questions predates nothing
            # renderable, so it is a status-only marker and is skipped rather
            # than emitted as an empty card.
            if rec.get("blocking") or not rec.get("questions"):
                continue
            out.append(
                {
                    "card_id": card_id,
                    "slot": slot_key,
                    "questions": rec.get("questions", []),
                    "ts": rec.get("ts", 0),
                }
            )
    return web.json_response(out)


async def api_ask_question_dismiss(request: web.Request) -> web.Response:
    """POST /api/ask-question/dismiss — retire a stateless card's status.

    Body: ``{slot, card_id}`` — the slot key and the card identity the
    ``question_card`` payload carries. Deliberately not a session key: a
    channel-born conversation's session key and its slot key differ, and the
    client holds only the slot. ``card_id`` is required because a dismissal is a
    round-trip: the card can be replaced by a newer ask before the request lands,
    and a slot-only clear would retire the NEW card's status, leaving it
    unanswered with nothing to say so.

    A stateless card (no ``ask_id``) blocks nothing, so dismissing it was purely
    a client-side removal — and the slot's ``needs_input`` status, which the
    sidebar and the sessions board read, would go on claiming the agent is
    waiting on an answer until the next message landed. This is the dismiss half
    of that record; the answer half retires through the ordinary user message the
    card's submit sends.

    Owner-only on the same grounds as the other endpoints: it mutates the
    owner's own session status.
    """
    state: DashboardState = request.app["state"]
    deny = _deny_app_token(request, "ask_question_dismiss")
    if deny is not None:
        return deny
    deny = _deny_non_owner(request, "ask_question_dismiss")
    if deny is not None:
        return deny
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object", "code": "invalid_body"}, status=400
        )
    slot_key = str(body.get("slot") or "")
    if not slot_key:
        return web.json_response({"error": "slot is required", "code": "missing_slot"}, status=400)
    card_id = str(body.get("card_id") or "")
    if not card_id:
        return web.json_response(
            {"error": "card_id is required", "code": "missing_card_id"}, status=400
        )
    # Only the stateless record is dismissible here. A blocking ask owns its own
    # lifecycle through the answer endpoint, and clearing its status from this
    # route would report a session as unblocked while its tool call is still
    # parked on the wait. A stale card_id, an unknown slot and an already-retired
    # record all land here too: from this route's point of view they are one
    # answer — there is nothing of yours left to dismiss.
    if not state.clear_question_pending(slot_key, blocking=False, card_id=card_id):
        return web.json_response(
            {
                "error": "no pending question card for that slot and card_id",
                "code": "question_card_not_found",
            },
            status=404,
        )
    return web.json_response({"ok": True})


async def api_ask_question_answer(request: web.Request) -> web.Response:
    """POST /api/ask-question/{ask_id}/answer — resolve a pending question.

    Body: ``{answers: {question: answer}}``, or ``{"dismissed": true}`` to
    unblock the caller with no answer.
    """
    state: DashboardState = request.app["state"]
    deny = _deny_app_token(request, "ask_question_answer")
    if deny is not None:
        return deny
    deny = _deny_non_owner(request, "ask_question_answer")
    if deny is not None:
        return deny
    ask_id = request.match_info["ask_id"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be a JSON object"}, status=400)

    if body.get("dismissed"):
        answers: dict[str, str] | None = None
    else:
        raw = body.get("answers")
        if not isinstance(raw, dict) or not raw:
            return web.json_response(
                {"error": "answers must be a non-empty object"}, status=400
            )
        if len(raw) > _ASK_MAX_QUESTIONS:
            return web.json_response(
                {"error": f"at most {_ASK_MAX_QUESTIONS} answers"}, status=400
            )
        # Keys and values are echoed back to the agent as tool output, so they
        # are coerced to str (a nested object cannot smuggle structure into the
        # transcript) and bounded.
        #
        # REJECT rather than truncate. Silently slicing resolves the wait and
        # clears the card, so the agent proceeds on input the user cannot see was
        # cut and has no way to resend — the answer is simply wrong. A 400 leaves
        # the card up (the frontend only clears on success or a 404), so the user
        # can shorten and retry.
        answers = {str(k): str(v) for k, v in raw.items()}
        for k, v in answers.items():
            if len(k) > _ASK_MAX_QUESTION_LEN:
                return web.json_response(
                    {"error": f"question key exceeds {_ASK_MAX_QUESTION_LEN} characters"},
                    status=400,
                )
            if len(v) > _ASK_MAX_ANSWER_LEN:
                return web.json_response(
                    {
                        "error": (
                            f"answer exceeds {_ASK_MAX_ANSWER_LEN} characters "
                            "— shorten it and submit again"
                        )
                    },
                    status=400,
                )

    if not state.resolve_question(ask_id, answers):
        return web.json_response(
            {"error": "no pending question with that id (already answered or expired)"},
            status=404,
        )
    return web.json_response({"ok": True})
