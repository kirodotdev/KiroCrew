"""The app-ownership decision every per-slot route goes through.

``/api/chat/slots/{slot}/...`` is one URL family, and an app's ``permissions.api``
grant is a PREFIX match (``token_auth._api_pattern_matches``): an app granted
``/api/chat`` reaches every path under it. Whether an app may act on a slot is
therefore decided once for the whole family rather than by each handler.

:func:`slot_ownership_middleware` decides it once, at the route, for every handler
registered under the family, whatever the slot segment is called:

* A caller with no app claim (the dashboard user, an internal-secret call with no
  derived app) is untouched here. Handlers keep their own owner and identity
  checks for those callers.
* An app caller passes only when it is the slot's OWNER app,
  ``slot._app == request["app"]``, AND the slot still runs on its own session and
  writes its own transcript (:func:`app_owns_slot_session`). Identity is positive:
  a slot with no app scope is refused, never read as "nobody's, so anyone's".
* Every refusal is the same 404 ``slot_not_found`` body, a missing slot included,
  so no response on these routes tells "not yours" from "does not exist". A
  refusal for a slot that exists is recorded in the security-event log; a name
  that matches no slot is not, so an app polling a closed tab cannot flood it.

The decision is keyed by the PATH segment. A handler that reads another slot key
from the body, query or a header decides that key itself.

A route takes a different decision only through :data:`SLOT_ROUTE_POLICIES`, with
a written reason. ``test/test_slot_ownership_checkpoint.py`` enumerates every
route under ``/api/chat/slots/{...}`` straight from the router and pins that the
checkpoint decides each one, so a new per-slot route is owner-gated by
construction and an exception is a reviewed edit.

The middleware publishes the slot object it judged as ``request[CHECKPOINT_SLOT_KEY]``.
A handler that awaits before it looks the slot up compares against it
(:func:`checkpoint_slot_replaced`), so a same-name replacement inside that await
is refused rather than acted on unjudged.

Handler-level calls to :func:`deny_app_slot_access` stay at the routes that carry
them. Behind the checkpoint they never refuse; they catch a handler mounted
outside the chain (a test app, a future router).
"""

from __future__ import annotations

import asyncio
import enum
import logging
import re
import threading
from collections import OrderedDict
from time import monotonic
from typing import Any, Awaitable, Callable

from aiohttp import web

from kiro_crew import members as members_mod
from kiro_crew.apps import permissions as app_permissions
from kiro_crew.dashboard.chat_utils import (
    _history_key_for,
    effective_session_key,
    slot_history_key,
)
from kiro_crew.dashboard.state import SlotOrigin
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: The per-slot URL family, matched against a route's canonical template. ANY
#: single path parameter in the slot position counts (``{slot}`` almost everywhere,
#: ``{name}`` on the channel-link duplicates), so a new spelling is owner-gated
#: by default rather than slipping past the matcher.
_SLOT_ROUTE_RE = re.compile(r"^/api/chat/slots/\{(?P<param>[^}/]+)\}(?:/|$)")


class SlotRoutePolicy(enum.Enum):
    """How the checkpoint decides an app caller on one per-slot route."""

    #: The default: only the slot's owner app, on its own session and transcript.
    OWNER = "owner"
    #: :func:`app_may_control_session`: the owner app, or an app holding
    #: ``permissions.sessionApproval`` acting on a local user session. The
    #: handler additionally requires the grant from every app caller.
    SESSION_GRANT = "session_grant"
    #: The route addresses a slot that is usually not live yet, so there is no
    #: slot object to judge here; the handler makes the ownership decision.
    HANDLER = "handler"


#: Every per-slot route that does NOT take the :attr:`SlotRoutePolicy.OWNER`
#: default, keyed by ``(method, canonical template)``, with the reason.
SLOT_ROUTE_POLICIES: dict[tuple[str, str], tuple[SlotRoutePolicy, str]] = {
    ("POST", "/api/chat/slots/{slot}/approve"): (
        SlotRoutePolicy.SESSION_GRANT,
        "approving or denying a pending tool request is what permissions.sessionApproval "
        "grants on a local user session",
    ),
    ("POST", "/api/chat/slots/{slot}/resume"): (
        SlotRoutePolicy.HANDLER,
        "resume opens a persisted transcript that usually has no live slot; "
        "resume_slot_from_history checks the live slot, the key it publishes under "
        "and the transcript it loads",
    ),
}

