"""Transparent respawn: replacing the process behind a live stub, or refusing to.

When a shared backend dies mid-call the stub keeps its transport: a fresh backend
is admitted like any other spawn, primed with the captured ``initialize`` and
re-bound -- but adopted only when what it publishes still supports every call the
session could be holding, and only for the owner the respawn was captured for
(``docs/system-specs/modules/mcp-gateway-backend-replacement.md``).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING, Any, NoReturn, Optional

from kiro_crew.mcp_caller import CallerContext
from kiro_crew.mcp_gateway import hazards, tool_surface
from kiro_crew.mcp_gateway.admission import Admission, SpawnGateClosed, SpawnGateTimeout
from kiro_crew.mcp_gateway.backend import _DEFAULT_INITIALIZE_TIMEOUT_SECS, Backend, BackendGone
from kiro_crew.mcp_gateway.daemon import logger
from kiro_crew.mcp_gateway.daemon.control_plane import _caller_for_backend
from kiro_crew.mcp_gateway.daemon.identity import _StubConn
from kiro_crew.mcp_gateway.daemon.launch import TargetResolver, _TargetUnknown
from kiro_crew.mcp_gateway.host_budget import HostBudgetExhausted
from kiro_crew.mcp_gateway.pool import BackendPool, BackendUnavailable, PoolAtCapacity, PoolKey

if TYPE_CHECKING:
    from kiro_crew.mcp_gateway import gatewayd as facade
else:
    from kiro_crew.mcp_gateway.daemon import facade


class _ReplacementRefused(Exception):
    """A respawn was rejected for a reason the SESSION should be told.

    Distinct from the plain ``None`` give-ups (no captured initialize, acquire
    rejected, prime failed) because those say nothing a client could act on
    beyond "the backend is gone", while this one names what changed underneath
    it. The message is put into the terminal JSON-RPC error verbatim, so it must
    stay bounded and free of line breaks — which is what
    :func:`~kiro_crew.mcp_gateway.tool_surface.describe_surface_change` already
    guarantees for the tool names it reports.
    """


def _rekey_refusal_reason(conn: Optional["_StubConn"], captured_key: str) -> str:
    """Why a respawn must be refused because its stub changed owner, or ``""``.

    A ``claim`` frame can retarget a connection's identity on any await, and both
    sides of the tool-set comparison belong to the CAPTURED caller — the anchor
    is the listing that principal was served, and the probe asks as that
    principal. Across a rekey the comparison therefore describes somebody who no
    longer owns this stub, and on a caller-scoped server it says nothing about
    what the live owner would be served. Re-probing would race the same way, so
    the answer is refusal on the same fail-closed terms the subscription replay
    already uses.

    A connection that cannot answer the question (``None``) is NOT read as a
    rekey: it simply cannot be checked.
    """
    live_key = _live_session_key(conn)
    if live_key is None or live_key == captured_key:
        return ""
    return (
        "the stub's owner was retargeted mid-respawn, so the tool set it was "
        "told about cannot speak for the live owner"
    )


def _refuse_replacement(
    stub_uuid: str, pool_key: PoolKey, captured_key: str, reason: str
) -> NoReturn:
    """Log, audit, and raise for a replacement that validation rejected.

    Fail loud rather than silently: the frozen tool set lives in the client and
    no gateway-side write can refresh it, so the only honest options are to adopt
    a process whose schema the session cannot see has moved, or to refuse and say
    why. Refusing lands on the give-up path this function already has — the
    caller answers the in-flight request with a terminal error and the stub
    reconnects — which is a stated failure at a call boundary instead of a wrong
    one.

    RAISES rather than returning ``None`` so the reason reaches the SESSION: the
    generic give-ups answer with "backend gone", indistinguishable from an
    unrecoverable spawn, and the one party that could act on knowing the tool set
    moved is the client, which sees neither the log nor the audit trail.

    One function for every refusal site, because a second copy is how the log
    line, the audit event and the client's message drift apart.
    """
    logger.warning(
        "respawn give-up (replacement rejected) stub=%s pool=%s: %s",
        stub_uuid,
        pool_key.human_readable(),
        reason,
    )
    facade._audit_replacement_validated(captured_key, pool_key.human_readable(), "denied", reason)
    raise _ReplacementRefused(
        f"the MCP server was replaced and its tool set changed ({reason}); "
        f"this session's tools are stale — reconnect to pick up the new ones"
    )


def _live_session_key(conn: Optional["_StubConn"]) -> Optional[str]:
    """The session key that owns ``conn`` RIGHT NOW, or ``None`` when unknowable.

    A ``claim`` frame retargets a connection's identity, and it can land on any
    await — so a decision a respawn made from the caller captured at request
    time has to be re-checked against this before it takes effect. ``None`` (no
    connection threaded through) means the question cannot be asked, which is
    NOT the same as "unchanged": a caller must not read it as agreement.
    """
    if conn is None:
        return None
    return conn.caller.session_key if conn.caller is not None else ""


async def _respawn_backend_for_stub(
    pool: BackendPool,
    pool_key: PoolKey,
    resolver: TargetResolver,
    stub_uuid: str,
    writer: asyncio.StreamWriter,
    captured_init: Optional[dict[str, Any]],
    old_backend: Backend,
    old_inbox: Optional["asyncio.Queue[bytes]"],
    old_writer_task: Optional[asyncio.Task[None]],
    caller: Optional[CallerContext] = None,
    conn: Optional[_StubConn] = None,
    admission: Optional[Admission] = None,
) -> Optional[tuple[Backend, "asyncio.Queue[bytes]", asyncio.Task[None]]]:
    """Rebuild a fresh backend for ``stub_uuid`` after its shared backend died
    (see :func:`_respawn_backend_for_stub_unrecorded` for the mechanics).

    This wrapper is the recovery ladder's L2 rung: a completed respawn is one
    ``record_restart(L2_backend)`` plus an ``observe_success`` for the server,
    and a give-up is one ``observe_failure`` -- the second give-up for the same
    server inside the layer's cooldown escalates to L3 (the ACP runtime), which
    is what the stub's terminal error then represents. The breaker's own
    ``OPEN`` cooldown stays the wait between rungs; the ladder counts, it does
    not sleep here.
    """
    from kiro_crew.recovery.ladder import L2_BACKEND, default_ladder

    result = await facade._respawn_backend_for_stub_unrecorded(
        pool,
        pool_key,
        resolver,
        stub_uuid,
        writer,
        captured_init,
        old_backend,
        old_inbox,
        old_writer_task,
        caller=caller,
        conn=conn,
        admission=admission,
    )
    unit = pool_key.server_name
    try:
        if result is None:
            default_ladder().observe_failure(
                L2_BACKEND, unit, reason=f"respawn give-up for stub {stub_uuid[:8]}"
            )
        else:
            default_ladder().record_restart(L2_BACKEND)
            default_ladder().observe_success(L2_BACKEND, unit)
    except Exception:  # pragma: no cover - the ladder must never break a respawn
        logger.debug("recovery ladder L2 bookkeeping failed", exc_info=True)
    return result


async def _respawn_backend_for_stub_unrecorded(
    pool: BackendPool,
    pool_key: PoolKey,
    resolver: TargetResolver,
    stub_uuid: str,
    writer: asyncio.StreamWriter,
    captured_init: Optional[dict[str, Any]],
    old_backend: Backend,
    old_inbox: Optional["asyncio.Queue[bytes]"],
    old_writer_task: Optional[asyncio.Task[None]],
    caller: Optional[CallerContext] = None,
    conn: Optional[_StubConn] = None,
    admission: Optional[Admission] = None,
) -> Optional[tuple[Backend, "asyncio.Queue[bytes]", asyncio.Task[None]]]:
    """Rebuild a fresh backend for ``stub_uuid`` after its shared backend
    died and re-bind this stub to it transparently.

    Returns ``(new_backend, new_inbox, new_writer_task)`` on success, or
    ``None`` when recovery is impossible / undesirable (no captured
    initialize to replay, circuit breaker open, capacity, or the prime
    handshake failed) — the caller then falls back to the terminal error so
    the stub can do a clean per-session exec instead of the gateway churning
    spawns against a broken backend.

    The replacement is admitted like any other spawn (``admission``): it waits
    in the spawn gate up to the daemon's queue budget, during which the caller
    keeps answering the stub's bridge pings.

    Never re-forwards the in-flight request itself: a ``tools/call`` may have
    executed on the old backend before it died, so replaying it could
    double-execute a non-idempotent tool. The caller fails just that one
    request with a retryable error instead.
    """
    # Stop the old inbox drain first so it cannot race the new writer task
    # onto the same socket, then flush whatever the dying backend already
    # broadcast (errors for other in-flight requests of this stub) so
    # kiro-cli does not hang waiting on those ids.
    if old_writer_task is not None:
        old_writer_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await old_writer_task
    if old_inbox is not None:
        _lock = getattr(writer, "_mc_write_lock", None)
        _guard: Any = _lock if _lock is not None else contextlib.nullcontext()
        with contextlib.suppress(Exception):
            async with _guard:
                while True:
                    try:
                        payload = old_inbox.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    writer.write(payload)
                # Bounded: a stub that stopped reading during the respawn flush
                # must not pin this handler forever (the outer suppress cannot
                # catch a hang). Mirrors _write_json_line's bounded drain.
                await asyncio.wait_for(writer.drain(), timeout=facade._WRITE_REPLY_TIMEOUT_SECS)

    # Captured BEFORE detach (which prunes them): the URIs whose live
    # subscriptions must be replayed onto the replacement backend, or they
    # silently go dark — kiro-cli never learns the old backend died, so it
    # will never re-subscribe on its own.
    replay_uris: list[str] = []
    with contextlib.suppress(Exception):
        replay_uris = old_backend.resource_subscription_uris(stub_uuid)
    # Captured BEFORE detach for the same reason as the URIs above: the tool set
    # THIS stub was told about is per-stub state that detach prunes, and reading
    # it afterwards would report "nothing was ever served" for a session that
    # was told plenty — which reads as agreement and adopts blindly.
    old_surface: Optional[tool_surface.ToolSurface] = None
    with contextlib.suppress(Exception):
        old_surface = old_backend.served_tool_surface(stub_uuid)
    # The principal this respawn is FOR, as of when the failing request arrived.
    # Both rekey gates below re-check the live owner against it.
    captured_key = caller.session_key if caller is not None else ""
    with contextlib.suppress(Exception):
        await old_backend.detach_stub(stub_uuid)

    if captured_init is None:
        # Never saw an initialize on this connection — a fresh backend cannot
        # be made usable without replaying it. Give up (terminal).
        logger.info(
            "respawn give-up (no captured initialize) stub=%s pool=%s",
            stub_uuid,
            pool_key.human_readable(),
        )
        return None

    # A respawn must honour the ledger too, or the retreat has a hole exactly
    # where it matters most. The recycle that follows an unroutable server
    # request comes straight back here, so re-pooling would hand the SAME stubs
    # a shared backend for the server just observed misbehaving -- and no new
    # register happens to re-decide it, so the retreat would not take effect
    # until those sessions reconnected.
    #
    # ONE local drives both the acquire below and the release in the ``finally``,
    # because those two must agree: only ``pool.get_or_create`` reserves, so a
    # release keyed on a different predicate than the acquire would decrement a
    # digest this respawn never reserved and drop a concurrent pooled
    # connection's eviction protection.
    respawn_exclusive_uuid = stub_uuid if old_backend.exclusive_token else ""
    if not respawn_exclusive_uuid and hazards.observed_codes(
        pool_key.server_name,
        hazards.launch_identity(
            pool_key.command_args_hash,
            pool_key.effective_env_hash,
            pool_key.binary_version,
        ),
    ):
        respawn_exclusive_uuid = stub_uuid
        logger.warning(
            "hazard retreat on respawn: %r comes back private because a "
            "hazard is on record for this launch",
            pool_key.server_name,
        )

    try:
        new_backend, _ = await facade._acquire_backend(
            pool,
            pool_key,
            resolver,
            # A respawn must not silently promote a private backend into the
            # shared bucket: the replacement inherits the original binding,
            # unless the ledger has since argued against sharing it at all.
            exclusive_stub_uuid=respawn_exclusive_uuid,
            admission=admission,
            wait_deadline=(
                None if admission is None else time.monotonic() + admission.spawn_queue_wait_secs
            ),
        )
    except (
        _TargetUnknown,
        BackendUnavailable,
        PoolAtCapacity,
        HostBudgetExhausted,
        SpawnGateTimeout,
        SpawnGateClosed,
        OSError,
    ) as exc:
        logger.info(
            "respawn give-up (acquire rejected) stub=%s pool=%s: %s",
            stub_uuid,
            pool_key.human_readable(),
            exc,
        )
        return None
    except Exception:  # pragma: no cover — defensive
        logger.exception(
            "respawn acquire crashed stub=%s pool=%s",
            stub_uuid,
            pool_key.human_readable(),
        )
        return None

    # _acquire_backend reserved the pool key; release it on every path below
    # (attached -> refcount>0 guards it; bailed -> let the sweeper reclaim it).
    # Without this the reserved digest is skipped by evict_idle/LRU forever,
    # leaking a pool slot for every key that ever mid-call respawned.
    try:
        try:
            # The backend's own bound, so the replay expires together with the
            # first-handshake deadline the daemon configured this backend with.
            await new_backend.prime_initialize(captured_init, timeout=_init_timeout_of(new_backend))
        except BackendGone as exc:
            logger.info(
                "respawn give-up (prime failed) stub=%s pool=%s: %s",
                stub_uuid,
                pool_key.human_readable(),
                exc,
            )
            return None
        # Validate the replacement's tool set BEFORE adopting it. Priming the
        # captured handshake proves the fresh process talks MCP; it says nothing
        # about what it publishes, and ``initialize`` metadata does not describe
        # a tool set — so up to here a server upgraded in place could keep its
        # protocolVersion, capabilities and serverInfo while renaming a tool or
        # tightening a required field, and this stub's session would go on
        # issuing calls built against the schema the DEAD process published.
        #
        # Only asked when this stub was actually served a listing: with no
        # claim on record there is nothing a replacement can contradict, and
        # refusing then would turn a recovery this path already performs today
        # into a failure on no evidence. ``old_surface`` was captured above,
        # before the detach that prunes it.
        if old_surface is not None:
            drift = tool_surface.describe_surface_change(
                old_surface,
                await new_backend.probe_tool_surface(
                    # Re-decided against the REPLACEMENT: ``caller`` arrives
                    # tokenless, and a fresh process that failed the
                    # control-plane check must not read the token the dead
                    # one was entitled to.
                    caller=_caller_for_backend(new_backend, caller, conn),
                    tenant_nonce=(conn.tenant_nonce if conn is not None else ""),
                ),
            ) or _rekey_refusal_reason(conn, captured_key)
            if drift:
                # The fresh backend is deliberately left in the pool. Its tool
                # set is wrong only for a session holding the OLD declaration;
                # a session that starts after this reads the new one correctly
                # and legitimately, so tearing it down would punish every
                # future session for this one's frozen view.
                _refuse_replacement(stub_uuid, pool_key, captured_key, drift)
        new_inbox = await new_backend.attach_stub(stub_uuid)
        if replay_uris and conn is not None:
            # Rekey race: a ``claim`` frame can retarget this connection's
            # identity during the awaits above (acquire + prime). The
            # captured URIs belong to the OLD principal — replaying them
            # now would resubscribe the old owner's resources onto the
            # rekeyed stub, the exact leak ``evict_stub_subscriptions``
            # exists to prevent. Recheck the live owner at the last moment
            # and skip the replay when it changed (fail closed: the new
            # owner subscribes on its own; the old owner's leases on the
            # dead backend died with it).
            if _live_session_key(conn) != captured_key:
                logger.info(
                    "respawn skipping subscription replay (owner rekeyed "
                    "mid-respawn) stub=%s pool=%s",
                    stub_uuid,
                    pool_key.human_readable(),
                )
                replay_uris = []
        if replay_uris:
            # A server refusal of an individual replayed subscribe is
            # fail-closed by design (the update goes undelivered, never
            # mis-attributed). A WRITE failure is different: the fresh
            # backend's pipe is already broken, so reporting this respawn
            # as a success would hand the stub a backend whose replayed
            # subscriptions are silently dark forever. Give up loudly —
            # the caller tears the stub down and kiro-cli reconnects.
            try:
                await new_backend.replay_resource_subscriptions(
                    stub_uuid, replay_uris, caller=_caller_for_backend(new_backend, caller, conn)
                )
            except BackendGone as exc:
                logger.info(
                    "respawn give-up (subscription replay failed) " "stub=%s pool=%s: %s",
                    stub_uuid,
                    pool_key.human_readable(),
                    exc,
                )
                await new_backend.detach_stub(stub_uuid)
                return None
    finally:
        # A private backend never took a reservation, and releasing one would
        # decrement a POOLED connection sharing this digest (see
        # ``_release_reservation`` in the connection handler). Read the SAME
        # local the acquire used, not ``old_backend.exclusive_token``: a hazard
        # retreat above can make this respawn private while the old backend was
        # pooled, and the two must not disagree.
        if not respawn_exclusive_uuid:
            pool.unreserve(pool_key)
    # LAST word on ownership, and the one that actually closes the window. The
    # check beside the comparison above is an optimisation — it avoids the attach
    # and the replay when the owner has already moved — but ``attach_stub`` and
    # ``replay_resource_subscriptions`` both await, so a claim can still land
    # between that check and here. Everything from this point to the return is
    # synchronous, so a re-check here leaves no gap.
    #
    # Only when a surface was validated: with no anchor the comparison never
    # happened, so a rekey invalidates nothing, and refusing would fail a
    # recovery this path performs today on no evidence.
    if old_surface is not None:
        late_rekey = _rekey_refusal_reason(conn, captured_key)
        if late_rekey:
            # Detach what was just attached, or this backend's refcount keeps a
            # stub that is about to be told the adoption failed.
            with contextlib.suppress(Exception):
                await new_backend.detach_stub(stub_uuid)
            _refuse_replacement(stub_uuid, pool_key, captured_key, late_rekey)
    if old_surface is not None:
        # The claim follows the SESSION, not the process that answered it. The
        # anchor was recorded on the backend that just died; leaving it there
        # would make this replacement anchor-less, so the NEXT respawn of this
        # stub would have nothing to compare and would adopt blindly — the guard
        # would cover only the first process swap in a session's life, while the
        # client's frozen tool set is still the one from its original listing.
        #
        # Set after the ownership re-check above, so a refused adoption never
        # seeds a surface onto a backend the stub is not going to use.
        new_backend.carry_served_tool_surface(stub_uuid, old_surface)
    new_writer_task = asyncio.create_task(
        facade._drain_inbox_to_stub(new_inbox, writer, stub_uuid),
        name=f"mcp-gateway-stub-writer-{stub_uuid[:8]}",
    )
    logger.info(
        "transparent respawn: stub=%s rebound to fresh backend pid=%s pool=%s",
        stub_uuid,
        new_backend.pid,
        pool_key.human_readable(),
    )
    # Audited HERE, not at the comparison: this is the point the replacement
    # actually gains authority to serve the session — stub attached,
    # subscriptions replayed, writer task live. An event emitted earlier would
    # record authority that a later give-up revokes.
    #
    # An adoption with no anchor is recorded too, and says so. The alternative —
    # audit only when a comparison ran — would leave the swap this guard exists
    # to make visible unrecorded in exactly the case where nothing checked it.
    facade._audit_replacement_validated(
        captured_key,
        pool_key.human_readable(),
        "allowed",
        (
            f"tool set verified: {len(old_surface)} tool(s) unchanged"
            if old_surface is not None
            else "tool set not verified: this stub was served no listing"
        ),
    )
    return new_backend, new_inbox, new_writer_task


def _init_timeout_of(backend: Any) -> float:
    """The initialize bound this backend was spawned with.

    Read through ``getattr`` because respawn tests hand in doubles that predate
    the field; the module default is what such a backend would have armed.
    """
    value = getattr(backend, "initialize_timeout_secs", None)
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return _DEFAULT_INITIALIZE_TIMEOUT_SECS
