"""Walking ``agent.acp_failover``'s chain: at spawn time, and once a run's first request is refused.

:mod:`kiro_crew.acp_failover` is the leaf policy (the parsed block and the pure
one-hop resolver). This module is the walk on top of it, which needs the loaded
config and the backend probe, so the spawn endpoint and the sub-agent manager
share one walk:

* :func:`failover_successor` walks from a backend that cannot start to the first
  chain member that can. ``/api/spawn`` calls it when its probe refuses the
  requested backend (Phase 1).
* :func:`runtime_hop_verdict` decides, synchronously, whether a run whose first
  request was refused with a terminal ``usage_limit`` or ``auth`` verdict should
  move, and :func:`take_runtime_hop` moves it once its first attempt is torn down
  (Phase 2).

The design is ``docs/request-for-change/rfc-acp-backend-failover.md``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Recovery reason for a run moved to the next chain member after its first
#: request was refused (``subagent_manager/cancellation.py`` respawns it).
RUNTIME_FAILOVER_REASON = "backend_failover"


def _failover_block() -> object:
    """The loaded ``agent.acp_failover`` value (``KiroCrewConfig.load``'s cached config)."""
    from kiro_crew.config import KiroCrewConfig

    return KiroCrewConfig.load().agent.acp_failover


async def failover_successor(
    raw: str, verdict: str, available: Any
) -> tuple[str | None, tuple[str, ...]]:
    """Walk ``agent.acp_failover``'s chain from *raw* to a member that can start.

    Returns ``(backend id, ())`` on a hop, else ``(None, tried)`` where *tried* is
    the chain members the walk tried and found unable to start, in order, so the
    refusal names only what it checked: empty when the policy did not apply or
    *raw* is the chain's last member. The walk advances by index, never revisits a member, and stops
    after ``len(chain)`` hops. Chain names are config spelling, read through
    ``backend_from_stored`` (``"kiro"`` is Kiro's ``""``). A member outside
    *available* is never started: *available* is a fresh, governance-narrowed
    probe, so a member a ceiling denied since the block was written is not even
    probed. The config read is ``KiroCrewConfig.load``'s fingerprint-checked
    cache, not a re-parse of ``config.json``.
    """
    from kiro_crew.acp_backends import backend_from_stored
    from kiro_crew.acp_failover import failover_policy, next_failover_backend

    policy = failover_policy(await asyncio.to_thread(_failover_block))
    if not policy.enabled or raw not in policy.chain or verdict not in policy.triggers:
        return None, ()
    current, seen = raw, {raw}
    tried: list[str] = []
    for _ in range(len(policy.chain)):
        nxt = next_failover_backend(
            current=current, chain=policy.chain, triggers=policy.triggers, verdict=verdict
        )
        if nxt is None or nxt in seen:
            break
        seen.add(nxt)
        candidate = backend_from_stored(nxt)
        if isinstance(candidate, str) and candidate in available:
            if not lts_kiro_successor(candidate):
                return candidate, ()
        tried.append(nxt)
        current = nxt
    return None, tuple(tried)


def lts_kiro_successor(backend: str) -> bool:
    """Whether *backend* is the Kiro CLI harness offered as an AUTOMATIC successor in an
    LTS build, which never takes one (RFC D6).

    The Kiro CLI harness is being retired, so an LTS build runs it only as an
    explicit pick the policy allows, never as a failover target. ``False`` in ``oss``.
    """
    from kiro_crew import edition
    from kiro_crew.acp_backends import ACP_BACKEND_KIRO

    return edition.is_lts() and backend == ACP_BACKEND_KIRO


def runtime_verdict(exc: BaseException) -> str | None:
    """The runtime trigger *exc* carries: ``usage_limit``, ``auth``, or ``None``.

    Read from typed data, never from the prose: ``usage_limit`` is the tag
    ``acp.transport_errors._raise_acp_error`` sets from the raw frame for a spent
    allowance, and ``auth`` is every sign-in refusal ``llm_helpers.acp_error_needs_sign_in``
    recognizes, the harness signed out at session start included.
    """
    from kiro_crew.llm_helpers import acp_error_needs_sign_in

    if getattr(exc, "usage_limit", False) is True:
        return "usage_limit"
    if acp_error_needs_sign_in(exc):
        return "auth"
    return None


def runtime_hop_verdict(info: Any, exc: BaseException, *, shutting_down: bool) -> str | None:
    """The trigger that moves *info* to the next chain member, or ``None`` to keep today's ending.

    A hop needs ALL of: a terminal runtime verdict (:func:`runtime_verdict`); a
    run that named its backend (``info.backend``), because only a named backend
    has a chain position, as at spawn time; a first attempt that produced
    nothing (no turn, text, tool call or result), because a replay after output
    could repeat or duplicate work; no continuation, because a conversation lives
    on the harness it first ran on; no pinned or routed model, because a model id
    belongs to one backend's catalogue (a pin is the per-call ``model`` or the
    effective ``requested_model``, which carries an ``agent.role_models`` pin
    too); no stop, reap or shutdown; and an enabled
    block whose ``triggers`` hold the verdict and whose chain has a member after
    this backend. Synchronous: the config read is the fingerprint-checked cache
    ``_should_use_session_sharing`` reads too.
    """
    from kiro_crew.agent_sdk.drivers.acp_vocab import is_unpinned_pick

    verdict = runtime_verdict(exc)
    if verdict is None or info.backend is None:
        return None
    if (
        info.conversation_key
        or info.turns
        or info.tool_count
        or info.streaming_text
        or info.result
        or info.user_stopped
        or info._reap_started
        or shutting_down
        or not is_unpinned_pick(info.model)
        or not is_unpinned_pick(info.requested_model)
        or info.routed_model
    ):
        return None
    from kiro_crew.acp_backends import backend_to_stored
    from kiro_crew.acp_failover import failover_policy, next_failover_backend

    try:
        policy = failover_policy(_failover_block())
    except Exception:
        logger.debug("agent.acp_failover unreadable; no runtime hop", exc_info=True)
        return None
    if not policy.enabled:
        return None
    nxt = next_failover_backend(
        current=backend_to_stored(info.backend),
        chain=policy.chain,
        triggers=policy.triggers,
        verdict=verdict,
    )
    return verdict if nxt is not None else None


def can_start(state: Any) -> bool:
    """Whether a ``probe_backend`` reading lets a run start on its backend now.

    Installed (or not provably missing: an ``unknown`` probe does not block) and
    not installed after the gateway cached its absence (``restart_required``).
    The spawn endpoint and the run-time hop read availability through this one rule.
    """
    from kiro_crew.agent_sdk.backend_install import MISSING

    return bool(state.installed != MISSING and not state.restart_required)


def _available_backends() -> list[str]:
    """The selectable backends this machine can start now (the spawn probe's set)."""
    from kiro_crew.acp_backends import selectable_backend_values
    from kiro_crew.agent_sdk.backend_install import probe_backend

    return [b for b in selectable_backend_values() if can_start(probe_backend(b))]


async def take_runtime_hop(info: Any, manager: Any) -> None:
    """Move *info* onto the next chain member that can start, or raise ``RuntimeError``.

    Called by the recovery respawn after the refused attempt's teardown, so the
    probe runs off the event loop once the old runtime is gone. On a hop
    ``info.backend`` becomes the new backend, which is what the respawned run
    starts on and what a continuation later reads, and ``info.failover_from``
    keeps the backend the caller asked for (the FIRST one, across several hops),
    so ``spawn_status`` and ``spawn_list`` say where the work went. The run's
    ``state.json`` ``backend`` is written FIRST, through the run's own off-loop
    writer, so a continuation after a restart resumes on the harness that holds
    the conversation; a write that did not land ends the recovery instead. The raise names the members tried; the recovery's failure
    path records it.
    """
    from kiro_crew.acp_backends import backend_to_stored

    verdict = info._failover_verdict
    current = backend_to_stored(info.backend)
    available = await asyncio.to_thread(_available_backends)
    hop, tried = await failover_successor(current, verdict, available)
    if hop is None:
        raise RuntimeError(
            f"no agent.acp_failover member after {current!r} can start (tried {list(tried)})"
        )
    # Persist before publishing: a continuation after a restart reads
    # ``state.json`` ``backend``, so a hop the record does not hold must not run.
    if not await manager._write_state_off_loop(info, "failover backend", backend=hop):
        raise RuntimeError(f"could not record the move to {backend_to_stored(hop)!r}")
    info.failover_from = info.failover_from or current
    info.backend = hop
    logger.warning(
        "Subagent %s: %s on %r before any output; agent.acp_failover moved it to %r",
        info.id,
        verdict,
        current,
        backend_to_stored(hop),
    )
