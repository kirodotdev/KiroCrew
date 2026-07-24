"""One-time relocation of the data root from ``~/.kirocrew`` to ``~/.kiro/crew``.

KiroCrew historically kept all of its state — config, credentials, session
history, databases, and the governance/security trust-root — under a top-level
``~/.kirocrew`` directory. The Labs product decision consolidates Kiro-family
apps under the shared ``~/.kiro/`` base, so the data root moves to
``~/.kiro/crew``. This module performs that move once, on the first run after
the upgrade, for an existing install.

Design (mirrors the KITCHEN-111 contract):

* **Copy-then-verify-then-archive** — the legacy tree is *copied* into the new
  home and every regular file is verified present at the destination BEFORE the
  source is touched. Only then is the source renamed to ``~/.kirocrew.archived``
  (a full rollback copy — never deleted). An interruption at any stage leaves
  the original ``~/.kirocrew`` fully intact, so there is no data-loss window.
* **Idempotent** — guarded by the caller so it runs only when the legacy home
  exists and the new home does not; a second call is a no-op.
* **Gateway-safe** — if a live gateway holds the legacy home's ``gateway.lock``,
  we skip the move for this run rather than relocating files out from under a
  running process. It retries on the next cold start.
* **No-op under ``KIROCREW_HOME``** — the caller only reaches here on the
  default (non-override) path, so dev/pod/worktree homes are never migrated.
* **Excludes regenerable bulk trees** — the ``models`` (sha256-pinned GGUF,
  re-downloaded on next start) and ``cache`` top-level dirs are NOT copied or
  archived (see ``_EXCLUDED_TOP_LEVEL_DIRS``): keeping them would double
  hundreds of MB on disk and leave a permanent bulk copy inside the archive for
  no benefit — the new home regenerates them, exactly as a fresh install does.

This module is a near-leaf: it imports only the stdlib plus
:mod:`kiro_crew.platform_compat` (cross-platform file lock) and
:mod:`kiro_crew.gateway_lock` (the lock filename), so importing it from the
``config.paths`` leaf does not create a heavy dependency cycle.
"""

from __future__ import annotations

import filecmp
import logging
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Callable

from kiro_crew import platform_compat
from kiro_crew.config.paths import ARCHIVED_LEGACY_DIR_NAME
from kiro_crew.gateway_lock import LOCK_FILENAME

logger = logging.getLogger(__name__)

# Top-level data-home subdirectories deliberately EXCLUDED from BOTH the
# migration copy and the archived rollback copy. They are large and/or fully
# regenerable, so carrying them forward would (a) make the first-run copy
# needlessly slow and (b) leave a permanent bulk duplicate — including a second
# on-disk copy of hundreds of MB — inside ``~/.kirocrew.archived``. The new home
# simply regenerates them on demand, which is exactly the fresh-install
# behavior, so nothing the user cannot trivially recreate is lost (even if the
# later archive step fails):
#   * ``models`` — the sha256-pinned GGUF embedding model(s), re-downloaded over
#     HTTPS on the next gateway start (hundreds of MB).
#   * ``cache``  — app-manifest / blob caches, rebuilt on access.
# Matched only at the legacy ROOT (a same-named nested dir is NOT excluded).
_EXCLUDED_TOP_LEVEL_DIRS = ("models", "cache")

# Grace window after which the archived rollback copy's SECRET leaves are shred
# (see ``shred_archive_secrets_if_stale``). Keyed off the completion marker's age
# so the new home has been the live root for this long before the credential
# copies are removed — long enough that a rollback is unlikely, short enough that
# frozen secrets don't linger for weeks. 7 days.
_ARCHIVE_SECRET_GRACE_SECONDS = 7 * 24 * 60 * 60


def _prune_excluded_dirs_from_walk(rel_root: Path, dirs: list[str]) -> None:
    """In-place prune ``_EXCLUDED_TOP_LEVEL_DIRS`` from an ``os.walk`` at the root.

    Mutating *dirs* stops ``os.walk`` from descending into the excluded trees, so
    their files never surface in the copy-verification / divergence walks — the
    same dirs the copytree ignore-callback skips. Only prunes at the legacy root
    (``rel_root == Path(".")``); a nested dir of the same name is left alone.
    """
    if rel_root == Path("."):
        dirs[:] = [d for d in dirs if d not in _EXCLUDED_TOP_LEVEL_DIRS]


def _relocate_excluded_dirs_into_new_home(snapshot: Path, new_home: Path) -> None:
    """Move regenerable bulk dirs from *snapshot* into *new_home* (best-effort).

    The ``_EXCLUDED_TOP_LEVEL_DIRS`` (``models``, ``cache``) are deliberately NOT
    copied (see ``_make_copy_ignore``), so after a normal migration they exist only
    in the quiesced legacy *snapshot* that is about to become the archive. Rather
    than let the later ``_strip_excluded_dirs`` DELETE them — which would force a
    fresh re-download of the sha256-pinned GGUF embedding model on the next start —
    RELOCATE them into the new home via an atomic rename. This is a strict
    improvement over strip-and-redownload:

    * **models** is offline-critical — embeddings are always-on in this fork, so a
      migrating air-gapped / offline / metered-connection user who lost ``models``
      would silently lose memory/knowledge search until an HTTPS download succeeds
      (which for an air-gapped host may be never). Carrying it forward keeps
      embeddings working across the upgrade.
    * Rename (not copy) preserves the two original goals: no slow copy (they were
      never copied) and no permanent second on-disk duplicate (the bytes MOVE, they
      are not duplicated).

    Only fills a GAP: a dir already present in *new_home* (a fresh partial, or a
    newer re-download) is authoritative and is kept; the snapshot's copy is left to
    be stripped. On any rename failure — most notably cross-device ``EXDEV`` when
    the new home is on a different filesystem than the legacy home — we leave the
    dir in the snapshot so the subsequent ``_strip_excluded_dirs`` removes it and
    the old strip-and-redownload behavior applies (no worse than before). A symlink
    is never followed (it is left for the strip step, which unlinks it in place).
    """
    for name in _EXCLUDED_TOP_LEVEL_DIRS:
        src = snapshot / name
        dest = new_home / name
        try:
            if src.is_symlink() or not src.is_dir():
                continue  # absent, or a symlink — leave for the strip step
            if dest.exists() or dest.is_symlink():
                continue  # new home already has it (authoritative) — keep it
            os.replace(src, dest)
            logger.info("relocated regenerable %s into %s (kept across migration)", name, new_home)
        except OSError:
            # Cross-device (EXDEV) or other rename failure → fall back to the strip
            # path (the dir stays in the snapshot; _strip_excluded_dirs removes it).
            logger.debug(
                "could not relocate %s from %s into %s; leaving for archive strip",
                name,
                snapshot,
                new_home,
                exc_info=True,
            )