#: The SEL ``source`` every app-isolation decision is filed under, shared with the
#: handler-level checks so one query finds all of them.
APP_ISOLATION_SOURCE = "app_isolation"

#: The audit reason for a refused session-control decision.
SESSION_CONTROL_DENIED = "app may not control this session"

#: Request key for the slot object the checkpoint judged (``None`` for none).
CHECKPOINT_SLOT_KEY = "slot_ownership.checkpoint_slot"

#: Request key memoizing this request's ``permissions.sessionApproval`` read.
_SESSION_GRANT_KEY = "slot_ownership.session_grant"

#: Slot-key prefixes the gateway mints for sessions no app owns: a cron job's tab
#: (``cron-<job_id>``) and a workflow result tab (``workflow-<run_id>``). Their
#: binders link the slot found under that key to the job's or run's own
#: transcript, so an app that could pick such a name would be handed that
#: transcript. Same spellings as ``session_control.CRON_SLOT_PREFIX`` and
#: ``WORKFLOW_SLOT_PREFIX`` (pinned by a test; importing them here would cycle).
CRON_SLOT_PREFIX = "cron-"
WORKFLOW_SLOT_PREFIX = "workflow-"

#: A task-runner result tab, ``task-review-<token>``. ``handlers/taskrunner``
#: mints it linked to ``taskrunner:<task_id>:chat:<token>``, a session it creates
#: for that one tab, and stamps it with the app the task ran for. That link is the
#: tab's own session, not a foreign one.
TASK_REVIEW_SLOT_PREFIX = "task-review-"
_TASK_REVIEW_SESSION_PREFIX = "taskrunner:"


def task_review_session_key(task_id: str, token: str) -> str:
    """The session ``task-review-<token>`` is minted on for the task *task_id*."""
    return f"{_TASK_REVIEW_SESSION_PREFIX}{task_id}:chat:{token}"


def _route_decision(method: str, canonical: str) -> tuple[str, SlotRoutePolicy] | None:
    """``(slot param, policy)`` for one route, or None outside the per-slot family."""
    found = _SLOT_ROUTE_RE.match(canonical)
    if found is None:
        return None
    entry = SLOT_ROUTE_POLICIES.get((method, canonical))
    return found.group("param"), entry[0] if entry is not None else SlotRoutePolicy.OWNER


def slot_route_param(canonical: str) -> str | None:
    """The match-info key naming the slot, or None when *canonical* is not per-slot."""
    found = _SLOT_ROUTE_RE.match(canonical)
    return found.group("param") if found else None


def slot_route_policy(method: str, canonical: str) -> SlotRoutePolicy | None:
    """The checkpoint's policy for one route, or None outside the per-slot family."""
    decision = _route_decision(method, canonical)
    return decision[1] if decision is not None else None


def slot_not_found() -> web.Response:
    """The one 404 an app gets for a slot it may not act on, or that does not exist."""
    return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)


def _record_isolation_denial(caller: str, operation: str, resources: str, reason: str) -> None:
    """One app-isolation SEL row. Best-effort: a log fault never changes the refusal."""
    try:
        sel().log_api_access(
            caller=caller,
            operation=operation,
            outcome="denied",
            source=APP_ISOLATION_SOURCE,
            resources=resources,
            error=reason,
        )
    except Exception:  # noqa: BLE001 - the refusal must still be returned
        logger.warning("failed to record an app-isolation denial", exc_info=True)


_ALLOW_AUDIT_WINDOW_SECS = 300.0
_ALLOW_AUDIT_MAX_ENTRIES = 1024
#: (app, operation, slot) -> (last emission time, suppressed count), oldest emission first.
_allow_audits: OrderedDict[tuple[str, str, str], tuple[float, int]] = OrderedDict()
_allow_audit_lock = threading.Lock()


