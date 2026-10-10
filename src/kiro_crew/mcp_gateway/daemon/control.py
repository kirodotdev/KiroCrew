"""The one-shot control frames served on the uid-gated socket.

``ping`` (the pong the manager's adoption gate reads), ``stats``, ``claim``,
``abort``, ``set-spawn-capacity``, ``stand-down`` and ``app-call`` each arrive as
the first frame of their own connection, get one reply, and end it. Trust basis
for all of them is the owner-only endpoint that authenticates Register.
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Any, Optional

from kiro_crew.code_fingerprint import code_fingerprint
from kiro_crew.mcp_gateway.admission import Admission
from kiro_crew.mcp_gateway.daemon import logger
from kiro_crew.mcp_gateway.daemon.audit import _audit_caller_claimed, _audit_stand_down
from kiro_crew.mcp_gateway.daemon.identity import (
    _CONN_INDEX,
    _bind_token,
    _caller_from_register,
    _pid_live_tenants,
)
from kiro_crew.mcp_gateway.daemon.launch import resolvable_target_stems
from kiro_crew.mcp_gateway.daemon.wire import _write_json_line
from kiro_crew.mcp_gateway.pool import BackendPool
from kiro_crew.mcp_gateway.prewarm import HotKeyStore

if TYPE_CHECKING:
    from kiro_crew.mcp_gateway import gatewayd as facade
else:
    from kiro_crew.mcp_gateway.daemon import facade


def _pong_payload() -> dict[str, Any]:
    """What a ping is answered with.

    ``targets`` lets the pinger detect a daemon whose baked target map does not
    cover its stubs. ``fingerprint`` lets it detect a daemon running DIFFERENT
    CODE -- the case the target check cannot see, since two checkouts resolve
    the same stems while disagreeing about a wire shape. ``owner_pid`` lets it
    tell whether anyone is still supervising this daemon; ``start_time`` is the
    identity a pinned kill must match. Every field is
    additive: an older manager reads ``type`` and ``targets`` and ignores the
    rest, and an older daemon omits the new ones, which the manager treats as
    unverifiable rather than as a match.
    """
    return {
        "type": "pong",
        "targets": resolvable_target_stems(),
        "fingerprint": code_fingerprint(),
        "owner_pid": facade._OWNER_PID,
        "pid": os.getpid(),
        # The daemon's own start-time identity, so a caller that decides to
        # signal this pid pins the signal on the process that ANSWERED, not on
        # whatever holds the number by the time the signal is sent.
        "start_time": facade._OWN_START_TIME,
    }


def _apply_stand_down(frame: dict[str, Any], stop_event: Optional[asyncio.Event]) -> dict[str, Any]:
    """Yield the socket voluntarily so a daemon with a current target map can bind.

    ``manager._report_adoption_drift`` can already SEE that an adopted survivor's
    baked target map does not cover the configured stub set; its own warning
    ends "Replace the daemon to restore them", and this frame is how that
    replacement happens without anyone unlinking a live socket.

    Setting ``stop_event`` takes exactly the graceful path SIGTERM takes (the
    signal handlers installed by ``_amain`` do only ``stop_event.set()``):
    accepts stop, attached stubs drain, ``pool.shutdown_all()`` runs, the
    endpoint is removed, the lock is released, the process exits. Doing it this
    way round is the whole point. The alternative -- the starting gateway
    unlinking the socket to take it -- is a connect-probe-then-unlink, which in
    its documented false-stale window steals a LIVE incumbent's endpoint and
    re-introduces the socket-theft class the flock guard exists to prevent. Here
    the incumbent decides, and the request only ever arrives over a connection
    that proves the incumbent is alive, so there is no stale-vs-live judgement to
    get wrong.

    ``need`` is the list of target stems the caller requires. A daemon that
    already resolves ALL of them is REFUSED: there is nothing to gain by cycling
    it, and honouring the request would turn this into a bare kill switch a
    confused caller could aim at a daemon serving it correctly. A SUPERSET is
    therefore fit -- extra stems a newer config does not ask for are harmless,
    and refusing on inequality would cycle a perfectly good daemon.

    Trust basis for the rest is the same uid-gated owner-only socket that
    authenticates Register/Claim/Abort.
    """
    # Two grounds, either sufficient. A caller running DIFFERENT CODE names
    # its own fingerprint; a daemon whose fingerprint differs yields, because
    # it cannot know which wire shapes the caller's code changed and serving
    # it anyway is how a two-day-old daemon answered a gateway that did not
    # read its control frames. A matching fingerprint is NOT a ground: the
    # caller is running this very code, so there is nothing to gain.
    caller_fp = frame.get("caller_fingerprint")
    stale_code = isinstance(caller_fp, str) and bool(caller_fp) and caller_fp != code_fingerprint()
    # Third ground: this daemon's own gateway has exited. The caller may CLAIM
    # it (``orphaned``), but the daemon decides from its own record -- a live
    # owner means the claim is false and nothing here yields. An orphan is
    # about to stop itself anyway (the owner sweeper); yielding now lets the
    # replacement bind before any session is handed a dying broker.
    orphaned = (
        frame.get("orphaned") is True
        and facade._OWNER_PID > 0
        and not facade._pid_exists(facade._OWNER_PID)
    )
    yield_regardless = stale_code or orphaned
    need = frame.get("need")
    if need is None and yield_regardless:
        need = []
    if not isinstance(need, list) or not all(isinstance(s, str) and s for s in need):
        _audit_stand_down("missing or invalid need list", "denied")
        return {"type": "stand-down-rejected", "reason": "missing or invalid 'need' stem list"}
    if not need and not yield_regardless:
        _audit_stand_down("missing or invalid need list", "denied")
        return {"type": "stand-down-rejected", "reason": "missing or invalid 'need' stem list"}
    served = set(resolvable_target_stems())
    missing = sorted(set(need) - served)
    if not missing and not yield_regardless:
        _audit_stand_down("already covers every needed stem", "denied")
        return {
            "type": "stand-down-rejected",
            "reason": "this daemon already resolves every requested target stem",
        }
    if stop_event is None:
        # Reached only by a handler wired without a stop event (unit tests
        # constructing _handle_connection directly). Refuse rather than claim a
        # shutdown that cannot happen -- an accepted-but-inert control frame is
        # worse than a rejected one, because the caller then waits for a lock
        # that is never released.
        _audit_stand_down("handler has no stop event", "denied")
        return {"type": "stand-down-rejected", "reason": "shutdown not wired on this handler"}
    grounds: list[str] = []
    if missing:
        grounds.append(f"cannot resolve {', '.join(missing)}")
    if stale_code:
        grounds.append(f"runs code {code_fingerprint()} while the caller runs {caller_fp}")
    if orphaned:
        grounds.append(f"is owned by gateway pid {facade._OWNER_PID}, which has exited")
    logger.warning(
        "gatewayd: standing down on request — this daemon %s; draining so a daemon "
        "matching the caller can bind",
        " and ".join(grounds),
    )
    _audit_stand_down("; ".join(grounds), "allowed")
    stop_event.set()
    return {
        "type": "standing-down",
        "missing": missing,
        "stale_code": stale_code,
        "orphaned": orphaned,
    }


async def _apply_claim(
    frame: dict[str, Any], pool: Optional["BackendPool"] = None
) -> dict[str, Any]:
    """Apply a ``claim`` frame to every indexed connection of the target PID.

    Returns the ack frame. Validation is deny-by-default: a non-integer or
    out-of-range pid, or an empty/malformed caller, updates nothing and is
    audited as denied. A valid claim REPLACES existing identities (gateway-
    trusted; this is what keeps callers correct across warm-pool re-claims) —
    except on a connection whose register-time start token for the PID
    definitively differs from the frame's ``pid_start_id`` (the PID was
    recycled to a different process); those are skipped and audited as
    denied rather than silently misattributed.

    ``stub_session_token`` narrows the claim from the RUNTIME to one of the ACP
    sessions it hosts: a connection is retargeted only when it carries that same
    token, or no token at all. A tokenless connection has no finer identity than
    its process tree, so it stays on the PID-wide behavior; only a connection
    that positively names a DIFFERENT session is excluded. A claim carrying no
    token retargets every connection under the PID, byte-for-byte as before —
    which is what a runtime whose sessions predate the token still needs.

    The binding is recorded even when the claim matches nothing: a session's
    claim is pushed before its stubs are launched, so "matched zero" is the
    normal ordering, and remembering the token is how the register that follows
    resolves to the right session instead of to the runtime's tree.
    """
    raw_pid = frame.get("pid")
    pid = raw_pid if isinstance(raw_pid, int) and not isinstance(raw_pid, bool) else 0
    updated_caller = _caller_from_register(frame)
    if pid <= 1 or updated_caller is None or not updated_caller.session_key:
        reason = f"malformed claim: pid={raw_pid!r} session_key={'' if updated_caller is None else updated_caller.session_key!r}"
        logger.warning("claim rejected: %s", reason)
        _audit_caller_claimed("", "", "pid-index", "denied", reason)
        return {"type": "claim-rejected", "reason": reason}
    raw_session_token = frame.get("stub_session_token")
    session_token = raw_session_token if isinstance(raw_session_token, str) else ""
    raw_token = frame.get("pid_start_id")
    claim_token = raw_token if isinstance(raw_token, str) else None
    _bind_token(session_token, updated_caller, pid, claim_token)
    # Does this runtime host more than one ACP session RIGHT NOW? The claiming
    # session is unioned in because its own stubs may not have registered yet --
    # a claim is pushed before they launch -- so the live index alone would miss
    # it. Two or more means the process tree names a RUNTIME and not a session,
    # which is what the two branches below consult it for.
    live_tenants = _pid_live_tenants(pid, session_token) | {updated_caller.session_key}
    shared_pid = len(live_tenants) >= 2
    conns = _CONN_INDEX.get(pid, set())
    if not conns:
        # Usually the normal ordering: a session's claim is pushed before its
        # stubs launch, and the binding recorded above is what the register that
        # follows resolves through. It was a WARNING saying "stub identity will
        # stay stale", which fired on every healthy session and sent operators
        # chasing it as the cause of unrelated identity_unattested refusals. It
        # can still mean a pid-index mismatch (host-pid claim vs namespace-pid
        # index), so it stays in the log and the audit trail at INFO.
        logger.info(
            "claim matched no connections yet: pid=%d session_key=%s; the session "
            "token binding is kept for the stubs that register next (a pid-index "
            "mismatch shows up the same way if they never do)",
            pid,
            updated_caller.session_key,
        )
        _audit_caller_claimed(
            "",
            updated_caller.session_key,
            "pid-index",
            "noop",
            f"claim pid={pid} matched no indexed connection",
        )
        return {"type": "claim-noop", "updated": 0, "connections": 0}
    updated = 0
    skipped = 0
    # PID-recycle guard: the frame's token identifies the process the gateway
    # actually claimed; the recorded token identifies the process that owned
    # the PID at register time. Skip a connection only on a DEFINITE mismatch
    # (both tokens known and unequal) — ``None`` on either side means
    # "identity unknown" (Windows, unreadable /proc, legacy claim frames) and
    # MUST count as a match, otherwise every claim on those platforms would
    # be rejected.
    # Pass 1: retarget every eligible connection SYNCHRONOUSLY (no awaits)
    # before any eviction runs — see the wrong-principal note below.
    retargeted: list[tuple[Any, str]] = []
    # Connections whose identity this claim CLEARED. They are not retargeted, so
    # they take no ``updated`` count and no allowed audit, but they owe the same
    # subscription eviction a retarget owes.
    cleared: list[Any] = []
    # Snapshot: the eviction below AWAITS, and a connection disconnecting
    # during that await mutates the live ``conns`` set mid-iteration —
    # aborting the claim with no ack and leaving the remaining stubs stale.
    for conn in list(conns):
        if session_token and conn.stub_session_token and conn.stub_session_token != session_token:
            # This connection belongs to a different session on the same
            # runtime — the ``spawn_run`` subagent case. Not a skip worth
            # auditing as denied: nothing was attempted against it.
            continue
        if session_token and not conn.stub_session_token and shared_pid:
            # A tokened claim meeting a TOKENLESS connection on a runtime serving
            # several sessions. The connection names no session, so the only thing
            # linking it to this claim is the PID — and the PID is shared, so that
            # link cannot tell whether the connection is this claim's session or a
            # co-tenant's. Retargeting it would hand one session's tools to
            # another: its work_brief / work_report would read and write the
            # claiming session's ledger.
            #
            # Leaving the identity in place is not the answer either: whatever it
            # holds is an EARLIER guess by this same PID, so a stub belonging to
            # the second tenant would go on answering as the first. That stops the
            # misattribution moving without stopping it. So the identity is
            # CLEARED, which is the same fail-closed state the register path gives
            # a token it cannot resolve: the stub's calls carry no caller, strict
            # tools refuse with a diagnosis, and a claim carrying ITS token repairs
            # it through the branch above.
            skipped += 1
            old_unattributable = conn.caller.session_key if conn.caller is not None else ""
            conn.caller = None
            # A verdict, not an absence: it is what stops the stub-initiated
            # recaller from filling the key-less state this clearing creates.
            conn.identity_refused = True
            if old_unattributable:
                # The identity is gone but its SUBSCRIPTIONS are not. They were
                # granted while this connection answered as the old session, and
                # resource-update URIs can carry tokens or presigned params, so
                # leaving them would keep delivering that session's updates to a
                # connection the gateway just declared unattributable. Evicted in
                # pass 2 with the retargeted ones, because eviction awaits and
                # every caller reassignment must land before the first await.
                cleared.append(conn)
            reason = (
                f"pid {pid} serves {len(live_tenants)} live sessions "
                f"and stub {conn.stub_uuid} carries no session token — refusing to "
                f"attribute it on PID alone (claim named "
                f"{updated_caller.session_key}); its identity is cleared until a "
                f"claim carrying its own token names it"
            )
            logger.warning("claim cleared a stub it cannot attribute: %s", reason)
            _audit_caller_claimed(
                old_unattributable,
                updated_caller.session_key,
                conn.pool_label,
                "denied",
                reason,
            )
            continue
        recorded_token = conn.pid_start_ids.get(pid)
        if claim_token is not None and recorded_token is not None and claim_token != recorded_token:
            skipped += 1
            reason = (
                f"pid {pid} recycled: claim start-token {claim_token} != "
                f"register-time token {recorded_token} — refusing to retarget "
                f"stub {conn.stub_uuid}"
            )
            logger.warning("claim skipped stale connection: %s", reason)
            _audit_caller_claimed(
                conn.caller.session_key if conn.caller is not None else "",
                updated_caller.session_key,
                conn.pool_label,
                "denied",
                reason,
            )
            continue
        old_key = conn.caller.session_key if conn.caller is not None else ""
        if old_key == updated_caller.session_key:
            continue  # already correct — idempotent re-claim
        # Reassign the owner BEFORE any eviction awaits — and reassign
        # EVERY eligible connection before the FIRST eviction awaits (the
        # second pass below): an eviction yields, and a sibling connection
        # still carrying the old caller during that await would forward
        # its frames as the previous session — wrong-principal execution.
        # A subscribe arriving during an await must likewise already be
        # authorized as the NEW caller on every connection.
        conn.caller = updated_caller
        retargeted.append((conn, old_key))
    # Pass 2: all connections now carry the new owner; run the evictions.
    #
    # The cleared ones first, and for the same reason the retargeted ones are
    # evicted at all: a subscription granted under an identity must not outlive
    # it. Here the identity went away rather than moving to a new owner, so
    # there is no new session to misdeliver to -- but the OLD session's update
    # URIs would still reach a connection that answers as nobody.
    for conn in cleared:
        if pool is None:
            break
        for backend in pool.backends_hosting_stub(conn.stub_uuid):
            try:
                await backend.evict_stub_subscriptions(conn.stub_uuid)
            except Exception:
                logger.exception(
                    "claim: subscription eviction failed for cleared stub %s",
                    conn.stub_uuid,
                )
    for conn, old_key in retargeted:
        if pool is not None:
            # The stub changed OWNER: its resource subscriptions belong to
            # the old principal, and without eviction the new session would
            # keep receiving the old session's resource-update URIs (which
            # can carry tokens or presigned params). Caller-binding
            # ownership lives here at the connection layer, so this is the
            # one moment the clearance fires; the backend releases upstream
            # as the grant-time caller.
            for backend in pool.backends_hosting_stub(conn.stub_uuid):
                try:
                    await backend.evict_stub_subscriptions(conn.stub_uuid)
                except Exception:
                    logger.exception(
                        "claim: subscription eviction failed for stub %s",
                        conn.stub_uuid,
                    )
        updated += 1
        _audit_caller_claimed(old_key, updated_caller.session_key, conn.pool_label, "allowed")
        # Every connection reaching pass 2 carries an identity DIFFERENT from the
        # claim's (an equal one is skipped above as an idempotent re-claim). On a
        # single-session runtime that is the warm-pool rekey this path exists for,
        # and it is routine. On a runtime hosting several sessions it means one
        # session's live identity was replaced by another's, which is worth an
        # operator's attention: the displaced session's tools now answer as the
        # claiming session until a claim naming its own token repairs it.
        if old_key and shared_pid:
            logger.warning(
                "stub %s claim replaced a live identity on a shared runtime: "
                "session_key=%s type=%s (was %s); pid=%d hosts %d claimed sessions",
                conn.stub_uuid,
                updated_caller.session_key,
                updated_caller.session_type,
                old_key,
                pid,
                len(live_tenants),
            )
        else:
            logger.info(
                "stub %s claim → session_key=%s type=%s (was %s)",
                conn.stub_uuid,
                updated_caller.session_key,
                updated_caller.session_type,
                old_key or "<none>",
            )
    return {"type": "claimed", "updated": updated, "connections": len(conns), "skipped": skipped}


async def _apply_abort(frame: dict[str, Any], pool: "BackendPool") -> dict[str, Any]:
    """Apply an ``abort`` frame: cancel in-flight requests for all stubs under
    the named PIDs.

    This is the gateway-authoritative abort path:
    on session hard-stop, the gateway sends abort for the killed runtime's
    PIDs so gatewayd can propagate MCP cancel notifications to backends.
    Backend recycle happens on the subsequent stub disconnect path, not here.
    """
    raw_pids = frame.get("pids")
    if not isinstance(raw_pids, list):
        facade._audit_abort_applied([], "missing or invalid pids", "denied")
        return {"type": "abort-rejected", "reason": "missing or invalid pids"}
    pids = [p for p in raw_pids if isinstance(p, int) and not isinstance(p, bool) and p > 1]
    if not pids:
        facade._audit_abort_applied([], "no valid pids", "denied")
        return {"type": "abort-rejected", "reason": "no valid pids"}
    reason = str(frame.get("reason", "session hard-stop"))

    total_cancelled = 0
    affected_stubs = set()
    for pid in pids:
        conns = _CONN_INDEX.get(pid, set())
        for conn in list(conns):
            affected_stubs.add(conn.stub_uuid)
    # Find backends attached to the affected stubs and cancel their in-flight work
    for backend in pool.all_backends():
        for stub_uuid in affected_stubs:
            cancelled = await backend.cancel_in_flight_for_stub(stub_uuid)
            total_cancelled += len(cancelled)

    logger.info(
        "abort applied: pids=%r reason=%s cancelled=%d stubs=%d",
        pids,
        reason,
        total_cancelled,
        len(affected_stubs),
    )
    facade._audit_abort_applied(pids, reason, "allowed", total_cancelled, len(affected_stubs))
    return {"type": "aborted", "cancelled": total_cancelled, "stubs": len(affected_stubs)}


def _apply_set_spawn_capacity(
    frame: dict[str, Any], admission: Optional[Admission]
) -> dict[str, Any]:
    """Move the spawn gate's live capacity for the adaptive controller.

    Replies ``{"type": "spawn-capacity", "capacity": <clamped>, ...}`` with the
    gate's snapshot, or ``spawn-capacity-rejected`` when the frame carries no
    usable integer or this daemon has no admission state. The gate clamps to
    ``[floor, ceiling]``; the reply reports what actually took effect so the
    controller's applied value never drifts from the daemon's.

    Every unusable value earns the typed refusal, ``inf`` and ``-inf`` included:
    ``int()`` raises ``OverflowError`` on those, and an exception here escapes to
    the connection handler, which drops the control connection with no reply of
    either kind -- so the controller learns nothing rather than being told no.
    ``1e400`` is a value ``json.loads`` produces from a well-formed frame, not a
    hostile one.
    """
    if admission is None:
        return {"type": "spawn-capacity-rejected", "reason": "no admission on this daemon"}
    raw = frame.get("capacity")
    if (
        isinstance(raw, bool)
        or not isinstance(raw, (int, float))
        or raw != raw
        or abs(raw) == float("inf")
    ):
        return {"type": "spawn-capacity-rejected", "reason": "missing or invalid capacity"}
    applied = admission.gate.set_capacity(int(raw))
    return {"type": "spawn-capacity", "capacity": applied, **admission.gate.snapshot()}


async def _serve_control_frame(
    register: dict[str, Any],
    writer: asyncio.StreamWriter,
    pool: BackendPool,
    hot_keys: Optional[HotKeyStore] = None,
    *,
    stop_event: Optional[asyncio.Event] = None,
    admission: Optional[Admission] = None,
) -> bool:
    """Answer a one-shot control frame; ``True`` when ``register`` was one.

    The connection that carried it ends once this returns ``True``: every control
    frame is the first and only frame of its own connection.
    """
    # Health-probe short-circuit: any caller can check gatewayd is alive
    # with one round-trip without advertising a PoolKey. GatewayManager
    # uses this to confirm the daemon is serving before returning from
    # ``start()``.
    if register.get("type") == "ping":
        # ``targets`` lets the pinger detect a STALE incumbent before adopting
        # it. Absent on a pre-#6xxx daemon, which the adoption gate treats as
        # unverifiable rather than assuming coverage.
        await _write_json_line(writer, _pong_payload())
        return True

    # Metrics short-circuit: return a point-in-time pool snapshot (backends,
    # sessions, RSS) for the dashboard metrics panel. Read-only, no PoolKey.
    # When prewarming is enabled, fold in the cumulative warm-pool hit tally
    # so the dashboard can show a hit rate; absent (hot_keys is None) the keys
    # simply don't appear and the card omits the metric.
    if register.get("type") == "stats":
        snapshot = await pool.metrics_snapshot_async()
        if hot_keys is not None:
            snapshot.update(hot_keys.hit_stats())
        # Plain blocking file I/O (≤ ~2 MiB of JSONL under the rotation cap) —
        # off the event loop, or every concurrent gateway task stalls behind a
        # stats poll.
        snapshot["stub_fallbacks"] = await asyncio.to_thread(facade.stub_fallback_counts)
        if admission is not None:
            snapshot["admission"] = admission.snapshot()
        await _write_json_line(writer, {"type": "stats", **snapshot})
        return True

    # Claim-push short-circuit (one-shot control connection from the main
    # gateway process): "session S now owns runtime PID P" — re-target the
    # caller identity of every live stub connection under that PID. This is
    # the event-driven replacement for the stub-side recaller poll, whose
    # bounded budget stranded pool runtimes claimed later than the budget.
    # Trust basis: the unix socket is uid-gated 0700 — the same gate that
    # authenticates Register — so a claim may REPLACE a stale identity
    # (fixes warm-pool re-claim staleness). Validation + auditing live in
    # ``_apply_claim``.
    if register.get("type") == "claim":
        await _write_json_line(writer, await _apply_claim(register, pool))
        return True

    # Abort-push short-circuit (one-shot control connection from the main
    # gateway process): "cancel all in-flight tool calls for runtime PIDs X"
    # — sends MCP notifications/cancelled to each backend. Backend recycle
    # happens on the subsequent stub disconnect path, not here. Trust basis:
    # same uid-gated 0700 socket as Register/Claim.
    if register.get("type") == "abort":
        await _write_json_line(writer, await _apply_abort(register, pool))
        return True

    # Spawn-capacity short-circuit (one-shot control connection from the main
    # gateway's adaptive controller): move the daemon-wide spawn gate's live
    # capacity. The gate clamps to its own [floor, ceiling] and never revokes
    # an in-flight spawn, so the worst a value it can USE does is admit fewer;
    # one it cannot use is answered ``spawn-capacity-rejected``, never raised —
    # an exception escaping here drops this connection with no reply at all.
    # Trust basis: same uid-gated 0700 socket as Register/Claim/Abort.
    if register.get("type") == "set-spawn-capacity":
        await _write_json_line(writer, _apply_set_spawn_capacity(register, admission))
        return True

    # Stand-down short-circuit (one-shot control connection from a STARTING
    # gateway): "your baked target map cannot resolve what my config needs --
    # yield the socket". The only frame that ends the daemon, and the mechanism
    # that turns _report_adoption_drift's warning into an actual repair. Trust
    # basis: same uid-gated owner-only socket as Register/Claim/Abort.
    # Validation, the already-covers refusal and auditing live in
    # ``_apply_stand_down``.
    if register.get("type") == "stand-down":
        await _write_json_line(writer, _apply_stand_down(register, stop_event))
        return True

    # App-call short-circuit (one-shot control connection from the dashboard):
    # an embedded MCP App iframe invoking one of its server's app-visible
    # tools. The frame carries only an opaque spool id — the gateway re-reads
    # its own spool record for routing, enforces _meta.ui.visibility, and
    # forwards through the normal stub seam. Trust basis: same uid-gated 0700
    # socket as Register/Claim/Abort. Validation + auditing live in
    # ``app_call.handle_app_call``.
    if register.get("type") == "app-call":
        # circular import: app_call/backend pull gatewayd-adjacent modules, so
        # these stay function-scoped to avoid an import cycle at module load.
        from kiro_crew.mcp_gateway.app_call import _audit, handle_app_call
        from kiro_crew.mcp_gateway.backend import _mcp_apps_enabled

        if not _mcp_apps_enabled():
            # Feature OFF ⇒ byte-identical legacy behavior on EVERY layer: never
            # execute an app-originated tool call, even if a spool capability is
            # still live within its 24h TTL from a window when the flag was on.
            # Audit the denial like every other app-call outcome (same SEL shape
            # as app_call.handle_app_call's allow/deny events).
            _audit(
                "denied",
                "mcp-apps feature disabled",
                spool_id=str(register.get("spool_id") or ""),
                tool=str(register.get("tool") or ""),
            )
            await _write_json_line(
                writer, {"type": "app-call-rejected", "reason": "mcp-apps feature disabled"}
            )
            return True
        await _write_json_line(writer, await handle_app_call(pool, register))
        return True
    return False