def _strip_excluded_dirs(root: Path) -> None:
    """Remove ``_EXCLUDED_TOP_LEVEL_DIRS`` from *root* (best-effort).

    Called on the ARCHIVE after it has been renamed into place, so the permanent
    rollback copy does not retain a second copy of the regenerable bulk trees.
    A regular top-level dir is removed; a top-level SYMLINK of the same name is
    only unlinked (never followed) so we can't delete through it to an external
    target. Failures are logged and ignored — a leftover bulk dir in the archive
    is wasteful but harmless.
    """
    for name in _EXCLUDED_TOP_LEVEL_DIRS:
        target = root / name
        try:
            if target.is_symlink():
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
        except OSError:
            logger.debug("could not strip %s from archive %s", name, root, exc_info=True)


def _crew_secret_leaves() -> list[str]:
    """The crew-home secret leaf names (single source of truth = security.py).

    Used for the archive PERMISSION lockdown (``_harden_archive_permissions``),
    which must cover EVERY sensitive file — credentials AND the governance/audit
    trust root — so none is left group/world-readable. Deferred import so this
    near-leaf module stays cheap; falls back to an empty list if security can't be
    imported (never blocks the migration) — the 0o700 tree lockdown still applies.
    """
    try:
        from kiro_crew.security import _CREW_SECRET_LEAVES

        return list(_CREW_SECRET_LEAVES)
    except Exception:  # pragma: no cover - defensive
        return []


# Archive end-of-life targets ONLY replaceable credential material — a STRICT
# subset of ``_crew_secret_leaves()``. The governance/security trust-root files
# (``security_policy.json``, ``profiles``, ``admission_policy.json``,
# ``denied_commands.json``) and the tamper-evident audit chain (``sel_hmac.key``,
# ``security_events.jsonl``, ``app_admission.json``) are DELIBERATELY EXCLUDED:
# if the archive is later restored as ``~/.kirocrew`` (the documented downgrade
# path), those files carry the release's SECURITY CEILING — expiring them would
# let the downgraded release boot WITHOUT its ceiling/profiles (a permission
# WIDENING), which is far worse than a stale credential. Credentials, by
# contrast, are re-enterable and rotate independently, so their frozen copies
# have no rollback value once the grace window elapses.
_EXPIRABLE_CREDENTIAL_LEAVES: tuple[str, ...] = (
    ".env",
    "browser-cookies.txt",
    "playwright-storage-state.json",
    "token_signing.key",
    "refresh_chains.json",
    ".local_secret",
)


def _harden_archive_permissions(archived: Path) -> None:
    """Lock the archived rollback copy to the owner (best-effort).

    ``0o700`` on the archive tree (root + subdirs) so other local users cannot
    traverse/list it, and ``restrict_to_owner`` (``0o600`` on POSIX, owner-only
    DACL on Windows) on each secret leaf file so a frozen ``.env`` /
    ``token_signing.key`` / ``sel_hmac.key`` is not left world/group-readable for
    backup and sync tools. Failures are logged and ignored — the migration has
    already succeeded and the keystone still gates the agent regardless.
    """
    try:
        platform_compat.chmod_safe(archived, 0o700)
        for root, dirs, _files in os.walk(archived):
            for d in dirs:
                p = Path(root) / d
                if not p.is_symlink():
                    try:
                        platform_compat.chmod_safe(p, 0o700)
                    except OSError:
                        logger.debug("could not chmod archive subdir %s", p, exc_info=True)
    except OSError:
        logger.debug("could not chmod archive root %s", archived, exc_info=True)

    for leaf in _crew_secret_leaves():
        target = archived / leaf
        try:
            if target.is_symlink() or not target.exists():
                continue
            if target.is_dir():
                # A secret "leaf" that is actually a dir (e.g. ``profiles``):
                # lock the dir and every file under it.
                platform_compat.chmod_safe(target, 0o700)
                for root, _dirs, files in os.walk(target):
                    for f in files:
                        fp = Path(root) / f
                        if not fp.is_symlink():
                            platform_compat.restrict_to_owner(fp)
            else:
                platform_compat.restrict_to_owner(target)
        except OSError:
            logger.debug("could not lock down protected archive leaf %s", target, exc_info=True)


