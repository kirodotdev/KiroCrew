"""The migration write-back: the backup, the locked rewrite and the adoption ledger.

An owner of the :mod:`kiro_crew.config.loader` facade; it never imports the facade.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

from kiro_crew.config import migration as _migration
from kiro_crew.config.loader_document import (
    _invalidate_config_cache,
    _lock_target,
    _notify_live_watch,
    read_config_for_update,
    stamp_config_meta,
    update_config_locked,
    write_config_atomically,
)
from kiro_crew.config.loader_locations import config_local_path, read_config_text
from kiro_crew.config.loader_snapshots import dispatch_kiro_agent
from kiro_crew.config.paths import config_dir
from kiro_crew.config.superseded_defaults import record_adoptions

logger = logging.getLogger("kiro_crew.config.loader")


def _inside_data_home(path: Path) -> bool:
    """Whether *path* lives in ``config_dir()``, the one directory we own.

    ``load()`` reads whatever ``config_path()`` resolves to, and callers can
    redirect that at a file they own -- tests and embedders point it at a
    ``tempfile`` entry in the shared ``TMPDIR``. Anything the loader drops beside
    such a path is a file nobody collects: the caller unlinks the path it created
    and never learns a sibling appeared. One dev host accumulated 72k orphaned
    ``tmpXXXXXXXX.json.bak`` files this way, 7% of a tmpfs inode budget whose
    exhaustion fails every process on the box.

    Ask about the path the sibling will ACTUALLY be written beside, which is not
    always the one you started from: ``<path>.bak`` lands beside the path as
    given, but ``update_config_locked`` resolves a symlinked config first, so its
    ``<path>.lock`` lands beside the TARGET. A ``config.json`` inside the data
    home that symlinks out of it is contained by one question and foreign by the
    other -- see :func:`_lock_target`.

    Containment is unprovable for a symlink loop or a vanished parent; treat that
    as foreign, since acting on a failed check is the worse error.
    """
    try:
        return path.parent.resolve() == config_dir().resolve()
    except OSError:
        return False


def _write_migration_backup(path: Path) -> None:
    """Copy the pre-migration config aside, but ONLY inside our own data home.

    The copy is gated on :func:`_inside_data_home`, which explains the orphan
    problem the gate exists for. In production the config always lives there
    (``config_path()`` is ``config_dir() / "config.json"``), which keeps the real
    backup exactly where it has always been; for a redirected path we write
    nothing rather than litter a directory belonging to someone else.

    Only the LOCATION decision is contained here. A failing copy still
    propagates, because the caller's ``except`` is what skips the migration
    write -- so a config we could not copy aside is not rewritten either,
    and the migration retries on the next load.
    """
    if not _inside_data_home(path):
        # info, not debug: the migration save that follows rewrites this
        # caller-owned file in place, and that now happens with no backup.
        logger.info("Config migrated; no backup written for %s (outside the data home)", path)
        return
    # NOT with_suffix(".json.bak"): that REPLACES the final suffix, so a
    # config path which is not *.json would be renamed rather than backed up.
    backup = Path(str(path) + ".bak")
    shutil.copy2(path, backup)
    logger.info("Config migrated — backup saved to %s", backup)


def _apply_document_migrations(
    data: dict,
    pending: frozenset[str],
    *,
    overlay_kiro_agent: str | None,
    default_kiro_agent: str,
    adopt_keys: frozenset[str] = frozenset(),
    recorded_adoptions: list[str] | None = None,
) -> bool:
    """Apply the pending write-back migrations; see :func:`migration.apply_document_migrations`.

    The adoption ledger writer is this module's ``record_adoptions``, read at call
    time, so a write-back and anything that replaces that name here agree on it.
    """
    return _migration.apply_document_migrations(
        data,
        pending,
        overlay_kiro_agent=overlay_kiro_agent,
        default_kiro_agent=default_kiro_agent,
        adopt_keys=adopt_keys,
        recorded_adoptions=recorded_adoptions,
        record_adoptions=record_adoptions,
        dispatch=dispatch_kiro_agent,
    )


def _overlay_kiro_agent() -> str | None:
    """``agent.default_agent`` as ``config.local.json`` states it, if it does.

    The overlay is deep-merged OVER the base at load time, so where it names this
    field the base cannot be the effective value. The seeded agent has to carry
    the effective one -- that is what the default crew dispatched before the
    migration existed -- so the seed consults this first.

    Best-effort by design: an unreadable or malformed overlay means "the overlay
    says nothing here", which lets the base value stand. It must not abort the
    migration, because the loader itself already tolerates a bad overlay (it warns
    and marks the file degraded) and a stricter rule here would make one broken
    user-owned file block a write that is correct without it.
    """
    try:
        local_path = config_local_path()
        if not local_path.is_file():
            return None
        raw = json.loads(read_config_text(local_path))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return None
    if not isinstance(raw, dict):
        return None
    section = raw.get("agent")
    if not isinstance(section, dict):
        return None
    value = section.get("default_agent")
    return value if isinstance(value, str) and value else None


def _persist_config_migration(
    path: Path,
    pending: frozenset[str],
    *,
    default_kiro_agent: str,
    adopt_keys: frozenset[str] = frozenset(),
    confirmed_adoptions: list[str] | None = None,
) -> bool:
    """Write the pending migrations to *path* as a read-modify-write.

    Writes as a read-modify-write rather than ``cfg.save()``. ``save()``
    re-serializes the WHOLE snapshot the load parsed, and that snapshot was read
    before this call: a dashboard PATCH or ``kirocrew config set`` landing in
    between is silently dropped, because nothing orders the two. ``load()`` already runs off
    the event loop in places (``chat_runner``'s stop-hook nudge-cap site awaits
    ``asyncio.to_thread(KiroCrewConfig.load)``), so the interleave is reachable.

    Two parts carry the fix, and they are worth separating because only one of
    them can be applied everywhere:

    * **The write is a delta.** Only the keys *pending* names are touched, and
      each is re-decided against the document as read HERE rather than against
      the load's older snapshot. A concurrent write to any other setting is
      therefore not merely ordered but untouchable -- those bytes are never part
      of our write. This part always applies.
    * **The read and the write are one critical section**, via
      :func:`update_config_locked`, so the migration's own keys are decided from
      current state and cannot interleave with another locked writer.

    The ordering the loader uses next door -- ``publish_autocompact_pct``'s
    monotonic ticket -- cannot close this. A ticket orders load against load, and
    the writer being lost here is not a load: no config writer outside this module
    draws a ticket, so the comparison never sees the PATCH at all. Nor is it
    enough to take a lock around the write alone, since the snapshot was read
    before the lock; the read has to move inside it.

    ``update_config_locked`` takes its advisory lock on a ``<path>.lock`` sidecar,
    which for a config path we do not own is the orphan the backup gate above
    exists to prevent -- and ``TestMigrationBackupContainment`` pins that a
    migrating ``load()`` leaves a caller-owned directory exactly as it found it.
    So the same containment predicate decides the lock, asked about the path the
    lock will actually land beside: ``update_config_locked`` resolves a symlinked
    config first, so a ``config.json`` inside the data home that points OUT of it
    would otherwise be classified as contained and drop its sidecar in a directory
    belonging to someone else. Contained, take the lock; foreign, do the delta off
    an immediate fresh read and skip the sidecar. What the foreign path gives up is
    ordering on the migration's OWN keys against an unlocked writer of those same
    keys -- and a redirected config has no gateway writing it, since the dashboard
    and CLI paths write ``config_path()``.

    The lock is taken WITHOUT waiting. ``load()`` is called from the event loop
    all over the tree, and a POSIX ``flock`` wait there would stall the gateway
    for as long as the holder keeps the lock -- so this asks once and gives up.
    Giving up costs nothing, because a held lock means another writer is mid-write
    and that writer's bytes are precisely what must not be clobbered: declining is
    the correct outcome, not a compromise. The migration is already
    retry-on-next-load by construction (the degraded-sections branch in
    :func:`persist_write_back` relies on the same property), so the next uncontended load
    performs it. The remaining file I/O on the loop -- one read, one atomic
    rename -- is what ``cfg.save()`` did here before, unchanged.

    The backup is taken immediately before the write -- inside the lock hold where
    there is one -- so it is the bytes being replaced rather than whatever was
    there when the load started. A failing copy still propagates and aborts the
    write, keeping :func:`_write_migration_backup`'s contract: a config we could
    not copy aside is not rewritten, and the migration retries on the next load.

    Returns True when a write happened.
    """
    wrote = False
    # The overlay is user-owned and never written back, so it is read OUTSIDE the
    # lock: there is no update of ours to lose against it, and a concurrent edit
    # to it is the operator's own action rather than a race. Read here rather than
    # taken from the load's snapshot so the seed reflects the file as it is now.
    overlay_kiro_agent = _overlay_kiro_agent()

    # Keys the migration recorded in the ledger before removing them. Only the ones
    # whose write LANDED are reported back, so the in-memory half never runs ahead of
    # the stored document. Nothing is undone on the failure path: see
    # ``record_adoptions`` for why the marker deliberately goes first and what the
    # residual is.
    recorded_adoptions: list[str] = []

    # ``applied`` means the delta was computed and the backup taken; ``wrote`` means
    # the ATOMIC WRITE returned. They are separate because the write happens AFTER
    # ``_mutate`` returns -- ``update_config_locked`` performs it -- so a flag set
    # inside the callback would report a write that had not happened yet, and the
    # ``finally`` below would then confirm an adoption whose removal never reached
    # disk, letting the in-memory half run ahead of the stored document. Neither
    # call site guards ``write_config_atomically``, so a failed write propagates and
    # ``wrote`` correctly stays False.
    applied = False

    def _mutate(current: dict) -> dict | None:
        nonlocal applied
        if not _apply_document_migrations(
            current,
            pending,
            overlay_kiro_agent=overlay_kiro_agent,
            default_kiro_agent=default_kiro_agent,
            adopt_keys=adopt_keys,
            recorded_adoptions=recorded_adoptions,
        ):
            # Nothing left to migrate -- another writer got here first. Returning
            # None skips the write, so we do not rewrite a file we agree with.
            return None
        _write_migration_backup(path)
        applied = True
        return current

    try:
        if _inside_data_home(_lock_target(path)):
            try:
                update_config_locked(path, mutate=_mutate, wait_for_lock=False)
            except BlockingIOError:
                # POSIX reports a contended single-shot acquire this way. Not a
                # failure worth a warning: info, because the migration simply moves
                # to the next load and nothing was written or lost.
                logger.info(
                    "config: migration deferred -- another writer holds %s.lock; "
                    "the next load will migrate",
                    path.name,
                )
                return False
            # Reached only when the locked read-modify-write completed, so an
            # ``applied`` delta is now on disk.
            wrote = applied
        else:
            # read_config_for_update fails CLOSED on an unreadable or non-object
            # file, and that exception reaches load()'s except: the file is left
            # alone and the migration retries. Same contract as the locked path.
            result = _mutate(read_config_for_update(path))
            if result is not None:
                write_config_atomically(path, stamp_config_meta(result))
                wrote = True
    finally:
        # ``wrote`` is the single fact that decides both halves, and it is read in a
        # ``finally`` because the write can be skipped WITHOUT an exception too (the
        # deferral return above, a concurrent writer that already migrated).
        #
        # Only a key whose removal actually LANDED is reported back for the in-memory
        # half, so the running config never holds a value the stored document does not
        # agree with.
        if recorded_adoptions and wrote and confirmed_adoptions is not None:
            confirmed_adoptions.extend(recorded_adoptions)
    if wrote:
        # Same reason save() did: drop the validated-data cache so the next load
        # re-reads this write even where the filesystem mtime resolution is coarse.
        _invalidate_config_cache()
        _notify_live_watch()
    return wrote
