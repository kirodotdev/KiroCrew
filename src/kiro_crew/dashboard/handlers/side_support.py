"""Side Chat helpers with no redaction of their own: steer requeue and delivery,
slot ownership and the queue request readers.

Split from ``dashboard/handlers/side.py`` (file line cap), which re-exports
every name; callers keep importing from there.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.side_state import (
    STEER_PENDING,
    STEER_REQUEUED,
)
from kiro_crew.dashboard.slot_ownership import deny_app_slot_access, slot_not_found
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.dashboard.ws import broadcast_side_queue

logger = logging.getLogger(__name__)

_MAX_QUESTION_BYTES = 32_768


def _side_session_key(slot_key: str, gen: str = "") -> str:
    """Return the isolated ACP session key for a side turn.

    ``side:<slot>:<gen>`` when the sidecar's generation is known (every live
    path), so a sidecar that was closed and reopened never shares a session with
    a turn still finishing on its predecessor: the old task can only destroy,
    acquire or release ITS generation's key. The bare ``side:<slot>`` form is the
    legacy spelling, kept for a caller with no sidecar in hand.
    """
    return f"side:{slot_key}:{gen}" if gen else f"side:{slot_key}"


def _requeue_unconsumed_side_steers(
    state: DashboardState, slot, run_id: str, side_at_start: Any = None
) -> None:
    """Degrade steers the backend never confirmed into visible queue cards.

    A steer is a fire-and-forget write, so ``steer()`` returning True only proves
    the bytes left; the ``steering_consumed`` echo is the authoritative signal.
    Anything still pending when the turn ends reached no generation and would
    otherwise vanish having been reported as delivered.

    Requeued at the HEAD, preserving relative order — a steer was meant to be
    injected before anything the user queued after it. Each becomes an ordinary
    card, so a user who changes their mind cancels it with one click.

    Gated on ``side_at_start`` identity as well as ``run_id``: the run id alone
    would let a superseded turn write onto a sidecar a close+reopen replaced.
    """
    side = slot._side
    if side is None or not side.open or side.last_run_id != run_id:
        return
    if side_at_start is not None and side is not side_at_start:
        return
    orphaned = side.steer_pending()
    if not orphaned:
        return
    for entry in orphaned:
        side.steer_mark(entry["id"], STEER_REQUEUED)
    for entry in reversed(orphaned):
        message = entry["text"]
        qid = side.queue_insert_front(message, steer_id=entry["id"])
        if qid is None:
            # Acceptance reserves a slot per in-flight steer, so this should be
            # unreachable. Log loudly rather than broadcasting a null id: a
            # phantom card the user can neither cancel nor run is worse than a
            # recorded gap.
            logger.error(
                "Side steer requeue found no room: slot=%s depth=%d",
                slot.key,
                len(side.queue),
            )
            continue
        broadcast_side_queue(
            state,
            slot_key=slot.key,
            action="push",
            queue_id=qid,
            content=message,
            depth=len(side.queue),
            front=True,
            # Name the steer this card came from. The card's id is brand new, so a
            # client holding the raw text of its own steer has no other way to
            # recognise it — and the content on the wire is redacted, which is a
            # permanently corrupted restore for a credential-bearing question.
            steer_id=entry["id"],
        )
    logger.info(
        "Requeued %d unconsumed side steer(s): slot=%s run_id=%s",
        len(orphaned),
        slot.key,
        run_id,
    )


async def _try_side_steer(state: DashboardState, slot, question: str) -> str | None:
    """Inject *question* into the side turn already streaming on this slot.

    Returns the ledger id the attempt was recorded under, or None when it was
    never attempted (no session, no steer support, no live turn) or the write
    failed — every one of which must fall through to the queue rather than drop
    the user's text.

    The entry is registered BEFORE the RPC suspends. ``steer()`` returning True
    only proves the bytes left; the ``steering_consumed`` echo is what proves
    injection. Registering first is what makes the ordering safe: if the turn's
    ``finally`` runs while this is suspended, it already sees the entry and
    requeues it. The caller then reads its OWN entry's state, so it never has to
    infer an outcome from an absence.
    """
    side = slot._side
    if side is None:
        return None
    provider = state.sessions.get_provider(_side_session_key(slot.key, side.gen))
    if provider is None or not getattr(provider, "supports_steer", False):
        return None
    steer = getattr(provider, "steer", None)
    if steer is None:
        return None
    # ``has_active_turn`` gates on a GENUINELY live prompt: the provider stays
    # registered through post-turn bookkeeping, and kiro-cli silently swallows a
    # steer for a prompt that already ended. Absent the probe, attempt the steer
    # and let its boolean decide.
    has_active = getattr(provider, "has_active_turn", None)
    if has_active is not None and not has_active():
        return None
    side = slot._side
    if side is None:
        return None
    if not side.steer_can_accept():
        # No room to requeue this steer if the turn ends without consuming it, and
        # a requeue that cannot land would drop a question already reported as
        # delivered. Decline here instead: the caller falls through to the queue,
        # which either takes it or answers 429 — both visible, both recoverable.
        return None
    steer_id = side.steer_register(question)
    if steer_id is None:
        # Every ledger slot is an in-flight steer. Fall through to the queue
        # rather than accept a steer whose outcome we could not record.
        return None
    try:
        delivered = bool(await steer(question))
    except Exception as exc:
        logger.warning("Side steer failed for slot %s: %s", slot.key, exc)
        delivered = False
    if not delivered:
        # The write did not land. If the entry is still pending it is ours to
        # discard; if the echo or the finally already resolved it, leave their
        # outcome alone and let the caller read it.
        if side.steer_state(steer_id) == STEER_PENDING:
            side.steers[:] = [e for e in side.steers if e["id"] != steer_id]
            return None
    return steer_id


def _check_slot_ownership(
    request: web.Request,
    slot,
    operation: str,
) -> web.Response | None:
    """The uniform app-isolation 404 if the request app can't access ``slot``, else None.

    App Kit §5.2: dashboard users (empty ``request_app``) can access everything.
    The auth gate is upstream in ``token_auth_middleware`` and the per-slot
    checkpoint (``slot_ownership_middleware``) has already made this decision for
    every /side route; this re-applies the same shared decision at the handler.
    """
    return deny_app_slot_access(request.get("app", ""), slot, slot.key, operation)


async def _read_side_queue_body(request: web.Request) -> str | web.Response:
    """Parse ``{"content": str}`` from a side-queue edit. Returns the trimmed
    content, or the error response to send."""
    if not request.body_exists:
        return web.json_response({"error": "missing JSON body", "code": "missing_body"}, status=400)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body", "code": "invalid_body"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object", "code": "invalid_body"},
            status=400,
        )
    content = body.get("content")
    if not isinstance(content, str):
        return web.json_response(
            {"error": "content must be a string", "code": "invalid_content"},
            status=400,
        )
    content = content.strip()
    if not content:
        return web.json_response(
            {"error": "content must not be empty", "code": "empty_content"},
            status=400,
        )
    if len(content.encode("utf-8")) > _MAX_QUESTION_BYTES:
        return web.json_response(
            {
                "error": f"content too long (max {_MAX_QUESTION_BYTES} bytes)",
                "code": "content_too_long",
            },
            status=400,
        )
    return content


def _resolve_side_queue_slot(
    request: web.Request, operation: str
) -> tuple[Any, web.Response | None]:
    """Shared lookup for the side-queue mutation endpoints.

    Returns ``(slot, None)`` on success or ``(None, response)`` to send.
    """
    state: DashboardState = request.app["state"]
    slot = state._slots.get(request.match_info["slot"])
    if not slot:
        return None, slot_not_found()
    own = _check_slot_ownership(request, slot, operation)
    if own is not None:
        return None, own
    if slot._side is None or not slot._side.open:
        return None, web.json_response(
            {"error": "side conversation is not open", "code": "side_not_open"},
            status=409,
        )
    return slot, None