def shred_archive_secrets_if_stale(archived: Path, marker: Path, *, min_age_seconds: float) -> None:
    """Remove stale replaceable CREDENTIALS from the archive, keeping everything else.

    End-of-life for the credential half of the rollback copy: once the migration
    is durably confirmed (the new home's completion *marker* exists) AND that
    marker is older than *min_age_seconds* (the new home has been the live root
    for a full grace window, so rollback is unlikely), the archived copies of the
    replaceable credentials have no remaining value — the live secrets are in the
    new home and rotate independently — while their continued presence is a
    standing exposure. Delete ONLY ``_EXPIRABLE_CREDENTIAL_LEAVES``.

    CRITICAL: the governance/security trust root and audit chain are NOT expired
    (see ``_EXPIRABLE_CREDENTIAL_LEAVES``) — those carry the release's security
    CEILING, and expiring them would let a downgrade that restores this archive
    boot without its ceiling (a permission widening). Non-secret config/history is
    likewise retained as a rollback copy.

    Best-effort and idempotent: any error is swallowed, and a second call after
    the credentials are gone is a no-op. Never touches the live new home. The
    caller (the resolver) invokes this only on the marker-verified, no-legacy path.
    """
    try:
        if not archived.is_dir() or not marker.exists():
            return
        try:
            marker_age = _clock_now() - marker.stat().st_mtime
        except OSError:
            return
        if marker_age < min_age_seconds:
            return
    except OSError:  # pragma: no cover - defensive
        return

    removed = 0
    for leaf in _EXPIRABLE_CREDENTIAL_LEAVES:
        target = archived / leaf
        try:
            if target.is_symlink():
                target.unlink()
                removed += 1
            elif target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
                removed += 1
            elif target.exists():
                target.unlink()
                removed += 1
        except OSError:
            logger.debug("could not shred stale protected archive leaf %s", target, exc_info=True)
    if removed:
        logger.info(
            "removed %d stale protected leaf/leaves from the migration archive %s "
            "(rollback window elapsed; non-sensitive rollback data retained)",
            removed,
            archived,
        )


def _clock_now() -> float:
    """Wall-clock seconds since the epoch (isolated for test monkeypatching)."""
    return time.time()


# Breadcrumb dropped into the archived directory so a human (or a support
# script) can see where the data went and that the move already happened.
_BREADCRUMB_NAME = "README.migrated.txt"
_BREADCRUMB_TEMPLATE = (
    "This directory is the archived pre-move KiroCrew data home.\n"
    "\n"
    "KiroCrew now stores its data under:\n"
    "    {new_home}\n"
    "\n"
    "The contents here were copied to that location on first run after the\n"
    "upgrade, then this directory was renamed from ~/.kirocrew to\n"
    "~/.kirocrew.archived as a rollback copy. It is safe to delete once you\n"
    "have confirmed the new location works. KiroCrew no longer reads from here.\n"
)


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

    Combines two exclusions:

    * **Non-regular special files** — Unix sockets / FIFOs / devices (e.g. a
      stale ``mcp-gateway/gateway.sock``) are runtime artifacts, not data, and
      ``copy2`` raises on them ("Operation not supported on socket"), which would
      abort the whole migration. Symlinks are NOT skipped (``copytree(symlinks=
      True)`` reproduces them). Directory entries are otherwise kept so recursion
      continues.
    * **Bulk/regenerable top-level dirs** — ``_EXCLUDED_TOP_LEVEL_DIRS`` at the
      legacy ROOT only, so the copy (and thus the archive) never carries the
      re-downloadable GGUF models or rebuildable caches forward.

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
                if p.is_symlink() or p.is_dir():
                    continue
                if not p.is_file():  # socket / fifo / device / char-block special
                    ignored.add(name)
            except OSError:
                ignored.add(name)  # unstatable → skip rather than crash the copy
        return ignored

    return _ignore


