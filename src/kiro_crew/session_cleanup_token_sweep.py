"""Session-token mapping half of the periodic cleanup tick.

Every started session publishes one ``session_token_*.sig`` mapping, and the
pass that removes them, ``session_pid._prune_stale_session_token_files``, is
otherwise reached only from ``cleanup_orphaned_sessions`` (startup and graceful
shutdown). A gateway that keeps running, or that is only ever stopped by a kill,
would hold one file per session it has started, so the tick in
:mod:`kiro_crew.session_cleanup` calls the pass on a bounded cadence.

The functions take the :class:`~kiro_crew.session_cleanup.SessionCleanup`
instance as ``cleanup`` and read ``cleanup._deps`` and ``cleanup.state``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.session_cleanup import SessionCleanup

# Floor between two prunes. The prune globs and stats every mapping, one per
# session started inside the TTL (thousands on a busy host), while removal is
# age-based over a multi-day TTL: running it hourly instead of on every 5-minute
# tick keeps an expired mapping at most an hour longer and cuts that filesystem
# work twelvefold.
SESSION_TOKEN_PRUNE_INTERVAL_SECS = 3600.0


async def maybe_sweep_session_token_mappings(cleanup: SessionCleanup) -> None:
    """Run the prune at most once per :data:`SESSION_TOKEN_PRUNE_INTERVAL_SECS`.

    Stamped before the prune, like the bytecode-cache GC, so a failing prune
    retries at this cadence rather than on every tick.
    """
    now = cleanup._deps.monotonic()
    last = cleanup.state.last_session_token_prune
    if last is not None and now - last < SESSION_TOKEN_PRUNE_INTERVAL_SECS:
        return
    cleanup.state.last_session_token_prune = now
    await cleanup._sweep_session_token_mappings()


async def sweep_session_token_mappings(cleanup: SessionCleanup) -> None:
    """Retract ``session_token_*.sig`` mappings unrefreshed past their age limit.

    Age is the pass's only signal and it is safe at any cadence: a live session
    republishes its mapping at the start of every turn, before anything in that
    turn can call a tool, so the TTL never costs a live session its identity.
    Blocking glob and unlinks over the same-uid agent-writable data home, so it
    runs on the maintenance executor like the session-pid mapping sweep.
    """
    deps = cleanup._deps
    try:
        removed = await asyncio.get_running_loop().run_in_executor(
            deps.get_maintenance_executor(),
            deps.prune_session_token_mappings,
        )
        if removed:
            deps.logger.info(
                "Periodic sweep: retracted %d stale session-token mappings",
                removed,
            )
    except Exception as exc:
        deps.logger.debug(
            "session token mapping sweep failed: %s",
            type(exc).__name__,
        )