def _record_isolation_allow(caller: str, operation: str, name: str) -> None:
    """Bound repeated app grants; eviction discards the oldest window and its count."""
    try:
        key = (caller, operation, name)
        with _allow_audit_lock:
            now = monotonic()
            entry = _allow_audits.get(key)
            if entry is not None and now - entry[0] < _ALLOW_AUDIT_WINDOW_SECS:
                _allow_audits[key] = (entry[0], entry[1] + 1)
                return
            suppressed = entry[1] if entry is not None else 0
            _allow_audits[key] = (now, 0)
            _allow_audits.move_to_end(key)
            if len(_allow_audits) > _ALLOW_AUDIT_MAX_ENTRIES:
                _allow_audits.popitem(last=False)
        # Reserve the window before logging, so a failed sink cannot cause a retry flood.
        resources = f"slot={name}"
        if suppressed:
            resources += f" (suppressed={suppressed})"
        sel().log_api_access(
            caller=caller,
            operation=operation,
            outcome="allowed",
            source=APP_ISOLATION_SOURCE,
            resources=resources,
        )
    except Exception:  # noqa: BLE001 - logging must not change the checkpoint verdict
        logger.warning("failed to record an app-isolation allow", exc_info=True)


def audit_app_slot_denial(request_app: str, operation: str, name: str, reason: str) -> None:
    """Record one app-isolation refusal of *request_app* on the slot *name*."""
    _record_isolation_denial(request_app, operation, f"slot={name}", reason)


def app_owns_slot(request_app: str, slot: Any) -> bool:
    """Whether *request_app* is positively the owner app of *slot*."""
    return bool(request_app) and slot is not None and getattr(slot, "_app", "") == request_app


def own_session_key(slot: Any) -> str:
    """The session and transcript key that belong to *slot* itself.

    ``dashboard:<key>``, except for a task-runner result tab, whose own session is
    the ``taskrunner:<task_id>:chat:<token>`` link minted for it
    (:data:`TASK_REVIEW_SLOT_PREFIX`). Judged on the key shape the gateway mints:
    the token is a fresh random id, and no request can set a slot's link.
    """
    key = str(getattr(slot, "key", "") or "")
    linked = str(getattr(slot, "linked_session_key", "") or "")
    token = key[len(TASK_REVIEW_SLOT_PREFIX) :] if key.startswith(TASK_REVIEW_SLOT_PREFIX) else ""
    suffix = f":chat:{token}"
    if (
        token
        and linked.startswith(_TASK_REVIEW_SESSION_PREFIX)
        and linked.endswith(suffix)
        and len(linked) > len(_TASK_REVIEW_SESSION_PREFIX) + len(suffix)
    ):
        return linked
    return _history_key_for(key)


def _foreign_binding_reason(slot: Any) -> str:
    """Why an app's own slot still reaches a conversation it has no claim on, or ``""``.

    Owning the slot does not imply owning the session it runs on or the transcript
    it writes: a slot named like a channel stem is linked to that thread, and an
    unbound channel-origin slot writes the channel's transcript. Authorize the key
    the request will really act on, against :func:`own_session_key`.
    """
    own_key = own_session_key(slot)
    if effective_session_key(slot) != own_key:
        return "app does not own the session this slot is linked to"
    if slot_history_key(slot) != own_key:
        return "app does not own the transcript this slot writes to"
    return ""


def app_owns_slot_session(request_app: str, slot: Any) -> bool:
    """Owner app AND the slot still runs on its own session and transcript."""
    return app_owns_slot(request_app, slot) and not _foreign_binding_reason(slot)


def deny_app_slot_access(
    request_app: str, slot: Any, name: str, operation: str
) -> web.Response | None:
    """The slot-ownership decision: None when the caller may act, else the uniform 404.

    An empty *request_app* is a caller with no app scope and always passes; the
    dashboard owner's own reach is decided by the handler, not here. This is the
    slot half only; :func:`deny_app_slot_session_access` adds the session and
    transcript halves the checkpoint applies.
    """
    if not request_app or app_owns_slot(request_app, slot):
        return None
    if slot is not None:
        reason = (
            "app does not own this slot"
            if getattr(slot, "_app", "")
            else "app cannot access unscoped slots"
        )
        audit_app_slot_denial(request_app, operation, name, reason)
    return slot_not_found()


