"""``crew=`` on the five session tools: the hub half (rfc-conductor-remote-dispatch.md, wave 1).

``session_create``, ``session_send``, ``session_read_message``, ``session_stop`` and
``session_close`` reach this module through their own ``/api/session-control/*``
routes when the body (or, for the read, the query) carries ``crew``. Each call is
mapped to the peer route RFC §1 names, all under ``api/chat``:

==========  ==========================================================
create      ``POST api/chat/slots``, then ``PATCH api/chat/slots/{s}/title``
send        ``POST api/chat?ws=1`` ``{message, slot}``
read        ``GET api/chat/slots/{s}?limit=N``
stop        ``POST api/chat/slots/{s}/stop``
close       ``DELETE api/chat/slots/{s}``
==========  ==========================================================

Four rules hold for every call, in this order:

1. **Switch.** ``work_ledger.remote_dispatch`` off refuses with
   ``remote_dispatch_disabled`` before anything else is read.
2. **Owner-only caller** (RFC §4, :func:`authorize_remote_dispatch`).
3. **Path fence.** Every peer path is built here from a fixed template and then
   passes :func:`_proxy_canonical_path`, the browser proxy's own allowlist; the
   canonical path is what is sent. ``SshTunnelManager.proxy_request`` checks no
   path, so this is the server-side fence RFC §1 asks for.
4. **Redaction.** Every peer reply runs :func:`_redact_peer_payload` before any
   field of it is read or returned. A reply that is not JSON, is too large, or
   cannot be redacted is refused, never forwarded.

**Target fence (wave 1).** ``send``/``read``/``stop``/``close`` with a crew reach
only a peer slot that THIS caller created with ``session_create(crew=)`` on THAT
crew, as recorded in gateway memory by :func:`_record_dispatch`. The record is not
persisted: after a hub restart the conductor cannot drive the slot, and the person
still can from the crew window. Wave 4 replaces this record with the work
item's ``worker_session_key`` + ``worker_instance_id`` pair, which survives a
restart.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import weakref
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import aiohttp
from aiohttp import web

import kiro_crew
from kiro_crew import session_ledger
from kiro_crew.apps.version import versions_compatible
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.dashboard.handlers_instances_gate import _registry
from kiro_crew.dashboard.handlers_instances_peer import (
    ProxyReplyUnredactable,
    _proxy_canonical_path,
    _redact_peer_payload,
)
from kiro_crew.dashboard.peer_redaction import redact_peer_text
from kiro_crew.instances.constants import PROXY_REDACT_BUFFER_MAX_BYTES
from kiro_crew.instances.crew_ref import clean_crew_ref
from kiro_crew.instances.ssh_tunnel_manager import ProxyRequestError
from kiro_crew.remote_dispatch import (
    REMOTE_DISPATCH_DISABLED,
    REMOTE_DISPATCH_DISABLED_MESSAGE,
    remote_dispatch_enabled,
)
from kiro_crew.sel import sel

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

#: The five verbs that take ``crew=``. ``read`` is ``session_read_message``.
CREW_VERBS = ("create", "send", "read", "stop", "close")

#: The create fields a crew create carries. Anything else a local create takes
#: (folder, agent_kind, reasoning_effort, autocompact_pct, dry_run) names a
#: local-only behaviour and is refused rather than dropped.
_CREATE_FIELDS = frozenset({"crew", "title", "agent", "model", "work_item"})

#: A peer slot key this hub will put in a path: one plain segment. A peer is
#: untrusted, and its create reply names the key every later path is built from,
#: so the key is held to this shape at create AND at every use.
_PEER_SLOT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")

#: Most peer slots one caller may hold through ``crew=`` at once. Bounds the
#: in-memory record; a conductor at the cap closes a worker before opening one.
MAX_CREW_SLOTS_PER_CALLER = 64

#: Peer read page bound, the same ceiling the local ``session_read_message`` has.
_READ_LIMIT_MAX = 100

#: ``caller slot key -> {(instance_id, peer slot key)}``, per gateway state.
_DISPATCHED: "weakref.WeakKeyDictionary[Any, dict[str, set[tuple[str, str]]]]" = (
    weakref.WeakKeyDictionary()
)


class CrewCallError(Exception):
    """A refused crew call: an HTTP status, a stable ``code`` and advisory prose."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


