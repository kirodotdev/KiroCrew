"""Who is calling the work ledger, and which ledger that makes it.

The entry gate every route passes: the internal-secret principal, the session
recognition and restricted-mode refusals, the channel containment and its owner-DM
exemption (re-checked after a read), the fold to the ledger spelling, and the caller's
own binding or conductor record.

Served through :mod:`kiro_crew.dashboard.handlers.work_ledger`, which callers and tests
import and patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from kiro_crew import session_ledger, work_ledger
from kiro_crew.dashboard import session_control
from kiro_crew.dashboard.handlers._shared import _is_restricted_session

# Module-scope like ``session_ledger.py``'s identical imports: the recognition
# gate and the incognito classifier are this module's own load-bearing deps.
from kiro_crew.dashboard.handlers.cron import _recognize_session
from kiro_crew.dashboard.handlers.work_ledger_refusals import (
    CODE_NOT_BOUND,
    _audit,
    _refuse_403,
    _refuse_404,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import is_incognito_transcript
from kiro_crew.messaging.link import is_channel_session_key

logger = logging.getLogger("kiro_crew.dashboard.handlers.work_ledger")


async def _caller_key(
    request: web.Request, operation: str
) -> tuple[str, None] | tuple[None, web.Response]:
    """Vet the calling session and fold its key to the ledger spelling.

    Returns ``(key, None)`` or ``(None, refusal)``. Identical in shape and in
    reasoning to ``session_ledger._resolve_ledger_key``: the recognition gate
    decides whether this key names a session at all, and the fold is the lossless
    dashboard-prefix strip so one session's two legitimate spellings reach one
    record while two distinct channel keys can never collide.
    """
    if not request.get("internal_auth"):
        # The internal-secret principal, positively confirmed — NOT inferred from
        # the path being in ``_STRICT_INTERNAL_API_PATHS``. That table denies a
        # NON-loopback cookie caller outright, but on loopback a strict path with
        # no secret header falls through to cookie auth and is GRANTED, reaching
        # this handler with ``internal_auth`` unset. ``_recognize_session`` then
        # accepts any ``X-Session-Key`` naming a known session and never ties it to
        # the authenticated principal, so without this check a cookie-authed
        # loopback caller could name ANOTHER session's key and read or write its
        # ledger. The unix-socket peer check that makes the header unforgeable for
        # MCP callers does not run on TCP loopback.
        #
        # Costs nothing reachable: all four routes are MCP-only by design (the
        # ``handlers.work_ledger`` docstring says so, and no browser code calls them). A future Crew
        # page reader is a browser caller and needs its own route with its own
        # identity rule — it must not arrive by relaxing this one.
        _audit(
            request.headers.get("X-Session-Key", "") or "anonymous",
            operation,
            "denied",
            resources="cookie_caller_block",
            error="internal_auth_required",
        )
        return None, _refuse_403(
            "internal_auth_required",
            "The work ledger is reachable only by an agent's MCP tools, which "
            "authenticate with the gateway's internal secret. A browser session "
            "cannot name which ledger it is.",
        )
    state: DashboardState = request.app["state"]
    sk = request.headers.get("X-Session-Key", "")
    refusal = await _recognize_session(
        state, sk, operation, blocks_persisted_mode=is_incognito_transcript
    )
    if refusal is not None:
        return None, refusal
    if _is_restricted_session(state, request):
        _audit(
            sk,
            operation,
            "denied",
            resources="restricted_session_block",
            error="Work-ledger access is not allowed in this session mode.",
        )
        return None, _refuse_403(
            "restricted_session",
            "The work ledger is not available in this session mode.",
        )
    why, caller_slot, admission = _caller_admission(request, sk)
    if why:
        # Containment for channel agents, held HERE rather than only in
        # ``channel.CHANNEL_AGENT_BLOCKED_TOOLS``. That list is matched against a
        # rendered permission request (``channel.py``'s ``EVENT_PERMISSION_REQUEST``
        # arm), so a tool auto-approved through ``allowedTools`` emits no permission
        # event and the block never runs — which is exactly the bypass an
        # ``autoApprove`` key would open, and the reason this server carries none.
        # The four tools grant themselves per-tool auto-approve on three specs, so
        # the block alone is not sufficient for them and the refusal has to be
        # server-side, where no spec and no client can route around it.
        #
        # Nothing reachable is lost. A channel caller that is not the owner's own
        # DM cannot be a conductor: ``session_control._refuse_ineligible_creator``
        # refuses it by the same predicate, so such a session can never dispatch
        # a worker and never owns items. And a worker is dispatched BY
        # ``session_create``, which mints a dashboard slot — never a channel key.
        # So a channel caller refused here is either a misconfiguration or the
        # containment case. The same holds for a dashboard-BORN session that was
        # later given an outbound mirror: its key looks local while every turn is
        # republished to Slack or Telegram, so the key alone is not the test — see
        # :func:`_reaches_a_channel` — and the same exemption reaches it when that
        # mirror is the owner's own DM. The reason rides along so the caller reads
        # the same clause session control would name for it.
        _audit(sk, operation, "denied", resources="channel_agent_block")
        return None, _refuse_403(
            "channel_session",
            "The work ledger is not reachable from a channel session. A channel "
            "agent has no dispatch relationship: it can neither create the worker "
            "sessions a conductor binds nor be one. The owner-DM exemption is "
            f"withheld because {why}.",
        )
    # Admitted. A brief, a ledger, a report echo -- what these routes return is
    # private dispatch data that becomes part of the caller's reply, published to
    # its mirror later and resolved live. Record the audience the caller was
    # admitted under -- the row the verdict above judged, never a second read --
    # at this one gate every ledger route passes (the same record session control's
    # caller gate writes), so a mirror gained or retargeted before that reply
    # publishes withholds it -- see ``session_control.record_audience_admission``.
    if admission is not None:
        session_control.record_audience_admission(caller_slot, admission)
    return session_ledger.ledger_key(sk), None


def _caller_admission(request: web.Request, sk: str) -> tuple[str, Any, dict[str, Any] | None]:
    """:func:`caller_admission` over a request. See it for the judgement."""
    return caller_admission(request.app["state"], sk)


def caller_admission(state: DashboardState, sk: str) -> tuple[str, Any, dict[str, Any] | None]:
    """The entry gate's one judgement of *sk*: ``(why, slot, admission)``.

    ``why`` is ``""`` when the caller may reach the ledger, else the refusal reason
    :func:`_contained_channel_caller` would name; ``slot`` is the caller's own live
    slot, resolved the way every session-control verb resolves its caller
    (``caller_slot_key``); ``admission`` is the containment to record on it -- the
    ``admission`` of the ONE :func:`session_control.judge_owner_dm` verdict this
    judgement rests on, so the row that decided the refusal and the row recorded as
    the admitted audience are the same object. Deciding from one verdict is the
    point: a boolean probe, then the predicate's own read, then a third read for
    the record would leave two windows in which a retarget lands and is recorded as
    admitted without having been judged.

    Whether the caller REACHES a channel at all is read off the same verdict: a
    channel key, a channel link on the slot, or the verdict's fail-closed
    ``mirrored`` flag. A caller that reaches none is admitted with its (unmirrored)
    containment recorded, so a mirror gained before its reply publishes still
    withholds that reply.

    A key that resolves to no open slot has no slot to record an audience on and
    nothing of its own that publishes; it is judged as before -- by its key and the
    live probe -- and records nothing.

    Takes the STATE rather than the request, because an in-process producer that
    delivers ledger state into a session's own turn has to pass this same gate and
    holds no request. One judgement, one implementation: a second copy of a
    containment test is how one of the two drifts open.
    """
    slot_key = session_control.caller_slot_key(state, sk)
    slot = state.get_slot(slot_key) if slot_key else None
    if slot is None:
        return contained_channel_caller(state, sk), None, None
    try:
        verdict = session_control.judge_owner_dm(state, slot)
    except Exception:  # pragma: no cover - the predicate fails closed itself
        logger.debug("owner-DM check failed for %s", sk, exc_info=True)
        return "the owner-DM check could not be completed", slot, None
    reaches = (
        is_channel_session_key(sk)
        or bool(session_control._channel_link_of(slot))
        or verdict.mirrored
    )
    return (verdict.refusal if reaches else ""), slot, verdict.admission


def _contained_channel_caller(request: web.Request, sk: str) -> str:
    """:func:`contained_channel_caller` over a request. See it for the judgement."""
    return contained_channel_caller(request.app["state"], sk)


def contained_channel_caller(state: DashboardState, sk: str) -> str:
    """Why *sk*'s turns reach a channel audience the ledger must stay out of; ``""`` if not.

    Two mechanisms reach a channel -- a channel-BORN key, and a dashboard-born
    session given an outbound mirror (:func:`_reaches_a_channel`) -- and one
    exemption applies to both: ``session_control.session_owner_dm_refusal``
    answering ``""``, a 1:1 DM whose only human is the configured owner -- the DM
    the session lives in, or the DM a dashboard tab mirrors to -- with no wider
    room bound beside it. It is the SAME predicate the session-control
    gates consult, over the slot ``caller_slot_key`` resolves, so a session this
    gate admits is one ``session_create`` admits as a conductor and vice versa --
    the two cannot disagree about a slot. Fails CLOSED with the predicate: an
    unreadable roster or store, an unknown origin conversation, a mirror whose
    peer the transport holds no record of, or a key no open slot answers to all
    read as contained. The string is the predicate's own reason, so the ledger
    and session control tell the caller the same thing.

    Re-checked after every read the routes await across, because the exemption
    rests on live state -- a mirror retargeted at a thread while the ledger was
    being read widens the audience exactly as a mirror gained by a dashboard
    session does. The ENTRY gate judges through :func:`_caller_admission` instead,
    which takes the same predicate's verdict once and records the row it judged;
    this reason-only form is what that gate falls back to for a key no open slot
    answers to, where there is nothing to record.
    """
    if not (is_channel_session_key(sk) or reaches_a_channel(state, sk)):
        return ""
    try:
        return session_control.session_owner_dm_refusal(state, sk)
    except Exception:  # pragma: no cover - the predicate fails closed itself
        logger.debug("owner-DM check failed for %s", sk, exc_info=True)
        return "the owner-DM check could not be completed"


def _reaches_a_channel(request: web.Request, sk: str) -> bool:
    """:func:`reaches_a_channel` over a request. See it for the predicate."""
    return reaches_a_channel(request.app["state"], sk)


def reaches_a_channel(state: DashboardState, sk: str) -> bool:
    """Whether this caller's turns reach a messaging channel, mirror included.

    ``is_channel_session_key`` catches a channel-BORN session. It does not catch a
    dashboard-born one that was later given an OUTBOUND mirror link — the link
    lives in the session store, not in the key, so a plain ``chat-*`` key can still
    be republishing every turn to a channel. A brief carries the acceptance bar of
    a private dispatch and the ledger carries worker-authored prose, so the mirror
    is the same disclosure as a channel key and gets the same refusal.

    Delegates to ``session_control._has_channel_mirror``, which is the same
    predicate ``_refuse_ineligible_creator`` applies to a session asking to create
    a peer, and which FAILS CLOSED: a session store that cannot answer counts as
    mirrored.
    """
    for candidate in (
        sk,
        session_ledger.ledger_key(sk),
        f"dashboard_{session_ledger.ledger_key(sk)}",
    ):
        try:
            slot = state.get_slot(candidate)
        except Exception:  # pragma: no cover - a slot read must not 500
            logger.debug("slot lookup failed for %s", candidate, exc_info=True)
            continue
        if slot is None:
            continue
        try:
            if session_control._has_channel_mirror(state, slot):
                return True
        except Exception:  # pragma: no cover
            # Fail CLOSED, matching the helper's own default: an unreadable link is
            # treated as mirrored rather than opening the boundary.
            logger.debug("channel-mirror probe failed for %s", candidate, exc_info=True)
            return True
    return False


async def _worker_binding(
    key: str, operation: str
) -> tuple[tuple[str, str], None] | tuple[None, web.Response]:
    """Resolve the caller's own binding, or refuse with ``not_bound``.

    The whole of a worker's addressing: the conductor key and item id come from
    ``bindings/<the caller's digest>.json`` and from nowhere else, which is why
    neither worker tool has a parameter that could name either.
    """
    binding = await asyncio.to_thread(work_ledger.read_binding, key)
    if binding is None:
        _audit(key, operation, "denied", error=CODE_NOT_BOUND)
        return None, _refuse_403(
            CODE_NOT_BOUND,
            "This session is not bound to a work item, so it has no brief to read "
            "or report against. A conductor binds an item to a worker session; "
            "only that session can use the worker tools.",
        )
    return binding, None


def session_is_bound_worker(session_key: str) -> bool:
    """Whether *session_key* is the bound worker of any work-ledger item here.

    The binding :func:`_worker_binding` resolves for ``work_brief``, under the same
    ledger-key fold, read strictly: a present but unreadable binding raises so a
    caller that gates on it fails closed. Blocking: it reads a file.
    """
    return work_ledger.read_binding(session_ledger.ledger_key(session_key), strict=True) is not None


async def _own_ledger(
    key: str, operation: str, caller: str | None = None
) -> tuple[work_ledger.ConductorRecord, None] | tuple[None, web.Response]:
    """Resolve the caller's own conductor record, or refuse with ``no_ledger``.

    *caller* is who the denial is audited against; it defaults to *key* for the
    MCP routes, where the session key IS the actor. A dashboard route that owns
    the request as an authenticated human passes that human as *caller* so the
    SEL row names the operator, not the conductor key being read.
    """
    record = await asyncio.to_thread(work_ledger.read_conductor, key)
    if record is None:
        _audit(
            caller if caller is not None else key,
            operation,
            "denied",
            error=work_ledger.CODE_NO_LEDGER,
        )
        return None, _refuse_404(
            work_ledger.CODE_NO_LEDGER,
            "This session owns no work ledger. Start one with "
            "work_ledger_record action=goal (or action=create) before reading it.",
        )
    return record, None