def _verify_copy(legacy: Path, new_home: Path) -> list[str]:
    """Return a list of regular files missing from *new_home* after the copy.

    Walks the ORIGINAL tree (still intact) and checks each regular file has a
    counterpart at the same relative path under the new home. Symlinks AND
    non-regular special files (sockets/FIFOs/devices) are skipped — the former
    because ``copytree(symlinks=True)`` reproduces them and a dangling link is
    not a data-loss concern, the latter because the copy-ignore callback
    deliberately does not copy them (they are runtime artifacts). The
    ``_EXCLUDED_TOP_LEVEL_DIRS`` (regenerable bulk trees) are likewise pruned so
    their intentionally-uncopied files don't count as missing. Verifying a
    skipped file would spuriously report it "missing" and abort the migration.
    An empty list means the copy is complete.
    """
    missing: list[str] = []
    for root, dirs, files in os.walk(legacy):
        rel_root = Path(root).relative_to(legacy)
        # Don't verify files under dirs the copy deliberately skipped, or they
        # would spuriously report "missing" and abort the migration.
        _prune_excluded_dirs_from_walk(rel_root, dirs)
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
    """Copy *legacy* into *new_home*, then archive *legacy*. Return the home to use.

    Preconditions (asserted by the caller in ``config.paths``): *legacy* is an
    existing directory and *new_home* is NOT yet marked complete (it may be
    absent, empty, or a partial/interrupted copy). Returns *new_home* on a
    successful migration (with *marker* written), or *legacy* if the migration
    is skipped/aborted for safety (a live gateway, or a verification failure) so
    the current run still has a fully intact data root and a later start retries.

    *marker* is the completion-marker path (``new_home / MIGRATION_MARKER_NAME``);
    it is written only after the copy is verified, so an interrupted run never
    leaves a home that a later start would mistake for finished.
    """
    # ── Cross-process guard ──
    # More than one KiroCrew process can start in the same first-boot instant
    # (e.g. the desktop app's gateway AND a cron-fired ``kirocrew`` invocation),
    # and each would independently see ``new_home`` absent and race into the
    # copy — clobbering the other's staging dir. Serialize with an advisory lock
    # on a lockfile in the shared ``~/.kiro`` parent (which always exists once we
    # create it). The winner performs the move; every other process blocks until
    # the winner releases, then falls through the re-check below and simply uses
    # the finished ``new_home`` — so the actual migration body runs exactly once.
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
        # The staging-then-atomic-rename below still prevents a half-populated
        # new_home; the worst case is duplicated copy work, not corruption.
        logger.debug("migration lock unavailable at %s; proceeding unlocked", lock_path)
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except OSError:
                pass
            lock_fd = None
    try:
        # Re-check under the lock: a process that was blocked while the winner
        # migrated now sees the completion MARKER (and, after a normal migration,
        # the legacy home is already archived/gone) and must NOT migrate again.
        # Mirror the caller's invariant exactly — trust the marker ONLY when no
        # legacy dir remains; if legacy still exists (a post-downgrade write-back)
        # fall through to _do_migrate so its divergence guard reconciles rather
        # than blindly trusting the marked-but-possibly-stale new home. (Bare
        # new_home.exists() is not enough — an empty/partial dir is not finished.)
        if marker.exists() and not legacy.is_dir():
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
    """Perform the copy/verify/archive/mark. Caller holds the cross-process lock."""
    # A gateway running on the legacy home means files are open/being written —
    # don't relocate underneath it. Try again on the next cold start.
    if _gateway_is_live(legacy):
        logger.info(
            "skipping data-home migration: a gateway is live on %s; will retry on next start",
            legacy,
        )
        return legacy

    # 1. Copy the whole tree into the new home. copytree creates new_home; its
    #    parent (~/.kiro) is created for us. Preserve symlinks and metadata.
    #    We copy into a temp sibling first, then rename into place, so a partial
    #    copy never leaves a half-populated ~/.kiro/crew that the idempotency
    #    guard would mistake for "already migrated".
    #
    #    ``_make_copy_ignore`` drops non-regular files (unix sockets, FIFOs,
    #    devices) — e.g. a stale ``mcp-gateway/gateway.sock`` a crashed gateway
    #    left behind. copytree copies those via copy2 and raises "Operation not
    #    supported on socket", which would abort the migration on EVERY boot for
    #    any user who enabled the MCP broker. They are runtime artifacts, never
    #    data worth migrating, so skipping them is correct (and _verify_copy also
    #    skips them, so they don't count as "missing"). It also drops the
    #    regenerable bulk top-level dirs (``_EXCLUDED_TOP_LEVEL_DIRS``) so the new
    #    home — and the archive — never carry a second copy of the hundreds-of-MB
    #    GGUF models or rebuildable caches.
    #
    #    Emit a visible line BEFORE the copy: on a large home (years of session
    #    history, vector DBs) copytree can block for a while, and this call sits
    #    behind the very first ``config_dir()`` of the run — without this the user
    #    sees an unexplained hang and may read it as a broken upgrade. stderr so
    #    it shows even when stdout is captured/piped.
    print(
        f"KiroCrew: migrating data home to {new_home} (one-time; this may take a moment)...",
        file=sys.stderr,
        flush=True,
    )
    logger.info("migrating data home %s -> %s (copy starting)", legacy, new_home)
    # Per-PID staging name. Under the normal cross-process lock only one process
    # is ever in this body, but on the DEGRADED unlocked path (an NFS home with
    # no lockd, or Windows' best-effort byte-lock) two first-boot processes can
    # reach here concurrently. A shared ``crew.migrating`` name would let each
    # ``rmtree`` the other's in-flight staging; per-PID staging makes them fully
    # independent so neither can corrupt the other's copy.
    staging = new_home.parent / f"{new_home.name}.migrating.{os.getpid()}"
    try:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        new_home.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(legacy, staging, symlinks=True, ignore=_make_copy_ignore(legacy))
    except Exception:
        logger.warning("data-home copy to %s failed; keeping %s", staging, legacy, exc_info=True)
        shutil.rmtree(staging, ignore_errors=True)
        return legacy

    # 1b. Retarget intra-home ABSOLUTE symlinks. copytree(symlinks=True) copies a
    #     link's target VERBATIM, so an absolute link that pointed inside the old
    #     home (e.g. ``~/.kirocrew/workspace/current -> ~/.kirocrew/workspace/x``)
    #     would still point at the OLD path after legacy is renamed to
    #     ``.archived`` — a dangling link in the live new home. Rewrite any staged
    #     absolute link whose target is under *legacy* to the corresponding path
    #     under *new_home*. RELATIVE links are already correct (they resolve within
    #     the tree) and links pointing OUTSIDE the home are left untouched (they
    #     still resolve to the same external target). Best-effort per link.
    _retarget_intra_home_symlinks(staging, legacy=legacy, new_home=new_home)

    # 2. Verify every regular file made it before we touch the source.
    missing = _verify_copy(legacy, staging)
    if missing:
        logger.warning(
            "data-home copy incomplete (%d file(s) missing, e.g. %s); keeping %s",
            len(missing),
            missing[:3],
            legacy,
        )
        shutil.rmtree(staging, ignore_errors=True)
        return legacy

    # 3. Promote the verified staging tree to the real new home.
    #    - If new_home does NOT exist: atomic rename (the fast, common path).
    #    - If new_home ALREADY exists (empty, or a partial/interrupted copy, or a
    #      dir another Kiro tool created): MERGE staging INTO it WITHOUT
    #      overwriting any file already there — pre-existing files win, so we
    #      never clobber data the user/tool put in the new home, and legacy files
    #      fill the gaps. Then drop the drained staging tree.
    try:
        if new_home.exists():
            _merge_without_overwrite(staging, new_home)
            shutil.rmtree(staging, ignore_errors=True)
        else:
            os.replace(staging, new_home)
    except OSError:
        logger.warning(
            "could not promote %s to %s; keeping %s", staging, new_home, legacy, exc_info=True
        )
        shutil.rmtree(staging, ignore_errors=True)
        return legacy

    # 3b. Quiesce-then-compare divergence guard (closes the compare→archive TOCTOU;
    #     applies to BOTH archive branches below). In the merge path a pre-existing
    #     (possibly STALE) file in new_home shadows the legacy copy — the
    #     no-overwrite merge kept the destination version. If ANY legacy regular
    #     file is therefore NOT byte-identical at new_home, proceeding would make
    #     the stale destination authoritative (marker written) while the CURRENT
    #     legacy copy is archived — silently losing recent config/db/session state
    #     until a manual rollback.
    #
    #     The migration lock only serializes MIGRATIONS; a normal legacy-era writer
    #     (an older release's CLI, or a cron-fired ``kirocrew`` on the old version)
    #     does NOT hold it. Comparing the LIVE legacy tree then archiving it leaves
    #     a window in which such a writer mutates a file AFTER the byte comparison
    #     but BEFORE the archive — the newest bytes then survive only in the
    #     rollback archive, absent from the live home. Close that window by
    #     atomically renaming legacy OUT of its live path to a private, per-PID
    #     quiesced snapshot FIRST, then compare (and, below, archive) that frozen
    #     snapshot: once renamed away, no writer using the canonical legacy path can
    #     touch it, so compare and archive see the identical bytes.
    quiesced = legacy.parent / f"{legacy.name}.quiescing.{os.getpid()}"
    try:
        if quiesced.exists():
            shutil.rmtree(quiesced, ignore_errors=True)
        os.replace(legacy, quiesced)
    except FileNotFoundError:
        # Legacy vanished before we could quiesce it — another first-boot process
        # (degraded unlocked path) is mid-migration: it quiesced the tree, but it
        # may not have FINISHED (its divergence check can still fail and restore
        # legacy, or it can abort without the marker). Adopting new_home
        # immediately would split-brain against that outcome: the racer restores
        # legacy as authoritative while this process writes to the stale new
        # home. Adopt new_home ONLY after observing the racer's completion
        # marker — the same "marker + no legacy ⇒ authoritative" invariant as
        # the under-lock re-check — within a bounded wait; if legacy reappears
        # (racer restored it) use THAT. On timeout, adopt new_home WITHOUT the
        # marker: the racer promoted a verified copy there before quiescing, and
        # returning the now-gone legacy path would make config_dir() recreate an
        # EMPTY ~/.kirocrew and pin this process to it (split-brain). A later
        # cold start re-verifies.
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if marker.exists() and not legacy.is_dir():
                return new_home
            if legacy.is_dir():
                logger.info(
                    "data-home migration: concurrent migrator restored %s; "
                    "retaining it for this run (retry on next start)",
                    legacy,
                )
                return legacy
            time.sleep(0.2)
        logger.warning(
            "data-home migration: legacy %s vanished (concurrent migrator) but its "
            "completion marker did not appear within 30s; adopting %s for this run "
            "WITHOUT writing the marker — a later start re-verifies.",
            legacy,
            new_home,
        )
        return new_home
    except OSError:
        # Could not rename legacy out of the way (rare — same-directory rename;
        # e.g. a permissions problem). We cannot guarantee a race-free compare, so
        # do the SAFE thing: leave legacy fully intact, do NOT archive, do NOT mark
        # complete, and fall back to it for this run. A later cold start retries.
        logger.warning(
            "data-home migration: could not quiesce %s for the divergence compare; "
            "RETAINING it and not marking complete (will retry on next start)",
            legacy,
            exc_info=True,
        )
        return legacy

    # Compare the frozen snapshot (link_base=legacy so absolute intra-home links,
    # which a rename preserves verbatim as into-OLD-home targets, are still judged
    # against the original home path).
    diverged = _legacy_files_not_identical_in(quiesced, new_home, link_base=legacy)
    if diverged:
        # Stale destination shadowed the current legacy data. Restore the quiesced
        # snapshot to its canonical (keystone-gated) legacy path, do NOT mark
        # complete, and fall back to the intact legacy home so a later run / human
        # reconciles. (The fast os.replace path — new_home did NOT pre-exist — has
        # no shadowed files: _verify_copy already confirmed staging holds every
        # legacy file, so the compare returns empty and this branch is not taken.)
        try:
            os.replace(quiesced, legacy)
            restored = legacy
        except OSError:
            # Restoring the canonical name failed (near-impossible for a
            # same-directory rename; e.g. a racer recreated a non-empty legacy).
            # The current data is intact under the quiesced snapshot — run on THAT
            # so this process never uses the stale new home. Surface it loudly.
            logger.error(
                "data-home migration diverged AND could not restore %s to %s; the current "
                "data is preserved at %s — reconcile manually.",
                quiesced,
                legacy,
                quiesced,
            )
            restored = quiesced
        logger.warning(
            "data-home migration: %d legacy file(s) differ from the destination %s (e.g. %s) — "
            "a pre-existing/stale copy shadowed the current legacy data. RETAINING %s and NOT "
            "marking migration complete to avoid making stale state authoritative; reconcile "
            "manually (the legacy home holds the current data).",
            len(diverged),
            new_home,
            diverged[:3],
            restored,
        )
        return restored

    # 3c. Relocate the regenerable bulk dirs (models/, cache/) from the quiesced
    #     snapshot into the new home BEFORE archiving. They were never copied, so
    #     they live only in the snapshot; moving (not copying) them forward keeps
    #     embeddings working across the upgrade for offline/air-gapped users — a
    #     strict improvement over the old strip-and-redownload — while preserving
    #     the no-slow-copy / no-permanent-duplicate goals. Anything left in the
    #     snapshot after this (EXDEV, or a dir the new home already has) is stripped
    #     from the archive below, so the archive is still slimmed. Done only AFTER
    #     the divergence compare (never mutate the snapshot before it is proven a
    #     safe rollback source).
    _relocate_excluded_dirs_into_new_home(quiesced, new_home)

    # 4. Archive the quiesced snapshot. Rename is atomic and keeps a full rollback
    #    copy — we never delete the ORIGINAL data. A failure here is non-fatal: the
    #    new home is already good, so we proceed on it.
    archived = legacy.parent / ARCHIVED_LEGACY_DIR_NAME
    try:
        if archived.exists():
            # A prior archived rollback copy already exists (an earlier completed
            # migration). The quiesced snapshot's data is now verified present +
            # identical in new_home (the compare above), so it is fully redundant —
            # remove it rather than mint a second archive name (a ``.archived.new``
            # would leave a secret-bearing tree at a path the security keystone does
            # not gate).
            shutil.rmtree(quiesced, ignore_errors=True)
            logger.info(
                "migrated data home %s -> %s (%s already exists; removed redundant legacy copy)",
                legacy,
                new_home,
                archived,
            )
        else:
            os.replace(quiesced, archived)
            # Slim the archive: drop any regenerable bulk dirs still present (a
            # relocate that hit EXDEV, or a dir the new home already had) so the
            # permanent rollback copy is NOT a second hundreds-of-MB model store.
            # Done only AFTER the atomic rename; best-effort — leftover bulk is
            # harmless. Safe for rollback too: an older release renames the archive
            # back and simply re-downloads models/ on its next start.
            _strip_excluded_dirs(archived)
            # Lock the credential-bearing rollback copy down to the owner. The
            # keystone already gates it from the AGENT, but the archive is a frozen
            # snapshot of .env / token_signing.key / sel_hmac.key / refresh_chains
            # that would otherwise stay readable to backup agents, cloud-sync tools,
            # and any other local process indefinitely. 0o700 on the tree + 0o600
            # on the secret leaves shrinks that exposure to the owner.
            _harden_archive_permissions(archived)
            _write_breadcrumb(archived, new_home)
            logger.info("migrated data home %s -> %s (archived at %s)", legacy, new_home, archived)
    except OSError:
        # Could not archive the quiesced snapshot. The new home is already good, so
        # this run proceeds on it; restore the snapshot to the canonical
        # (keystone-gated) legacy path best-effort so its frozen secrets are not
        # left at the transient quiescing path. A later cold start re-attempts the
        # archive (marker + legacy present → the caller re-runs migration).
        logger.warning(
            "migrated data home to %s but could not archive %s (harmless; new home is live)",
            new_home,
            legacy,
            exc_info=True,
        )
        try:
            if quiesced.is_dir() and not legacy.exists():
                os.replace(quiesced, legacy)
        except OSError:
            logger.debug("could not restore quiesced snapshot to %s", legacy, exc_info=True)

    # 4b. Secret-safety guard — MUST run before the completion marker. The quiesced
    #     snapshot (``~/.kirocrew.quiescing.<pid>``) holds the FROZEN legacy secrets
    #     (.env, token_signing.key, sel_hmac.key, security_policy.json, …) at a path
    #     the security keystone does NOT gate (it gates only ``.kiro/crew`` /
    #     ``.kirocrew.archived`` / ``.kirocrew``). The archive step normally removes
    #     it — ``os.replace`` renames it to the archive, or the prior-archive branch
    #     ``rmtree``s it — but ``rmtree(ignore_errors=True)`` (or a failed archive)
    #     can leave a residue. Writing the completion marker while such a residue
    #     exists would make every future start skip migration/cleanup, leaving those
    #     secrets agent-readable at an ungated path indefinitely. So: if any residue
    #     remains, re-home it at the keystone-gated legacy path (or, if a racer has
    #     already recreated legacy, hard-remove the now-redundant residue), and if it
    #     STILL cannot be cleared, RETURN WITHOUT marking so a later cold start
    #     reconciles rather than sealing an exposed snapshot.
    if quiesced.exists():
        try:
            if not legacy.exists():
                os.replace(quiesced, legacy)
                logger.error(
                    "data-home migration: quiesced snapshot %s could not be archived/removed; "
                    "restored it to the keystone-gated %s and NOT marking complete — a later "
                    "cold start will reconcile.",
                    quiesced,
                    legacy,
                )
                return legacy
            # Legacy was recreated concurrently (itself keystone-gated and holding
            # current data); the snapshot is redundant — force-remove it so no
            # ungated secret residue lingers.
            shutil.rmtree(quiesced, ignore_errors=True)
        except OSError:
            logger.error(
                "data-home migration: could not relocate/remove quiesced snapshot %s "
                "(frozen secrets at an ungated path); NOT marking complete — reconcile manually.",
                quiesced,
                exc_info=True,
            )
        if quiesced.exists():
            # Residue survived even the hard removal — refuse to mark so cleanup
            # retries on the next start rather than sealing an exposed snapshot.
            return legacy if legacy.is_dir() else new_home

    # 5. Stamp the completion marker LAST — only now is the new home verified
    #    authoritative. A later start sees the marker and skips migration; an
    #    interrupted run (crash before this line) leaves no marker, so the next
    #    start safely re-runs against the still-intact archived/legacy data.
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

    return new_home