#: A peer's typed refusal of a work-item create that it has no room for (wave 3).
CREW_BUSY = "crew_busy"


class CrewBusyError(CrewCallError):
    """The peer answered ``crew_busy``: nothing was created there (RFC §2b)."""

    def __init__(self, payload: dict[str, Any]) -> None:
        short = payload.get("short")
        short = short if short in ("memory", "cpu", "disk") else "a resource"
        super().__init__(503, CREW_BUSY, f"that crew has no room: it is short on {short}")


def _response(exc: CrewCallError) -> web.Response:
    """The JSON refusal for *exc*, its status from a closed set (else 400)."""
    if exc.status == 403:
        return web.json_response({"error": exc.message, "code": exc.code}, status=403)
    if exc.status == 404:
        return web.json_response({"error": exc.message, "code": exc.code}, status=404)
    if exc.status == 409:
        return web.json_response({"error": exc.message, "code": exc.code}, status=409)
    if exc.status == 502:
        return web.json_response({"error": exc.message, "code": exc.code}, status=502)
    if exc.status == 503:
        return web.json_response({"error": exc.message, "code": exc.code}, status=503)
    return web.json_response({"error": exc.message, "code": exc.code}, status=400)


def _audit(caller: str, verb: str, outcome: str, *, crew: str = "", error: str = "") -> None:
    """One SEL event per crew call, on every outcome, naming the caller."""
    try:
        sel().log_tool_invocation(
            session_key=caller or "unknown",
            tool_name=f"session_control_crew_{verb}",
            outcome=outcome,
            request_id=crew,
            source="dashboard",
            error=error,
        )
    except Exception:  # audit must never break the request path
        logger.debug("SEL audit failed for session_control_crew_%s", verb, exc_info=True)


def has_crew(body: dict[str, Any]) -> bool:
    """Whether a request named a crew at all: the key's presence, whatever its value.

    An empty, ``null`` or non-string ``crew`` is still a crew call and is refused
    as one, never read as "no crew" and run locally.
    """
    return "crew" in body


async def placement_enabled() -> bool:
    """Whether a create with ``work_item`` and no ``crew`` goes to hub placement.

    Read per call. Off, such a create stays on the local path exactly as before.
    """
    return await asyncio.to_thread(remote_dispatch_enabled)


async def refuse_if_disabled(caller: str, verb: str) -> web.Response | None:
    """The ``remote_dispatch_disabled`` refusal while the switch is off, else ``None``."""
    if await asyncio.to_thread(remote_dispatch_enabled):
        return None
    _audit(caller, verb, "denied", error=REMOTE_DISPATCH_DISABLED)
    return web.json_response(
        {"error": REMOTE_DISPATCH_DISABLED_MESSAGE, "code": REMOTE_DISPATCH_DISABLED},
        status=403,
    )