def deny_app_slot_session_access(
    request_app: str, slot: Any, name: str, operation: str
) -> web.Response | None:
    """:func:`deny_app_slot_access` plus the session and transcript halves."""
    denied = deny_app_slot_access(request_app, slot, name, operation)
    if denied is not None or not request_app:
        return denied
    reason = _foreign_binding_reason(slot)
    if not reason:
        return None
    audit_app_slot_denial(request_app, operation, name, reason)
    return slot_not_found()


def app_reserved_key_reason(slot_key: str, history_key: str) -> str:
    """Why an app may never hold the slot *slot_key*, whose history key is *history_key*, or ``""``.

    Judged on the HISTORY key, the one every spelling of a slot name folds to, so
    a doubled ``dashboard:`` prefix or a ``dashboard_`` stem cannot slip a member,
    cron or workflow key past the check. A slot key that is not the bare form of
    its own history key (``dashboard_s9`` writes ``dashboard:s9``) would share a
    transcript with the slot that IS that bare form, so an app may not hold one.
    """
    bare = history_key.split(":", 1)[1] if history_key.startswith("dashboard:") else history_key
    if bare != slot_key:
        return "app cannot name a session by another session's transcript key"
    folded = bare.casefold()
    if folded.startswith(members_mod.DM_SLOT_KEY_PREFIX):
        return "app cannot access member slots"
    if folded.startswith((CRON_SLOT_PREFIX, WORKFLOW_SLOT_PREFIX)):
        return "app cannot name a cron or workflow session"
    return ""


#: The audit reason for a requested key that names another live session up to letter case.
CASE_ALIAS_DENIED = "another live session has this key or transcript up to letter case"


def under_construction_alias(state: Any, slot_key: str) -> bool:
    """Whether *slot_key*, in any letter case, is a key under construction."""
    folded = slot_key.casefold()
    return any(
        str(key).casefold() == folded for key in getattr(state, "_slots_under_construction", ())
    )


def live_case_alias_reason(state: Any, slot_key: str, history_key: str) -> str:
    """Why *slot_key* would share ANOTHER live slot's transcript, or ``""``.

    On a case-insensitive filesystem (the macOS and Windows defaults)
    ``dashboard_CHAT-1.jsonl`` and ``dashboard_chat-1.jsonl`` are one file, so a
    slot whose key or transcript key differs from a live slot's only in letter
    case writes that slot's transcript. The transcript check cannot see it while
    the live slot has written nothing yet, so the live table is compared here,
    casefolded, whatever filesystem this host runs on. The slot under exactly
    *slot_key* is the caller's own decision, not an alias.
    """
    folded_key, folded_history = slot_key.casefold(), history_key.casefold()
    for live_key, slot in list(getattr(state, "_slots", {}).items()):
        if live_key == slot_key:
            continue
        if live_key.casefold() == folded_key or (
            slot_history_key(slot).casefold() == folded_history
        ):
            return CASE_ALIAS_DENIED
    return ""


def app_new_key_refusal(state: Any, slot_key: str, history_key: str) -> tuple[bool, str]:
    """Whether an app may not name *slot_key* for a slot it would create, and why.

    Returns ``(refused, reason)``. The checks shared by send, create and resume's
    publish name, in order: a reserved key (:func:`app_reserved_key_reason`), a
    key under construction in any letter case, and a letter-case alias of another
    live slot (:func:`live_case_alias_reason`). An empty *reason* on a refusal is
    the key under construction, which is answered with no audit row: a session
    being imported is nobody's to name yet.
    """
    reserved = app_reserved_key_reason(slot_key, history_key)
    if reserved:
        return True, reserved
    if under_construction_alias(state, slot_key):
        return True, ""
    alias = live_case_alias_reason(state, slot_key, history_key)
    return bool(alias), alias