def _merge_without_overwrite(staging: Path, new_home: Path) -> None:
    """Move every entry from *staging* into *new_home*, keeping existing files.

    Used only when *new_home* already exists (empty, or a partial/interrupted
    copy, or a dir another Kiro tool created). A file/dir already present in
    *new_home* is authoritative and is NEVER overwritten; only gaps are filled
    from the migrated legacy data. Directories are merged recursively.

    SECURITY (symlink traversal, BOTH directions): a symlink is never *followed*
    on either side.
    * DESTINATION side — a crafted ``~/.kiro/crew/sessions -> /tmp/leak`` would
      otherwise make ``dest.is_dir()`` follow the link and recursion would move
      legacy files THROUGH it to an attacker path outside the keystone. We leave
      ``child`` in staging so the post-merge divergence guard
      (``_legacy_files_not_identical_in``, walked with ``followlinks=False``)
      flags the legacy file as absent-at-destination and ABORTS the migration.
    * SOURCE side — a legacy ``~/.kirocrew/sessions -> /external/real-dir`` is
      reproduced by ``copytree(symlinks=True)`` as a symlinked staging entry. If
      ``new_home/sessions`` already exists as a real dir, following the source
      link (``child.is_dir()`` is True through the link) and recursing would
      ``shutil.move`` the EXTERNAL target's real files into the home — physically
      emptying a directory outside the home during a contractually copy-only
      migration, and the divergence guard is blind to it (it skips symlinks in
      the legacy walk). So a symlinked SOURCE child is never recursed into: if
      the destination is absent we relocate the LINK verbatim (``os.rename``
      moves the link, not its target — exactly what ``copytree(symlinks=True)``
      intended); if the destination exists we keep it and drop the staged link.
    ``new_home`` itself is only ever the resolved, contained data home.
    """
    if new_home.is_symlink():
        # The recursion target is a symlink — refuse to descend/move into it.
        # Leaving staging untouched makes the divergence guard abort the migration.
        logger.warning("refusing to merge into symlinked destination %s", new_home)
        return
    for child in staging.iterdir():
        dest = new_home / child.name
        if dest.is_symlink():
            # Symlinked destination: do NOT follow. Keep src in staging so the
            # divergence guard flags it and the migration aborts (data preserved).
            logger.warning("refusing to merge onto symlinked destination entry %s", dest)
            continue
        if child.is_symlink():
            # Symlinked SOURCE: never follow it (following would move an external
            # target's files into/out of the home). Relocate the link verbatim
            # only into a genuine gap; otherwise keep the existing dest.
            if not dest.exists():
                shutil.move(str(child), str(dest))
            continue
        if not dest.exists():
            shutil.move(str(child), str(dest))
        elif child.is_dir() and dest.is_dir():
            _merge_without_overwrite(child, dest)
        # else: dest already exists (file, or type mismatch) → keep it, drop src.