async def authorize_remote_dispatch(request: web.Request, verb: str) -> str:
    """The verified caller's slot key, or raise :class:`CrewCallError` (403/409).

    RFC §4: only an owner-minted dashboard session may use ``crew=``. The arms:

    1. The declared session key is owner-minted (``is_owner_minted_session_key``:
       the ``dashboard`` namespace). A channel,
       ``cron:``, workflow, ``subagent:`` or ``side:`` key, and any namespace added
       later, is refused before anything is resolved for it (``owner_only``).
    2. The shared agent-caller resolver (``handlers/guide.py``
       ``_resolve_agent_caller``): internal secret, no app claim, a key the
       transport attests, a live slot, not app-scoped, not an unattended
       (cron/workflow) slot, a turn running in it, and not a channel turn. Its own
       refusal codes pass through.
    3. The slot was not opened on another caller's behalf (``_created_by`` or
       ``_lineage_minted``): a worker cannot reach a crew, so dispatch stays one
       level deep, and is not a crew member's own thread (``owner_only``).
    4. The session is not bound to a private member memory store
       (``private_memory``).
    5. A ``user`` claim, when present, is the owner.
    6. The session is not the bound worker of any work-ledger item
       (:func:`caller_is_bound_worker`, ``crew_caller_is_worker``); an unreadable
       binding refuses too.
    7. The running turn is one the owner sent (:func:`owner_unverifiable_reason`,
       ``crew_owner_unverifiable``).

    Arms 1-6 refuse first, with their own codes, so a refusal still says which
    rule a caller fails.
    """
    from kiro_crew import members as members_mod
    from kiro_crew.dashboard.guide_runs import GuideError
    from kiro_crew.dashboard.handlers._shared import _read_session_key
    from kiro_crew.dashboard.handlers.guide import _resolve_agent_caller
    from kiro_crew.member_memory_auth import private_memory_store_for_session

    declared = _read_session_key(request)
    from kiro_crew.constants import is_owner_minted_session_key

    if not is_owner_minted_session_key(declared):
        _audit(declared, verb, "denied", error="not an owner-minted dashboard session")
        raise CrewCallError(
            403, "owner_only", "crew= is open only to the owner's own dashboard sessions"
        )
    try:
        slot_key, caller_key = await _resolve_agent_caller(request, f"session_control.crew_{verb}")
    except GuideError as exc:
        _audit(declared, verb, "denied", error=exc.code)
        raise CrewCallError(exc.status, exc.code, exc.message) from None
    state: DashboardState = request.app["state"]
    slot = state.get_slot(slot_key)
    if (
        slot is None
        or bool(getattr(slot, "_lineage_minted", False))
        or str(getattr(slot, "_created_by", "") or "")
        or getattr(slot, "mode", "") == members_mod.DM_SLOT_MODE
    ):
        _audit(caller_key, verb, "denied", error="slot opened on another caller's behalf")
        raise CrewCallError(
            403,
            "owner_only",
            "crew= is open only to the owner's own tab, not a session opened for someone else",
        )
    if getattr(slot, "memory_mode", "persistent") != "persistent":
        # The local create's rule (``ephemeral_caller``): an incognito or temporary
        # chat opens no session, so its content never reaches a persistent one.
        _audit(caller_key, verb, "denied", error="ephemeral_caller")
        raise CrewCallError(
            403, "ephemeral_caller", "incognito and temporary sessions cannot use crew="
        )
    try:
        fenced = await asyncio.to_thread(private_memory_store_for_session, caller_key)
    except (OSError, ValueError):
        fenced = "unreadable"
    if fenced:
        _audit(caller_key, verb, "denied", error="private-memory session")
        raise CrewCallError(
            403, "private_memory", "a private-memory session cannot drive another crew"
        )
    if str(request.get("user") or "") and not is_owner_dashboard_request(request):
        _audit(caller_key, verb, "denied", error="non-owner identity rejected")
        raise CrewCallError(403, "owner_only", "crew= is owner-only")
    try:
        bound = await asyncio.to_thread(caller_is_bound_worker, caller_key)
    except Exception:
        logger.info("crew caller binding unreadable for %s", caller_key, exc_info=True)
        bound = True
    if bound:
        _audit(caller_key, verb, "denied", error=CALLER_IS_WORKER)
        raise CrewCallError(
            403,
            CALLER_IS_WORKER,
            "a session bound to a work item is a worker, and a worker cannot dispatch "
            "to a crew; only the conductor that owns the ledger can",
        )
    reason = owner_unverifiable_reason(slot)
    if reason:
        _audit(caller_key, verb, "denied", error=OWNER_UNVERIFIABLE)
        raise CrewCallError(
            403,
            OWNER_UNVERIFIABLE,
            f"crew= is open only to a turn the owner sent: {reason}",
        )
    return slot_key


#: The refusal while a non-owner dashboard user is possible on this gateway.
OWNER_UNVERIFIABLE = "crew_owner_unverifiable"

#: The refusal for a caller that is the bound worker of a work-ledger item.
CALLER_IS_WORKER = "crew_caller_is_worker"


def caller_is_bound_worker(session_key: str) -> bool:
    """Whether *session_key* is the bound worker of any work-ledger item here.

    The binding ``work_brief`` resolves, read through the work-ledger routes' own
    seam (``session_is_bound_worker``) so identity stays server-resolved, and
    strictly: an unreadable binding raises and the caller fails closed. This is
    what keeps dispatch one level deep across machines: a session another
    gateway's conductor opened on this one is bound to its mailbox item once the
    peer binds at create (RFC wave 2), so it cannot dispatch onward, and a nested
    conductor (itself a bound worker) is refused too. Blocking: it reads a file.
    """
    from kiro_crew.dashboard.handlers import work_ledger as work_ledger_routes

    return bool(work_ledger_routes.session_is_bound_worker(session_key))


