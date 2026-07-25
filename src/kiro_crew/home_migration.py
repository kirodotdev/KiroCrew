"""One-time relocation of the data root from ``~/.kirocrew`` to ``~/.kiro/crew``.

KiroCrew historically kept all of its state — config, credentials, session
history, databases, and the governance/security trust-root — under a top-level
``~/.kirocrew`` directory. The Labs product decision consolidates Kiro-family
apps under the shared ``~/.kiro/`` base, so the data root moves to
``~/.kiro/crew``. This module performs that move once, on the first run after
the upgrade, for an existing install.

Design (deliberately simple):

* **Copy-and-overwrite, verify, then delete the source** — the legacy tree is
  copied into the new home; a conflicting file/dir at the same relative path
  is OVERWRITTEN by the legacy copy (legacy always wins), while a new-home
  entry with no legacy counterpart is left untouched. Every regular file is
  then verified present at the destination, and only after that succeeds is
  ``~/.kirocrew`` removed outright. There is no rollback copy and no backup of
  anything overwritten — once the move completes, that data is gone.
* **Idempotent** — guarded by the caller so it runs only when the legacy home
  exists and the new home is not yet marked complete; a second call is a no-op.
* **Gateway-safe** — if a live gateway holds the legacy (or a pre-existing new)
  home's ``gateway.lock``, we skip the move for this run rather than relocating
  files out from under a running process, and the caller JOINS the live
  gateway's home (whichever side holds the lock) so its ``.local_secret``
  matches for internal IPC. The completion marker is NEVER written on a
  liveness skip — it is reserved for a verified copy — so a fail-safe
  ``_gateway_is_live`` OSError can't brand a partial home as migrated; the
  one-time copy simply completes on the next clean cold start.
* **No-op under ``KIROCREW_HOME``** — the caller only reaches here on the
  default (non-override) path, so dev/pod/worktree homes are never migrated.
* **Excludes regenerable bulk trees** — the ``models`` (sha256-pinned GGUF,
  re-downloaded on next start) and ``cache`` top-level dirs are NOT copied:
  keeping them would make the first-run copy needlessly slow for no benefit —
  the new home regenerates them, exactly as a fresh install does.

This module is a near-leaf: it imports only the stdlib plus
:mod:`kiro_crew.platform_compat` (cross-platform file lock) and
:mod:`kiro_crew.gateway_lock` (the lock filename), so importing it from the
``config.paths`` leaf does not create a heavy dependency cycle.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Callable

from kiro_crew import platform_compat
from kiro_crew.gateway_lock import LOCK_FILENAME

logger = logging.getLogger(__name__)

# Top-level data-home subdirectories deliberately EXCLUDED from the migration
# copy. They are large and fully regenerable, so carrying them forward would
# make the first-run copy needlessly slow for no benefit — the new home
# regenerates them on demand, exactly as a fresh install does:
#   * ``models`` — the sha256-pinned GGUF embedding model(s), re-downloaded over
#     HTTPS on the next gateway start (hundreds of MB).
#   * ``cache``  — app-manifest / blob caches, rebuilt on access.
# Matched only at the legacy ROOT (a same-named nested dir is NOT excluded).
_EXCLUDED_TOP_LEVEL_DIRS = ("models", "cache")


def _gateway_is_live(home: Path) -> bool:
    """Return True if a gateway currently holds *home*'s singleton lock.

    Non-destructive probe: we try to take the same advisory lock the gateway
    uses (non-blocking). If we get it, no gateway is running on this home and we
    immediately release it; if we cannot, a gateway is live. Any error is
    treated as "assume live" so we never relocate under a running process.
    """
    lock_path = home / LOCK_FILENAME
    if not lock_path.exists():
        return False
    fd = None
    try:
        fd = os.open(str(lock_path), os.O_RDWR)
        if platform_compat.try_acquire_lock(fd, exclusive=True):
            platform_compat.release_lock(fd)
            return False
        return True
    except OSError:
        # Cannot open/lock — be conservative and assume a gateway may hold it.
        return True
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _make_copy_ignore(legacy_root: Path) -> Callable[[str, list[str]], set[str]]:
    """Build the ``shutil.copytree`` ignore-callback for *legacy_root*.

    Combines three exclusions:

    * **Symlinks** — skipped entirely rather than followed or reproduced.
      ``copytree`` without ``symlinks=True`` tries to dereference a symlink and
      copy its target; a dangling link (or a target it can't read) would raise
      and abort the whole migration, and a legacy symlink whose name collides
      with a real file already at the destination can't overwrite it either
      (``os.symlink`` refuses when the target exists). Skipping symlinks
      avoids both failure modes at the cost of not carrying them forward —
      acceptable for a data home, which has no user-facing symlinks.
    * **Non-regular special files** — Unix sockets / FIFOs / devices (e.g. a
      stale ``mcp-gateway/gateway.sock``) are runtime artifacts, not data, and
      ``copy2`` raises on them ("Operation not supported on socket"), which
      would abort the whole migration. Directory entries are otherwise kept so
      recursion continues.
    * **Bulk/regenerable top-level dirs** — ``_EXCLUDED_TOP_LEVEL_DIRS`` at the
      legacy ROOT only, so the copy never carries the re-downloadable GGUF
      models or rebuildable caches forward.

    A closure over *legacy_root* is required because copytree invokes the
    callback for every directory level and only the root's children should be
    matched against the top-level exclusion list.
    """
    root = legacy_root

    def _ignore(directory: str, names: list[str]) -> set[str]:
        ignored: set[str] = set()
        at_root = Path(directory) == root
        for name in names:
            if at_root and name in _EXCLUDED_TOP_LEVEL_DIRS:
                ignored.add(name)
                continue
            p = Path(directory) / name
            try:
                if p.is_symlink():
                    ignored.add(name)
                    continue
                if p.is_dir():
                    continue
                if not p.is_file():  # socket / fifo / device / char-block special
                    ignored.add(name)
            except OSError:
                ignored.add(name)  # unstatable → skip rather than crash the copy
        return ignored

    return _ignore


def _copy_overwrite(src: str, dst: str, *, follow_symlinks: bool = True) -> object:
    """``copytree`` copy-function that can overwrite a READ-ONLY destination.

    ``shutil.copytree(..., dirs_exist_ok=True)`` defaults to ``copy2``, which
    opens each destination ``O_WRONLY|O_CREAT|O_TRUNC``. When the new home is
    already populated — a no-marker migration re-running over a partial or
    interrupted earlier copy, or over a directory another Kiro tool created —
    the force-copy writes legacy over the new home, and that ``open`` fails
    with ``PermissionError`` on any destination file that is read-only. Git writes packfiles (``*.pack`` / ``*.idx`` / ``*.rev`` under
    ``.git/objects/pack``) mode ``0o444``, and app-source checkouts under the
    data home carry them — so a real merge reliably hit ``[Errno 13]`` and
    aborted the whole migration, stranding the user in a permanent split-brain
    (legacy home authoritative, new home half-populated, gateway pinned to
    legacy). This mirrors ``copy2`` but first clears the destination's read-only
    bit if it already exists, so legacy always wins the overwrite as intended.

    The chmod is best-effort (a failure just lets ``copy2`` raise as before, no
    worse than the pre-fix behavior) and only touches a path that already exists
    at the destination — never the read-only source, which stays untouched.
    """
    if os.path.lexists(dst) and not os.path.islink(dst):
        try:
            # Ensure owner-write so copy2's truncate-open succeeds; copy2 then
            # copies the source's own mode bits over, restoring 0o444 et al.
            st_mode = os.stat(dst).st_mode
            os.chmod(dst, st_mode | stat.S_IWUSR)
        except OSError:
            pass  # let copy2 surface the real error if the write still fails
    return shutil.copy2(src, dst, follow_symlinks=follow_symlinks)


def _verify_copy(legacy: Path, new_home: Path) -> list[str]:
    """Return a list of regular files missing from *new_home* after the copy.

    Walks the legacy tree (still intact — the source is only deleted after this
    passes) and checks each regular file has a counterpart at the same relative
    path under the new home. Symlinks AND non-regular special files (sockets/
    FIFOs/devices) are skipped, matching what the copy-ignore callback
    deliberately did not copy. The ``_EXCLUDED_TOP_LEVEL_DIRS`` (regenerable
    bulk trees) are likewise pruned so their intentionally-uncopied files don't
    count as missing. An empty list means the copy is complete.
    """
    missing: list[str] = []
    for root, dirs, files in os.walk(legacy):
        rel_root = Path(root).relative_to(legacy)
        if rel_root == Path("."):
            dirs[:] = [d for d in dirs if d not in _EXCLUDED_TOP_LEVEL_DIRS]
        for name in files:
            src = Path(root) / name
            if src.is_symlink():
                continue
            try:
                if not src.is_file():  # socket / fifo / device — not copied
                    continue
            except OSError:
                continue
            dest = new_home / rel_root / name
            if not dest.exists():
                missing.append(str(rel_root / name))
    return missing


def migrate_home(*, legacy: Path, new_home: Path, marker: Path) -> Path:
    """Copy *legacy* into *new_home* (overwriting conflicts), then delete *legacy*.

    Preconditions (asserted by the caller in ``config.paths``): *legacy* is an
    existing directory and *new_home* is NOT yet marked complete (it may be
    absent, empty, or hold unrelated content). Returns *new_home* on a
    successful migration (with *marker* written). When the migration is
    skipped/aborted for safety, returns the home that keeps this process
    coherent with reality: the LIVE GATEWAY's home when one is running
    (legacy or new — whichever holds ``gateway.lock``; joining any other home
    would desync ``.local_secret`` and 403 every internal API call), or the
    still-intact *legacy* on a copy/verification failure. A skipped migration
    retries on the next cold start.

    Every legacy file OVERWRITES a conflicting file of the same relative path
    already present at *new_home* — the legacy copy always wins on a conflict
    — while any *new_home* entry with no legacy counterpart is left untouched
    (a merge, not a wipe). *marker* is the completion-marker path (``new_home /
    MIGRATION_MARKER_NAME``); it is written only after the copy is verified, so
    an interrupted run never leaves a home that a later start would mistake for
    finished.
    """
    # ── Cross-process guard ──
    # More than one KiroCrew process can start in the same first-boot instant
    # (e.g. the desktop app's gateway AND a cron-fired ``kirocrew`` invocation),
    # and each would independently see the migration unmarked and race into the
    # copy. Serialize with an advisory lock on a lockfile in the shared
    # ``~/.kiro`` parent (which always exists once we create it). The winner
    # performs the move; every other process blocks until the winner releases,
    # then falls through the re-check below and simply uses the finished
    # ``new_home`` — so the actual migration body runs exactly once.
    lock_parent = new_home.parent
    try:
        lock_parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Cannot even create ~/.kiro — fall back to the intact legacy home.
        logger.warning("cannot create %s for migration lock; keeping %s", lock_parent, legacy)
        return legacy
    lock_path = lock_parent / ".crew-migration.lock"
    lock_fd: int | None = None
    try:
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        # BLOCKING acquire: a loser waits here until the winner finishes, rather
        # than bailing to the legacy home (which would leave it on the old root
        # for this run even though the migration is about to complete).
        platform_compat.acquire_lock(lock_fd, exclusive=True)
    except OSError:
        # Locking unavailable/failed — proceed unlocked rather than block boot.
        logger.debug("migration lock unavailable at %s; proceeding unlocked", lock_path)
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except OSError:
                pass
            lock_fd = None
    try:
        # Re-check under the lock: a process that was blocked while the winner
        # migrated now sees the completion MARKER and must NOT migrate again.
        # The marker is AUTHORITATIVE regardless of whether the legacy dir
        # still exists (matching _maybe_migrate_legacy_home): a winner that
        # migrated + wrote the marker but could not delete legacy (permission,
        # a still-open handle) must NOT let a blocked second starter recopy the
        # now-debris legacy over the authoritative new home (GPT 5.6 HIGH — the
        # concurrent-starter race). A legacy dir alongside the marker is debris,
        # never authoritative; it is left in place, never promoted.
        if marker.exists():
            return new_home
        return _do_migrate(legacy=legacy, new_home=new_home, marker=marker)
    finally:
        if lock_fd is not None:
            try:
                platform_compat.release_lock(lock_fd)
            except OSError:
                pass
            try:
                os.close(lock_fd)
            except OSError:
                pass


def _do_migrate(*, legacy: Path, new_home: Path, marker: Path) -> Path:
    """Perform the copy + verify + delete. Caller holds the cross-process lock."""
    # A gateway running on the legacy home means files are open/being written —
    # don't relocate underneath it. Joining the legacy home keeps this process
    # IPC-coherent with that gateway (same ``.local_secret``); the move retries
    # on the next cold start.
    if _gateway_is_live(legacy):
        logger.info(
            "skipping data-home migration: a gateway is live on %s; joining it "
            "(migration retries on next cold start)",
            legacy,
        )
        return legacy
    # A gateway running on a pre-existing new home is also live data — don't
    # force-overwrite underneath it. CRITICALLY, join the NEW home (the live
    # gateway's home), NOT legacy: the gateway validates internal API calls
    # against the ``.local_secret`` of the home it booted on, so a process
    # pinned to legacy here would fail EVERY internal call with 403 (spawn,
    # CLI, MCP) and — worse — keep writing into legacy, resurrecting it and
    # making the split-brain permanent. Copy still retries on next cold start.
    if new_home.exists() and _gateway_is_live(new_home):
        # Join the live gateway's home for IPC coherence, but do NOT stamp the
        # completion marker here (GPT 5.6 HIGH): `_gateway_is_live` fails SAFE —
        # any locking OSError (stale/unreadable lock, unsupported-locking FS)
        # returns True, so a spurious entry to this branch must not brand a
        # possibly-partial new home as fully migrated. The marker is reserved
        # for a verified copy (`_verify_copy` success). Returning new_home keeps
        # this process coherent with the live gateway; the one-time copy simply
        # completes on the next clean cold start. (This branch is near-
        # unreachable on the default path anyway: a default-path gateway on an
        # UNMARKED new home cannot exist — migration marks before the gateway
        # binds — and a KIROCREW_HOME override bypasses this resolver entirely.)
        #
        # INVARIANT — join-don't-mark on a liveness skip (please do NOT "fix"
        # this by writing the marker here, nor by "distinguish confirmed
        # contention from a probe error and retain legacy on error"): both were
        # implemented and REVERTED. `_gateway_is_live` fails SAFE on purpose, so
        # stamping the marker (or otherwise treating this branch as a verified
        # migration) could brand a partial/empty new home as authoritative off a
        # stale/unreadable lock — strictly worse than the near-unreachable
        # next-cold-start overwrite it would prevent. The marker is written in
        # exactly one place: after `_verify_copy` succeeds.
        logger.info(
            "skipping data-home migration: a gateway is live on %s; joining it "
            "(migration retries on next cold start)",
            new_home,
        )
        return new_home

    print(
        f"KiroCrew: migrating data home to {new_home} (one-time; this may take a moment)...",
        file=sys.stderr,
        flush=True,
    )
    logger.info("migrating data home %s -> %s (copy starting)", legacy, new_home)

    # Copy directly into new_home. ``dirs_exist_ok=True`` makes this a MERGE
    # when new_home already has content: files copytree encounters at the same
    # relative path are overwritten by the legacy copy (legacy always wins),
    # while a new_home-only entry with no legacy counterpart is left alone.
    # This also transparently handles new_home being a symlink to another
    # location (e.g. a user relocated the data dir with `ln -s`) — writes just
    # follow the symlink like any normal filesystem path would.
    #
    # ``symlinks`` is deliberately left at its default (False): a symlink in
    # the legacy tree is copied as the real file/dir it points to, not
    # reproduced as a link. This sidesteps two sharp edges of preserving
    # symlinks across a merge — a legacy symlink can't overwrite a real file
    # already at the destination (os.symlink refuses if the target exists),
    # and an absolute symlink into the old home would dangle once legacy is
    # deleted — at the cost of a legacy symlink becoming a real copy post-
    # migration. copytree also never writes back through a source symlink to
    # its external target, so nothing outside the home is touched.
    #
    # ``copy_function=_copy_overwrite`` (instead of the default ``copy2``) is
    # REQUIRED for the merge to survive a read-only destination file: git
    # packfiles under an app-source checkout are mode 0o444, and overwriting one
    # with plain copy2 raises PermissionError, aborting the whole migration and
    # trapping the user in a permanent split-brain. See _copy_overwrite.
    try:
        shutil.copytree(
            legacy,
            new_home,
            dirs_exist_ok=True,
            ignore=_make_copy_ignore(legacy),
            copy_function=_copy_overwrite,
        )
    except Exception:
        logger.warning(
            "data-home copy to %s failed; keeping %s (will retry on next start)",
            new_home,
            legacy,
            exc_info=True,
        )
        return legacy

    # Verify every regular file made it before we touch the source.
    missing = _verify_copy(legacy, new_home)
    if missing:
        logger.warning(
            "data-home copy incomplete (%d file(s) missing, e.g. %s); keeping %s "
            "(will retry on next start)",
            len(missing),
            missing[:3],
            legacy,
        )
        return legacy

    # Delete the legacy home outright — no rollback copy is kept.
    try:
        shutil.rmtree(legacy)
    except OSError:
        # The new home is already good. The marker is written below, so a later
        # start is marker-authoritative: it trusts the new home and does NOT
        # re-migrate or re-attempt this delete. The leftover legacy is therefore
        # RETAINED as debris — surfaced (never silently) as a data-home conflict
        # via the one-time WARNING + `kirocrew doctor` line for manual cleanup.
        logger.warning(
            "migrated data home to %s but could not remove %s; it is now unused "
            "leftover — delete it manually (see `kirocrew doctor`)",
            new_home,
            legacy,
            exc_info=True,
        )

    # Stamp the completion marker LAST — only now is the new home verified
    # authoritative. A later start sees the marker and skips migration outright
    # (marker-authoritative: a legacy dir still present alongside it is treated
    # as debris and left in place, never re-copied or re-deleted). An interrupted
    # run (crash before this line) leaves no marker, so the next start safely
    # re-runs against the still-intact legacy data.
    try:
        marker.write_text("migrated\n", encoding="utf-8")
    except OSError:  # pragma: no cover - defensive
        logger.warning(
            "migrated data home to %s but could not write completion marker %s "
            "(next start will re-verify)",
            new_home,
            marker,
            exc_info=True,
        )

    logger.info("migrated data home %s -> %s (legacy removed)", legacy, new_home)
    return new_home
