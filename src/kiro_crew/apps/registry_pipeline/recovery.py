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
import time
import uuid
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.apps.registry_pipeline import _FACADE
from kiro_crew.config.loader import config_dir

logger = logging.getLogger(_FACADE)


# Number of days to retain moved-aside .stale-* / .partial-* checkouts before
# the best-effort sweep removes them.
_STALE_CHECKOUT_RETENTION_DAYS = 7


class _MoveAsideUndoFailed(OSError):
    """A move-aside undo failed, stranding the checkout at *aside*.

    Whenever a checkout is left at ``aside`` (possibly with an expired mtime), the
    caller MUST report that exact path, or the age-based sweep deletes an unnamed
    recovery copy. ``aside`` is carried as an attribute so the true on-disk path is
    reported. :func:`_rename_and_refresh_mtime` does not raise this; it guards any
    remaining stranding path in :func:`_move_checkout_aside`.
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

    Refresh first, so *aside* never appears under the swept ``.stale-*`` name with
    an old (possibly expired) mtime that a concurrent ``install_from_registry``
    sweep could act on. Both syscalls run in one thread call, so cancellation
    cannot reorder them.

    The refresh is NOT best-effort: the fresh mtime is the recovery copy's whole
    retention window (:data:`_STALE_CHECKOUT_RETENTION_DAYS`). A ``utime`` failure
    re-raises before anything moves, and :func:`_move_checkout_aside` turns it into
    ``None`` and the non-destructive ``stale_clone_not_removed`` refusal.
    ``Path.rename`` is atomic, so a rename failure also strands nothing.
    """
    # Never rename after a failed refresh: that strands an un-refreshed copy
    # under a swept name.
    os.utime(dest)
    dest.rename(aside)


async def _move_checkout_aside(dest: Path, log_lines: list[str]) -> Path | None:
    """Atomically rename *dest* to a sibling temp path under the app-sources root.

    Returns the new path, or ``None`` if the move failed (logged as ``Could not
    move aside``). Nothing is deleted here; the retention sweep owns removal.

    Every branch that strands a copy at a ``.stale-*`` path logs it with
    ``logger.warning`` as well as *log_lines*: a shutdown cancel discards the
    request-local list, and an unnamed copy would be swept.

    Cancellation: cancelling the task does not stop the executor thread, so the
    handler SETTLES the worker before checking ``aside.exists()``, absorbing any
    further ``CancelledError`` until it is done. Then it synchronously moves
    *aside* back to *dest* (or logs the retained path if that fails) and re-raises
    one ``CancelledError``. No ``await`` runs after settlement, so the undo-or-log
    cannot be interrupted.
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
        # Stranded at *aside*: report the exact path, durably as well, so the
        # sweep does not delete an unnamed copy. Must precede the OSError
        # handler since _MoveAsideUndoFailed subclasses OSError.
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
        # Only a settled worker makes aside.exists() an honest reading. Keep
        # awaiting across further cancels so none skips the undo-or-log below;
        # asyncio.wait does not re-raise the worker's own exception.
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
                # Stranded at .stale-*. A shutdown cancel discards log_lines, so
                # log durably FIRST; the append covers cancels that do return.
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
    """Return ~/.kiro/crew/app-sources/{name}/ — persistent clone directory.

    Raises ValueError unless *name* passes ``app_name_error``, the app-name contract
    every install already enforces, so a name read back from app-writable metadata
    (``registry:<name>`` in ``installed.json``) cannot aim a caller outside the root
    or at the root itself.
    """
    # Deferred: every module-level name here is also exported by the registry facade.
    from kiro_crew.apps.manifest import app_name_error

    error = app_name_error(name)
    if error:
        raise ValueError(error)
    return _app_sources_dir() / name


def _restore_moved_aside(
    moved_aside: Path | None, pkg_dir: Path, log_lines: list[str], reason: str
) -> None:
    """Put a moved-aside checkout back at *pkg_dir*, setting the replacement aside.

    One restoration path, called from every exit that abandons a replacement clone.
    A pinned install moves the previous checkout aside on EVERY reinstall, so an exit
    that forgets this leaves the user's only edited copy as a `.stale-*` sibling that
    the retention sweep later deletes.

    TWO RENAMES, NO RECURSIVE DELETE, so it is callable from anywhere: awaiting is
    unsound during cancellation (``RuntimeError: Event loop is closed``), and a
    synchronous ``rmtree`` of a large checkout stalls the loop thread. The replacement
    is renamed to a ``.partial-*`` sibling for :func:`_sweep_stale_checkouts`, which
    owns both the ``stale`` and ``partial`` prefixes.
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
        # Safe to remove — best-effort. `rmtree_force`, not `ignore_errors`: an aged
        # checkout is a git tree, and Windows will not unlink its read-only packs.
        try:
            if platform_compat.rmtree_force(child):
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
