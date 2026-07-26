"""Per-turn session-identity publication — the single shared writer.

Every surface that runs an agent turn (the dashboard, native Slack, and each
channel ``transport_dispatch``) must publish the ``session_pid_<pid>.txt``
mapping so the gateway's ancestor PID-walk can resolve the caller's
``X-Session-Key`` for session-keyed managed MCP tools (``learn_add``, cron
management, and every other such handler). When a surface omits it the header
is empty and those tools reject the call with HTTP 400 ``missing
X-Session-Key`` (#232).

The obligation lives here — in one function every turn-running surface calls —
rather than as a copy-pasted, per-surface opt-in block. That duplication is the
architectural root cause of #232: two pre-existing writers (dashboard, native
Slack) did not stop five channel dispatchers from shipping the gap. Centralizing
means a new channel gets identity publication by calling one function, and any
change to the publish contract happens in exactly one place.
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

logger = logging.getLogger(__name__)


async def publish_turn_identity(sessions: Any, session_key: str) -> None:
    """Publish this turn's ``session_pid_<pid>.txt`` mapping (+ HMAC sidecar).

    Keyed by the session's kiro-cli host PID (via ``sessions.get_pid``) so the
    gateway PID-walk resolves ``X-Session-Key``. Offloaded to the maintenance
    executor: publishing does a key read plus two ``atomic_write()``
    replacements — blocking filesystem work that must not run on the event
    loop. Fail-safe: a missing pid (session not yet spawned) or any filesystem
    error is swallowed so identity publication can never break a turn.
    """
    try:
        pid = sessions.get_pid(session_key)
        if isinstance(pid, int):
            await asyncio.get_running_loop().run_in_executor(
                maintenance_executor(), publish_session_pid, pid, session_key
            )
    except Exception:
        logger.debug("publish_turn_identity failed for %s", session_key, exc_info=True)


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
    handling is byte-identical to today. Does blocking profile-file I/O, so callers
    MUST offload it (see :func:`channel_inbound_permitted`).
    """
    try:
        decision = governance_permits(
            "channels", channel_type, session_key=HOST_SESSION_KEY, fail_closed=True
        )
        permitted = bool(getattr(decision, "permitted", False))
        # Durable SEL audit. EVERY inbound decision is recorded (the codebase
        # invariant: audit every permission decision). A GOVERNED ALLOW (layer ∈
        # {policy,profile,both}) is AUDIT-OR-DENY, matching the host transport-start
        # gate: the SEL write is ``critical=True`` (synchronous + raising), so if it
        # can't be persisted (unwritable SEL / full disk) the exception propagates
        # to the outer ``except`` and the inbound is DENIED — a governed message
        # never drives a turn unaudited. A DENY (any layer) is logged BEST-EFFORT
        # (``critical=False``): the message is dropped either way, so availability
        # must not hinge on SEL disk health. The UNGOVERNED default-permit is not
        # logged at all — see below. File-backed SEL, safe in this worker thread.
        layer = getattr(decision, "layer", "")
        governed = layer in ("policy", "profile", "both")
        # Every GOVERNED decision, and every deny, leaves a SEL record. Disposition
        # splits on how a persistence failure is handled:
        #   * GOVERNED ALLOW → AUDIT-OR-DENY (critical=True, synchronous+raising): a
        #     SEL write failure raises to the outer except → the inbound is DENIED,
        #     so a governed channel never receives unaudited.
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
