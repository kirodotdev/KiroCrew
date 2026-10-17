"""The stub side of a shared ``Backend``.

Attaching and detaching stubs; forwarding a stub's frames upstream with their ids rewritten
and the caller identity injected; delivering frames into stub inboxes and answering a stub
locally; cancelling a departing stub's in-flight work; and the tool set each stub was told
about, with the probe that measures a replacement backend's.

Each function here whose first parameter is ``self`` is a ``Backend`` method: the class
binds it by name, so it reads and writes the backend's own state on the instance, and a
patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_dead_reason``, ``_forward_id_seq``, ``_grant_callers``,
``_init_pending``, ``_lease_pending_riders``, ``_lease_release_waiters``,
``_lease_replacement_subscribes``, ``_orphaned_leases``, ``_pending_requests``,
``_rekey_generation``, ``_resource_subscriptions``, ``_served_tool_surfaces``,
``_stub_agents``, ``_stub_inboxes``, ``refcount``.

Instance fields read: ``_inbox_lock``, ``_lease_awaiting_release``, ``pool_key``,
``stdin``, ``supports_caller_identity``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from typing import TYPE_CHECKING, Any, Optional

from kiro_crew.mcp_caller import CallerContext
from kiro_crew.mcp_gateway.backend_handshake import _REHANDSHAKE_RETRY_METHODS
from kiro_crew.mcp_gateway.backend_leases import (
    _ORPHANED_LEASES_MAX,
    _RELEASE_STUB_SENTINEL,
    _RESOURCES_SUBSCRIBE_METHOD,
    _RESOURCES_UNSUBSCRIBE_METHOD,
)
from kiro_crew.mcp_gateway.backend_wire import (
    TOOL_SURFACE_STUB_PREFIX,
    BackendGone,
    _inject_caller_meta,
    _inject_tenant_meta,
    _PendingRequest,
    _strip_caller_meta,
    _write_json_line,
)
from kiro_crew.mcp_gateway.tool_surface import ToolSurface, project_tool_surface

if TYPE_CHECKING:
    from kiro_crew.mcp_gateway.backend import Backend

#: The facade's logger: log capture and filters select this code's records by that name.
logger = logging.getLogger("kiro_crew.mcp_gateway.backend")


# Budget for the tool-surface probe (see ``Backend.probe_tool_surface``). Same
# 10s as the app-call listing, and for the same reason: a backend that has just
# completed its handshake answers ``tools/list`` in milliseconds, so a longer
# wait only stretches how long a mid-call recovery holds its stub. Overrunning
# it reads as "could not establish", never as agreement.
_TOOL_SURFACE_PROBE_TIMEOUT_SECS = 10.0


# Upper bound on a single stub's pending-delivery inbox. Backend->stub frames
# are enqueued by the stdout pump without awaiting the stub's socket drain, so
# a stub that has stopped reading must not let a chatty backend grow gateway
# RSS without bound. Past this many undrained frames the slow stub is dropped
# (see ``Backend._enqueue_to_stub``) so co-pooled sessions are protected.
# Generous enough that normal bursts (large tools/list, rapid tool calls)
# never trip it.
_STUB_INBOX_MAXSIZE = 4096


async def attach_stub(self: Backend, stub_uuid: str, *, agent: str = "") -> "asyncio.Queue[bytes]":
    """Register ``stub_uuid`` as an active consumer of this backend.

    Returns a fresh inbox queue the connection handler must drain. The
    refcount bumps so the idle-sweep skips this backend.

    ``agent`` is the attaching stub's declared agent, recorded for
    :meth:`agent_for_stub`. It defaults to empty so a caller with no session
    behind it — the ephemeral app-call stub — does not have to invent one,
    and an empty value refuses downstream rather than widening anything.
    """
    inbox: "asyncio.Queue[bytes]" = asyncio.Queue(maxsize=_STUB_INBOX_MAXSIZE)
    async with self._inbox_lock:
        if stub_uuid in self._stub_inboxes:
            raise RuntimeError(f"stub_uuid={stub_uuid} already attached to backend pid={self.pid}")
        self._stub_inboxes[stub_uuid] = inbox
        if agent:
            self._stub_agents[stub_uuid] = agent
        self.refcount = len(self._stub_inboxes)
    self.touch()
    logger.debug(
        "attach_stub pool=%s stub=%s refcount=%d",
        self.pool_key.human_readable(),
        stub_uuid,
        self.refcount,
    )
    return inbox


async def detach_stub(self: Backend, stub_uuid: str) -> int:
    """Drop ``stub_uuid``'s inbox and clean up any pending requests
    owned by it. Returns the remaining refcount so the caller can
    decide whether to trigger a drain.
    """
    async with self._inbox_lock:
        self._stub_inboxes.pop(stub_uuid, None)
        # Under the same lock as the inbox it shadows, so a render spooled
        # after a detach can never be stamped with a gone stub's agent.
        self._stub_agents.pop(stub_uuid, None)
        self.refcount = len(self._stub_inboxes)
    # Bounded-table hygiene. (A replay in flight for a detached stub is
    # already handled at grant time: a replay grant is honoured only
    # while its stub is still attached.)
    self._rekey_generation.pop(stub_uuid, None)
    # The tool set this stub was told about goes with it. A respawn reads it
    # BEFORE detaching for exactly this reason (see ``served_tool_surface``);
    # keeping it here instead would grow one entry per session this backend
    # ever served.
    self._served_tool_surfaces.pop(stub_uuid, None)
    # A departing stub parked as a rider expects no further reply. This
    # prune runs BEFORE the pending scan below: the scan promotes the
    # first parked rider into the departing stub's in-flight subscribe,
    # and a duplicate parking owned by the departing stub itself would
    # otherwise be promoted — recording a phantom subscriber that blocks
    # the release and swallows updates.
    for uri in list(self._lease_pending_riders):
        remaining = [r for r in self._lease_pending_riders[uri] if r[0] != stub_uuid]
        if remaining:
            self._lease_pending_riders[uri] = remaining
        else:
            del self._lease_pending_riders[uri]
    # Likewise a departing stub parked behind an in-flight release.
    for uri in list(self._lease_release_waiters):
        remaining = [r for r in self._lease_release_waiters[uri] if r[0] != stub_uuid]
        if remaining:
            self._lease_release_waiters[uri] = remaining
        else:
            del self._lease_release_waiters[uri]
    # And a departing stub whose replacement subscribe is parked.
    for uri in list(self._lease_replacement_subscribes):
        remaining = [r for r in self._lease_replacement_subscribes[uri] if r[0] != stub_uuid]
        if remaining:
            self._lease_replacement_subscribes[uri] = remaining
        else:
            del self._lease_replacement_subscribes[uri]
    # Drop any pending-request entries owned by the departing stub so the
    # stdout pump does not try to send into a dead inbox — EXCEPT an
    # in-flight coalesced lease transition, whose response still has to
    # settle shared state: a subscribe with parked riders is handed to the
    # first rider (which then receives the server's verdict under its own
    # id); one without riders is converted to the lease sentinel so a
    # granted-but-unwanted lease is released instead of leaking.
    stale = [fid for fid, p in self._pending_requests.items() if p.stub_uuid == stub_uuid]
    for fid in stale:
        p = self._pending_requests.get(fid)
        if p is None:
            continue
        if p.resource_uri and p.method == _RESOURCES_SUBSCRIBE_METHOD:
            # BOTH regimes: dropping the pending would drop the server's
            # verdict — a late identity grant would strand the upstream
            # lease with nobody to release it. Coalesced: hand it to the
            # first parked rider; otherwise (including every identity
            # pending, which never has riders) convert to the sentinel,
            # whose grant arm releases a granted-but-unwanted lease as
            # ``pending.caller`` — the right principal.
            riders = (
                []
                if self.supports_caller_identity
                else self._lease_pending_riders.get(p.resource_uri, [])
            )
            if riders:
                p.stub_uuid, p.original_id = riders.pop(0)
            else:
                p.origin_stub = stub_uuid
                p.stub_uuid = _RELEASE_STUB_SENTINEL
                p.original_id = None
            continue
        if p.resource_uri and p.method == _RESOURCES_UNSUBSCRIBE_METHOD:
            # Let the release-in-flight settle silently; the table entry
            # is pruned below either way.
            p.origin_stub = stub_uuid
            p.stub_uuid = _RELEASE_STUB_SENTINEL
            p.original_id = None
            continue
        self._pending_requests.pop(fid, None)
    # Drop the departing stub from the resource-subscription table so a
    # later ``notifications/resources/updated`` is not routed toward a
    # departed stub — and release upstream whatever the departure
    # strands. Identity-less: release when the departing stub was a
    # URI's LAST subscriber (the one shared lease). Identity-capable:
    # every grant is per caller, so the departing stub's grant is
    # released AS the caller recorded at grant time — unless another
    # still-routed stub shares that same caller for the URI (two stubs
    # claimed to one session hold ONE per-caller grant upstream;
    # releasing on the first detach would kill the survivor's updates).
    # Without a release the server keeps firing updates nobody will
    # receive, each landing in the deny-by-default drop and unfairly
    # recording a hazard against the server for a gap that is ours.
    # ALL table transitions commit before the release writes (a write
    # can re-enter detach via a full-inbox drop).
    emptied = []
    for uri, subscribers in self._resource_subscriptions.items():
        subscribers.discard(stub_uuid)
        if not subscribers:
            emptied.append(uri)
    if self.supports_caller_identity:
        identity_releases: list[tuple[str, CallerContext]] = []
        departed_keys = [key for key in self._grant_callers if key[1] == stub_uuid]
        for uri, _stub in departed_keys:
            grant_caller = self._grant_callers.pop((uri, stub_uuid))
            shared = any(
                other != stub_uuid and self._grant_callers.get((uri, other)) == grant_caller
                for other in self._resource_subscriptions.get(uri, set())
            )
            if not shared:
                identity_releases.append((uri, grant_caller))
        for uri in emptied:
            del self._resource_subscriptions[uri]
            if not any(u == uri for u, _c in identity_releases):
                # Routed grant with no recorded caller (identity server,
                # grant predates tracking or replay went unrecorded): a
                # bare release would be adjudicated as the wrong
                # principal, so the lease is knowingly RETAINED — mark
                # it orphaned so its updates are dropped without
                # charging a hazard to a blameless server.
                while len(self._orphaned_leases) >= _ORPHANED_LEASES_MAX:
                    self._orphaned_leases.pop()
                self._orphaned_leases.add(uri)
        for uri, grant_caller in identity_releases:
            await self._release_upstream_subscriptions([uri], caller=grant_caller)
    else:
        to_release = []
        for uri in emptied:
            del self._resource_subscriptions[uri]
            # A URI whose release is already in flight (final
            # unsubscribe forwarded, response pending) is not released
            # twice; its awaiting-release flag stays up so the
            # converted-to-sentinel response settles it (marking the
            # lease orphaned on a refusal).
            if uri not in self._lease_awaiting_release:
                to_release.append(uri)
        if to_release:
            await self._release_upstream_subscriptions(to_release)
    # Initialize-cache cleanup: if the departing stub was mid-wait for
    # a cached initialize reply, drop it from the pending list.
    self._init_pending = [entry for entry in self._init_pending if entry[0] != stub_uuid]
    if self.refcount == 0:
        self.touch()  # start the idle clock fresh
    logger.debug(
        "detach_stub pool=%s stub=%s refcount=%d",
        self.pool_key.human_readable(),
        stub_uuid,
        self.refcount,
    )
    return self.refcount


def _next_forward_id(self: Backend) -> str:
    """Return a monotonic gateway-scoped id for rewriting stub requests."""
    self._forward_id_seq += 1
    return f"gw-{self.pid}-{self._forward_id_seq}"


async def forward_from_stub(
    self: Backend,
    stub_uuid: str,
    msg: dict[str, Any],
    *,
    caller: Optional[CallerContext] = None,
    tenant_nonce: str = "",
) -> None:
    """Forward one JSON-RPC message from ``stub_uuid`` to the backend.

    Implements the three pieces of Milestone 2 correctness:

    1. Initialize caching — only the first stub's ``initialize`` reaches
       the backend. Later stubs receive the cached result locally so
       the backend state machine does not see a double-initialize.
    2. Id rewriting — the stub's id is replaced with a gateway-scoped
       monotonic id. The mapping survives in ``_pending_requests`` so
       the stdout pump can put the original id back on the response.
    3. Caller-identity injection — ``tools/call`` requests get a
       ``params._meta.kirocrew.caller`` block when the backend
       advertised the capability at initialize time. The block is
       built via :func:`kiro_crew.mcp_caller.build_caller_meta` so
       gateway + backend share exactly one wire format.

    ``tenant_nonce`` is this CONNECTION's namespace separator, injected
    alongside (3) and independently of it: a caller the gateway cannot name
    gets no identity block but still gets a nonce, which is what keeps two
    unnamed co-tenants of one pooled backend out of each other's per-tenant
    state. Empty means "no separator available" and leaves the
    backend on its own per-process fallback.
    """
    if not self.is_alive:
        raise BackendGone(self._dead_reason or "backend is not alive")

    method = msg.get("method") if isinstance(msg, dict) else None

    if method == "initialize":
        await self._handle_initialize(stub_uuid, msg)
        return
    if method == "notifications/initialized":
        # ALWAYS suppress stub-originated
        # ``notifications/initialized``. The gateway sends exactly one
        # synthetic notification to the backend from
        # ``_on_upstream_initialize`` once the handshake completes. A stub
        # cannot emit this until it has received its initialize response,
        # which is only delivered AFTER ``_init_state`` is already
        # ``"ready"`` — so the previous "let the first stub through during
        # in_flight" branch was unreachable and the real backend never
        # received the notification at all. A spec-compliant backend that
        # gates tool processing on it would hang forever. Suppressing every
        # stub echo here + one synthetic upstream send preserves the
        # one-initialized-per-backend invariant.
        return

    if method in (_RESOURCES_SUBSCRIBE_METHOD, _RESOURCES_UNSUBSCRIBE_METHOD):
        # Subscription lease bookkeeping. Returns True when the request
        # was answered locally (a later subscriber joining a held lease,
        # or a non-final unsubscribe) and must NOT reach the backend —
        # the server holds one subscription per URI, and forwarding
        # either frame would break a co-tenant's live subscription.
        if await self._handle_resource_subscription(stub_uuid, method, msg):
            return

    # Request/response rewrite: only requests carry both method AND id.
    # Pure notifications (method, no id) and pure responses (id, no
    # method) pass through without rewrite. Pure responses are kiro-cli
    # answering a server-to-client request — the backend owns that id
    # table, not us.
    retry_fid: Optional[str] = None
    if isinstance(msg, dict):
        orig_id = msg.get("id")
        has_method = "method" in msg
        if has_method and orig_id is not None:
            fid = self._next_forward_id()
            if method in _REHANDSHAKE_RETRY_METHODS:
                retry_fid = fid
            msg = dict(msg)  # shallow copy — we mutate id + maybe _meta
            msg["id"] = fid
            progress_token = None
            tool_name = ""
            tool_arguments = None
            resource_uri = ""
            list_paginated = False
            _params = msg.get("params")
            if isinstance(_params, dict):
                _meta = _params.get("_meta")
                if isinstance(_meta, dict):
                    progress_token = _meta.get("progressToken")
                if method == "tools/list":
                    # A cursor means this response is a CONTINUATION page,
                    # so on its own it is not the session's whole tool set.
                    # Captured here because the recording side sees only the
                    # response, and a final page is indistinguishable from a
                    # complete listing without knowing what was asked.
                    list_paginated = _params.get("cursor") is not None
                if method == "tools/call":
                    _name = _params.get("name")
                    if isinstance(_name, str):
                        tool_name = _name
                    _args = _params.get("arguments")
                    if isinstance(_args, dict):
                        tool_arguments = _args
                if method in (_RESOURCES_SUBSCRIBE_METHOD, _RESOURCES_UNSUBSCRIBE_METHOD):
                    _uri = _params.get("uri")
                    if isinstance(_uri, str):
                        resource_uri = _uri
            self._pending_requests[fid] = _PendingRequest(
                stub_uuid=stub_uuid,
                original_id=orig_id,
                method=str(method or ""),
                t_start_ms=time.monotonic() * 1000.0,
                progress_token=progress_token,
                session_key=(caller.session_key if caller is not None else ""),
                tool_name=tool_name,
                tool_arguments=tool_arguments,
                resource_uri=resource_uri,
                list_paginated=list_paginated,
                caller=(caller if resource_uri else None),
            )
        elif method == "notifications/cancelled":
            # A cancellation is a notification (method, no top-level id);
            # its target lives in params.requestId and still holds the
            # STUB's original id. The backend tracks that request under our
            # gateway-scoped fid, so forwarding verbatim makes the cancel a
            # silent no-op (the tool call keeps running, pinning the shared
            # backend). Remap params.requestId to the fid we assigned this
            # stub's request (scoped to this stub, so no cross-tenant
            # mis-cancel).
            _cparams = msg.get("params")
            if isinstance(_cparams, dict) and "requestId" in _cparams:
                orig_req = _cparams["requestId"]
                cancel_fid = next(
                    (
                        f
                        for f, pend in self._pending_requests.items()
                        if pend.stub_uuid == stub_uuid and pend.original_id == orig_req
                    ),
                    None,
                )
                if cancel_fid is not None:
                    msg = dict(msg)
                    new_params = dict(_cparams)
                    new_params["requestId"] = cancel_fid
                    msg["params"] = new_params
                    # Mark rather than drop: the backend answers a
                    # cancelled call with NO response, so the wedge
                    # detector skips a marked entry instead of aging the
                    # shared backend into its ceiling, while notification
                    # attribution and the last-stub recycle still see it.
                    # A subscribe/unsubscribe is not marked: its verdict
                    # still settles the shared lease tables.
                    cancelled = self._pending_requests[cancel_fid]
                    if not (
                        cancelled.resource_uri
                        and cancelled.method
                        in (
                            _RESOURCES_SUBSCRIBE_METHOD,
                            _RESOURCES_UNSUBSCRIBE_METHOD,
                        )
                    ):
                        cancelled.cancelled = True
        # Trust boundary: unconditionally strip any stub-supplied caller
        # identity on EVERY forwarded request regardless of method, then
        # inject the authoritative caller block when known.
        msg = _strip_caller_meta(msg)
        if self.supports_caller_identity and caller is not None:
            msg = _inject_caller_meta(msg, caller)
        if self.supports_caller_identity and tenant_nonce:
            msg = _inject_tenant_meta(msg, tenant_nonce)
        if retry_fid is not None:
            # The frame exactly as it goes upstream -- identity blocks
            # included -- so a retry after a rehandshake is the same request.
            retry_pending = self._pending_requests.get(retry_fid)
            if retry_pending is not None:
                retry_pending.retry_frame = msg

    self.touch()
    try:
        await _write_json_line(self.stdin, msg)
    except (BrokenPipeError, ConnectionResetError) as exc:
        self._dead_reason = f"stdin closed: {exc}"
        raise BackendGone(self._dead_reason) from exc


def served_tool_surface(self: Backend, stub_uuid: str) -> Optional[ToolSurface]:
    """The tool set this backend told ``stub_uuid`` about.

    ``None`` when that stub was never served a model-facing ``tools/list``,
    or was served one this host could not project — in both cases there is
    no claim a replacement could contradict, which is a different answer
    from an empty tool set.

    A caller that will also detach the stub must read this FIRST:
    :meth:`detach_stub` prunes the entry, and a read after it would report
    "nothing was ever served" for a session that was told plenty.
    """
    return self._served_tool_surfaces.get(stub_uuid)


def carry_served_tool_surface(self: Backend, stub_uuid: str, surface: ToolSurface) -> None:
    """Adopt ``stub_uuid``'s existing tool-set claim onto THIS backend.

    Called by the respawn path once a replacement has been validated and the
    stub rebound to it. The claim belongs to what the SESSION holds, not to
    the process that answered it: without carrying it, the anchor dies with
    the backend it was recorded on, the replacement starts anchor-less, and a
    SECOND respawn of the same stub has nothing to compare — so the guard
    would protect only the first process swap in a session's life while the
    client's frozen tool set is still the one from its original listing.

    Carries what the session was TOLD, not what the replacement published.
    The two agree on the tools the client holds, but a replacement may
    legitimately offer more (an addition is not drift), and those extra tools
    are not in the client's frozen set — so recording them would let a later
    respawn refuse over a tool no call could name.
    """
    self._served_tool_surfaces[stub_uuid] = surface


async def probe_tool_surface(
    self: Backend,
    *,
    caller: Optional[CallerContext] = None,
    tenant_nonce: str = "",
    timeout: float = _TOOL_SURFACE_PROBE_TIMEOUT_SECS,
) -> Optional[ToolSurface]:
    """Ask this backend what it publishes, projected for comparison.

    Returns ``None`` when the answer cannot be projected into a surface —
    the request failed, timed out, the server answered with a JSON-RPC
    error, or the listing is not shaped like one
    :func:`~kiro_crew.mcp_gateway.tool_surface.project_tool_surface` can
    read. Every one of those is "we could not establish what it publishes",
    which the caller must not be able to confuse with agreement.

    Runs on its own internal stub so the reply is routed to us and to
    nobody else, and so the model-visibility filter does not narrow the
    server's own declaration on the way (see
    :data:`INTERNAL_STUB_PREFIXES`). *caller* and *tenant_nonce* are carried
    through so the probe asks in the SAME tenant context the recorded
    listing was answered in: an identity-scoped server answers about the
    session whose surface is being compared, and an unnamed caller keeps the
    connection's ``_meta.kirocrew.tenant`` namespace instead of falling into
    the per-process one. Asking without either would compare two answers the
    server gave to different tenants and read the difference as drift.

    Never raises: this is called from an adoption path whose entire purpose
    is to be the safe one, so a probe that blows up must read as "could not
    establish" rather than take the recovery down with it.
    """
    stub_uuid = f"{TOOL_SURFACE_STUB_PREFIX}{uuid.uuid4().hex[:12]}"
    frame = {
        "jsonrpc": "2.0",
        "id": f"tool-surface-{uuid.uuid4().hex[:8]}",
        "method": "tools/list",
        "params": {},
    }
    try:
        inbox = await self.attach_stub(stub_uuid)
    except Exception:  # pragma: no cover — defensive
        logger.debug("tool-surface probe could not attach", exc_info=True)
        return None
    try:
        await self.forward_from_stub(stub_uuid, frame, caller=caller, tenant_nonce=tenant_nonce)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            data = await asyncio.wait_for(inbox.get(), timeout=remaining)
            try:
                msg = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(msg, dict) or msg.get("id") != frame["id"]:
                continue
            if "result" not in msg:
                # A JSON-RPC error answers the id but establishes nothing.
                return None
            if not isinstance(msg["result"], dict):
                return None
            if msg["result"].get("nextCursor") is not None:
                # Only the FIRST page. This probe asks without a cursor by
                # design — following the chain would make a recovery path
                # issue an unbounded request sequence — so a paginated answer
                # establishes nothing about the whole tool set. Reads as
                # unmeasurable, never as agreement.
                logger.info(
                    "tool-surface probe on pid=%s got a paginated listing; "
                    "the replacement's tool set cannot be established",
                    self.pid,
                )
                return None
            return project_tool_surface(msg["result"])
    except (asyncio.TimeoutError, BackendGone, ConnectionError, OSError) as exc:
        logger.info("tool-surface probe on pid=%s did not answer: %s", self.pid, exc)
        return None
    except Exception:  # pragma: no cover — defensive
        logger.warning("tool-surface probe raised", exc_info=True)
        return None
    finally:
        # Mirrors the app-call relay's teardown: cancel first so a listing
        # still in flight is not left running with no consumer, then detach
        # unconditionally or this backend's refcount never returns to what
        # it was before the probe.
        with contextlib.suppress(Exception):
            await self.cancel_in_flight_for_stub(stub_uuid)
        with contextlib.suppress(Exception):
            await self.detach_stub(stub_uuid)


async def _enqueue_to_stub(
    self: Backend, stub_uuid: str, inbox: "asyncio.Queue[bytes]", data: bytes
) -> bool:
    """Non-blocking enqueue into a stub's inbox.

    Returns ``True`` on success. If the inbox is full — the stub has
    stopped draining its socket — the stub is dropped via
    :meth:`detach_stub` and ``False`` returned. A wedged stub must never
    apply backpressure to the shared stdout pump nor let a chatty backend
    grow gateway RSS without bound; dropping the one slow stub protects
    every co-pooled session.
    """
    try:
        inbox.put_nowait(data)
        return True
    except asyncio.QueueFull:
        logger.warning(
            "backend pid=%s stub=%s inbox full (cap=%d); dropping slow stub",
            self.pid,
            stub_uuid,
            _STUB_INBOX_MAXSIZE,
        )
        await self.detach_stub(stub_uuid)
        return False


async def _deliver_to_stub(self: Backend, stub_uuid: str, msg: dict[str, Any]) -> None:
    async with self._inbox_lock:
        inbox = self._stub_inboxes.get(stub_uuid)
    if inbox is None:
        logger.debug(
            "backend pid=%s response for detached stub=%s; dropping",
            self.pid,
            stub_uuid,
        )
        return
    await self._enqueue_to_stub(
        stub_uuid, inbox, (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")
    )


async def _broadcast(self: Backend, msg: dict[str, Any]) -> None:
    # FIXED: Server-to-client requests now route via the
    # priority chain (relatedRequestId -> single-stub -> last-requester)
    # before falling back here. Broadcast is only used for notifications
    # and as a last-resort fallback when no stub can be identified.
    payload = (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")
    async with self._inbox_lock:
        inboxes = list(self._stub_inboxes.items())
    for stub_uuid, inbox in inboxes:
        await self._enqueue_to_stub(stub_uuid, inbox, payload)


async def _reply_locally(
    self: Backend,
    stub_uuid: str,
    original_id: Any,
    *,
    result: Optional[dict[str, Any]] = None,
    error: Optional[dict[str, Any]] = None,
) -> None:
    """Answer a stub's request from the gateway without involving the
    backend. A request without an id expects no reply, so none is sent."""
    if original_id is None:
        return
    reply: dict[str, Any] = {"jsonrpc": "2.0", "id": original_id}
    if error is not None:
        reply["error"] = error
    else:
        reply["result"] = result if result is not None else {}
    await self._deliver_to_stub(stub_uuid, reply)


async def cancel_in_flight_for_stub(self: Backend, stub_uuid: str) -> list[str]:
    """Send MCP ``notifications/cancelled`` for every in-flight request
    owned by ``stub_uuid``. Returns the list of cancelled forward-ids.

    This is the core Scope-A fix: when a stub disconnects (session killed),
    the backend receives explicit cancellation so it can abort long-running
    tool work instead of running to completion with no consumer.
    """
    # Collect in-flight requests for this stub (before detach clears them)
    in_flight = [(fid, p) for fid, p in self._pending_requests.items() if p.stub_uuid == stub_uuid]
    cancelled_ids: list[str] = []
    for fid, pending in in_flight:
        if not self.is_alive:
            break
        cancel_notification = {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {
                "requestId": fid,
                "reason": "Session stopped — caller disconnected",
            },
        }
        try:
            await _write_json_line(self.stdin, cancel_notification)
            cancelled_ids.append(fid)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # Backend already dead — no point sending more
            break
    if cancelled_ids:
        logger.info(
            "backend pid=%s: sent %d cancel notifications for stub=%s [ids: %s]",
            self.pid,
            len(cancelled_ids),
            stub_uuid,
            ", ".join(cancelled_ids[:5]) + ("..." if len(cancelled_ids) > 5 else ""),
        )
    return cancelled_ids
