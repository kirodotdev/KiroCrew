"""The shared process's primitives: its runtime class, its liveness, its session records, and teardown.

:func:`_acp_runtime_factory` resolves the agent-runtime class through the session manager's own
loader -- the indirection tests substitute a fake runtime for, and the reason no warm module
imports ``kiro_crew.acp``. A warm session injects an EMPTY ``mcp_servers`` list
(:func:`_warm_session_mcp_servers`), because a remote server passed through ``session/new``
kills the process together with every pending verifier in it. :func:`_collect_oauth_requests`
drains one session's challenges until the queue stays quiet, bounded by the expected roster.

Teardown is best-effort and never raises, but it REPORTS: :func:`_destroy_session_quietly` and
:func:`_kill_quietly` answer ``False`` when the session may still listen or the child may still
run, so every caller keeps the only reference to it for a retry. The records
(:class:`_WarmSession`, :class:`_WarmMintResult`) are what ``warm._WarmMintRuntime`` registers and
returns. The timeouts and drain bounds stay with the facade's other tunables, so they are read
there at call time (:func:`_warm_facade`); tests substitute some of them on the facade.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from types import ModuleType
from typing import Any

from kiro_crew.connections.registry import Provider

logger = logging.getLogger("kiro_crew.connections.warm")


def _warm_facade() -> ModuleType:
    """The warm facade, resolved per call because it holds the names this module reads late.

    Imported here rather than at module scope because the facade imports this module: a
    top-level import would make a cold import of either module circular.
    """
    from kiro_crew.connections import warm  # circular import: the facade composes this module

    return warm


def _acp_runtime_factory() -> Any:
    """The agent-runtime class, resolved through the session manager's own loader.

    Still the indirection tests substitute a fake runtime class for, and now also why
    this module carries no ``kiro_crew.acp`` import: application code may not reach the
    ACP layer (``scripts/check_agent_sdk_boundary.py``), and the sanctioned surface
    ``kiro_crew.agent_sdk`` is deliberately EMPTY -- its RFC schedules runtime ownership
    for the supervisor phase, with ``connections/`` migrating in the consumer wave after
    it. Until then ``session._load_bg_runtime_types`` is the tree's single accessor for
    the class, already consumed that way by ``session_background`` and
    ``session_allocation`` through an injected callable; when the supervisor takes the
    pool, this one call site moves with it.

    Imported inside the call for the same reason that loader is itself lazy: it crosses
    the ``session -> acp.runtime -> acp.client -> session`` import cycle.
    """
    from kiro_crew.session import _load_bg_runtime_types

    return _load_bg_runtime_types()[0]


def _warm_session_mcp_servers() -> list[dict[str, Any]]:
    """The session-injected MCP servers for a warm mint: ALWAYS empty.

    A remote server passed through ``session/new`` kills the process together with every
    pending verifier in it, so the spec -- never the session -- is what mounts servers.
    """
    return []


async def _collect_oauth_requests(handle: Any, wanted: frozenset[str]) -> list[dict[str, str]]:
    """Collect challenges until the queue stays quiet, bounded by the expected roster."""
    collected: dict[str, dict[str, str]] = {}
    quiet_drains = 0
    drains = 0
    hard_drain_limit = max(1, len(wanted)) * _warm_facade()._WARM_OAUTH_SETTLE_ROUNDS
    while True:
        before = len(collected)
        for request in handle.pop_pending_oauth_requests():
            name = str(request.get("serverName") or "")
            if name and request.get("oauthUrl"):
                collected[name] = request
        if len(collected) > before:
            quiet_drains = 0
        if wanted and wanted <= collected.keys():
            break
        if quiet_drains >= _warm_facade()._WARM_OAUTH_SETTLE_ROUNDS or drains >= hard_drain_limit:
            break
        await handle.drain_init(
            duration=_warm_facade()._WARM_OAUTH_SETTLE_SECONDS,
            idle_exit=_warm_facade()._WARM_OAUTH_SETTLE_SECONDS,
            no_report_ceiling=0.0,
        )
        drains += 1
        quiet_drains += 1
    return list(collected.values())


def _runtime_alive(runtime: Any) -> bool:
    """Liveness of one warm process. Never raises into a mint."""
    if runtime is None:
        return False
    try:
        return bool(runtime.is_alive())
    except Exception:  # noqa: BLE001 — liveness must never raise into a mint
        logger.debug("warm mint liveness check failed", exc_info=True)
        return False


@dataclass
class _WarmSession:
    """One live ACP session on the shared process, and what it owns.

    Held because the session owns the loopback callback servers for its challenges.
    ``settled`` flips once the URLs are absorbed into the mint table, which is what makes
    the sweep safe -- an activation still in flight is referenced by no row. ``expires_at``
    is why a session outlives the rows that pointed at it: a replaced URL may still be open
    on the provider's consent page, and one mint TTL is exactly the window in which that
    code is still redeemable.
    """

    generation: int
    handle: Any
    expires_at: float
    settled: bool = False
    providers: tuple[Provider, ...] = ()


@dataclass(frozen=True)
class _WarmMintResult:
    """One activation's product, plus the snapshot and process it ran on."""

    generation: int
    activation: int
    providers: list[Provider]
    requests: list[dict[str, str]]


async def _destroy_session_quietly(handle: Any) -> bool:
    """Terminate one warm session. ``False`` when it may still be listening.

    The return value is the point, and it is the same contract as :func:`_kill_quietly`. A
    destroy that TIMES OUT leaves a live session and the loopback callback servers it owns,
    and reporting that as success let every caller drop the record -- which is the ONLY
    remaining reference to the handle, so the session became unaddressable and nothing could
    ever retry it. ``asyncio.TimeoutError`` IS an ``Exception``, so the retention the three
    call sites already had -- each written for a ``CancelledError``, which is not -- never
    saw the case it matters most for.

    Still no raise: teardown is best-effort by design and runs from ``finally`` blocks where
    raising would mask the original failure. The callers RETAIN instead of propagating, and
    the reaper's periodic session sweep is what performs the retry.
    """
    try:
        await asyncio.wait_for(
            handle.destroy(), timeout=_warm_facade()._WARM_SESSION_DESTROY_TIMEOUT_SECONDS
        )
    except Exception:  # noqa: BLE001 — best-effort teardown of our own session
        logger.warning("warm mint session destroy failed; the session stays tracked for a retry")
        logger.debug("warm mint session destroy failed", exc_info=True)
        return False
    return True


async def _kill_quietly(runtime: Any) -> bool:
    """Kill one process. ``False`` when it may still be running.

    The return value is the point. A kill that TIMES OUT leaves a live child, and reporting
    that as success let every caller discard the only reference to it -- the generation was
    forgotten while the process, its sessions and their loopback listeners stayed resident,
    so repeated activations accumulated them with nothing left that could retire them.
    ``asyncio.TimeoutError`` IS an ``Exception``, so the three retention paths
    ``warm._WarmMintRuntime`` already has -- all built for a ``CancelledError``, which is not --
    never saw it.

    Still no raise: teardown is best-effort by design and runs from ``finally`` blocks where
    raising would mask the original failure. The callers RETAIN instead of propagating.
    """
    try:
        await asyncio.wait_for(
            runtime.kill(expected=True, reason="warm mint teardown"),
            timeout=_warm_facade()._WARM_KILL_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 — best-effort teardown of our own child
        logger.warning("warm mint runtime kill failed; the process stays tracked for a retry")
        logger.debug("warm mint runtime kill failed", exc_info=True)
        return False
    return True
