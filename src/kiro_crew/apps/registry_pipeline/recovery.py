"""Where each app's checkout lives, and how one is set aside without being lost.

``app_source_dir`` is the persistent clone slot. A checkout an install replaces is
renamed to a ``.stale-*`` sibling with a refreshed retention clock, never deleted,
put back when the transaction fails (``_restore_moved_aside``), and reclaimed only
by the age-based sweep of ``.stale-*`` / ``.partial-*`` siblings.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path

from kiro_crew.apps.registry_pipeline import _FACADE
from kiro_crew.config.loader import config_dir

logger = logging.getLogger(_FACADE)


# Number of days to retain moved-aside .stale-* / .partial-* checkouts before
# the best-effort sweep removes them.
_STALE_CHECKOUT_RETENTION_DAYS = 7


class _MoveAsideUndoFailed(OSError):
    """A move-aside undo failed, stranding the checkout at *aside*.

    The retained-path carrier for the round-11 undo contract: whenever a
    checkout is left physically at ``aside`` (not ``dest``) — possibly holding
    an already-expired mtime — the caller MUST learn the exact ``aside`` path
    to report it retained instead of letting the age-based sweep delete an
    unnamed recovery copy. Carries ``aside`` as an attribute (not a re-derived
    string) so :func:`_move_checkout_aside` reports the true on-disk path.

    :func:`_rename_and_refresh_mtime` now refreshes *dest*'s mtime BEFORE the
    rename, so a refresh failure fails closed before anything moves and never
    strands a copy under a ``.stale-*`` name — it does not raise this. The
    class and its handler are kept as the fail-closed contract for any residual
    stranding path (the cancellation-settlement undo in
    :func:`_move_checkout_aside`).
    """

    def __init__(self, aside: Path, cause: BaseException) -> None:
        super().__init__(f"move-aside undo failed; checkout retained at {aside}: {cause}")
        self.aside = aside


def _stale_sibling(path: Path) -> Path:
    """The path a move-aside of *path* goes to: ``<path>.stale-<8 hex>`` beside it.

    The one spelling of the retention sweep's ``.stale-*`` name
    (:data:`_STALE_CHECKOUT_PATTERN`), so everything set aside under
    ``app-sources`` -- a whole checkout, or the layout files a refused install
    script created -- is retired by the same sweep after the same
    :data:`_STALE_CHECKOUT_RETENTION_DAYS`, and nothing is stranded under a name
    the sweep does not know.
    """
    return path.with_name(f"{path.name}.stale-{uuid.uuid4().hex[:8]}")


def _rename_and_refresh_mtime(dest: Path, aside: Path) -> None:
    """Refresh *dest*'s mtime, then rename it to *aside*, in one thread call.

    The mtime refresh runs BEFORE the rename, so the moved-aside directory
    already carries a fresh retention clock the instant it appears under its
    sweep-recognized ``.stale-*`` name. This closes a concurrent-sweep race the
    old rename-then-utime ordering left open: between ``dest.rename(aside)``
    landing and a following ``os.utime(aside)`` completing, *aside* is already
    visible under the sweepable name while still holding the checkout's OLD
    (possibly already-expired) mtime, so a CONCURRENT ``install_from_registry``
    running the age-based sweep in that sub-syscall window could delete the
    user's recovery copy. Refreshing *dest* first means *aside* is never
    observable under a swept name with a stale clock — the two syscalls stay in
    one ``asyncio.to_thread`` invocation, so a cancellation between them cannot
    reorder them either.

    The mtime refresh is NOT best-effort. A moved-aside checkout is the user's
    recovery copy, and the fresh mtime is the whole retention window: a
    checkout already older than :data:`_STALE_CHECKOUT_RETENTION_DAYS` would be
    sweep-eligible the instant it appears under the ``.stale-*`` name if its
    mtime were not renewed, so a silently-swallowed ``utime`` failure would hand
    the age-based sweep a green light to delete the recovery copy on the very
    next install. So a ``utime`` failure fails CLOSED before anything moves:
    *dest* has not been renamed yet, so it is still the caller's checkout at its
    original path with its original clock untouched, and this simply re-raises.
    Nothing is ever stranded under a ``.stale-*`` name by a failed refresh
    because the refresh precedes the rename. The caller
    (:func:`_move_checkout_aside`) turns the re-raised error into a ``None``
    return, and every caller of that fails closed with the non-destructive
    ``stale_clone_not_removed`` refusal — the checkout is left in place.

    Only once the refresh SUCCEEDS is the rename attempted. ``Path.rename`` is a
    single atomic syscall: it either moves *dest* to *aside* (now carrying the
    fresh clock) or leaves *dest* exactly where it was, so a rename failure
    strands nothing and re-raises for the same fail-closed handling. The
    round-11 :class:`_MoveAsideUndoFailed` retained-path contract is preserved
    on the caller side: its handler still reports the exact ``.stale-*`` path if
    a checkout is ever found stranded there (e.g. the cancellation-settlement
    undo below), so no recovery copy is ever swept unnamed.
    """
    # Refresh the retention clock IN PLACE first, so the moved-aside dir carries
    # a fresh mtime the instant it becomes visible under the sweep-recognized
    # name. A utime failure fails closed here: dest has not moved, so the caller
    # keeps its checkout untouched at its original path and refuses
    # non-destructively. Never rename after a failed refresh — that is what
    # would strand an un-refreshed copy under a swept name.
    os.utime(dest)
    dest.rename(aside)


async def _move_checkout_aside(dest: Path, log_lines: list[str]) -> Path | None:
    """Atomically rename *dest* to a sibling temp path under the app-sources root.

    Returns the new path, or ``None`` if the rename failed. Nothing is ever
    deleted here: the caller reports the failure and the retention sweep owns the
    moved-aside directory's eventual removal. The mtime refresh now runs BEFORE
    the rename, so a refresh failure fails closed with *dest* untouched at its
    original path and strands nothing under a ``.stale-*`` name — it surfaces as
    the ``Could not move aside`` log line and a ``None`` return. The
    ``.stale-*`` retained-path report (via :class:`_MoveAsideUndoFailed`) is
    still emitted for the one residual strander, the cancellation-settlement
    undo below, so no stranded recovery copy is ever left unreported.

    Report durability: every branch that strands a recovery copy at a
    ``.stale-*`` path records it with ``logger.warning`` as well as appending to
    *log_lines*. The request-local list is discarded when a gateway shutdown
    cancels the update before it returns (the response the list renders into is
    never sent), so a list-only report would leave the age-based sweep to delete
    an unnamed recovery copy. The durable process-log line is what survives that
    shutdown, so the retained path is always recoverable.

    Cancellation safety: the rename+mtime-refresh runs on a retained worker
    future, and the handler SETTLES that worker before inspecting *aside*.
    Cancelling the awaiting task does not cancel a thread already running in
    the executor -- ``asyncio.Future.cancel()`` returns while the worker runs
    on -- so a bare ``if aside.exists():`` check would race the in-thread
    rename: a worker past dispatch but pre-rename at check time would complete
    the rename after the handler re-raised, stranding the checkout at *aside*
    unrecorded. Awaiting the worker to completion first makes the inspection
    deterministic: if the rename ran, this synchronously moves *aside* back to
    *dest* so the caller's state is unchanged by the attempt; if that undo
    itself fails, the aside path is logged durably (``logger.warning``, so it is
    never silently strandable even when the cancelling shutdown discards
    *log_lines*) before the cancellation is re-raised. Repeated cancellation
    does NOT get
    to skip that undo-or-log: the worker is an executor THREAD and will finish
    regardless of how many times the awaiting task is cancelled, so the
    settlement loop absorbs every further ``CancelledError`` until the worker
    future is done, THEN runs the synchronous undo-or-log, THEN re-raises a
    single ``CancelledError``. The earlier "acceptable to skip on a second
    cancel" behavior was wrong by this PR's own standard: a skipped undo leaves
    the checkout at an UNREPORTED ``.stale-*`` path that the retention sweep
    later deletes -- exactly the silent-deletion class this surface exists to
    close, and an mtime refresh only delays the sweep, it does not report the
    path. No new ``await`` runs after settlement: the undo/log is synchronous
    so it cannot itself be interrupted.
    """
    aside = _stale_sibling(dest)
    loop = asyncio.get_running_loop()
    # Retain the worker future so the CancelledError handler can settle it
    # before inspecting *aside*; shield keeps a task cancel from propagating
    # into the executor item (a thread cannot be cancelled anyway).
    worker = loop.run_in_executor(None, _rename_and_refresh_mtime, dest, aside)
    try:
        await asyncio.shield(worker)
    except _MoveAsideUndoFailed as undo_failed:
        # The utime refresh failed AND the rename-back failed: the checkout is
        # stranded at *aside* (NOT dest) with a possibly-expired mtime. Report
        # the exact retained path so the sweep does not delete an unnamed
        # recovery copy -- the same undo-or-log contract the cancellation path
        # below honours. Must come before the generic OSError handler since
        # _MoveAsideUndoFailed subclasses OSError.
        #
        # Record it DURABLY as well as into log_lines: every branch that strands
        # a recovery copy at a .stale-* path names it in the process log, not
        # only in the request-local list, so the retained path survives a
        # gateway shutdown that discards the response the list would have
        # rendered into.
        logger.warning("Previous checkout retained at: %s", undo_failed.aside)
        log_lines.append(f"Previous checkout retained at: {undo_failed.aside}")
        return None
    except OSError as exc:
        # utime failed but the rename-back succeeded: the checkout is back at
        # *dest* with its original clock, nothing stranded, so naming dest is
        # the honest report.
        log_lines.append(f"Could not move aside the checkout at {dest}: {exc}")
        return None
    except asyncio.CancelledError:
        # Settle the worker before inspecting *aside*: the shield delivered the
        # cancel to us while the thread may still be mid-flight, and only once
        # the worker has finished is aside.exists() an honest reading of whether
        # the rename ran. The worker is a thread and WILL finish, so keep
        # awaiting it across any FURTHER cancellation: a second cancel delivered
        # during settlement must not skip the undo-or-log below and strand the
        # checkout at an unreported .stale-* path. Absorb each extra cancel and
        # re-await until the future is done; asyncio.wait never re-raises the
        # worker's own exception (we do not need its result, only that it
        # settled).
        while not worker.done():
            try:
                await asyncio.wait({worker})
            except asyncio.CancelledError:
                # A repeated cancel landed on the settling await. Loop: the
                # thread is still running and the undo-or-log is owed either way.
                continue
        # Settled. The undo-or-log is synchronous, so it runs to completion
        # even under a pending cancellation, then a single CancelledError is
        # re-raised to the caller.
        if aside.exists():
            try:
                aside.rename(dest)
            except OSError as undo_exc:
                # The undo failed, so the recovery checkout is stranded at the
                # .stale-* aside path. The cancellation that brought us here is
                # typically a gateway shutdown, which DISCARDS log_lines (the
                # request never returns to render them), so the request-local
                # append alone would leave the age-based sweep to delete an
                # unnamed recovery copy. Emit a durable logger.warning FIRST so
                # the retained path survives the shutdown in the process log;
                # the log_lines append still carries it into the response on the
                # non-shutdown cancellation paths that do return.
                logger.warning(
                    "Cancelled while moving aside %s; the checkout is retained "
                    "at %s and could not be restored: %s",
                    dest,
                    aside,
                    undo_exc,
                )
                log_lines.append(
                    f"Cancelled while moving aside {dest}; the checkout is "
                    f"retained at {aside} and could not be restored: {undo_exc}"
                )
        raise
    return aside


def _app_sources_dir() -> Path:
    return config_dir() / "app-sources"


def app_source_dir(name: str) -> Path:
    """Return ~/.kiro/crew/app-sources/{name}/ — persistent clone directory."""
    return _app_sources_dir() / name


def _restore_moved_aside(
    moved_aside: Path | None, pkg_dir: Path, log_lines: list[str], reason: str
) -> None:
    """Put a moved-aside checkout back at *pkg_dir*, setting the replacement aside.

    One restoration path, called from every exit that abandons a replacement clone.
    A pinned install moves the previous checkout aside on EVERY reinstall, so an exit
    that forgets this leaves the user's only edited copy as a `.stale-*` sibling that
    the retention sweep later deletes.

    TWO RENAMES, NO RECURSIVE DELETE, and that shape is what makes it callable from
    anywhere. Two review rounds pulled in opposite directions here: awaiting is unsound
    during cancellation (re-entering a loop being torn down surfaces as
    ``RuntimeError: Event loop is closed``), while a synchronous ``rmtree`` of a large
    checkout stalls the gateway's tasks and heartbeat on the loop thread. Both are right,
    so neither answer is -- the deletion itself is what has to go.

    What actually saves the user's data is the rename, which is O(1) on one filesystem.
    The discarded replacement is renamed to a ``.partial-*`` sibling and left for
    :func:`_sweep_stale_checkouts`, which already owns both the ``stale`` and ``partial``
    prefixes. So this function is cheap enough to run inline on any path, needs no thread
    and no loop, and cannot block.
    """
    if moved_aside is None or not moved_aside.exists():
        return
    if pkg_dir.exists():
        discarded = pkg_dir.with_name(f"{pkg_dir.name}.partial-{uuid.uuid4().hex[:8]}")
        try:
            pkg_dir.rename(discarded)
        except OSError as exc:
            # Cannot clear the destination, so the restore rename below would collide.
            # Leave both trees in place and say where the copy is.
            log_lines.append(
                f"WARNING: could not set aside the replacement at {pkg_dir}: {exc}; "
                f"the previous checkout is retained at {moved_aside.name}"
            )
            return
    try:
        moved_aside.rename(pkg_dir)
        log_lines.append(f"Restored the previous checkout after {reason}")
    except OSError as exc:
        log_lines.append(
            f"WARNING: could not restore the previous checkout from "
            f"{moved_aside.name}: {exc}; it is retained there for manual recovery"
        )


# The sweep removes .stale-* / .partial-* siblings under app-sources that are
# older than _STALE_CHECKOUT_RETENTION_DAYS; the desktop gate's preview holder
# (_installed_tree_preview) takes the .partial-* name so a copy an unclean exit
# leaves behind goes with them.
_STALE_CHECKOUT_PATTERN = re.compile(r"^.+\.(stale|partial)-[0-9a-f]{8}$")


def _is_stale_candidate(p: Path) -> bool:
    """Return True if *p* matches the .stale-*/.partial-* naming convention.

    Both are moved-aside checkouts the sweep may retire after the retention
    window.
    """
    return bool(_STALE_CHECKOUT_PATTERN.match(p.name))


def _sweep_stale_checkouts_sync(sources_dir: Path, now_ts: float) -> list[str]:
    """Synchronous sweep of aged stale/partial dirs (runs in a thread).

    Returns a list of removed directory names (for logging).
    Only targets immediate children of *sources_dir* whose names match the
    fixed naming pattern AND whose mtime is older than the retention window.
    Symlinks pointing outside *sources_dir* are skipped (containment check).
    """
    if not sources_dir.is_dir():
        return []
    cutoff = now_ts - (_STALE_CHECKOUT_RETENTION_DAYS * 86400)
    removed: list[str] = []
    try:
        children = list(sources_dir.iterdir())
    except OSError:
        return []
    for child in children:
        if not _is_stale_candidate(child):
            continue
        # Containment check: resolve symlinks and verify the target is still
        # inside sources_dir. This prevents an attacker-placed symlink from
        # causing rmtree to delete files outside app-sources.
        try:
            resolved = child.resolve(strict=True)
        except OSError:
            # Cannot resolve — skip rather than delete blindly.
            continue
        try:
            resolved.relative_to(sources_dir.resolve())
        except ValueError:
            # Points outside app-sources — do not follow.
            continue
        # Age check via mtime.
        try:
            mtime = child.stat(follow_symlinks=False).st_mtime
        except OSError:
            continue
        if mtime >= cutoff:
            continue
        # Safe to remove — best-effort.
        try:
            shutil.rmtree(child, ignore_errors=True)
            if not child.exists():
                removed.append(child.name)
        except Exception:  # noqa: BLE001 — best-effort
            pass
    return removed


async def _sweep_stale_checkouts() -> None:
    """Best-effort async sweep of aged stale/partial dirs under app-sources.

    Called at the start of each install_from_registry invocation so old
    checkouts are eventually cleaned up without blocking or failing the
    install.
    """
    sources_dir = _app_sources_dir()
    now_ts = time.time()
    try:
        removed = await asyncio.to_thread(_sweep_stale_checkouts_sync, sources_dir, now_ts)
        if removed:
            logger.info(
                "Swept %d aged stale checkout(s): %s",
                len(removed),
                ", ".join(removed),
            )
    except Exception:  # noqa: BLE001 — never fail the install
        logger.debug("Stale checkout sweep failed (best-effort)", exc_info=True)


def _restorable_or_none(pending: list[Path] | None, restorable: list[Path] | None) -> Path | None:
    """Return the moved-aside checkout a refusal may restore, or None.

    ``pending`` mirrors ``_pending_stale_cleanup`` (every move-aside this run,
    regardless of reason) while ``restorable`` mirrors ``_restorable_stale``
    (the same-repository subset — branch drift, not a different repo). Only a
    path present in BOTH is safe to hand back as ``restore_from``: restoring
    an origin-mismatched move-aside would give a rejection the exact tree an
    earlier gate already refused.
    """
    if not pending:
        return None
    candidate = pending[0]
    return candidate if candidate in (restorable or []) else None
