"""Notes (md-notebook) — the background auto-sync loop.

A single asyncio loop, started from ``server.create_app()`` via ``app.on_startup``
and cancelled on ``app.on_cleanup``. Every :data:`TICK_SEC` seconds it re-reads
``settings.json`` and, when the configured interval has elapsed, commits, merges
and pushes every writable vault exactly as ``POST /api/sync`` does.

WHY IT LIVES HERE rather than in the page. Auto-sync used to be a
``window.setInterval`` inside the Notes React page, so closing the tab — or
navigating to another app — stopped syncing entirely, silently and with no
indication that notes had stopped reaching the remote. A backup that only runs
while you are looking at it is the one case where the user believes they are
covered and are not. Living in the app's own backend means the interval is
honoured while the gateway runs, with no dashboard tab open.

It is NOT a cron job: it runs inside this backend process only, and stops when the
process does.

Fail-safe by design:
  * ``autoSync`` is False by default and the loop does nothing at all while it is
    off — enabling it is what authorizes an unattended ``git push`` to a remote;
  * the vault's ``trusted_remote`` / ``trusted_gitdir`` pins are passed on every
    call, so a repointed remote refuses to push instead of sending notes
    somewhere new;
  * a vault that raises is logged and skipped, so an unreachable remote costs one
    vault one cycle rather than stopping auto-sync for the others;
  * one bad cycle never kills the loop;
  * a conflicted merge records no timestamp, so the UI cannot report notes as
    backed up when the merge was aborted and nothing was pushed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

from aiohttp import web

from kiro_crew.apps.builtins.md_notebook import git_ops, server

logger = logging.getLogger(__name__)

#: How often the loop wakes to re-read ``settings.json``. This is NOT the sync
#: period — it is how quickly a settings change is noticed — which is why it is
#: far shorter than the shortest selectable interval
#: (``server.MIN_AUTO_SYNC_MINS``). Sleeping for the configured interval instead
#: would mean a user who drops 1440 minutes to 1 waits up to a day for the new
#: value to apply, and a user who has just switched auto-sync on waits a whole
#: interval before anything happens. A tick is one small JSON read.
TICK_SEC = 20

#: Unit conversion for the interval, which the settings file stores in minutes
#: because that is what the picker offers.
SECONDS_PER_MINUTE = 60

# Module-level strong ref so the task isn't garbage-collected mid-flight.
_sync_task: Optional[asyncio.Task] = None

# Whether a cycle is in flight. The loop awaits each cycle before starting the
# next, so it cannot overlap itself; this covers the case that sequencing does
# not — a second entrant (a re-armed loop, or a future manual trigger) reaching
# the same working trees while a cycle is mid-flight. Two concurrent commits in
# one git repository race on ``index.lock`` and one of them fails, which would
# drop that cycle's notes from history.
_cycle_running = False


async def start_syncer(app: web.Application) -> None:
    """``app.on_startup`` hook — launch the single background sync loop.

    Idempotent: a second call while a loop is already running is a no-op.
    """
    global _sync_task
    if _sync_task is not None and not _sync_task.done():
        return
    _sync_task = asyncio.create_task(_sync_loop(), name="md-notebook-autosync")
    logger.info("md-notebook auto-sync loop started (settings re-read every %ds)", TICK_SEC)


async def stop_syncer(app: web.Application) -> None:
    """``app.on_cleanup`` hook — cancel the loop on shutdown."""
    global _sync_task
    task = _sync_task
    _sync_task = None
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:  # pragma: no cover - defensive
        logger.debug("md-notebook auto-sync shutdown raised", exc_info=True)


def _interval_sec(settings: dict[str, Any]) -> float:
    """The configured interval in seconds.

    ``autoSyncMins`` is already clamped to ``MIN_AUTO_SYNC_MINS`` by the settings
    read, so this can never return 0 and turn the schedule below into a busy loop.
    """
    return float(settings["autoSyncMins"]) * SECONDS_PER_MINUTE


async def _sync_loop() -> None:
    """Wake on every tick, re-read the settings, sync when one interval is due."""
    loop = asyncio.get_running_loop()
    # Monotonic, never wall clock: an NTP step or a laptop resuming into a
    # corrected clock would otherwise either push the next sync hours out or fire
    # a burst of them.
    #
    # Seeded to now, so the first sync is a full interval away rather than
    # immediate — gateway startup is already spending its budget on process
    # spawns and must not also be met with a git subprocess per vault.
    last_attempt = loop.time()
    while True:
        try:
            await asyncio.sleep(TICK_SEC)
            # Re-read EVERY cycle rather than capturing the interval once, so
            # changing it in Settings takes effect without a gateway restart.
            settings = await server.read_settings()
            if not settings.get("autoSync"):
                # The opt-in gate. Also re-seed the deadline, so switching
                # auto-sync on starts a fresh interval instead of firing a push
                # on the very next tick from time that accrued while it was off.
                last_attempt = loop.time()
                continue
            if loop.time() - last_attempt < _interval_sec(settings):
                continue
            # Advanced BEFORE the cycle, so a slow or failing cycle spaces the
            # next attempt from when this one started rather than retrying on
            # every tick until it succeeds.
            last_attempt = loop.time()
            await _sync_once(_auto_sync_enabled)
        except asyncio.CancelledError:
            break
        except Exception:
            # One bad cycle must never kill the loop: a transient failure would
            # otherwise stop auto-sync for every vault until the gateway
            # restarts, and nobody is watching to notice.
            logger.warning("md-notebook auto-sync cycle failed", exc_info=True)


async def _auto_sync_enabled() -> bool:
    """The live opt-in gate, re-read each time so a mid-cycle revocation is seen."""
    return bool((await server.read_settings()).get("autoSync"))


async def _sync_once(still_enabled: Optional[Callable[[], Awaitable[bool]]] = None) -> None:
    """One pass over every writable vault.

    ``still_enabled`` is re-checked before EACH vault so a cycle stops the moment
    auto-sync is turned off mid-run. The loop passes the live settings gate; it
    defaults to None (always proceed) for the manual path and tests, which reach
    this only when the caller already decided the pass should run.
    """
    global _cycle_running
    if _cycle_running:
        logger.debug("md-notebook auto-sync: previous cycle still running, skipping")
        return
    _cycle_running = True
    try:
        for vault in await server.read_vaults():
            # A cycle pushes every writable vault in turn and each push can take
            # seconds, so a gate checked only once (before the cycle) would let a
            # run that began while auto-sync was on finish pushing the remaining
            # vaults after the user turned it off mid-cycle — an unattended push
            # against a revoked authorization. Re-checking here stops the run the
            # moment the gate closes; the vault already in flight is the
            # unavoidable tail, the rest are never reached.
            if still_enabled is not None and not await still_enabled():
                logger.info("md-notebook auto-sync: disabled mid-cycle, stopping the run")
                return
            if vault.get("readOnly"):
                # sync commits, merges and pushes — all writes — so a read-only
                # vault must not reach it, exactly as on the manual path.
                continue
            # The vault list was snapshotted at the top of the cycle. A vault the
            # user FORGOT mid-cycle has had its authorization revoked, so re-read
            # the live registry and skip it rather than pushing it from the stale
            # snapshot — a forget is a revocation, same as turning auto-sync off.
            # `continue`, not `return`: forgetting one vault must not stop the
            # others that are still connected.
            live_ids = {v.get("id") for v in await server.read_vaults()}
            if vault.get("id") not in live_ids:
                continue
            try:
                await _sync_vault(vault)
            except Exception:
                # Per-vault containment: an unreachable remote, a rejected push, or
                # a repointed `.git` must not stop the remaining vaults.
                logger.warning(
                    "md-notebook auto-sync failed for vault %s", vault.get("id"), exc_info=True
                )
    finally:
        _cycle_running = False


async def _sync_vault(vault: dict[str, Any]) -> None:
    """Sync one vault, with the same arguments the manual handler passes.

    The ``trusted_remote`` / ``trusted_gitdir`` pins refuse to push when the
    vault's remote URL or ``.git`` pointer has moved since it was connected. They
    matter MORE on this path than on the manual one: nobody is watching an
    unattended run, so a repointed remote would send the user's notes somewhere
    new with no step at which they could intervene.
    """
    result = await git_ops.sync(
        vault["localPath"],
        branch=vault.get("branch"),
        pat=await server.resolve_auth(),
        subfolder=vault.get("subfolder"),
        trusted_remote=vault.get("remoteUrl"),
        trusted_gitdir=vault.get("gitDir"),
        local_only=bool(vault.get("localOnly")),
        # A timer chose this moment, not the user, so stage notes ONLY — a stray
        # non-note file dropped in the vault must not be committed and pushed to
        # the remote without the user deciding to send it. The manual Sync keeps
        # staging the whole scope because the user pressed it.
        notes_only=True,
    )
    await server.rebuild_cache(vault)
    if server.synced_cleanly(result):
        await server.record_last_sync(vault["id"])