def owner_unverifiable_reason(slot: object) -> str:
    """Why *slot*'s running turn is not provably the owner's, or ``""`` when it is.

    The one place the owner question is answered for ``crew=``. A dashboard login
    can belong to someone other than the owner (a messaging channel mints one for
    an allow-listed user), so the tab alone proves nothing; what does is the
    gateway's own per-turn record of who dispatched the turn. This reuses the
    crew-read gate's slot check (``handlers_instances_crew_reads._slot_refusal``):
    the running turn was sent by the OWNER (``_turn_owner_sent``) and the slot was
    not opened on another caller's behalf. A turn the person did not send -- a
    loop wake, a ``session_send``, a sub-agent completion -- is refused, the same
    as for a crew read.
    """
    from kiro_crew.dashboard.handlers_instances_crew_reads import _slot_refusal

    refusal = _slot_refusal(slot)
    return refusal[1] if refusal is not None else ""


def _instances(state: "DashboardState") -> list[Any]:
    """The registered crews. Blocking: the registry re-reads its file."""
    return list(_registry(state).list())


async def resolve_crew(state: "DashboardState", crew: object) -> tuple[str, str]:
    """``(instance_id, display name)`` for an id or exact name, else raise.

    An exact id wins across every entry before any name is tried, so an id is
    never shadowed by another crew's name. A name resolves only when exactly one
    crew carries it.
    """
    ref = clean_crew_ref(crew)
    if not ref:
        raise CrewCallError(400, "crew_invalid", "crew must be a crew instance id or exact name")
    try:
        rows = await asyncio.to_thread(_instances, state)
    except Exception:
        logger.debug("crew registry unreadable", exc_info=True)
        rows = []
    for row in rows:
        if str(getattr(row, "id", "")) == ref:
            return ref, str(getattr(row, "name", "") or ref)
    named = [row for row in rows if str(getattr(row, "name", "")) == ref]
    if len(named) == 1:
        return str(named[0].id), ref
    if len(named) > 1:
        raise CrewCallError(
            409, "crew_ambiguous", "more than one crew has that name; use its instance id"
        )
    raise CrewCallError(404, "crew_unknown", "no connected crew has that id or name")


def _manager(state: "DashboardState") -> Any:
    mgr = getattr(state, "instances_manager", None)
    if mgr is None:
        raise CrewCallError(503, "instances_manager_unavailable", "remote crews are unavailable")
    return mgr


async def check_version(mgr: Any, instance_id: str) -> None:
    """Refuse a crew whose ``major.minor`` differs from this hub's (RFC §5)."""
    try:
        ok, payload = await mgr.peer_capability(instance_id, "/api/version")
    except Exception as exc:
        logger.info("crew version read on %s raised (%s)", instance_id, type(exc).__name__)
        ok, payload = False, {}
    if not ok:
        code = payload.get("code") if isinstance(payload, dict) else ""
        # ``peer_capability`` spells the down tunnel ``capability_peer_not_connected``.
        if code == "capability_peer_not_connected":
            raise CrewCallError(503, "proxy_peer_not_connected", "that crew is not connected")
        raise CrewCallError(502, "crew_version_unknown", "could not read that crew's version")
    peer = payload.get("version") if isinstance(payload, dict) else ""
    peer = redact_peer_text(peer)[:64] if isinstance(peer, str) else ""
    if not versions_compatible(kiro_crew.__version__, peer):
        raise CrewCallError(
            409,
            "crew_version_mismatch",
            f"that crew runs {peer or 'an unknown version'}; this hub runs "
            f"{kiro_crew.__version__} (major.minor must match)",
        )


