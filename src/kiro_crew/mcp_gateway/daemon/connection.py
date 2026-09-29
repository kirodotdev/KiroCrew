"""One stub connection end to end: first frame, Register, bridge, teardown.

The handler reads the first frame; a control frame is served by
:mod:`~kiro_crew.mcp_gateway.daemon.control` and ends the connection, and a Register
builds the PoolKey and the connection's identity. The bridge phase then forwards
frames to a backend acquired on ``ensure_backend`` (or lazily on the first MCP
frame), replaces a backend that dies mid-call, and on any exit detaches the stub
and reaps what it owned.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from kiro_crew.mcp_gateway import hazards
from kiro_crew.mcp_gateway.admission import Admission
from kiro_crew.mcp_gateway.backend import INTERNAL_STUB_PREFIXES, Backend, BackendGone
from kiro_crew.mcp_gateway.daemon import logger
from kiro_crew.mcp_gateway.daemon.admission_protocol import (
    _LEGACY_SPAWN_WAIT_SECS,
    _await_answering_pings,
    _negotiated_wait_budget,
    _PeerGone,
    _refuse_ensure_backend,
    _refuse_lazy_spawn,
)
from kiro_crew.mcp_gateway.daemon.audit import _audit_disconnect_cancel
from kiro_crew.mcp_gateway.daemon.control import _serve_control_frame
from kiro_crew.mcp_gateway.daemon.control_plane import _caller_for_backend
from kiro_crew.mcp_gateway.daemon.diagnostics import (
    _emit_backend_acquire_metric,
    _emit_lazy_load_metrics,
)
from kiro_crew.mcp_gateway.daemon.identity import (
    _apply_recaller,
    _conn_index_discard,
    _peer_admitted,
    _resolve_register_identity,
    _StubConn,
)
from kiro_crew.mcp_gateway.daemon.launch import TargetResolver
from kiro_crew.mcp_gateway.daemon.replacement import _ReplacementRefused
from kiro_crew.mcp_gateway.daemon.wire import (
    _jsonrpc_error,
    _read_first_frame,
    _stub_probe_add,
    _stub_probe_discard,
    _StubProbe,
    _write_json_line,
)
from kiro_crew.mcp_gateway.pool import BackendPool, PoolKey
from kiro_crew.mcp_gateway.prewarm import HotKeyStore

if TYPE_CHECKING:
    from kiro_crew.mcp_gateway import gatewayd as facade
else:
    from kiro_crew.mcp_gateway.daemon import facade


# Advertised to every stub in the Registered reply so it can negotiate rather
# than assume. Each entry means "this daemon implements it":
#   ensure_backend — the pre-flight control frame
#   bridge_ping    — the bridge-phase liveness monitor
#   poolable_ack   — the register payload's ``poolable`` field is READ. A stub
#                    that asked for a private backend has no other way to tell:
#                    a daemon predating the field ignores it and routes the
#                    register through the shared index, silently co-tenanting a
#                    server the operator never allowlisted. Such a daemon is
#                    reachable, because the manager adopts anything answering
#                    ``pong`` with no version handshake — so one that outlived a
#                    package upgrade serves new stubs.
#   spawn_queue    — ``ensure_backend`` may carry ``wait_budget_secs``; the daemon
#                    then QUEUES the spawn behind the global spawn gate and emits
#                    ``{"type": "queued", ...}`` keepalives while it waits, and its
#                    ``rejected`` frames carry a ``class``. A stub that did not
#                    see this capability never receives ``queued`` (its
#                    single-response pre-flight would read it as a rejection).
#   tenant_nonce   — every registered connection is given a per-connection nonce,
#                    forwarded in ``params._meta`` on each request, including the
#                    requests whose caller this daemon cannot name. A stub that
#                    asked to POOL a server separating unnamed co-tenants by that
#                    nonce (``POOLING_REQUIRES_TENANT_NONCE``) has no other way to
#                    tell, and the backend has none either: for an unnamed caller,
#                    an absent tenant block is equally what a 1:1 topology with no
#                    gateway looks like, and the two need opposite answers — the
#                    per-process fallback separates sessions exactly right in the
#                    first and collapses every co-tenant onto one namespace in the
#                    second. Same reachability as ``poolable_ack``: a daemon that
#                    outlived a package upgrade is adopted and serves new stubs.
REGISTERED_CAPABILITIES: tuple[str, ...] = (
    "ensure_backend",
    "bridge_ping",
    "poolable_ack",
    "spawn_queue",
    "tenant_nonce",
)


async def _handle_connection(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    pool: BackendPool,
    resolver: TargetResolver,
    socket_path: Path,
    hot_keys: Optional[HotKeyStore] = None,
    *,
    stop_event: Optional[asyncio.Event] = None,
    admission: Optional[Admission] = None,
) -> None:
    """Process one stub connection end-to-end.

    Phases:

    0. **Peer gate** (:func:`~kiro_crew.mcp_gateway.daemon.identity._peer_admitted`),
       before any frame is read.
    1. **Health probe and control frames** (optional): a client may send
       ``{"type": "ping"}`` as its first frame. The gateway replies ``{"type":
       "pong"}`` and closes — used by :class:`GatewayManager` to confirm the
       daemon is serving before returning from ``start()``. Every other one-shot
       control frame is answered the same way, by
       :func:`~kiro_crew.mcp_gateway.daemon.control._serve_control_frame`.
    2. **Handshake** (bounded by ``_REGISTER_TIMEOUT_SECS``): read the
       Register message, build the :class:`PoolKey`, reply with a
       Registered envelope containing a provisional ``backend_id``
       (the real backend is spawned lazily on the first MCP message —
       keeps idle stubs from pinning a backend).
    3. **Bridge** (no timeout wrapper — learned correction): stub frames
       go into :meth:`Backend.forward_from_stub`; a concurrent writer
       task drains the stub's inbox queue populated by the backend's
       stdout pump. Exits on any of: stub EOF, backend death, shutdown
       cancellation; :func:`_teardown_connection` then undoes the attach.
    """
    if not _peer_admitted(writer, socket_path):
        return
    register = await _read_first_frame(reader)
    if register is None:
        logger.debug("stub disconnected before first frame")
        return

    if await _serve_control_frame(
        register, writer, pool, hot_keys, stop_event=stop_event, admission=admission
    ):
        return

    if register.get("type") not in (None, "register"):
        logger.warning(
            "stub first frame has type=%r, want 'register' or 'ping'",
            register.get("type"),
        )
        return

    try:
        pool_key = PoolKey.from_register(register)
    except ValueError as exc:
        await _write_json_line(
            writer,
            {"type": "rejected", "reason": f"malformed Register: {exc}"},
        )
        logger.warning("rejected Register: %s", exc)
        return

    stub_uuid = str(register.get("stub_uuid", ""))
    # Absent ``poolable`` means this connection gets its own backend. Absence is
    # the safe default in both directions: an overlay written before the flag
    # existed never silently starts sharing, and a malformed frame cannot widen
    # a connection's blast radius beyond itself.
    poolable_requested = register.get("poolable") is True
    exclusive_stub_uuid = "" if poolable_requested else stub_uuid

    # Retreat: a server OBSERVED behaving per-client while shared is not pooled
    # again, whatever the overlay still says. This is the consuming half of the
    # hazard ledger. Without it, recording a hazard changed a label on the MCP
    # page and nothing else, so "share by default, retreat when observed" had no
    # retreat -- the ledger's own evidence never reached a routing decision.
    #
    # The identity-checked read is the right one precisely BECAUSE this acts on
    # the verdict: it answers for the program this launch actually runs, so an
    # upgrade or a config edit re-earns pooling instead of leaving the server
    # stranded on evidence about the build it replaced.
    #
    # Per-connection and per-key, so nothing global is switched off: every other
    # server keeps pooling, and this one still gets a working PRIVATE backend --
    # the same topology it would have with no gateway at all. The cost of a
    # wrong retreat is therefore lost process reuse, never a broken server.
    if poolable_requested:
        observed = hazards.observed_codes(
            pool_key.server_name,
            hazards.launch_identity(
                pool_key.command_args_hash,
                pool_key.effective_env_hash,
                pool_key.binary_version,
            ),
        )
        if observed:
            exclusive_stub_uuid = stub_uuid
            logger.warning(
                "hazard retreat: serving %r a private backend because %s was "
                "observed while it was shared",
                pool_key.server_name,
                ", ".join(observed),
            )

    def _release_reservation() -> None:
        """Release the hand-out reservation this connection actually took.

        Keyed on the OUTCOME, because that is what decides which acquire path
        ran: ``pool.get_or_create`` reserves, ``pool.acquire_exclusive`` does
        not. A hazard-retreated connection therefore reserved nothing even
        though it asked to pool, so releasing on the REQUEST would decrement a
        digest this connection never reserved.

        A private backend takes none: it never enters the shared index, so no
        sweeper can reclaim it between hand-out and attach. Releasing one anyway
        would be actively harmful — the reservation refcount is per DIGEST, and
        ``poolable`` is not a PoolKey dimension, so a pooled connection with an
        identical PoolKey shares the digest. That pairing is reachable whenever
        the allowlist changes under a daemon that outlives the gateway: the old
        overlay's stub still registers poolable while the new one does not, and
        now also whenever a retreat lands beside a concurrent pooled connection
        on the same key. The stray decrement would drop the pooled connection's
        eviction protection before its stub attaches.
        """
        if not exclusive_stub_uuid:
            pool.unreserve(pool_key)

    if not stub_uuid:
        await _write_json_line(
            writer,
            {"type": "rejected", "reason": "missing stub_uuid"},
        )
        logger.warning("rejected Register: missing stub_uuid")
        return

    # A reserved prefix is the gateway's OWN marker: `Backend` treats any stub
    # uuid starting with one as an internal request and skips both the MCP Apps
    # render path and the model-visibility filter (`INTERNAL_STUB_PREFIXES`).
    # That check is correct for requests the gateway mints itself, so the gap is
    # here: a stub that simply NAMES itself with the prefix at registration
    # inherits the exemption and can list tools the model is meant not to see.
    # Refused at the door, because the prefix is not a namespace a client may
    # enter.
    if stub_uuid.startswith(INTERNAL_STUB_PREFIXES):
        await _write_json_line(
            writer,
            {"type": "rejected", "reason": "reserved stub_uuid prefix"},
        )
        logger.warning("rejected Register: reserved stub_uuid prefix %r", stub_uuid)
        facade._audit_reserved_stub_prefix_denied(stub_uuid)
        return

    conn = await _resolve_register_identity(register, writer, stub_uuid, pool_key)
    caller = conn.caller

    # Register this connection for the keepalive probe. Scoped to the handler's
    # own task so a dead transport can cancel exactly the coroutine that is
    # parked on the read, letting its finally run the detach.
    _probe: Optional[_StubProbe] = None
    _self_task = asyncio.current_task()
    if _self_task is not None:
        _probe = _StubProbe(stub_uuid, writer, _self_task)
        _stub_probe_add(_probe)

    # Provisional backend_id: the real pid isn't known until the backend
    # spawns. Using the pool digest gives operators a stable grep key that
    # ties together every stub sharing the same backend even before spawn.
    provisional_id = f"pending-{pool_key.stable_hash()[:12]}"
    await _write_json_line(
        writer,
        {
            "type": "registered",
            "backend_id": provisional_id,
            "pool_label": pool_key.human_readable(),
            # Capability advertisement: lets a new stub detect a
            # new gateway and run the ensure_backend pre-flight. Absent on an
            # old gateway, so the new stub skips the pre-flight (no 25s skew
            # penalty) and falls back to the legacy lazy-spawn path.
            #
            # ``bridge_ping`` gates the stub's bridge-phase liveness monitor the
            # same way. It must be negotiated rather than assumed: a daemon that
            # outlived a package upgrade has no ``{"type": "ping"}`` handler, so
            # the frame would fall through to the forward path and no pong would
            # ever return — turning any call slower than the grace window into a
            # forced degrade of a perfectly healthy pooled session.
            "capabilities": list(REGISTERED_CAPABILITIES),
        },
    )
    logger.info(
        "registered stub_uuid=%s pool=%s",
        stub_uuid,
        pool_key.human_readable(),
    )
    # Accepting an identified stub is a permission decision; record it in the
    # SEL alongside the denial path so the audit trail covers both outcomes.
    facade._audit_peer_allowed(caller.session_key if caller else "", pool_key.human_readable())

    # Warm-pool observation: tally this accepted register so the hottest
    # PoolKeys can be prewarmed on the next startup. In-memory only here —
    # O(1), no IO — so it never slows the handshake; persistence is batched
    # by the flush sweeper. ``None`` when prewarming is disabled.
    if hot_keys is not None:
        hot_keys.record(register)
        # Hit-rate metric: a warm backend already pooled for this key (from a
        # prewarm or a prior chat) is a HIT; otherwise this register will fall
        # through to a lazy spawn below — a MISS. ``get`` is a non-mutating
        # lookup, so reading it here does not pin or alter the backend.
        hot_keys.record_outcome(hit=await pool.get(pool_key) is not None)

    # Bridge phase — ensure any attach is undone even if we bail early.
    backend: Optional[Backend] = None
    inbox: Optional["asyncio.Queue[bytes]"] = None
    writer_task: Optional[asyncio.Task[None]] = None
    # Per-connection write serialization. The outbound pump
    # (_drain_inbox_to_stub) and the forward loop's direct error replies both
    # write to this one StreamWriter; two concurrent writer.drain() calls trip a
    # CPython assert in _drain_helper and tear the transport down. Every
    # write+drain path acquires this lock (looked up off the writer).
    setattr(writer, "_mc_write_lock", asyncio.Lock())
    # Captured ``initialize`` frame for this connection. Stashed the first
    # time kiro-cli sends it so the transparent-respawn path can re-prime a
    # freshly spawned backend (kiro-cli never re-sends initialize after a
    # backend dies). Persists across warm-pool rekey since the stub process
    # — and this coroutine — outlive a single chat.
    captured_init: Optional[dict[str, Any]] = None
    # Frames read while a spawn wait was being served (see
    # ``_await_answering_pings``): pings were answered on the spot, everything
    # else is parked here and processed in arrival order before the socket is
    # read again, so nothing kiro-cli sent during a respawn is dropped. Bounded
    # by ``_MAX_PENDING_FRAMES`` / ``_MAX_PENDING_BYTES``, since only the loop
    # below drains it and it cannot run while a wait is being served.
    pending: deque[bytes] = deque()
    try:
        while True:
            try:
                line = pending.popleft() if pending else await reader.readuntil(b"\n")
            except asyncio.IncompleteReadError:
                return
            except asyncio.LimitOverrunError:
                logger.warning(
                    "stub %s frame exceeded %d bytes; dropping conn",
                    stub_uuid,
                    facade._MAX_FRAME_BYTES,
                )
                return
            if not line:
                return
            if len(line) > facade._MAX_FRAME_BYTES:
                logger.warning("stub %s frame too large (%d bytes); dropping", stub_uuid, len(line))
                return
            try:
                msg = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                logger.warning("stub %s sent non-JSON frame: %s", stub_uuid, exc)
                continue
            if not isinstance(msg, dict):
                logger.warning("stub %s sent non-object frame; dropping", stub_uuid)
                continue
            # Claim-push pickup: a concurrent ``claim`` connection may have
            # re-targeted this connection's identity via ``conn.caller``.
            # Sync per-frame so the very next forward carries the new caller.
            caller = conn.caller
            if msg.get("type") == "unregister":
                logger.info("stub %s sent Unregister; closing", stub_uuid)
                return

            # Warm-pool caller repair: a stub that registered key-less (its
            # kiro-cli was pool-spawned before the session was claimed) sends
            # this once its session key materializes. Update the caller used
            # for subsequent forwards so ``_meta.kirocrew.caller`` carries the
            # real identity — without it, pooled state-mutating tools see an
            # empty session key. Never forwarded to the backend. An empty /
            # malformed key yields ``None`` from ``_caller_from_register`` and
            # is ignored, so a bad recaller can never clobber a good caller.
            if msg.get("type") == "recaller":
                _apply_recaller(msg, conn, pool_key.human_readable())
                continue

            # Bridge-phase liveness ping: the stub sends ``{"type": "ping"}``
            # while it has outstanding requests to verify the gateway is still
            # responsive. Reply with ``{"type": "pong"}`` — never forwarded
            # to the backend.
            if msg.get("type") == "ping":
                try:
                    await _write_json_line(writer, {"type": "pong"})
                except (OSError, ConnectionError):
                    return
                continue

            # B1 pre-flight: the stub sends ``ensure_backend``
            # before forwarding any real MCP frame. Spawning (or reusing)
            # the backend here — instead of lazily on the first real frame —
            # means a capacity / circuit-breaker rejection reaches the stub
            # BEFORE kiro-cli's ``initialize`` is consumed, so the stub can
            # fall back to a clean per-session exec (the unread ``initialize``
            # is still in its stdin). This control frame is never forwarded
            # downstream to the backend.
            if msg.get("type") == "ensure_backend":
                if backend is None:
                    # ``spawn_queue`` negotiation: a finite ``wait_budget_secs``
                    # on the frame means the stub understands ``queued``
                    # keepalives and will wait for them. Anything else is an
                    # old stub, which keeps the single-response behaviour and
                    # a short, silent gate wait.
                    wait_budget = _negotiated_wait_budget(msg, admission)
                    queue_aware = wait_budget is not None
                    deadline: Optional[float] = None
                    if admission is not None:
                        deadline = time.monotonic() + (
                            wait_budget if wait_budget is not None else _LEGACY_SPAWN_WAIT_SECS
                        )

                    async def _on_queued(position: Any) -> None:
                        await _write_json_line(writer, position.frame())

                    _acquire_t0 = time.monotonic()
                    try:
                        backend, _was_spawned = await _await_answering_pings(
                            reader,
                            writer,
                            pending,
                            facade._acquire_backend(
                                pool,
                                pool_key,
                                resolver,
                                exclusive_stub_uuid=exclusive_stub_uuid,
                                admission=admission,
                                wait_deadline=deadline,
                                on_queued=_on_queued if queue_aware else None,
                            ),
                            stub_uuid=stub_uuid,
                        )
                        # acquire-only duration, captured before the attach_stub
                        # + create_task overhead so the metric stays true to name.
                        _acquire_ms = (time.monotonic() - _acquire_t0) * 1000.0
                    except _PeerGone:
                        return
                    except Exception as exc:
                        await _refuse_ensure_backend(
                            exc,
                            writer,
                            reader,
                            exclusive=bool(exclusive_stub_uuid),
                            caller=caller,
                            pool_key=pool_key,
                            admission=admission,
                        )
                        return
                    # Attach BEFORE replying ``ready`` so the stub can never
                    # forward a frame before its inbox exists.
                    try:
                        inbox = await backend.attach_stub(stub_uuid)
                    finally:
                        # Once attached, refcount>0 keeps the backend from
                        # eviction, so the hand-out reservation can go.
                        _release_reservation()
                    writer_task = asyncio.create_task(
                        facade._drain_inbox_to_stub(inbox, writer, stub_uuid),
                        name=f"mcp-gateway-stub-writer-{stub_uuid[:8]}",
                    )
                    # OTEL metric: acquire-only duration (captured above, before
                    # attach_stub + create_task overhead).
                    _emit_backend_acquire_metric(_acquire_ms, warm=not _was_spawned)
                await _write_json_line(writer, {"type": "ready"})
                continue

            # Lazy backend spawn on first forwarded message. The pool
            # dedups concurrent first-attaches so even if two stubs race
            # into this block at the same tick they share one backend.
            if backend is None:
                _lazy_t0 = time.monotonic()
                try:
                    backend, _lazy_was_spawned = await _await_answering_pings(
                        reader,
                        writer,
                        pending,
                        facade._acquire_backend(
                            pool,
                            pool_key,
                            resolver,
                            exclusive_stub_uuid=exclusive_stub_uuid,
                            admission=admission,
                            wait_deadline=(
                                None
                                if admission is None
                                else time.monotonic() + _LEGACY_SPAWN_WAIT_SECS
                            ),
                        ),
                        stub_uuid=stub_uuid,
                    )
                    # acquire/spawn-only duration, captured before the attach +
                    # create_task overhead.
                    _lazy_elapsed_ms = (time.monotonic() - _lazy_t0) * 1000.0
                except _PeerGone:
                    return
                except Exception as exc:
                    await _refuse_lazy_spawn(exc, writer, caller=caller, pool_key=pool_key)
                    return
                try:
                    inbox = await backend.attach_stub(stub_uuid)
                finally:
                    _release_reservation()
                writer_task = asyncio.create_task(
                    facade._drain_inbox_to_stub(inbox, writer, stub_uuid),
                    name=f"mcp-gateway-stub-writer-{stub_uuid[:8]}",
                )
                # OTEL metrics: lazy-load count + duration + acquire duration
                # (elapsed captured above, before attach + task overhead).
                _emit_lazy_load_metrics(_lazy_elapsed_ms, warm=not _lazy_was_spawned)

            # Stash the initialize frame so a transparent respawn can re-prime
            # a fresh backend without kiro-cli re-sending initialize.
            if msg.get("method") == "initialize":
                captured_init = dict(msg)

            # ``caller`` itself stays tokenless for the life of the connection:
            # the token is attached to a per-forward copy, decided against the
            # backend that receives THIS frame. A respawn hands the tokenless
            # base on and re-decides for the replacement.
            forward_caller = _caller_for_backend(backend, caller, conn)

            try:
                await backend.forward_from_stub(
                    stub_uuid, msg, caller=forward_caller, tenant_nonce=conn.tenant_nonce
                )
            except BackendGone as exc:
                # Transparent respawn: a shared backend dying must NOT brick
                # this stub's transport (which would make kiro-cli mark the
                # MCP server dead for the whole session AND poison the warm
                # pool for new tabs). Rebuild a fresh backend, re-prime its
                # handshake from the captured initialize, re-attach this stub,
                # and fail ONLY this one in-flight request with a retryable
                # error. The transport stays open, so the next call self-heals.
                try:
                    recovered = await _await_answering_pings(
                        reader,
                        writer,
                        pending,
                        facade._respawn_backend_for_stub(
                            pool,
                            pool_key,
                            resolver,
                            stub_uuid,
                            writer,
                            captured_init,
                            backend,
                            inbox,
                            writer_task,
                            caller=caller,
                            conn=conn,
                            admission=admission,
                        ),
                        stub_uuid=stub_uuid,
                    )
                except _PeerGone:
                    return
                except _ReplacementRefused as refusal:
                    # A replacement was available but validating it said no. The
                    # session gets the REASON, not "backend gone": that is the
                    # difference between a stated failure it can act on and one
                    # indistinguishable from an unrecoverable spawn.
                    await _write_json_line(writer, _jsonrpc_error(msg, str(refusal)))
                    return
                if recovered is None:
                    # Genuinely unrecoverable (no captured init, circuit
                    # breaker open / capacity, or prime failed): fall back to
                    # the terminal error so the stub can do a clean
                    # per-session exec rather than churn against a dead server.
                    await _write_json_line(writer, _jsonrpc_error(msg, f"backend gone: {exc}"))
                    return
                backend, inbox, writer_task = recovered
                # Fail only this in-flight request; kiro-cli retries it on the
                # now-healthy transport. A duplicate error for this id from the
                # dying backend's broadcast is harmless — clients dedupe by id.
                if isinstance(msg, dict) and "method" in msg and msg.get("id") is not None:
                    await _write_json_line(
                        writer,
                        _jsonrpc_error(msg, f"backend restarted mid-call, retry: {exc}"),
                    )
                continue
            except Exception as exc:  # pragma: no cover — defensive
                logger.exception("forward_from_stub failed for %s", stub_uuid)
                await _write_json_line(writer, _jsonrpc_error(msg, f"forward failed: {exc}"))
                return
    finally:
        await _teardown_connection(conn, _probe, backend, writer_task, pool, stub_uuid)


async def _teardown_connection(
    conn: _StubConn,
    probe: Optional[_StubProbe],
    backend: Optional[Backend],
    writer_task: Optional[asyncio.Task[None]],
    pool: BackendPool,
    stub_uuid: str,
) -> None:
    """Undo everything a registered connection held, whatever ended it.

    Runs in the handler's ``finally``: the claim index and the keepalive probe
    forget the connection, in-flight calls it owned are cancelled before its stub
    detaches, a private backend goes to the pool's tracked reap, and the stub
    writer stops last.
    """
    _conn_index_discard(conn)
    if probe is not None:
        _stub_probe_discard(probe)
    if backend is not None:
        # Scope A: before detaching, cancel any in-flight tool calls this
        # stub owned — the backend would otherwise run them to completion
        # with no consumer (the root cause of the stop/kill bug).
        # Best-effort: a failure here must never skip detach_stub below,
        # or the backend's refcount leaks and it can never be recycled.
        had_in_flight = any(p.stub_uuid == stub_uuid for p in backend._pending_requests.values())
        cancelled: list = []
        try:
            cancelled = await backend.cancel_in_flight_for_stub(stub_uuid)
        except Exception:
            logger.warning(
                "cancel_in_flight_for_stub failed for %s",
                stub_uuid,
                exc_info=True,
            )
        remaining = await backend.detach_stub(stub_uuid)
        if cancelled:
            logger.info(
                "stub %s detached with %d in-flight request(s) %s -> cancelled; refcount=%d",
                stub_uuid,
                len(cancelled),
                cancelled[:5],
                remaining,
            )
            # SEL audit: cancelling in-flight tool work on a plain stub
            # disconnect is the same security-relevant action as the abort
            # frame path (which audits via _audit_abort_applied).
            _audit_disconnect_cancel(stub_uuid, remaining, len(cancelled))
        else:
            logger.debug("stub %s detached; refcount=%d", stub_uuid, remaining)
        # Scope B: if no consumers remain and the backend had in-flight
        # work, kill+respawn (the cancel notification is best-effort —
        # the backend may not honour it).
        if remaining == 0 and had_in_flight:
            await backend.recycle_if_idle()
        # Scope B: if quarantined and now drained, recycle
        elif remaining == 0 and backend.quarantined:
            await backend.recycle_if_idle()
    # A connection-private backend has no second consumer to wait for and no
    # reuse value, so its stub going away is the end of its life. Reap it
    # here rather than leaving it to a sweeper: it is deliberately outside
    # the pooling maps, so no sweeper is watching it. A no-op for a pooled
    # stub, which is why it is unconditional. Fully suppressed: this runs in
    # ``finally``, where raising would skip the writer-task cancel below and
    # mask whatever ended the connection.
    #
    # Scheduled through the pool's tracked reap, NOT awaited here. This
    # ``finally`` also runs when the daemon's own teardown cancels the bridge
    # connections, and an inline ``await orphan.shutdown()`` was cancelled
    # with them: ``release_exclusive`` had already dropped the backend from
    # the exclusive map, so ``shutdown_all`` never saw it either, and the
    # child was left to exit on its own -- with the SIGKILL escalation never
    # reached if it did not. ``shutdown_all`` joins the tracked task, so the
    # daemon now returns only once the backend is reaped.
    try:
        orphan = await pool.release_exclusive(stub_uuid)
        if orphan is not None:
            pool.spawn_shutdown(orphan)
    except Exception:
        logger.warning("releasing private backend for stub %s failed", stub_uuid, exc_info=True)
    if writer_task is not None:
        writer_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await writer_task