def _legacy_files_not_identical_in(
    legacy: Path, new_home: Path, *, link_base: Path | None = None
) -> list[str]:
    """Return legacy regular files whose content is NOT identical at *new_home*.

    Guards the destructive archive/removal step: legacy may only be archived or
    dropped if every one of its regular files AND symlinks is already present at
    the same relative path under *new_home* identically. A regular file that is
    missing or byte-differing, or a SYMLINK that is missing / not a symlink at the
    destination / points at a different target, is returned — its presence means
    archiving legacy would make the stale destination authoritative, so the caller
    must retain legacy instead.

    *legacy* is the tree to WALK. It may be the live legacy home or a quiesced
    snapshot of it that the caller renamed out of the live path before comparing
    (the migration compares — and archives — that quiesced snapshot so no
    concurrent legacy-era writer can mutate a file between the compare and the
    archive). Because a rename preserves symlink *contents* verbatim, an absolute
    intra-home link inside a quiesced snapshot still points at the ORIGINAL home
    path; *link_base* (defaulting to *legacy*) is the path those absolute targets
    are resolved against so ``_expected_migrated_target`` still recognizes an
    into-old-home link. Pass ``link_base=<original legacy>`` when *legacy* is a
    quiesced snapshot.

    Symlinks are compared by ``os.readlink()`` target (NOT followed — following a
    legacy link could reach outside the home). This is load-bearing: the
    no-overwrite merge drops a staged legacy symlink whenever the destination name
    already exists (``_merge_without_overwrite``), so if the divergence check
    skipped symlinks it would wrongly declare the homes identical and the
    archive/removal step would permanently lose the link.

    Non-regular special files (sockets/FIFOs/devices) are ignored (``_verify_copy``
    doesn't copy them), as are the intentionally-uncopied
    ``_EXCLUDED_TOP_LEVEL_DIRS`` (regenerable bulk trees).
    """
    link_root = link_base if link_base is not None else legacy
    diverged: list[str] = []
    for root, dirs, files in os.walk(legacy):
        rel_root = Path(root).relative_to(legacy)
        # Excluded bulk dirs are intentionally not copied, so they are legitimately
        # absent at the destination — pruning them keeps the guard from treating
        # that intended absence as divergence and aborting every migration.
        _prune_excluded_dirs_from_walk(rel_root, dirs)
        # A symlinked DIRECTORY entry lands in ``dirs`` (os.walk, followlinks=False,
        # does not descend it) — check it here as a link, not as a traversable dir.
        for name in list(dirs):
            entry = Path(root) / name
            if entry.is_symlink():
                if _symlink_diverges(
                    entry, new_home / rel_root / name, legacy=link_root, new_home=new_home
                ):
                    diverged.append(str(rel_root / name))
        for name in files:
            src = Path(root) / name
            rel = rel_root / name
            if src.is_symlink():
                # A legacy symlink (to a file, or a dangling/── link that stat'd as
                # non-dir): must be reproduced identically at the destination or
                # legacy is not safely redundant.
                if _symlink_diverges(src, new_home / rel, legacy=link_root, new_home=new_home):
                    diverged.append(str(rel))
                continue
            try:
                if not src.is_file():  # socket / fifo / device
                    continue
            except OSError:
                continue
            dest = new_home / rel
            # A symlink anywhere on the destination path (the leaf or any parent
            # dir under new_home) means the merge refused to populate through it
            # (see _merge_without_overwrite), so the legacy file is NOT safely
            # present at a contained location — treat as diverged so the caller
            # retains legacy instead of archiving it via a symlink escape.
            if dest.is_symlink() or _has_symlink_parent(dest, new_home):
                diverged.append(str(rel))
                continue
            try:
                if not dest.is_file() or not filecmp.cmp(str(src), str(dest), shallow=False):
                    diverged.append(str(rel))
            except OSError:
                diverged.append(str(rel))  # unreadable → treat as diverged
    return diverged


