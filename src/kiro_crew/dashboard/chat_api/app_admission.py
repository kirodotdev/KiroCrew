"""App-token admission: whether an app may take a slot for a send, a create or a resume,
may send to or cancel on a slot, or may change its session settings, and the re-check
a slot write runs after every await.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_handlers import (
        _RESUME_APP_NOT_FOUND,
        SESSION_CONTROL_DENIED,
        DashboardState,
        ResumeRefusal,
        _ChatSlot,
        _history_key_for,
        _normalize_slot_key,
        app_may_control_session,
        app_new_key_refusal,
        app_owns_slot_session,
        app_owns_transcript_meta,
        audit_app_slot_denial,
        deny_app_slot_access,
        deny_app_slot_session_access,
        own_session_key,
        read_session_grant,
        sel,
        session_grant,
        slot_not_found,
        transcript_acquisition_reason,
    )


async def _app_slot_acquisition_denial(
    request: web.Request,
    state: "DashboardState",
    request_app: str,
    slot_name: str,
    operation: str,
    *,
    session_grant_route: bool,
) -> tuple[web.Response | None, Any]:
    """The app-ownership decision for a request that may CREATE the slot it names.

    ``POST /api/chat`` and ``POST /api/chat/slots`` resolve their slot through
    ``get_or_create_slot``, which refuses a memory-mode mismatch or an
    under-construction slot with a 409. That 409 must not tell an app that a
    session it may not see exists, or what its memory mode is, so for an app
    caller this runs first, on a lookup that creates nothing, and answers the
    same 404 for every refusal:

    * a member, cron or workflow key (:func:`app_reserved_key_reason`): an app
      never owns one, and the cron and workflow binders would link a slot under
      that key to the job's or run's own transcript. So is a ``dashboard_`` key,
      whose transcript is another slot's;
    * a key under construction, in any letter case: a session being imported is
      nobody's to name yet (the person still gets the 409; the app gets the 404,
      with no audit row);
    * a key that differs from another live slot's key, or names its transcript,
      only in letter case (:func:`live_case_alias_reason`): on a case-insensitive
      filesystem both slots would write one file;
    * a live slot: the per-slot decision (owner app on its own session and
      transcript, or the ``sessionApproval`` grant when *session_grant_route*);
    * no live slot but a persisted transcript under that key: the transcript must
      record this app (:func:`transcript_acquisition_reason`). Without it a closed
      user session could be re-created as an app-owned slot that reopens and
      appends to the user's transcript.

    *slot_name* is the RAW requested name, normalized here exactly once, as
    ``get_or_create_slot`` will normalize it. Returns ``(refusal, judged)``:
    *judged* is the live slot the decision was made on, or ``None``. It is an
    early out, not the last word: the caller awaits again before it acquires the
    slot, so it re-checks synchronously that the slot it acquires is *judged*
    (:func:`_acquired_slot_was_judged`) and keeps its own post-create check.
    """
    key = _normalize_slot_key(slot_name)
    history_key = _history_key_for(key)
    granted = False
    if session_grant_route:
        # Read before anything about the slot is looked at, for every app caller,
        # so the cost of the answer does not depend on which session was named.
        granted = await session_grant(request, request_app)
    refused, reason = app_new_key_refusal(state, key, history_key)
    if refused:
        if reason:
            audit_app_slot_denial(request_app, operation, key, reason)
        return slot_not_found(), None
    existing = state._slots.get(key)
    if existing is not None:
        allowed = (
            app_may_control_session(request_app, existing, granted)
            if session_grant_route
            else app_owns_slot_session(request_app, existing)
        )
        if allowed:
            return None, existing
        audit_app_slot_denial(request_app, operation, key, SESSION_CONTROL_DENIED)
        return slot_not_found(), None
    log = state.conversation_log
    if log is None:
        return None, None
    reason = await asyncio.to_thread(transcript_acquisition_reason, log, history_key, request_app)
    if not reason:
        return None, None
    audit_app_slot_denial(request_app, operation, key, reason)
    return slot_not_found(), None


async def _app_slot_acquisition_recheck(
    state: "DashboardState", request_app: str, key: str, judged: Any, operation: str
) -> web.Response | None:
    """Recheck a named acquisition after setup, without publishing an unjudged slot.

    A free key is reserved through the off-loop transcript read. Other acquirers
    respect the construction marker, so a create-and-close cannot hide between
    that read and the loop resuming. The caller must acquire synchronously after
    this returns; releasing the marker does not yield.
    """
    history_key = _history_key_for(key)
    refused, reason = app_new_key_refusal(state, key, history_key)
    if not refused and not _acquired_slot_was_judged(state, key, judged):
        refused, reason = True, "slot was replaced while the request was authorized"
    if not refused and judged is None and state._slots.get(key) is None:
        state.begin_slot_construction(key)
        try:
            log = state.conversation_log
            if log is not None:
                reason = await asyncio.to_thread(
                    transcript_acquisition_reason, log, history_key, request_app
                )
                refused = bool(reason)
        finally:
            state.end_slot_construction(key)
        if not refused:
            refused, reason = app_new_key_refusal(state, key, history_key)
        if not refused and state._slots.get(key) is not None:
            refused, reason = True, "slot was replaced while the request was authorized"
    if refused:
        if reason:
            audit_app_slot_denial(request_app, operation, key, reason)
        return slot_not_found()
    return None


def _acquired_slot_was_judged(state: "DashboardState", key: str, judged: Any) -> bool:
    """Whether the slot about to be acquired under *key* is the one ownership was decided on.

    Synchronous, for the moment right before ``get_or_create_slot``. A slot the
    pre-check approved that has since been closed would otherwise be minted
    afresh, app-owned, on the key of a session the app was only granted to act
    in -- and the post-create check passes on the new object. A slot that
    appeared where none was judged is decided by the post-create check; that
    acquisition performs no transcript-read await.
    """
    return judged is None or state._slots.get(key) is judged


def _app_acquisition_conflict(
    state: "DashboardState", request_app: str, key: str, judged: Any, exc: ValueError
) -> web.Response:
    """An app's answer when ``get_or_create_slot`` refused *key* with a ValueError.

    The 409 text stays only for the app's OWN live slot, the one the pre-check
    judged and that still holds *key*: it tells the app nothing it does not
    already know, and a 404 there would tell it its live session is gone. Every
    other conflict is the uniform 404, because its text names a session the app
    may not see.
    """
    if (
        judged is not None
        and state._slots.get(key) is judged
        and key not in getattr(state, "_slots_under_construction", ())
        and app_owns_slot_session(request_app, judged)
    ):
        return web.json_response({"error": str(exc)}, status=409)
    return slot_not_found()


def _deny_app_yolo(request_app: str, operation: str) -> web.Response:
    """App tokens never arm or revoke the process-global YOLO override."""
    sel().log_api_access(
        caller=request_app,
        operation=operation,
        outcome="denied",
        source="app_isolation",
        error="app tokens cannot arm yolo",
    )
    return web.json_response(
        {"ok": False, "error": "app tokens cannot arm yolo", "code": "app_yolo_forbidden"},
        status=403,
    )


def _deny_app_session_settings(request_app: str, slot_key: str, trigger: str) -> web.Response:
    """App tokens send turns to a user's session but never change its settings.

    *trigger* names what the send would have changed (``agent=<name>``,
    ``field=color_theme`` or ``command=<word>``) and rides the SEL row.
    """
    reason = (
        "app cannot change the agent binding or persona settings of a session it "
        "does not own, or run a harness slash command there"
    )
    sel().log_api_access(
        caller=request_app,
        operation="chat_send",
        outcome="denied",
        source="app_isolation",
        resources=f"slot={slot_key} {trigger}",
        error=reason,
    )
    return web.json_response(
        {"error": reason, "code": "app_session_settings_forbidden"}, status=403
    )


def _slot_not_found() -> web.Response:
    """The uniform per-slot 404, :func:`slot_ownership.slot_not_found`."""
    return slot_not_found()


async def _app_may_send_to_slot(request_app: str, slot: Any) -> bool:
    """Whether *request_app* may control *slot* through the session APIs.

    The session-control rule (:func:`app_may_control_session`) on a live read of
    the ``sessionApproval`` grant, judged after that read completes.
    """
    if not request_app:
        return True
    granted = await read_session_grant(request_app)
    return app_may_control_session(request_app, slot, granted)


async def _app_claim_refused(
    state: DashboardState, request_app: str, operation: str, keys: Iterable[str]
) -> bool:
    """True, audited, when an app's NEW slot would bind to a transcript it does not own.

    Any of *keys* (the transcripts the new slot would read or save) that exists
    and does not record *request_app* refuses it; see
    ``handlers.sessions._app_may_claim_transcript``. The caller answers with the
    uniform ``slot_not_found`` 404. A dashboard caller (no app) is never refused.
    """
    log = state.conversation_log
    if not request_app or log is None:
        return False
    # Imported here: this module imports the handlers package at module scope.
    from kiro_crew.dashboard.handlers.sessions import (
        _NOT_TRANSCRIPT_OWNER,
        _app_may_claim_transcript,
        _audit_app_allow,
        _audit_app_denial,
    )

    claimed = list(dict.fromkeys(keys))
    for key in claimed:
        if not await asyncio.to_thread(_app_may_claim_transcript, log, request_app, key):
            _audit_app_denial(request_app, operation, f"session={key}", _NOT_TRANSCRIPT_OWNER)
            return True
    for key in claimed:
        _audit_app_allow(request_app, operation, f"session={key}")
    return False


def _app_cancel_denied(
    request: web.Request | Mapping[str, Any], slot: _ChatSlot, operation: str, target_key: str
) -> web.Response | None:
    """Whether *request* may cancel *target_key*, as an indistinguishable 404.

    Two conditions for an app token, because slot ownership does NOT imply
    ownership of the session the cancel would land on:

    1. the app owns the slot (App Kit §5.2, deny-by-default), and
    2. the session about to be cancelled is still the slot's own session
       (``slot_ownership.own_session_key``), not one the app has no claim on.

    Condition 2 is load-bearing. ``get_or_create_slot`` takes ``app`` and, for a
    name shaped like a channel session stem, resolves ``linked_session_key``
    from the session map in the same call — so an app that names a live channel
    thread ends up owning a slot bound to a conversation it has no claim on.
    Ownership alone would then authorize cancelling that channel's turn, turning
    a slot binding into capability escalation.

    It tests *target_key* — the key the caller will actually cancel — rather
    than re-reading the slot, so authorization and action cannot disagree. That
    is not only a TOCTOU guard: for a turn that started on the app's own session
    and was rebound mid-flight, re-reading would DENY the app its own running
    turn, because the routing now points somewhere it does not own.

    A dashboard caller has no app scope and may cancel either kind.

    Shared by the cancel routes so /stop and /interrupt cannot drift onto two
    policies. Condition 1 is :func:`deny_app_slot_access`, the decision the
    per-slot checkpoint already made; condition 2 is layered on top of it.
    """
    request_app = request.get("app", "")
    denied = deny_app_slot_access(request_app, slot, slot.key, operation)
    if denied is not None or not request_app:
        return denied
    if target_key == own_session_key(slot):
        return None
    audit_app_slot_denial(
        request_app, operation, slot.key, "app does not own the session this slot is linked to"
    )
    return slot_not_found()


async def _app_resume_refusal(
    state: "DashboardState", request_app: str, name: str, history_key: str, meta: dict
) -> ResumeRefusal | None:
    """Whether an app may resume *history_key* into a slot published as *name*.

    Two transcripts are at stake, and both must be the app's. The one READ is
    *history_key*, whose metadata line *meta* must record this app. The one the
    new slot WRITES is ``dashboard:<name>``, because a resumed slot saves under
    its own key: when that is a different key, the same acquisition rule as
    send and create applies to it (no member, cron or workflow name; no
    transcript there that records anyone else). Without the second check an app
    could load its own transcript under the name of a person's closed session
    and be handed that session's live slot.

    A publish name under construction is the same 404, with no audit row, as on
    send and create: a session being imported is nobody's to name yet. So is one
    that aliases another live slot by letter case, recorded as on send and create.
    """
    log = state.conversation_log
    if not app_owns_transcript_meta(meta, request_app):
        # Re-read to file the refusal under its real reason: an unreadable line
        # is a read fault, not an isolation breach, and no transcript at all is a
        # missing session, which is answered without an audit row.
        reason = ""
        if log is not None:
            reason = await asyncio.to_thread(
                transcript_acquisition_reason, log, history_key, request_app
            )
        if reason:
            audit_app_slot_denial(request_app, "slot_resume", name, reason)
        return _RESUME_APP_NOT_FOUND
    publish_key = _history_key_for(name)
    refused, reason = app_new_key_refusal(state, name, publish_key)
    if not refused and publish_key != history_key and log is not None:
        reason = await asyncio.to_thread(
            transcript_acquisition_reason, log, publish_key, request_app
        )
        refused = bool(reason)
    if reason:
        audit_app_slot_denial(request_app, "slot_resume", name, reason)
    return _RESUME_APP_NOT_FOUND if refused else None


def _reauthorize_after_await(
    state: DashboardState, slot: _ChatSlot, name: str, request_app: str, operation: str
) -> web.Response | None:
    """Re-authorize *slot* after an await, immediately before touching it.

    The ownership gate necessarily runs before the request body is read, and
    that ``await`` is a window rather than a formality: ``linked_session_key``
    is rebound on ALREADY-LIVE slots with no ``running`` gate -- a cron
    completion (``cron_inject.py:96``), a workflow injection
    (``workflow_inject.py:156``) -- so a slow caller can be authorized against
    its own session and land on somebody else's conversation. The same identity
    check ``_app_cancel_denied`` makes for /stop, moved to the point of use.

    Requires the same slot OBJECT, not just the same name: a delete and
    re-create under one name would pass an ownership re-check while being a
    different conversation. Callers must run this before the first read of slot
    state too, since ``running`` and the hold queue belong to whichever
    conversation the slot now routes to.
    """
    if state._slots.get(name) is not slot:
        if request_app:
            sel().log_api_access(
                caller=request_app,
                operation=operation,
                outcome="denied",
                source="app_isolation",
                resources=f"slot={name}",
                error="slot was replaced while the request body was read",
            )
        return slot_not_found()
    return deny_app_slot_session_access(request_app, slot, name, operation)
