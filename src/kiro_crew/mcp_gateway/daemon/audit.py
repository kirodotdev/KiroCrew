"""The daemon's access-decision trail in the HMAC-chained security event log.

One row per decision the daemon makes about who may connect, act as a session,
cancel work, fall back, spawn from persisted data, replace a backend or end the
daemon. Every row is written through ``gatewayd.SecurityEventLog`` and none may
raise: an audit failure never breaks the connection, recovery or shutdown path
that asked for it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kiro_crew.mcp_gateway.daemon import logger
from kiro_crew.mcp_gateway.daemon.launch import resolvable_target_stems

if TYPE_CHECKING:
    from kiro_crew.mcp_gateway import gatewayd as facade
else:
    from kiro_crew.mcp_gateway.daemon import facade


def _audit_peer_denied(reason: str) -> None:
    """Emit a SEL audit event for a denied gateway connection.

    The peer-uid / socket-perms rejection is a security-sensitive access
    decision, so it is recorded in the HMAC-chained security event log
    (:mod:`kiro_crew.sel`) in addition to the WARNING log line. Wrapped
    defensively -- an audit-log failure must never break connection handling.
    The companion :func:`_audit_peer_allowed` records accepted connections,
    so the SEL captures both outcomes of the peer access decision.
    """
    try:
        facade.SecurityEventLog().log_api_access(
            caller="unverified-peer",
            operation="mcp-gateway.connect",
            outcome="denied",
            source="gateway",
            error=reason,
        )
    except Exception:  # pragma: no cover — audit must never break the handler
        logger.debug("SEL audit emit for gateway denial failed", exc_info=True)


def _audit_peer_allowed(caller: str, pool_label: str) -> None:
    """Emit a SEL audit event for an accepted gateway connection.

    Accepting a stub connection is a permission decision just like rejecting
    one, so for a complete access-decision trail it is recorded in the
    HMAC-chained security event log (:mod:`kiro_crew.sel`) alongside the
    denial path. Unlike a denial -- which fires before identity is known and
    is logged as ``unverified-peer`` -- an accept runs after the Register
    handshake, so it carries the real caller identity. It fires once per stub
    connection (at registration), not per request, so the volume sits far
    below the per-tool-call events SEL already records. Wrapped defensively --
    an audit-log failure must never break connection handling.
    """
    try:
        facade.SecurityEventLog().log_api_access(
            caller=caller or "unknown",
            operation="mcp-gateway.connect",
            outcome="allowed",
            source="gateway",
            resources=pool_label,
        )
    except Exception:  # pragma: no cover — audit must never break the handler
        logger.debug("SEL audit emit for gateway accept failed", exc_info=True)


def _audit_caller_rekey(caller: str, pool_label: str) -> None:
    """Emit a SEL audit event when a stub's caller identity is updated
    mid-connection via a ``recaller`` frame (warm-pool caller repair).

    Re-binding the connection's caller from key-less to a real session
    identity is a security-relevant authorization change: it moves the
    connection from effectively unauthorized (no ``_meta.kirocrew.caller`` on
    forwarded tool calls, so pooled state-mutating tools are refused) to acting
    as a specific session. Recording it in the HMAC-chained SEL gives an
    auditable trail of identity transitions alongside the
    :func:`_audit_peer_allowed` event from the original register — so a stub
    that sends a spoofed recaller claiming another session leaves a record.
    Wrapped defensively -- an audit-log failure must never break connection
    handling.
    """
    try:
        facade.SecurityEventLog().log_api_access(
            caller=caller or "unknown",
            operation="mcp-gateway.caller-rekey",
            outcome="allowed",
            source="gateway",
            resources=pool_label,
        )
    except Exception:  # pragma: no cover — audit must never break the handler
        logger.debug("SEL audit emit for gateway caller-rekey failed", exc_info=True)


def _audit_recaller_rejected(existing_caller: str, pool_label: str, reason: str) -> None:
    """Emit a SEL audit event when a ``recaller`` frame is REJECTED — either a
    pivot attempt (the connection already carries a session identity) or a
    malformed/empty ``session_key`` claim.

    Rejecting an identity claim is a security-relevant permission decision —
    potentially a compromised or misbehaving stub — so EVERY rejection is
    recorded in the HMAC-chained SEL alongside the accept path
    (:func:`_audit_caller_rekey`), mirroring the :func:`_audit_peer_allowed` /
    :func:`_audit_peer_denied` pairing. ``reason`` describes the rejection (and
    any attempted target) for the trail. Wrapped defensively -- an audit-log
    failure must never break connection handling.
    """
    try:
        facade.SecurityEventLog().log_api_access(
            caller=existing_caller or "unknown",
            operation="mcp-gateway.caller-rekey",
            outcome="denied",
            source="gateway",
            resources=pool_label,
            error=reason,
        )
    except Exception:  # pragma: no cover — audit must never break the handler
        logger.debug("SEL audit emit for gateway recaller reject failed", exc_info=True)


def _audit_caller_claimed(
    old_caller: str, new_caller: str, pool_label: str, outcome: str, reason: str = ""
) -> None:
    """Emit a SEL audit event for a ``claim`` frame (claim-push identity set).

    A claim frame re-binds — and unlike ``recaller``, may REPLACE — the caller
    identity of every connection owned by the claimed runtime PID. That is an
    authorization change and is recorded per connection in the HMAC-chained
    SEL, mirroring :func:`_audit_caller_rekey`. The trust basis for allowing
    replacement is the socket itself: it is uid-gated 0700, the same trust
    level that authenticates Register frames. Wrapped defensively — an audit
    failure must never break connection handling.
    """
    try:
        facade.SecurityEventLog().log_api_access(
            caller=new_caller or "unknown",
            operation="mcp-gateway.caller-claim",
            outcome=outcome,
            source="gateway",
            resources=pool_label,
            error=reason or (f"replaced caller={old_caller}" if old_caller else ""),
        )
    except Exception:  # pragma: no cover — audit must never break the handler
        logger.debug("SEL audit emit for gateway caller-claim failed", exc_info=True)


def _audit_peer_identity_resolved(caller: str, peer_pid: int, stub_uuid: str) -> None:
    """SEL audit: gatewayd granted a key-less stub an identity via the
    SO_PEERCRED + /proc-ancestry mechanism. Granting identity server-side is
    a permission decision — leave a trail. Wrapped defensively; audit failure
    must never break the handshake."""
    try:
        facade.SecurityEventLog().log_api_access(
            caller=caller,
            operation="mcp-gateway.peer-identity-resolved",
            outcome="allowed",
            source="gateway",
            resources=f"peer_pid={peer_pid} stub_uuid={stub_uuid}",
        )
    except Exception:  # pragma: no cover — audit must never break the handler
        logger.debug("SEL audit emit for peer identity resolution failed", exc_info=True)


def _audit_peer_identity_denied(reason: str, peer_pid: int | None, stub_uuid: str) -> None:
    """SEL audit: a key-less peer whose credentials could not be positively
    attested was refused server-side identity resolution (potential
    unauthorized identity acquisition). Deny arm of
    :func:`_audit_peer_identity_resolved`."""
    try:
        facade.SecurityEventLog().log_api_access(
            caller="unknown",
            operation="mcp-gateway.peer-identity-denied",
            outcome="denied",
            source="gateway",
            resources=f"peer_pid={peer_pid} stub_uuid={stub_uuid} reason={reason}",
        )
    except Exception:  # pragma: no cover — audit must never break the handler
        logger.debug("SEL audit emit for peer identity denial failed", exc_info=True)


def _audit_reserved_stub_prefix_denied(stub_uuid: str) -> None:
    """SEL audit: a registrant naming a reserved internal stub prefix was refused.

    The prefix decides whether `Backend` treats a request as the gateway's own
    and skips the model-visibility filter, so claiming one is an attempt to
    acquire an exemption rather than a malformed frame -- the same class of
    access decision as :func:`_audit_peer_identity_denied`, and recorded the
    same way. The sibling rejects on this path (a malformed Register, an empty
    stub_uuid) stay WARNING-only because they are schema failures with no
    control being evaded.

    ``stub_uuid`` is peer-supplied, but the SEL serializes each event with
    ``json.dumps``, which escapes the control characters a forged log line would
    need, so it is recorded as received rather than pre-sanitized.
    """
    try:
        facade.SecurityEventLog().log_api_access(
            caller="unverified-peer",
            operation="mcp-gateway.reserved-stub-prefix-denied",
            outcome="denied",
            source="gateway",
            resources=f"stub_uuid={stub_uuid}",
        )
    except Exception:  # pragma: no cover — audit must never break the handler
        logger.debug("SEL audit emit for reserved stub prefix denial failed", exc_info=True)


def _audit_abort_applied(
    pids: list[int], reason: str, outcome: str, cancelled: int = 0, stubs: int = 0
) -> None:
    """Emit a SEL audit event for an ``abort`` frame (gateway-authoritative
    cancel of in-flight tool calls, with possible backend recycle).

    Cancelling another runtime's in-flight tool work is a security-relevant
    action: it terminates executing tools and may SIGKILL a pooled backend.
    Recorded in the HMAC-chained SEL mirroring :func:`_audit_caller_claimed`.
    Trust basis: the uid-gated 0700 socket, same as Register/Claim. Wrapped
    defensively — an audit failure must never break the abort path.
    """
    try:
        facade.SecurityEventLog().log_api_access(
            caller="gateway",
            operation="mcp-gateway.abort-in-flight",
            outcome=outcome,
            source="gateway",
            resources=f"pids={pids} stubs={stubs}",
            error=f"reason={reason} cancelled={cancelled}" if outcome == "allowed" else reason,
        )
    except Exception:  # pragma: no cover — audit must never break the handler
        logger.debug("SEL audit emit for gateway abort failed", exc_info=True)


def _audit_pool_fallback(caller: str, pool_label: str, reason: str) -> None:
    """Emit a SEL audit event when the gateway directs a stub to fall back to a
    direct, unpooled per-session exec.

    Telling a stub to run its backend outside the pool is an operational
    degradation worth a security-audit trail: a sustained storm of them (a target
    map that drifted, or a server the jail cannot launch for a reason an exec
    would not share) is then visible in the HMAC-chained SEL, not just in the
    stub's best-effort jsonl + the pool ``capacity_rejects`` counter. A saturated
    pool is NOT among them: capacity carries no fallback, so pressure shows up as
    a rejection rather than a fork. Wrapped defensively -- an audit-log failure
    must never break connection handling.
    """
    try:
        facade.SecurityEventLog().log_api_access(
            caller=caller or "unknown",
            operation="mcp-gateway.fallback",
            outcome="fallback",
            source="gateway",
            resources=pool_label,
            error=reason,
        )
    except Exception:  # pragma: no cover — audit must never break the handler
        logger.debug("SEL audit emit for gateway fallback failed", exc_info=True)


def _audit_pool_rejected(caller: str, pool_label: str, reason: str) -> None:
    """Emit a SEL audit event for a TERMINAL backend-acquire denial.

    Refusing a stub a backend with no fallback is a permission decision just like
    the fallback path, so for a complete access-decision trail it is recorded in
    the HMAC-chained SEL alongside :func:`_audit_pool_fallback`. Its population is
    every EXEC-less refusal: an unexpected gateway-internal error, breaker-open on
    the legacy lazy path, and every ``capacity``-classed refusal at the pre-flight
    (a full pool, an exhausted host budget, a spent or closed spawn gate, a
    pressure errno) -- which is the larger half, because pressure is what a loaded
    daemon refuses most. Wrapped defensively -- an audit-log failure must never
    break connection handling.
    """
    try:
        facade.SecurityEventLog().log_api_access(
            caller=caller or "unknown",
            operation="mcp-gateway.ensure_backend",
            outcome="denied",
            source="gateway",
            resources=pool_label,
            error=reason,
        )
    except Exception:  # pragma: no cover — audit must never break the handler
        logger.debug("SEL audit emit for gateway reject failed", exc_info=True)


def _audit_replacement_validated(caller: str, pool_label: str, outcome: str, detail: str) -> None:
    """Emit a SEL audit event for a backend replacement, adopted OR refused.

    A transparent respawn is the one place the gateway swaps the process behind
    a live session, so both outcomes are access decisions about which server may
    answer that session — the same class as :func:`_audit_pool_rejected`, and the
    same two-sided shape as the peer-identity and app-call trails, which record
    allow as well as deny. Recording only the refusal would leave the event this
    whole guard exists to make visible — a process gaining authority to serve a
    session that did not ask for it — as a rotating log line and nothing more.

    ``detail`` carries what the decision was made on: WHICH tools moved on a
    refusal, and whether the tool set was verified or there was nothing to verify
    on an adoption. That is what separates a server upgrade from a server that
    answers differently per caller. Wrapped defensively — an audit-log failure
    must never break the recovery path.
    """
    try:
        facade.SecurityEventLog().log_api_access(
            caller=caller or "unknown",
            operation="mcp-gateway.respawn-toolset",
            outcome=outcome,
            source="gateway",
            resources=pool_label,
            error=detail,
        )
    except Exception:  # pragma: no cover — audit must never break the handler
        logger.debug("SEL audit emit for backend replacement failed", exc_info=True)


def _audit_prewarm_spawn(pool_label: str) -> None:
    """Emit a SEL audit event for a backend spawned by the warm-pool prewarmer.

    Prewarming spawns a backend subprocess from a PERSISTED hot key, before any
    stub connects, so it bypasses the Register handshake that drives
    :func:`_audit_peer_allowed` on the live path. Spawning from persisted data
    is a distinct security-relevant event (new pid, new time, no live peer to
    attribute), so it gets its own access-decision record in the HMAC-chained
    SEL. ``caller`` is the synthetic ``prewarm`` principal — there is no live
    peer — and the volume is bounded by the prewarm count, far below per-call
    events. Wrapped defensively: an audit-log failure must never abort a warm.
    """
    try:
        facade.SecurityEventLog().log_api_access(
            caller="prewarm",
            operation="mcp-gateway.prewarm-spawn",
            outcome="allowed",
            source="gateway",
            resources=pool_label,
        )
    except Exception:  # pragma: no cover — audit must never break prewarm
        logger.debug("SEL audit emit for prewarm spawn failed", exc_info=True)


def _audit_stand_down(reason: str, outcome: str) -> None:
    """Emit a SEL audit event for a stand-down request.

    A stand-down ends the daemon, so it is the most consequential frame the
    control surface accepts and belongs in the HMAC-chained SEL alongside the
    claim/abort/peer decisions. Wrapped defensively -- an audit-log failure must
    never break connection handling.
    """
    try:
        facade.SecurityEventLog().log_api_access(
            caller="gateway-manager",
            operation="mcp-gateway.stand_down",
            outcome=outcome,
            source="gateway",
            resources=",".join(resolvable_target_stems()) or "(none)",
            error=reason,
        )
    except Exception:  # pragma: no cover — audit must never break the handler
        logger.debug("SEL audit emit for stand-down failed", exc_info=True)


def _audit_disconnect_cancel(stub_uuid: str, remaining: int, cancelled: int) -> None:
    """Emit a SEL audit event for in-flight calls cancelled on a stub disconnect.

    Cancelling in-flight tool work on a plain stub disconnect is the same
    security-relevant action as the abort frame path (which audits via
    :func:`_audit_abort_applied`), so a disconnect-triggered cancellation has an
    audit trail too. Wrapped defensively -- an audit failure must never break
    the detach.
    """
    try:
        facade.SecurityEventLog().log_api_access(
            caller="gatewayd",
            operation="mcp-gateway.disconnect-cancel",
            outcome="cancelled",
            source="gateway",
            resources=f"stub={stub_uuid} refcount={remaining}",
            error=f"cancelled={cancelled} in-flight on stub disconnect",
        )
    except Exception:  # pragma: no cover — audit must never break detach
        logger.debug("SEL audit for disconnect-cancel failed", exc_info=True)