def _expected_migrated_target(src_target: str, *, legacy: Path, new_home: Path) -> str:
    """The link target *src_target* should have AFTER migration.

    A RELATIVE target is unchanged (resolves within the moved tree). An ABSOLUTE
    target pointing INSIDE *legacy* is rewritten to the corresponding path under
    *new_home* (so it doesn't dangle once legacy is archived). An absolute target
    OUTSIDE *legacy* is unchanged (still valid). Pure/deterministic — shared by
    the staging retarget pass and the divergence guard so they agree on what
    "identically reproduced" means for an intra-home link.
    """
    if not os.path.isabs(src_target):
        return src_target
    try:
        legacy_resolved = legacy.resolve()
    except OSError:
        legacy_resolved = legacy
    try:
        rel = Path(src_target).resolve().relative_to(legacy_resolved)
    except (ValueError, OSError):
        return src_target  # outside legacy (or unresolvable) — leave as-is
    return str(new_home / rel)


def _retarget_intra_home_symlinks(staging: Path, *, legacy: Path, new_home: Path) -> None:
    """Rewrite staged ABSOLUTE symlinks that point inside *legacy* to *new_home*.

    ``copytree(symlinks=True)`` reproduces link targets verbatim. An absolute link
    into the old home would dangle once legacy is archived, so we re-point it at
    the equivalent location under the new home (via ``_expected_migrated_target``).
    Relative links and links pointing OUTSIDE the home are left untouched.
    Best-effort: a failure to rewrite one link is logged and skipped — the archive
    still holds the original, and ``_verify_copy`` only checks regular files.

    Uses ``os.walk(followlinks=False)`` so we never descend THROUGH a link; both
    file-position and dir-position symlink entries are handled.
    """
    for root, dirs, files in os.walk(staging):
        for name in list(dirs) + files:
            entry = Path(root) / name
            if not entry.is_symlink():
                continue
            try:
                target = os.readlink(entry)
                new_target = _expected_migrated_target(target, legacy=legacy, new_home=new_home)
                if new_target != target:
                    entry.unlink()
                    entry.symlink_to(new_target)
            except OSError:
                logger.debug("could not retarget staged symlink %s", entry, exc_info=True)