def app_holds_gateway_key(state: Any, name: str, operation: str, actor: str = "gateway") -> bool:
    """Whether an app owns the live slot under a key the gateway mints for itself.

    For the binders that find a ``cron-<job_id>`` or ``workflow-<run_id>`` slot
    by name and link it to the job's or run's own transcript. An app-owned slot
    under such a key is never adopted by them: linking it would hand the app
    that transcript, and the binder's rows would land in the app's session. The
    binder stands down instead, and its result stays in the job's or run's own
    record. Apps cannot pick these names (:func:`app_reserved_key_reason`); this
    covers a slot that holds one anyway.

    The refusal is recorded under *actor*, who asked for the bind (the gateway
    for a run, the request's caller for a to-chat click), with the holder app in
    the resources: the holder made no request, so it is not filed as the caller.
    """
    getter = getattr(state, "get_slot", None)
    slot = getter(name) if getter is not None else getattr(state, "_slots", {}).get(name)
    holder = getattr(slot, "_app", "") if slot is not None else ""
    if not isinstance(holder, str) or not holder:
        return False
    logger.warning("not adopting the app-owned slot %s for %s", name, operation)
    _record_isolation_denial(
        actor,
        operation,
        f"slot={name} holder={holder}",
        "an app-owned slot holds a gateway-minted key",
    )
    return True


def app_owns_transcript_meta(meta: Any, request_app: str) -> bool:
    """Whether the transcript metadata line *meta* records *request_app* as its owner app.

    Positive identity: a line with no recorded app is the person's, never an
    app's. Distinct from ``token_auth.app_owns_transcript``, which asks whether a
    LIVE slot of the app writes that transcript; this reads the persisted record,
    for a session that has no live slot.
    """
    return (
        bool(request_app) and isinstance(meta, dict) and str(meta.get("app") or "") == request_app
    )


def transcript_acquisition_reason(log: Any, history_key: str, request_app: str) -> str:
    """Why *request_app* may not take over the session stored at *history_key*, or ``""``.

    Blocking (a stat and a first-line read); run it off the loop. No transcript
    there is nobody's session and may be created. One that exists must record
    this app. An unreadable metadata line refuses too, under its own reason so a
    read fault is not filed as an isolation breach, and a key the filesystem
    rejects (an over-long name) refuses instead of raising.
    """
    try:
        if not log.has_log(history_key):
            return ""
        meta, readable = log.get_metadata_status(history_key)
    except (OSError, ValueError):
        return "transcript key cannot be read"
    if not readable:
        return "transcript metadata unreadable"
    if app_owns_transcript_meta(meta, request_app):
        return ""
    return "app does not own this transcript"


def app_slot_is_local_user_session(slot: Any) -> bool:
    """A USER-origin, dashboard-run slot: the only kind the grant reaches.

    Judged on the EFFECTIVE session, not the origin alone -- a user-created slot
    that a cron injection or channel binder re-linked to ``cron:<id>`` /
    ``slack:<ts>`` runs its turns on that foreign session.
    """
    if str(getattr(slot, "_origin", "") or "") != SlotOrigin.USER:
        return False
    if getattr(slot, "mode", "") == "member" or getattr(slot, "executor", "") == "remote":
        return False
    return effective_session_key(slot).startswith("dashboard:")


async def read_session_grant(request_app: str) -> bool:
    """One live read of the app's ``permissions.sessionApproval`` grant, off the loop."""
    return bool(
        await asyncio.to_thread(app_permissions.app_can_manage_session_approvals, request_app)
    )


async def session_grant(request: web.Request, request_app: str, *, fresh: bool = False) -> bool:
    """This request's grant verdict, read once and shared by every check in it.

    Never cached across requests: the manifest is read live so that removing the
    flag revokes the grant at once. *fresh* re-reads, for a check that runs after
    a long await such as a body upload.
    """
    cached = request.get(_SESSION_GRANT_KEY)
    if fresh or not isinstance(cached, bool):
        cached = await read_session_grant(request_app)
        request[_SESSION_GRANT_KEY] = cached
    return cached


