"""The question card of ONE slot, for that slot's owner app or the dashboard owner.

``GET /api/chat/slots/{slot}/question`` lists the slot's pending cards and
``POST .../question/answer`` answers a BLOCKING one (an ``ask_id``, the MCP
``ask_question`` tool), so a host that embeds a chat (the app-sdk ``ChatEmbed``)
can show and answer the card its own session raised.

A NATIVE mid-turn card (kiro-cli's ``AskUserQuestion``: ``native: true``, a
``card_id`` and no ``ask_id``) is different. It blocks nothing in the wait
registry; its answer has to reach the RUNNING turn that is waiting on it, the
way the dashboard's own ``onDirectQuestionSend`` steers it. A plain send through
``POST /api/chat`` would queue the answer BEHIND that very turn (and an app send
is refused the steer path there outright, to keep the live turn's human
provenance off an app-authored message), so the embed has two more
ownership-scoped routes for it:

* ``POST .../question/steer``   steer a native card's answer into the slot's
  running turn, with APP provenance (``user_origin=False``), never the human's;
* ``POST .../question/dismiss`` retire a native card's ``needs_input`` status,
  the per-slot owner-app form of ``POST /api/ask-question/dismiss`` (owner-only).

The owner-wide ``/api/ask-question/*`` routes stay closed to every app token
(``ask_question._deny_app_token``); these routes are the ownership-scoped way in.

Who may call (all four routes):

* The per-slot checkpoint (``slot_ownership``) decides an app caller first, with
  the default OWNER policy: only the slot's owner app, on the slot's own session
  and transcript. Every other slot is the uniform 404 ``slot_not_found``. There is
  no session-grant reach onto a user session here, unlike ``/approve``.
* :func:`deny_session_approval_caller` then requires the app to hold
  ``permissions.sessionApproval``, the same grant ``/approve`` needs. The dashboard
  owner passes, and every bare internal-secret caller is refused.
* A write re-reads the grant after its body upload and refuses a slot replaced
  under the same name meanwhile.

An answer names the card it answers (its ``ask_id`` on ``/answer``, its
``card_id`` on ``/steer`` and ``/dismiss``), and the card must be pending on
THIS slot: a card on any other slot reads as the same 404, so these routes say
nothing about another slot's asks.
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


def _native_card_not_found() -> web.Response:
    return web.json_response(
        {
            "error": "no pending native question card with that id on this slot",
            "code": "question_card_not_found",
        },
        status=404,
    )


def _native_card_pending(slot: Any, card_id: str) -> bool:
    """Whether *card_id* is a pending NATIVE (stateless) card on *slot*.

    A blocking ask (an ``ask_id``, answered through ``/answer``) is never steered,
    so it reads as absent here: these native routes name only the mid-turn card.
    """
    rec = (getattr(slot, "_question_pending", None) or {}).get(card_id)
    return isinstance(rec, dict) and not rec.get("blocking") and bool(rec.get("native"))


def _reserve_native_steer(slot: Any, card_id: str) -> bool:
    """Claim *card_id* for one in-flight steer on *slot*, synchronously.

    Returns True when this call made the reservation, False when a steer for the
    same card is already in flight. ``steer_into_running_turn`` deduplicates only
    by message TEXT, so two owner-app requests submitting DIFFERENT answers for the
    same card would both pass :func:`_native_card_pending` (the card is retired
    only after the first delivers) and both reach the running turn. The claim is a
    membership on the slot's OWN ``_native_steer_in_flight`` set -- NOT a flag on
    the card's pending record, which is serialized out through GET .../question
    and the question_card broadcast -- taken with no await between it and the
    pending check, so a second concurrent request sees it and is refused.
    """
    reserved = getattr(slot, "_native_steer_in_flight", None)
    if not isinstance(reserved, set):
        # A slot rebuilt from disk or by a path that predates the set: create it
        # so the claim still holds rather than silently admitting a second steer.
        reserved = set()
        slot._native_steer_in_flight = reserved
    if card_id in reserved:
        return False
    reserved.add(card_id)
    return True


def _release_native_steer(slot: Any, card_id: str) -> None:
    """Drop *card_id*'s in-flight claim, so a later steer of the same card may run.

    Called when delivery did not happen (unavailable, or an error) -- a delivered
    steer retires the card itself, so the next request is a 404, not a re-steer.
    """
    reserved = getattr(slot, "_native_steer_in_flight", None)
    if isinstance(reserved, set):
        reserved.discard(card_id)


def _steer_in_flight() -> web.Response:
    return web.json_response(
        {"error": "a steer for this card is already in flight", "code": "steer_in_flight"},
        status=409,
    )


async def api_chat_slot_question_steer(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/question/steer — steer a native card's answer.

    Body: ``{card_id, message}``. The embed shows kiro-cli's native mid-turn
    ``AskUserQuestion`` card, whose answer must reach the turn still waiting on it
    rather than queue behind it. This route injects *message* into the slot's
    RUNNING turn through the same mechanism the dashboard's steer uses
    (:func:`steer_into_running_turn`), with APP provenance: ``user_origin=False``
    so the row never carries the human-turn marker, and ``appLabel`` stamped from
    the authenticated caller so the row reads as the app's, never the session's
    human (the same ``meta.appLabel`` an app inject row uses).

    Why an app may steer here when ``POST /api/chat`` refuses every app steer:
    that route's refusal is blanket because a general app send has no ask to
    answer, so steering it would inject app text into a human turn on the app's
    say-so. This route is narrow in three ways that remove that risk — it is
    bounded to ONE pending NATIVE card (``card_id``) on the app's OWN slot
    (``_caller_refusal``/``_after_body_refusal`` + :func:`_native_card_pending`),
    it answers a question the app's own session RAISED, and it retires that card
    on delivery so a second ``/steer`` for the same ``card_id`` is a 404: one
    steer per card, not an open steer channel. ``admission`` is passed exactly as
    the composer/peer callers pass it, so the requeue reads the real containment
    rather than landing on the fail-closed floor.

    *card_id* must name a pending native card on THIS slot, so an app can only
    steer where its own session actually asked — not arbitrary text into any
    running turn it owns. When no turn is running the route steers nothing and
    answers ``{ok: True, steered: False, unavailable: True}`` so the embed can
    fall back to an ordinary queued send; the card's ``needs_input`` status is
    retired only once the answer was delivered into the turn.
    """
    operation = "chat_slot_question_steer"
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
    card_id = body.get("card_id")
    if not isinstance(card_id, str) or not card_id:
        return web.json_response(
            {"error": "card_id is required", "code": "missing_card_id"}, status=400
        )
    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        return web.json_response(
            {"error": "message is required", "code": "missing_message"}, status=400
        )
    message = message.strip()
    # Bounded exactly as POST /api/chat bounds a send/steer message, so a native
    # card answer cannot be larger here than the composer's own would be.
    from kiro_crew.dashboard.session_control_parts.session_messaging import (
        MAX_SEND_MESSAGE_CHARS,
    )

    if len(message) > MAX_SEND_MESSAGE_CHARS:
        return web.json_response(
            {
                "error": f"message exceeds {MAX_SEND_MESSAGE_CHARS} characters",
                "code": "message_too_long",
            },
            status=400,
        )
    # No await from here to the steer: the card is judged pending, reserved, and
    # the steer attempted together, so a card retired meanwhile is not steered
    # against and two concurrent requests cannot both pass.
    if not _native_card_pending(slot, card_id):
        return _native_card_not_found()
    # A native card is raised mid-turn; its answer only has somewhere to go while
    # that turn is live. When it has ended the embed queues an ordinary send
    # instead, so say so rather than dropping the answer.
    if not slot.running:
        return web.json_response({"ok": True, "steered": False, "unavailable": True})
    # Reserve the card BEFORE the delivery await: the helper below deduplicates
    # only by message text, so two owner-app requests with different answers for
    # the same card would otherwise both reach the running turn. Synchronous with
    # the pending check (no await between), so a concurrent second request is
    # refused rather than racing. Released on every non-delivery path; a delivered
    # steer retires the card, so the next request is a 404 not a re-steer.
    if not _reserve_native_steer(slot, card_id):
        return _steer_in_flight()
    from kiro_crew.dashboard.chat_delivery import (
        STEER_REQUEUED,
        STEER_STEERED,
        steer_into_running_turn,
    )

    # App provenance on the persisted row: the AUTHENTICATED caller identity
    # (`request.get("app")`), stamped as `meta.appLabel` so the row reads as the
    # app's — the same pill an app inject row and a per-slot note use. Bounded by
    # the same structural rule those apply (`slot_note`), so a malformed label is
    # dropped rather than rendered. The dashboard owner has no app identity, so
    # the stamp is simply absent for it (its row is the owner's own).
    from kiro_crew.dashboard.slot_buffers import MAX_SOURCE_LABEL_LEN, SOURCE_LABEL_CTRL_RE

    request_app = str(request.get("app") or "")
    app_label = request_app
    if len(app_label) > MAX_SOURCE_LABEL_LEN or SOURCE_LABEL_CTRL_RE.search(app_label):
        app_label = ""

    # Containment captured synchronously with the running-turn gate above, exactly
    # as the composer and peer callers capture it: `steer_into_running_turn`
    # documents every caller passing `admission`, and omitting it lands the
    # requeue on the fail-closed floor instead of the containment the ownership
    # check just cleared. circular import: session_control imports this package.
    from kiro_crew.dashboard.session_control import containment_meta

    try:
        outcome = await steer_into_running_turn(
            state,
            slot,
            message,
            # The answer is the APP's, not the session's own human: `user_origin=False`
            # keeps the human-turn marker off the row and the human's exemption from
            # the drain's LINKED drop out of the requeue. Stated rather than defaulted,
            # as the composer and peer paths do.
            user_origin=False,
            admission=containment_meta(state, slot),
            # Row attribution: the app's identity as meta.appLabel (display only).
            app_label=app_label or None,
            # The routing actor, keyed on the AUTHENTICATED caller, not on the
            # label surviving its length/control-char bound: an app whose label is
            # malformed/over-long still requeues as an app turn, never ``user``.
            app_actor=bool(request_app),
        )
    except Exception:
        # Delivery did not happen: free the card so a retry (or a later card of
        # the same id) is not permanently refused as in-flight.
        _release_native_steer(slot, card_id)
        raise
    if outcome in (STEER_STEERED, STEER_REQUEUED):
        # Delivered (steered now, or requeued by the turn's teardown): the card's
        # answer has gone where it belongs, so retire the stateless status the
        # sidebar and sessions board read. Matched on card_id so a newer card
        # that replaced it meanwhile is left standing. The reservation goes with
        # the retired card; a repeat request is then a plain 404.
        _release_native_steer(slot, card_id)
        state.clear_question_pending(name, blocking=False, card_id=card_id)
        return web.json_response({"ok": True, "steered": True, "queued": outcome == STEER_REQUEUED})
    # Unavailable (no live steer-capable client): the turn moved on between the
    # busy check and the RPC. Release the reservation so the embed's queued-send
    # fallback, or a later steer, is not refused as in-flight.
    _release_native_steer(slot, card_id)
    return web.json_response({"ok": True, "steered": False, "unavailable": True})