def _symlink_diverges(src_link: Path, dest: Path, *, legacy: Path, new_home: Path) -> bool:
    """True if legacy symlink *src_link* is not identically reproduced at *dest*.

    Compared by link TARGET via ``os.readlink`` (never followed), accounting for
    the intra-home retarget: the destination is "identical" ONLY when it matches
    the ``_expected_migrated_target`` of the legacy link — i.e. the retargeted
    form for an absolute intra-legacy link, or the verbatim target for a relative
    / external link. Diverges when the destination is absent, is not a symlink, or
    holds any other target.

    Crucially, a destination still holding the RAW absolute-into-legacy target
    (e.g. a stale ``new_home/ws/current -> <legacy>/ws/x`` that a user ``cp -a``
    left, which the no-overwrite merge KEPT over the correctly-retargeted staged
    link) is treated as DIVERGED — that path will dangle once legacy is
    archived/removed, so accepting it would leave a broken link in the live home
    (and, on the rmtree re-migration branch, destroy the working original). Only
    the retargeted ``expected`` form is safe. An unreadable *src_link* is treated
    as diverged (retain legacy to be safe).
    """
    try:
        src_target = os.readlink(src_link)
    except OSError:
        return True
    if not dest.is_symlink():
        return True
    expected = _expected_migrated_target(src_target, legacy=legacy, new_home=new_home)
    try:
        return os.readlink(dest) != expected
    except OSError:
        return True


def _has_symlink_parent(path: Path, stop_at: Path) -> bool:
    """True if any directory component of *path* below *stop_at* is a symlink."""
    cur = path.parent
    try:
        stop = stop_at.resolve()
    except OSError:
        stop = stop_at
    while True:
        if cur.is_symlink():
            return True
        if cur == stop_at or str(cur) == str(stop) or cur.parent == cur:
            return False
        cur = cur.parent


def _write_breadcrumb(archived: Path, new_home: Path) -> None:
    """Drop a human-readable pointer into the archived directory (best effort)."""
    try:
        (archived / _BREADCRUMB_NAME).write_text(
            _BREADCRUMB_TEMPLATE.format(new_home=new_home), encoding="utf-8"
        )
    except OSError:
        logger.debug("could not write migration breadcrumb in %s", archived, exc_info=True)
