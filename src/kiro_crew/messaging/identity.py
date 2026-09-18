"""Per-turn session-identity publication — the single shared writer.

Every surface that runs an agent turn (the dashboard, native Slack, and each
channel ``transport_dispatch``) must publish the ``session_pid_<pid>.txt``
mapping so the gateway's ancestor PID-walk can resolve the caller's
``X-Session-Key`` for session-keyed managed MCP tools (``learn_add``, cron
management, and every other such handler). When a surface omits it the header
is empty and those tools reject the call with HTTP 400 ``missing
X-Session-Key``.

The obligation lives here — in one function every turn-running surface calls —
rather than as a copy-pasted, per-surface opt-in block: centralizing means a new
channel gets identity publication by calling one function, and any change to the
publish contract happens in exactly one place.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from kiro_crew.executors import governance_executor, maintenance_executor
from kiro_crew.platform.context import PlatformCompositionError
from kiro_crew.platform.governance_profiles import (
    HOST_SESSION_KEY,
    audit_governance_degraded,
    governance_permits,
)
from kiro_crew.sel import sel
from kiro_crew.session_pid_sig import publish_session_pid
from kiro_crew.session_token_sig import publish_session_token

logger = logging.getLogger(__name__)


async def publish_turn_identity(sessions: Any, session_key: str) -> None:
    """Publish this turn's identity mappings: the pid file AND the session token.

    Keyed by the session's kiro-cli host PID (via ``sessions.get_pid``) so the
    gateway PID-walk resolves ``X-Session-Key``. Offloaded to the maintenance
    executor: publishing does a key read plus two ``atomic_write()``
    replacements — blocking filesystem work that must not run on the event
    loop. Fail-safe: a missing pid (session not yet spawned) or any filesystem
    error is swallowed so identity publication can never break a turn.

    The same turn boundary re-pushes this session's gateway claim
    (:meth:`AcpClient.reclaim`), for the same reason the pid file is rewritten
    here rather than once at spawn: the mapping lives outside this process and
    can be lost while the session is alive. gatewayd holds the token ->  session
    binding in memory only, so a daemon respawn leaves every live session's
    stubs carrying a token nothing names — refused, not resolved from the shared
    process tree — and this is what re-binds it, bounding the outage to the turn
    it happened in.
    """
    try:
        pid = sessions.get_pid(session_key)
        if isinstance(pid, int):
            await asyncio.get_running_loop().run_in_executor(
                maintenance_executor(), publish_session_pid, pid, session_key
            )
    except Exception:
        logger.debug("publish_turn_identity failed for %s", session_key, exc_info=True)
    # ONE provider lookup for both steps below. Resolving it twice puts a second
    # call on a per-turn hot path for no gain, and what a session manager does on a
    # lookup is its own business rather than something this function should invoke
    # more often than it needs to.
    provider = None
    try:
        provider = sessions.get_provider(session_key)
    except Exception:
        logger.debug("provider lookup failed for %s", session_key, exc_info=True)
    try:
        await _publish_session_token(provider, session_key)
    except Exception:
        # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure - logs the session key, never the token, which is not in scope here  # noqa: E501
        logger.debug("identity mapping publication failed for %s", session_key, exc_info=True)
    try:
        _reclaim_gateway_stubs(provider)
    except Exception:
        logger.debug("stub re-claim failed for %s", session_key, exc_info=True)


def _provider_inner(provider: Any) -> Any:
    """The object carrying the ACP-provider surface, or ``None``.

    Both steps below reach it the same way and through ``getattr``: only the ACP
    providers expose ``reclaim`` and ``session_identity_token``, and every other
    provider and test double simply has neither and is left alone.
    """
    if provider is None:
        return None
    return getattr(provider, "client", None) or getattr(provider, "_client", None) or provider


async def _publish_session_token(provider: Any, session_key: str) -> None:
    """Publish this session's TOKEN -> key mapping, if its provider carries a token.

    The pid mapping above answers "which session owns this PROCESS", and on a
    session-SHARING runtime that is the parent's answer for every subagent on it.
    The token answers "which session is THIS one" — it is minted per ACP
    ``session/new`` and rides that session's own MCP elements — so the two are
    published together rather than one standing in for the other.

    Republished every turn for the same reason the pid file is: a warm-pool process
    is re-keyed to a new session while its MCP children keep the token they were
    spawned with, so the FILE is what has to move to the new owner. ``rekey()``
    publishes it at claim time and this bounds how long a missed publication (a
    transient write failure, a trust root restored since) can cost identity — one
    turn.

    Takes the ALREADY-RESOLVED provider rather than the session manager, so this
    step adds no lookup of its own — see the caller.

    Offloaded: publication is a key read plus an ``atomic_write``.
    """
    inner = _provider_inner(provider)
    if inner is None:
        return
    token = getattr(inner, "session_identity_token", "")
    if not isinstance(token, str) or not token:
        return
    await asyncio.get_running_loop().run_in_executor(
        maintenance_executor(), publish_session_token, token, session_key
    )


def _reclaim_gateway_stubs(provider: Any) -> None:
    """Ask this session's provider to re-push its gateway claim, if it has one.

    Takes the ALREADY-RESOLVED provider, so the turn pays one lookup rather than
    two. Fire-and-forget inside ``reclaim`` itself, so this adds no await to the
    turn's critical path.
    """
    inner = _provider_inner(provider)
    if inner is None:
        return
    reclaim = getattr(inner, "reclaim", None)
    if callable(reclaim):
        reclaim()


def _channel_inbound_permitted_sync(channel_type: str) -> bool:
    """Blocking ``channels`` governance check for an INBOUND message (worker only).

    Mirrors the connect-time host gate (``slack.gateway._channel_transport_permitted``)
    and the outbound chokepoint (``mcp_core._vet_channel_governance``): the SAME
    ``channels`` ScopedMap ``members`` allowlist, resolved on the host surface
    (``HOST_SESSION_KEY``) with ``fail_closed=True``. Gating per-message (not only
    at connect) closes the "listener still connected and received messages" gap the
    startup-only gate left open: the transport can be denied for reasons the connect
    gate never saw, and the message is dropped before it drives a turn.

    Fail-CLOSED: an inbound message is externally reachable, so an internal
    governance-evaluation error DENIES (returns False) rather than dispatching an
    ungoverned turn. Default OSS build (no ``channels`` policy) → permits, so inbound
    handling is unchanged. Does blocking profile-file I/O, so callers
    MUST offload it (see :func:`channel_inbound_permitted`).
    """
    try:
        decision = governance_permits(
            "channels", channel_type, session_key=HOST_SESSION_KEY, fail_closed=True
        )
        permitted = bool(getattr(decision, "permitted", False))
        layer = getattr(decision, "layer", "")
        governed = layer in ("policy", "profile", "both")
        # Durable SEL audit, on the codebase invariant that every permission
        # DECISION is recorded — an ungoverned default-permit is not one, per the
        # third bullet. File-backed SEL, safe in this worker thread. Every
        # GOVERNED decision, and every deny, leaves a record; the disposition
        # splits on how a persistence failure is handled:
        #   * GOVERNED ALLOW (layer ∈ {policy,profile,both}) → AUDIT-OR-DENY
        #     (critical=True, synchronous + raising), matching the host
        #     transport-start gate: a SEL write that cannot be persisted
        #     (unwritable SEL / full disk) raises to the outer ``except`` → the
        #     inbound is DENIED, so a governed message never drives a turn
        #     unaudited.
        #   * DENY (any layer) → best-effort (critical=False): the message is dropped
        #     either way, and availability must not hinge on SEL disk health.
        #   * UNGOVERNED ALLOW (the default build, no `channels` policy at all) →
        #     NOT logged. This gate sits on the per-message hot path of five
        #     transports, including observe-mode channel traffic the bot merely sees,
        #     so auditing the default-permit would append one HMAC-chained SEL row
        #     per message on installs with no governance configured — hot-path write
        #     amplification that also drowns real governance signal in the log. There
        #     is no decision to record: nothing was governed. A governed decision
        #     (allow or deny) and every deny ARE recorded, which is the trail that
        #     matters.
        if governed and permitted:
            sel().log_governance_decision(
                session_key=HOST_SESSION_KEY,
                tool_name=f"inbound:{channel_type}",
                scope="channels",
                item=channel_type,
                outcome="allowed",
                rule=getattr(decision, "rule", ""),
                layer=layer,
                reason=getattr(decision, "reason", ""),
                critical=True,
            )
        elif not permitted:
            # DENY (any layer) — always recorded; a blocked inbound is always
            # security-relevant. Best-effort so SEL disk health can't drop traffic.
            try:
                sel().log_governance_decision(
                    session_key=HOST_SESSION_KEY,
                    tool_name=f"inbound:{channel_type}",
                    scope="channels",
                    item=channel_type,
                    outcome="denied",
                    rule=getattr(decision, "rule", ""),
                    layer=layer,
                    reason=getattr(decision, "reason", ""),
                )
            except Exception:
                logger.debug("inbound governance decision audit failed", exc_info=True)
        return permitted
    except PlatformCompositionError:
        # A broken CPP composition must not silently deny every inbound message;
        # re-raise so the boot/compose failure surfaces, matching the host gate.
        raise
    except Exception:
        # Any other governance-evaluation error → deny-by-default for a
        # network-reachable inbound surface, and record the degrade.
        try:
            audit_governance_degraded(
                f"inbound:{channel_type}",
                session_key=HOST_SESSION_KEY,
                scope="channels",
                failed_closed=True,
            )
        except Exception:
            logger.debug("inbound governance degrade audit failed", exc_info=True)
        return False


async def channel_inbound_permitted(channel_type: str) -> bool:
    """Return True only if the ``channels`` policy permits inbound via *channel_type*.

    Off-loop wrapper around :func:`_channel_inbound_permitted_sync` — the check
    walks the ProfileStore (blocking filesystem I/O), so it must not run on the
    event loop. Each channel dispatcher calls this at the TOP of ``handle_message``
    (before driving a turn) so a policy that denies the transport after it
    connected stops dispatching inbound messages without a restart.

    Runs on the dedicated ``governance_executor`` (``mc-gov``), NOT the shared
    maintenance pool: this check is paced by REMOTE senders (one per inbound
    message + approval callback across all five transports), so a message burst queues
    among itself here instead of occupying the ``mc-maint`` workers the orphan
    sweeps need.
    """
    return await asyncio.get_running_loop().run_in_executor(
        governance_executor(), _channel_inbound_permitted_sync, channel_type
    )