async def peer_call(
    mgr: Any,
    instance_id: str,
    method: str,
    path: str,
    *,
    params: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    """One fenced, redacted peer call: ``(status, decoded redacted JSON)``.

    *path* must pass :func:`_proxy_canonical_path`; the canonical form is what is
    sent. The reply must be JSON within :data:`PROXY_REDACT_BUFFER_MAX_BYTES`, and
    is redacted as one document before it is decoded for use.
    """
    canonical, reason = _proxy_canonical_path(path)
    if reason:
        raise CrewCallError(400, "proxy_path_denied", reason)
    data = json.dumps(body).encode() if body is not None else None
    try:
        async with mgr.proxy_request(
            instance_id,
            method,
            canonical,
            params=params,
            data=data,
            content_type="application/json" if data is not None else "",
        ) as upstream:
            ctype = (upstream.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                raise CrewCallError(
                    502, "proxy_content_type_refused", "that crew answered with a non-JSON reply"
                )
            raw = bytearray()
            async for chunk in upstream.content.iter_any():
                raw += chunk
                if len(raw) > PROXY_REDACT_BUFFER_MAX_BYTES:
                    raise CrewCallError(
                        502, "proxy_reply_too_large", "that crew's reply is too large"
                    )
            status = int(upstream.status)
    except ProxyRequestError as exc:
        raise CrewCallError(503 if exc.http_status == 503 else 502, exc.code, exc.message) from None
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        # The carrier wraps only the phase before the response: a tunnel that
        # drops mid-body or a read that idles out raises straight through it.
        logger.info("crew call to %s was interrupted (%s)", instance_id, type(exc).__name__)
        raise CrewCallError(
            502, "crew_peer_interrupted", "that crew stopped answering partway through"
        ) from None
    try:
        text = await asyncio.to_thread(_redact_peer_payload, raw.decode("utf-8", "replace"))
    except ProxyReplyUnredactable:
        raise CrewCallError(
            502, "proxy_reply_unredactable", "that crew's reply could not be redacted"
        ) from None
    try:
        return status, json.loads(text)
    except (ValueError, RecursionError):
        raise CrewCallError(502, "crew_reply_malformed", "that crew's reply is not JSON") from None


def _peer_error(status: int, payload: Any, what: str) -> CrewCallError:
    """A peer refusal as a hub refusal. The peer's text is already redacted."""
    msg = payload.get("error") if isinstance(payload, dict) else ""
    code = payload.get("code") if isinstance(payload, dict) else ""
    detail = f": {str(msg)[:300]}" if isinstance(msg, str) and msg else ""
    peer_code = f" ({str(code)[:64]})" if isinstance(code, str) and code else ""
    if status == 404:
        return CrewCallError(404, "crew_slot_not_found", f"{what}: the crew has no such session")
    return CrewCallError(
        502, "crew_peer_refused", f"{what}: the crew answered {status}{detail}{peer_code}"
    )


def _outbound(text: str) -> str:
    """Agent-authored *text* on its way to another machine, scrubbed first.

    The same two passes the create route applies to a title: exfiltration URLs,
    then credentials. Model text can quote a credential it read; the crew is
    another machine's transcript and model, so it is scrubbed before it leaves.
    """
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls

    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


def _slot_ok(slot: object) -> bool:
    return isinstance(slot, str) and bool(_PEER_SLOT_RE.match(slot)) and bool(slot.strip("."))


def _record(state: "DashboardState") -> dict[str, set[tuple[str, str]]]:
    rec = _DISPATCHED.get(state)
    if rec is None:
        rec = {}
        _DISPATCHED[state] = rec
    return rec


def _record_dispatch(state: "DashboardState", caller: str, instance_id: str, slot: str) -> None:
    _record(state).setdefault(caller, set()).add((instance_id, slot))


def _forget_dispatch(state: "DashboardState", caller: str, instance_id: str, slot: str) -> None:
    held = _record(state).get(caller)
    if held is not None:
        held.discard((instance_id, slot))
        if not held:
            _record(state).pop(caller, None)


def dispatched_by(state: "DashboardState", caller: str, instance_id: str, slot: str) -> bool:
    """Whether *caller* created peer *slot* on *instance_id* through ``crew=``."""
    return (instance_id, slot) in _record(state).get(caller, set())


def _fenced_target(state: "DashboardState", caller: str, instance_id: str, target: object) -> str:
    """*target* when this caller dispatched it on this crew, else raise ``crew_target_not_dispatched``."""
    if not isinstance(target, str) or not target.strip():
        raise CrewCallError(400, "target_required", "target is required")
    slot = target.strip()
    if slot.startswith("dashboard:"):
        slot = slot[len("dashboard:") :]
    if not _slot_ok(slot) or not dispatched_by(state, caller, instance_id, slot):
        raise CrewCallError(
            403,
            "crew_target_not_dispatched",
            "that crew session was not opened by this session with session_create(crew=)",
        )
    return slot


async def _fenced_target_or_item(
    state: "DashboardState", caller: str, board: str, instance_id: str, target: object
) -> str:
    """:func:`_fenced_target`, or a slot an open item on *board* is bound to on that crew.

    The item pair (``worker_session_key`` + ``worker_instance_id``) survives a hub
    restart, which the in-memory record does not (wave 4).
    """
    try:
        return _fenced_target(state, caller, instance_id, target)
    except CrewCallError as exc:
        if exc.code != "crew_target_not_dispatched" or not isinstance(target, str):
            raise
        slot = target.strip()
        if slot.startswith("dashboard:"):
            slot = slot[len("dashboard:") :]
        from kiro_crew.dashboard.handlers import remote_ingest

        if _slot_ok(slot) and await remote_ingest.holds_peer_slot(board, instance_id, slot):
            return slot
        raise


async def _create(
    state: "DashboardState",
    mgr: Any,
    caller: str,
    instance_id: str,
    body: dict[str, Any],
    *,
    board: str = "",
) -> dict[str, Any]:
    extra = sorted(k for k in body if k not in _CREATE_FIELDS)
    if extra:
        raise CrewCallError(
            400,
            "crew_field_unsupported",
            f"session_create with crew= does not take {', '.join(extra)}",
        )
    fields = {}
    for name in ("title", "agent", "model"):
        value = body.get(name, "")
        if not isinstance(value, str):
            raise CrewCallError(400, "invalid_field_type", f"{name} must be a string")
        fields[name] = value.strip()
    for name in ("agent", "model"):
        # A selection is a name, not prose: scrubbing cannot leave a usable one,
        # so a value the scrub would change is refused, never sent.
        if fields[name] and _outbound(fields[name]) != fields[name]:
            raise CrewCallError(
                400, "crew_field_invalid", f"{name} carries text that cannot leave this machine"
            )
    if len(_record(state).get(caller, set())) >= MAX_CREW_SLOTS_PER_CALLER:
        raise CrewCallError(
            409,
            "crew_dispatch_limit",
            f"this session already holds {MAX_CREW_SLOTS_PER_CALLER} crew sessions; close one first",
        )
    from kiro_crew.config.loader import KiroCrewConfig

    memory_mode = (await asyncio.to_thread(KiroCrewConfig.load)).dashboard.default_memory_mode
    # `memory_mode` always rides the create (the window view's rule: it is the
    # person's privacy boundary). An omitted agent sends nothing, so the peer
    # applies its own default; this hub's "inherit the caller's agent" fallback
    # names an agent from THIS machine's roster and does not cross.
    create: dict[str, Any] = {"memory_mode": memory_mode}
    if fields["agent"]:
        create["agent"] = fields["agent"]
    if fields["model"]:
        create["model"] = fields["model"]
    work_item_id = ""
    if body.get("work_item") is not None:
        # Wave 4: born bound (RFC §3). The peer binds the slot to its mailbox copy
        # and echoes the id; this hub records the pair once it did.
        from kiro_crew.dashboard.handlers import remote_ingest

        slot, payload, bound = await remote_ingest.create_bound(
            state, mgr, board, instance_id, body.get("work_item"), create
        )
        work_item_id = bound.item_id
        if not fields["title"]:
            fields["title"] = bound.title
    else:
        status, payload = await peer_call(mgr, instance_id, "POST", "api/chat/slots", body=create)
        if not 200 <= status < 300:
            raise _peer_error(status, payload, "could not create the crew session")
        key = payload.get("key") if isinstance(payload, dict) else None
        if not _slot_ok(key):
            raise CrewCallError(
                502, "crew_reply_malformed", "the crew's create reply named no usable session key"
            )
        assert isinstance(key, str)
        slot = key
    # Recorded before the rename, so a failed rename still leaves the slot this
    # caller's to close.
    _record_dispatch(state, caller, instance_id, slot)
    result: dict[str, Any] = {
        "target": slot,
        "crew": instance_id,
        "title": fields["title"],
        "agent": payload.get("agent") if isinstance(payload.get("agent"), str) else fields["agent"],
        "model": fields["model"],
        "memory_mode": memory_mode,
    }
    if work_item_id:
        result["work_item_id"] = work_item_id
        if isinstance(payload, dict) and payload.get("decision_error"):
            # The latest decision did not reach the new worker; it holds the one
            # the create carried until the next decide.
            result["decision_error"] = payload["decision_error"]
    if fields["title"]:
        # The session exists from here on: a failed rename is reported beside its
        # key, never as a refusal that would hide a session the caller must close.
        try:
            t_status, t_payload = await peer_call(
                mgr,
                instance_id,
                "PATCH",
                f"api/chat/slots/{quote(slot, safe='')}/title",
                body={"title": _outbound(fields["title"])},
            )
        except CrewCallError as exc:
            t_status, t_payload = 0, {"error": exc.message, "code": exc.code}
        if not 200 <= t_status < 300:
            result["title_error"] = _peer_error(t_status, t_payload, "rename failed").message
            result["title"] = ""
    return result


async def _send(mgr: Any, instance_id: str, slot: str, body: dict[str, Any]) -> dict[str, Any]:
    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        raise CrewCallError(400, "message_required", "message is required")
    steer = body.get("steer", False)
    if not isinstance(steer, bool):
        raise CrewCallError(400, "invalid_steer", "steer must be a boolean")
    from kiro_crew.dashboard.agent_origin import TURN_ORIGIN_AGENT, TURN_ORIGIN_KEY

    # The crew must not read a conductor's message as a person's turn: the
    # agent-origin mark removes every person-only right from that turn.
    peer_body: dict[str, Any] = {
        "message": _outbound(message),
        "slot": slot,
        TURN_ORIGIN_KEY: TURN_ORIGIN_AGENT,
    }
    if steer:
        peer_body["steer"] = True
    status, payload = await peer_call(
        mgr, instance_id, "POST", "api/chat", params={"ws": "1"}, body=peer_body
    )
    if not 200 <= status < 300:
        raise _peer_error(status, payload, "could not send to the crew session")
    # What the crew did, not what was asked: a steer it could not land is queued.
    steered = isinstance(payload, dict) and payload.get("steered") is True
    return {"target": slot, "crew": instance_id, "delivered": True, "steered": steered}


async def _read(mgr: Any, instance_id: str, slot: str, limit: int) -> dict[str, Any]:
    status, payload = await peer_call(
        mgr,
        instance_id,
        "GET",
        f"api/chat/slots/{quote(slot, safe='')}",
        params={"limit": str(limit)},
    )
    if not 200 <= status < 300:
        raise _peer_error(status, payload, "could not read the crew session")
    if not isinstance(payload, dict):
        raise CrewCallError(502, "crew_reply_malformed", "the crew's read reply is not an object")
    rows = payload.get("messages")
    messages = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        role = row.get("role")
        content = row.get("content")
        if isinstance(content, list):
            content = "\n".join(
                str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("text")
            )
        messages.append(
            {
                "role": role if isinstance(role, str) else "",
                "content": content if isinstance(content, str) else "",
            }
        )
    return {
        "target": slot,
        "crew": instance_id,
        "title": payload.get("title") if isinstance(payload.get("title"), str) else "",
        "running": payload.get("running") is True,
        "messages": messages[-limit:],
    }


async def _stop(mgr: Any, instance_id: str, slot: str) -> dict[str, Any]:
    status, payload = await peer_call(
        mgr, instance_id, "POST", f"api/chat/slots/{quote(slot, safe='')}/stop", body={}
    )
    if not 200 <= status < 300:
        raise _peer_error(status, payload, "could not stop the crew session")
    # The crew can decline: a slot that is compacting answers 200 with
    # ``compacting`` and keeps running, so the receipt says what happened.
    compacting = isinstance(payload, dict) and payload.get("compacting") is True
    return {
        "target": slot,
        "crew": instance_id,
        "stopped": not compacting,
        "compacting": compacting,
    }


async def _close(
    state: "DashboardState", mgr: Any, caller: str, instance_id: str, slot: str
) -> dict[str, Any]:
    status, payload = await peer_call(
        mgr, instance_id, "DELETE", f"api/chat/slots/{quote(slot, safe='')}"
    )
    if status == 404:
        # Already gone on the crew: nothing left to drive, so the record goes too.
        _forget_dispatch(state, caller, instance_id, slot)
        raise _peer_error(status, payload, "could not close the crew session")
    if not 200 <= status < 300:
        raise _peer_error(status, payload, "could not close the crew session")
    _forget_dispatch(state, caller, instance_id, slot)
    return {"target": slot, "crew": instance_id, "closed": True}


def _read_limit(raw: object) -> int:
    if raw in (None, ""):
        return 20
    try:
        limit = int(str(raw))
    except ValueError:
        raise CrewCallError(400, "invalid_pagination", "limit must be an integer") from None
    if not 1 <= limit <= _READ_LIMIT_MAX:
        raise CrewCallError(400, "invalid_pagination", f"limit must be 1-{_READ_LIMIT_MAX}")
    return limit


async def handle_crew_call(request: web.Request, verb: str, body: dict[str, Any]) -> web.Response:
    """Run one ``crew=`` session-control call. *body* is the JSON body (query for read).

    The caller has already passed the route's ``_require_internal``.
    """
    from kiro_crew.dashboard.handlers._shared import _read_session_key

    declared = _read_session_key(request)
    disabled = await refuse_if_disabled(declared, verb)
    if disabled is not None:
        return disabled
    # ``agent.session_control`` withdraws every session verb at once; a crew call
    # is one of them, so it honours the same switch the local verbs do.
    from kiro_crew.dashboard import session_control as sc

    if not await asyncio.to_thread(sc.session_control_enabled):
        _audit(declared, verb, "denied", error="session_control_disabled")
        return web.json_response(
            {
                "error": "session control is disabled (agent.session_control)",
                "code": "session_control_disabled",
            },
            status=403,
        )
    state: DashboardState = request.app["state"]
    crew_ref = clean_crew_ref(body.get("crew"))
    slot, limit, name = "", 20, ""
    try:
        caller = await authorize_remote_dispatch(request, verb)
        if verb == "create" and not has_crew(body):
            # Wave 5: a work item with no crew is placed by the hub (RFC §2b).
            from kiro_crew.dashboard.handlers import remote_placement

            board = session_ledger.ledger_key(declared)
            result, name = await remote_placement.place(
                state, _manager(state), caller, body, board=board
            )
            _audit(declared, verb, "success", crew=result["crew"])
            return web.json_response({**result, "crew_name": name, "placed": True})
        instance_id, name = await resolve_crew(state, body.get("crew"))
        mgr = _manager(state)
        if verb == "read":
            limit = _read_limit(body.get("limit"))
            if body.get("since") not in (None, ""):
                raise CrewCallError(
                    400,
                    "crew_field_unsupported",
                    "session_read_message with crew= does not take since",
                )
        board = session_ledger.ledger_key(declared)
        if verb != "create":
            slot = await _fenced_target_or_item(
                state, caller, board, instance_id, body.get("target")
            )
        await check_version(mgr, instance_id)
        if verb == "create" and body.get("work_item") is not None:
            # Counted in flight so a placement running meanwhile sees it (wave 5).
            from kiro_crew.dashboard.handlers import remote_placement

            async with remote_placement.holding(state, instance_id):
                result = await _create(state, mgr, caller, instance_id, body, board=board)
        elif verb == "create":
            result = await _create(state, mgr, caller, instance_id, body, board=board)
        elif verb == "send":
            result = await _send(mgr, instance_id, slot, body)
        elif verb == "read":
            result = await _read(mgr, instance_id, slot, limit)
        elif verb == "stop":
            result = await _stop(mgr, instance_id, slot)
        elif verb == "close":
            result = await _close(state, mgr, caller, instance_id, slot)
        else:  # pragma: no cover - the routes pass a literal from CREW_VERBS
            raise CrewCallError(400, "crew_verb_unknown", "unknown crew verb")
    except CrewCallError as exc:
        _audit(
            declared,
            verb,
            "denied" if exc.status < 500 else "failure",
            crew=crew_ref,
            error=exc.code,
        )
        return _response(exc)
    _audit(declared, verb, "success", crew=instance_id)
    return web.json_response({**result, "crew_name": name})