def app_may_control_session(request_app: str, slot: Any, granted: bool) -> bool:
    """The session-control rule, given the grant verdict.

    The owner app on its own session and transcript (:func:`app_owns_slot_session`),
    or an app holding the grant on a local user session.
    """
    if not request_app:
        return True
    if getattr(slot, "_app", ""):
        return app_owns_slot_session(request_app, slot)
    return granted and app_slot_is_local_user_session(slot)


def _slot_still_live(request: web.Request, slot: Any) -> bool:
    """Whether *slot* is still the live slot under its own key in this app's state."""
    state = request.app.get("state") if request.app is not None else None
    slots = getattr(state, "_slots", None)
    if not isinstance(slots, dict):
        return True
    return slots.get(getattr(slot, "key", None)) is slot


async def deny_app_session_control(
    request: web.Request,
    request_app: str,
    slot: Any,
    name: str,
    operation: str,
    *,
    fresh: bool = False,
) -> web.Response | None:
    """The session-control decision as a response: None to proceed, else the uniform 404.

    The grant is read before the slot is judged, whatever the slot is, so the cost
    of a refusal does not depend on which kind of session the name addressed.
    """
    if not request_app:
        return None
    granted = await session_grant(request, request_app, fresh=fresh)
    if slot is not None and not _slot_still_live(request, slot):
        # The grant read awaits: a close and same-name create inside it leaves
        # *slot* detached, and a verdict on it says nothing about the replacement.
        audit_app_slot_denial(request_app, operation, name, "slot replaced during the grant read")
        return slot_not_found()
    if slot is not None and app_may_control_session(request_app, slot, granted):
        return None
    if slot is not None:
        audit_app_slot_denial(request_app, operation, name, SESSION_CONTROL_DENIED)
    return slot_not_found()


def checkpoint_slot_replaced(request: web.Request, slot: Any) -> bool:
    """Whether an app request now addresses a different slot than the checkpoint judged.

    For a handler that awaits before it looks its slot up: a close and same-name
    create inside that await would otherwise run the handler on a slot no
    decision covered. Requests with no app claim, or that never passed the
    checkpoint, are not affected.
    """
    if not request.get("app", "") or CHECKPOINT_SLOT_KEY not in request:
        return False
    return request[CHECKPOINT_SLOT_KEY] is not slot


async def slot_route_denial(request: web.Request) -> web.Response | None:
    """Apply the checkpoint to *request*: the refusal to send, or None to proceed."""
    request_app = request.get("app", "")
    if not request_app:
        return None
    resource = request.match_info.route.resource
    canonical = resource.canonical if resource is not None else ""
    decision = _route_decision(request.method, canonical)
    if decision is None or decision[1] is SlotRoutePolicy.HANDLER:
        return None
    param, policy = decision
    name = request.match_info.get(param, "")
    if not name:
        # A per-slot template that resolved without its slot segment: fail closed.
        return slot_not_found()
    state = request.app.get("state")
    operation = f"slot_route {request.method} {canonical}"
    grant_route = policy is SlotRoutePolicy.SESSION_GRANT
    if grant_route:
        # Read the grant before the lookup, for every app caller alike. The
        # decision below reuses this read, so nothing suspends between the
        # lookup and the verdict.
        await session_grant(request, request_app)
    slot = getattr(state, "_slots", {}).get(name)
    request[CHECKPOINT_SLOT_KEY] = slot
    if grant_route:
        denied = await deny_app_session_control(request, request_app, slot, name, operation)
    else:
        denied = deny_app_slot_session_access(request_app, slot, name, operation)
    if denied is None and slot is not None:
        _record_isolation_allow(request_app, operation, name)
    return denied


Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


@web.middleware
async def slot_ownership_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Refuse an app caller on a per-slot route it may not act on, before the handler.

    Registered inner to ``token_auth_middleware``, which publishes the ``app``
    claim this reads, and inner to ``sel_audit_middleware`` so a refusal here is in
    that request record too.
    """
    denied = await slot_route_denial(request)
    if denied is not None:
        return denied
    return await handler(request)