async def api_chat_slot_question_dismiss(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/question/dismiss — retire a native card's status.

    Body: ``{card_id}``. The per-slot, owner-app form of
    ``POST /api/ask-question/dismiss`` (owner-only): a native card blocks nothing,
    so dismissing it is a client-side removal, but the slot's ``needs_input``
    status would otherwise keep claiming the agent is waiting. Only a NATIVE
    (stateless) card is dismissible here; a blocking ask owns its lifecycle
    through ``/answer`` and is reported absent.
    """
    operation = "chat_slot_question_dismiss"
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
    card_id = body.get("card_id")
    if not isinstance(card_id, str) or not card_id:
        return web.json_response(
            {"error": "card_id is required", "code": "missing_card_id"}, status=400
        )
    # Only a stateless native record is dismissible. A blocking ask (cleared via
    # /answer) and an unknown or already-retired card all read as one 404: from
    # this route there is nothing of yours left to dismiss.
    if not _native_card_pending(slot, card_id):
        return _native_card_not_found()
    # A steer for this same card may be mid-delivery: its answer is already on
    # the way into the running turn, but the card is retired only AFTER it lands.
    # Retiring it here meanwhile would leave the two routes disagreeing about the
    # card's state. Refuse with the same 409 the steer route uses for a
    # concurrent second answer; the dismiss can be retried once the steer settles
    # (by then the card is gone, so the retry is a 404 -- already dismissed).
    if card_id in getattr(slot, "_native_steer_in_flight", set()):
        return _steer_in_flight()
    state.clear_question_pending(name, blocking=False, card_id=card_id)
    return web.json_response({"ok": True})
