"""Restoring into a live data home: the checks a restore refuses on, then the atomic replace.

:func:`kiro_crew.snapshot.restore_main` sequences extraction and the bundle-shape
refusals, and writes their ``state_restore_rejected`` audits. This module supplies the
bundle predicates those refusals rest on (:func:`_component_payload_absent`,
:func:`_components_absent_from_bundle`, :func:`_trees_absent_from_bundle`), the
content-soundness refusal (:func:`_refuse_corrupt_source_databases`), the destination
guards, and the replace transaction with its rollback. The helpers and limits a test
replaces on :mod:`kiro_crew.snapshot` are read through it when used
(:func:`kiro_crew.snapshot_components._facade`), so a patch there reaches the calls here.

Each refusal runs before live state moves, because that is where declining still costs
nothing. Replace takes a complete rollback set first and mutates second, so a failure in
the mutation phase is reverted target by target from a set known to be whole
(:func:`_do_replace`). Merge mode reuses the destination guards and the locked-document
installer from here; its algorithms live in :mod:`kiro_crew.snapshot_merge`.
"""

from __future__ import annotations

import functools
import json
import os
import shutil
import stat as _stat
from contextlib import ExitStack, closing
from datetime import timezone
from pathlib import Path, PurePosixPath
from typing import Callable, cast

from kiro_crew import pinned_fs, platform_compat
from kiro_crew.atomic_write import replace_with_retry
from kiro_crew.config import loosening_provenance
from kiro_crew.member_memory_backup import StoresInUse
from kiro_crew.memory_stores import (
    MEMBER_BACKUPS_DIR_NAME,
    MEMORY_STORES_DIR_NAME,
    is_host_local_store_state,
    memory_store_namespace_lock,
)
from kiro_crew.snapshot_archive import (
    _bundle_carries_named_stores,
    _recorded_skips,
    _records_omission_of,
    _safe_name,
)
from kiro_crew.snapshot_components import (
    _CORE_FILE_COMPONENTS,
    _DERIVED_INDEXES,
    _FLAT_DOCUMENT_VALIDATORS,
    _JSON_OBJECT_LISTS,
    _LOCKED_DOCUMENT_TREES,
    _TREE_DOCUMENT_VALIDATORS,
    _WHOLE_TREE_COMPONENTS,
    COMPONENT_JSON_OBJECTS,
    COMPONENT_TREES,
    COMPONENTS,
    CORE_FILES,
    CORE_FILES_FLAT,
    UnsafeComponentRoot,
    _facade,
    _want,
    is_product_tree_database,
    safe_tree_root,
)
from kiro_crew.user_json import MAX_DOCUMENT_NESTING, exceeds_nesting


def _save_locked_document_to(backup: Path, rel: str) -> Callable[[Path], None]:
    """The rollback copy of a locked document, taken INSIDE the store's lock hold.

    Phase one of replace copies every other target before any mutation. This document
    is the exception: a copy taken there, outside the lock, could miss a team write
    that commits between the copy and phase two's install, and that write would then
    be lost from the live store AND from the rollback. So the installer takes the
    copy itself, under the same lock hold that replaces or removes the document.
    """

    def _save(live: Path) -> None:
        # Staged beside its final name and PUBLISHED by rename only once the copy has
        # completed: a copy that fails midway must never leave a truncated file at the
        # name recovery reads, or the rollback would put that truncation back over a
        # live document the failed install never touched.
        final = backup / rel
        final.parent.mkdir(parents=True, exist_ok=True)
        partial = final.with_name(final.name + ".partial")
        try:
            pinned_fs.copy_file_pinned(
                str(live),
                str(partial),
                on_skip=pinned_fs.fatal_skip_reporter(f"backup of {rel!r}"),
            )
            replace_with_retry(partial, final)
        finally:
            try:
                partial.unlink()
            except FileNotFoundError:
                pass

    return _save


def _install_locked_document(
    tree: str,
    snap: Path,
    mc: Path,
    *,
    only_if_absent: bool = False,
    save_existing: Callable[[Path], None] | None = None,
) -> bool:
    """Install (or, with *only_if_absent*, offer) a bundle's locked document into *mc*."""
    from kiro_crew import crew_teams  # a store module; imported on first use only

    rel = _LOCKED_DOCUMENT_TREES[tree]
    try:
        return crew_teams.install_document(
            snap / rel, mc / tree, only_if_absent=only_if_absent, save_existing=save_existing
        )
    except crew_teams.TeamsUnreadable as e:  # pragma: no cover - validated before mutation
        raise SourceComponentUnsound(
            f"{rel} in this snapshot would be refused by its reader ({e})."
        ) from e


def _restore_locked_document(tree: str, saved: Path, mc: Path) -> None:
    from kiro_crew import crew_teams  # a store module; imported on first use only

    crew_teams.restore_document(saved, mc / tree)


def _remove_locked_document(
    tree: str, mc: Path, *, save_existing: Callable[[Path], None] | None = None
) -> None:
    from kiro_crew import crew_teams  # a store module; imported on first use only

    crew_teams.remove_document(mc / tree, save_existing=save_existing)


def _refuse_dropped_entries(label: str, dropped: int) -> None:
    """Raise when a settings document holds entries its own store would refuse.

    A restore installs the bundle's file VERBATIM, not the reader's filtered copy, so
    an entry the reader drops still lands: a key past the store's limits makes every
    later save fail, and a non-boolean ``muted`` silences a channel. The product's own
    writers never produce such an entry, so a bundle carrying one is refused whole.
    """
    if dropped:
        raise SourceComponentUnsound(
            f"{label} in this snapshot holds {dropped} entr{'y' if dropped == 1 else 'ies'} "
            "its store would refuse.\n"
            "   Refusing to restore it: the file is installed as-is, so those entries "
            "would land and break later settings saves or silence notifications."
        )


def _refuse_unless_valid_tree_document(src: Path, label: str, validator: str) -> None:
    """Raise `SourceComponentUnsound` unless *src* passes its own consumer's reader."""
    if validator == "crew_teams":
        from kiro_crew import crew_teams  # a store module; imported on first use only

        try:
            crew_teams.read_document(src)
        except crew_teams.TeamsUnreadable as e:
            raise SourceComponentUnsound(
                f"{label} in this snapshot would be refused by its reader ({e}).\n"
                "   Refusing to restore it: an installed document the team store cannot "
                "read fails every team route and refuses every crew create."
            ) from e
        return
    if validator == "ui_prefs":
        from kiro_crew import ui_prefs  # a store module; imported on first use only

        try:
            _, dropped = ui_prefs.parse_imported_ui_prefs(src)
        except (ui_prefs.UiPrefsError, OSError, RecursionError) as e:
            raise SourceComponentUnsound(
                f"{label} in this snapshot would be refused by its reader ({e}).\n"
                "   Refusing to restore it: the dashboard would read it as no saved "
                "browser settings, or refuse every later settings save."
            ) from e
        _refuse_dropped_entries(label, dropped)
        return
    if validator == "notification_settings":
        from kiro_crew.notifications import settings as notification_settings

        try:
            _, dropped = notification_settings.parse_imported_settings(
                src.read_text(encoding="utf-8")
            )
        except (
            notification_settings.ChannelSettingsError,
            OSError,
            ValueError,
            RecursionError,
        ) as e:
            raise SourceComponentUnsound(
                f"{label} in this snapshot would be refused by its reader ({e}).\n"
                "   Refusing to restore it: the notification store would read it as no "
                "saved mutes or priorities."
            ) from e
        _refuse_dropped_entries(label, dropped)
        return
    raise AssertionError(f"no validator named {validator!r}")


class RollbackIncomplete(OSError):
    """The restore failed AND putting the previous state back did not fully succeed.

    Distinct from the restore failure itself, because the two need opposite messages: one
    says "you are back where you started", the other says "some of your previous state is
    only in the rollback directory now". Reporting the first when the second is true is
    the worst of the three outcomes -- the operator stops looking.
    """

    def __init__(
        self,
        cause: BaseException,
        failed: list[str],
        backup: Path,
        *,
        store_backup: Path | None = None,
    ) -> None:
        self.cause = cause
        self.failed = failed
        self.backup = backup
        self.store_backup = store_backup
        super().__init__(str(cause))


class NamedStoresInUse(Exception):
    """A named memory store the replace would remove is open in some process right now.

    Raised before any live state moves. Replace removes each store directory and refills
    it, and on POSIX an open SQLite handle survives that removal pointing at an unlinked
    file: the process keeps writing memory nothing will ever open again. The gateway
    check at the top of ``restore`` catches the ordinary case; this catches the one it
    cannot -- an import applied INSIDE the running gateway, or a second process holding a
    store -- by taking every store's lifetime lock exclusively for the whole replace
    (``member_memory_backup.hold_stores_for_replace``), which fails at once for a store
    that is open and holds a store that tries to open until the replace is done.
    """

    def __init__(self, names: list[str]) -> None:
        self.names = names
        super().__init__(
            "named memory store(s) "
            + ", ".join(_safe_name(n) for n in names)
            + " are open right now, so replacing memory_stores/ would leave their live "
            "writes in an unlinked database. Nothing has been changed. Stop the gateway "
            "(kirocrew stop) and any other process using them, then re-run."
        )


class SourceComponentUnsound(Exception):
    """An incoming component in a bundle is unsound, so nothing may be restored from it.

    Covers both kinds of unsoundness this path can detect before mutating: a database
    that fails its integrity check, and a component JSON whose reader would treat it as
    empty. Both share one boundary handler because both mean the same thing to the
    operator — the bundle cannot be applied — and neither should surface as a traceback.

    Raised before any live state moves, because the point of the check is that it still
    costs nothing to decline.
    """


def _trees_absent_from_bundle(snap: Path, names: list[str], mc: Path) -> list[str]:
    """Return the declared trees of *names* that *snap* lacks while *mc* HAS them.

    Per COMPONENT, presence is "any declared path is there" (a memory bundle may lack
    ``memory_index.db``). Per TREE under ``--mode replace`` that leniency is destructive:
    :func:`_do_replace_mutations` clears the destination BEFORE it knows whether the archive
    has a replacement, so one present file makes the component look carried while an
    absent tree is cleared and the restore reports success.

    Both halves are needed: a home that never used a tree produces a bundle without it, so
    the loss is only real when the live side HAS the tree. Not applied to a complete
    bundle, where a missing tree is one the source did not have; only a partial bundle with
    no component map cannot tell the two apart.
    """
    absent = []
    for name in names:
        for tree in COMPONENTS[name].trees:
            d = mc / tree
            # Through the chokepoint like every other tree-root site. A live root that
            # redirects elsewhere, or escapes the home, is not the tree this component
            # declared, so it must not count as state worth refusing to protect -- and
            # deciding that from `is_dir()` alone would follow the link.
            if safe_tree_root(d, what="destination root", home=mc) is None:
                continue
            if not (snap / tree).is_dir() and d.is_dir():
                absent.append(tree)
    return absent


def _components_absent_from_bundle(snap: Path, names: list[str]) -> list[str]:
    """Return the *names* whose declared paths are all missing from *snap*.

    Fallback for a root marked partial whose manifest has no component map. Naming a
    component is not evidence the bundle holds it, and replace mode moves live files aside
    and clears trees before it knows whether the archive has replacements.

    Presence is "any declared path is there", not "all of them": a memory bundle from a
    home that never wrote ``memory_index.db`` carries only ``memory.db`` and is sound.
    """
    absent = []
    for name in names:
        spec = COMPONENTS[name]
        carried = any((snap / f).is_file() for f in spec.files) or any(
            (snap / t).is_dir() for t in spec.trees
        )
        if not carried:
            absent.append(name)
    return absent


def _backup_and_copy(
    mc: Path,
    backup: Path,
    snap: Path,
    component: str,
    *,
    allow_unpinned: bool = False,
    installed: set[str] | None = None,
) -> None:
    """Move the live core files aside, then restore the archive's, destination pinned.

    *installed*, when given, is the rollback ledger. Only this function adds to it, one
    name immediately before that file's first mutation, because recovery reads membership
    as "this run reached this path". Adding every DECLARED file up front would mark files
    the bundle skips, and recovery would delete the operator's untouched copies.

    The data home is pinned once and each file is created relative to that descriptor with
    ``O_EXCL``, not by name through ``shutil.copy2``: a by-name write follows a data-home
    component swapped for a link, or a symlink left at the core file's name. A name still
    occupied after the backup move is refused instead of written through.
    """
    if not _facade()._staging_is_pinned(
        allow_unpinned=allow_unpinned, what=f"restore of {component!r}"
    ):
        for f in CORE_FILES.get(component, ()):
            # Validated before the live file is touched, and a symlink at the live name is
            # MOVED aside rather than skipped -- the same two properties the pinned branch
            # has. Skipping would skip both the backup AND the replacement, so the archive's
            # file would never be applied and the command would report success anyway.
            # Moving a symlink moves the link, never its target.
            if not (snap / f).is_file() or pinned_fs.is_reparse_point(snap / f):
                if (snap / f).exists():
                    print(f"⚠️  Skipping symlinked file from snapshot: {snap / f}")
                continue
            # Past the skip, so this file WILL be mutated. Recorded now, before the move
            # below, so a crash mid-write still leaves the name known to have been reached
            # -- and, just as importantly, a file skipped above is never recorded at all.
            if installed is not None:
                installed.add(f)
            live = mc / f
            if live.is_symlink() or pinned_fs.is_reparse_point(live):
                print(f"⚠️  Moving symlinked core file aside during backup: {live}")
                shutil.move(str(live), str(backup / f))
            elif live.is_file():
                shutil.move(str(live), str(backup / f))
            # Not `copy2`, which opens the destination by name and follows a symlink
            # planted after the move; copy_file_pinned uses O_CREAT|O_EXCL|O_NOFOLLOW.
            # Skips are fatal: the live file is already in the backup, so a skip would
            # lose it AND never apply the archive's version. A collision means something
            # recreated the name; refuse with a message pointing at the backup.
            try:
                pinned_fs.copy_file_pinned(
                    str(snap / f),
                    str(mc / f),
                    on_skip=pinned_fs.fatal_skip_reporter(f"restore of {f!r}"),
                )
            except FileExistsError as exc:
                raise pinned_fs.PinnedPathRefusal(
                    f"refusing to restore {f!r}: its name was recreated while the restore "
                    "was running, so writing it would overwrite that file. The previous "
                    f"version is in {backup}. Re-run with the gateway stopped."
                ) from exc
            _lock_down_restored(mc / f, component)
            _endorse_restored(mc / f, functools.partial(Path.read_bytes, snap / f))
        return

    src_fd = pinned_fs.open_dir_pinned(snap, what=f"snapshot payload for {component!r}")
    try:
        dst_fd = pinned_fs.open_dir_pinned(mc, what=f"data home for {component!r}")
        try:
            backup_fd = pinned_fs.create_and_open_dir_pinned(
                backup, what=f"pre-restore backup for {component!r}"
            )
            try:
                for f in CORE_FILES.get(component, ()):
                    live = mc / f
                    # Checked BEFORE the live file is touched: the archive is untrusted, and
                    # finding a FIFO or directory only after the move would leave nothing
                    # restored, reported as success. Asked through src_fd so a snapshot
                    # root swapped after pinning cannot redirect the check.
                    if not pinned_fs.is_regular_at(src_fd, f):
                        continue
                    # Same point as the fallback branch: past the skip, so this file is
                    # about to be mutated and is recorded BEFORE the move. A file the
                    # bundle does not carry never reaches here, so recovery correctly reads
                    # it as never touched and leaves it alone.
                    if installed is not None:
                        installed.add(f)
                    # A symlink at a core file's name is MOVED aside (the link, never its
                    # target), not skipped: skipping would let the copy write through it,
                    # or leave the archive's file unrestored. os.rename through both pinned
                    # descriptors cannot be redirected by a swapped ancestor, and is atomic.
                    live_st = pinned_fs.stat_at(dst_fd, f)
                    if live_st is not None and _stat.S_ISLNK(live_st.st_mode):
                        print(f"⚠️  Moving symlinked core file aside during backup: {live}")
                        os.rename(f, f, src_dir_fd=dst_fd, dst_dir_fd=backup_fd)
                    elif live_st is not None and _stat.S_ISREG(live_st.st_mode):
                        os.rename(f, f, src_dir_fd=dst_fd, dst_dir_fd=backup_fd)
                    try:
                        copied = pinned_fs.copy_file_pinned(
                            str(snap / f),
                            dir_fd=src_fd,
                            name=f,
                            dst_dir_fd=dst_fd,
                            dst_name=f,
                            # Owner-only applied through the destination DESCRIPTOR in the
                            # same call: a by-name lockdown could chmod a swapped-in link's
                            # target, and the archive's own mode is untrusted (a tarball
                            # can record 0o777 on telemetry_salt).
                            force_mode=(
                                0o600
                                if component == "security" or f in _OWNER_ONLY_SETTINGS_FILES
                                else None
                            ),
                            # The live file is already moved aside, so a skip here would
                            # lose it AND never write the archive's version.
                            on_skip=pinned_fs.fatal_skip_reporter(f"restore of {f!r}"),
                        )
                    except FileExistsError as exc:
                        raise pinned_fs.PinnedPathRefusal(
                            f"refusing to restore {f!r}: something still occupies that "
                            "name in the data home after the backup pass, so it is a "
                            "hardlink alias or a name this restore could not move "
                            "aside. Writing to it could follow whatever it points at. "
                            "Remove it and re-run."
                        ) from exc
                    if copied and component == "security":
                        # Nothing to re-apply: force_mode above already set owner-only
                        # through the descriptor. On Windows the by-name branch still
                        # needs restrict_to_owner for its DACL, which is why that call
                        # survives there and not here.
                        pass
                    if copied:
                        _endorse_restored(live, functools.partial(_read_at, src_fd, f))
            finally:
                os.close(backup_fd)
        finally:
            os.close(dst_fd)
    finally:
        os.close(src_fd)


def _endorse_restored(live: Path, read: Callable[[], bytes]) -> None:
    """Endorse the archive bytes just installed at *live* when it is a sealed keystone file.

    A restore is the owner's own write, so the restored ``config.json`` /
    ``config.local.json`` takes effect instead of being held as pending behind the
    endorsed copy of the file it replaced (``config.loosening_provenance``). *read* reads
    the SOURCE, never the installed destination. Anything else is left alone, unread.
    """
    if loosening_provenance.leaf_for(live) is not None:
        loosening_provenance.endorse_installed(live, read())


def _read_at(dir_fd: int, name: str) -> bytes:
    """The bytes of regular file *name* under the pinned *dir_fd*, never following a link."""
    fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
    with os.fdopen(fd, "rb") as handle:
        return handle.read()


#: Settings documents whose own writers create them owner-only. The config overlay can
#: hold channel tokens, so a restored copy must not inherit a looser directory ACL
#: on Windows, or the umask default on POSIX, from the copy that installed it.
_OWNER_ONLY_SETTINGS_FILES = frozenset({"config.json", "config.local.json", "ui-prefs.json"})


def _lock_down_restored(path: Path, component: str) -> None:
    """Apply the owner-only lockdown a restored security or settings file needs.

    restrict_to_owner (fail-loud), NOT chmod_safe (which swallows OSError): security
    files include sel_hmac.key. Mirrors the create path's deliberate fail-loud
    lockdown -- better to abort than silently land a restored secret group- or
    world-readable. POSIX applies chmod 0o600; Windows applies an owner-only DACL
    in-process. The freshly copied file is unlinked on failure so the abort this promises
    actually removes the exposed artifact, instead of leaving the restored secret
    under the destination's inherited DACL after the OSError propagates.
    """
    if component != "security" and path.name not in _OWNER_ONLY_SETTINGS_FILES:
        return
    try:
        platform_compat.restrict_to_owner(str(path))
    except OSError:
        path.unlink(missing_ok=True)
        raise


def _backup_tree_or_refuse(
    src: Path,
    dst: Path,
    *,
    allow_unpinned: bool = False,
    ignore: Callable[[str, list[str]], set[str]] | None = None,
) -> None:
    """Back a live tree up, and refuse the replace if the backup is not complete.

    Replace mode later runs ``rmtree`` on the live tree, so a file the backup pass
    SKIPPED is a file the restore is about to delete with no copy anywhere -- and the
    call site is deliberately hoisted ahead of every live mutation, so a refusal
    arrives while nothing has been swapped yet. The staging
    walk legitimately skips a hardlink alias, a symlink and a non-regular file -- which
    is right when producing an archive and catastrophic here, because the skip is
    followed by a delete rather than by an omission.

    Without the refusal, a concurrent writer's hardlinks are skipped at backup time and
    then ``rmtree`` removes the only copies. Refusing before the delete is the only
    ordering that cannot lose data: the operator keeps a complete tree and a message
    naming what could not be copied.
    """
    _facade()._copytree_safe(
        src,
        dst,
        allow_unpinned=allow_unpinned,
        ignore=ignore,
        on_skip=pinned_fs.fatal_skip_reporter(f"backup of {src.name!r} before replacing it"),
    )


def _refuse_unsafe_destination_roots(mc: Path, components: list[str] | None) -> None:
    """Refuse before touching anything if a selected component's tree root is unsafe.

    Hoisted ahead of every mutation on purpose. Checking inside the per-tree loops was
    too late in the worst way: `_backup_and_copy` has already swapped the databases by
    then, so skipping an unsafe markdown tree left memory split between two versions —
    and the command still reported success. A partial restore reported as complete is
    the same lie as a partial backup reported as complete.

    Both restore modes call this. Merge is additive and destroys nothing, but a merge
    that silently omits a tree is still a merge that claims to have imported it.
    """
    offenders = []
    for comp in COMPONENTS:
        if not _want(components, comp):
            continue
        for tree in COMPONENTS[comp].trees:
            d = mc / tree
            if safe_tree_root(d, what="destination root", home=mc) is None:
                offenders.append(f"{comp}:{tree}")
    if offenders:
        raise UnsafeComponentRoot(
            "these destination trees do not resolve inside the data home: "
            + ", ".join(offenders)
            + ". Nothing has been changed. Inspect those paths (usually a symlink) "
            "and re-run — restoring past them would leave memory split between the "
            "old and new versions while reporting success."
        )


def _refuse_corrupt_source_databases(
    snap: Path,
    components: list[str] | None,
    *,
    mc_for_merge: Path | None,
) -> None:
    """Refuse a bundle whose incoming components are unsound, BEFORE any live state moves.

    A bundle is untrusted input, so it is validated while refusing still costs nothing;
    checking after the install would leave the home on a corrupt database.

    The condition is "does this restore read or install the file", not "is this replace
    mode". Replace installs everything (*mc_for_merge* is ``None``); merge is per-file, see
    `_merge_reads`. Every incoming database for the SELECTED components is checked.

    Unreadable counts as unsound: tolerable when *creating* a snapshot, wrong when the
    file is about to BECOME the operator's memory.
    """

    def _merge_reads(rel: str) -> bool:
        """Whether MERGE reads or installs *rel*, so validation has to cover it.

        "Is the destination missing" is not enough in two places:

        * `crons.json` is PARSED when a local one exists (`_merge_crons` loads both sides)
          and copied when it does not, so merge always reads it.
        * `memory_index.db` is copied alongside `memory.db` exactly when the live
          `memory.db` is ABSENT, whatever the index's own destination looks like.

        Named stores are installed only where the whole store is absent. Other tree
        files are installed only where their own destination is missing.
        """
        assert mc_for_merge is not None
        if rel == "crons.json":
            return True
        if rel == "memory_index.db":
            return not (mc_for_merge / "memory.db").exists()
        parts = PurePosixPath(rel).parts
        if len(parts) >= 2 and parts[0] == MEMORY_STORES_DIR_NAME:
            return not (mc_for_merge / MEMORY_STORES_DIR_NAME / parts[1]).exists()
        return not (mc_for_merge / rel).exists()

    def _will_install(rel: str) -> bool:
        if mc_for_merge is None:
            return True  # replace installs everything the bundle carries
        return _merge_reads(rel)

    for component, files in CORE_FILES.items():
        if not _want(components, component):
            continue
        for name in files:
            src = snap / name
            if not src.exists() and not platform_compat.is_link_or_junction(src):
                continue  # absent from a selective bundle; nothing to validate
            if not _will_install(name):
                continue
            # "Not a file" is NOT the same as "not there". A directory (or a symlink)
            # occupying a declared file's name would otherwise read as absent, skip every
            # check below, and then let replace move the operator's live copy aside and
            # report success having restored nothing in its place.
            if not src.is_file() or platform_compat.is_link_or_junction(src):
                raise SourceComponentUnsound(
                    f"{name} in this snapshot is not a regular file.\n"
                    "   Refusing to restore: a declared component file that is a "
                    "directory or a link cannot replace the live one."
                )
            if name.endswith((".db", ".sqlite3")):
                _refuse_unless_sound(src, name, strict=True)
            elif name in COMPONENT_JSON_OBJECTS:
                # "Will this file reach a consumer?", not "is this a replace?". Merge copies
                # the bundle's file verbatim when the destination is ABSENT (the fresh
                # machine case), and a JSON array there makes the cron loader read zero jobs.
                will_install = mc_for_merge is None or not (mc_for_merge / name).is_file()
                _refuse_unless_json_object(src, name, installed=will_install)
                if will_install and name in _FLAT_DOCUMENT_VALIDATORS:
                    _refuse_unless_valid_tree_document(src, name, _FLAT_DOCUMENT_VALIDATORS[name])

    # A document inside a component tree is validated by ITS OWN reader. The tree is copied
    # wholesale on replace and file-by-file where the destination lacks the file on merge,
    # so "installed" is the same question as for a flat file; only an installed document
    # is checked, because only an installed one reaches the reader.
    for component, rel, validator in _TREE_DOCUMENT_VALIDATORS:
        if not _want(components, component):
            continue
        src = snap / rel
        if not src.exists() and not platform_compat.is_link_or_junction(src):
            continue  # absent from a selective bundle; nothing to validate
        # A directory or a link standing at the document's name is refused outright, in
        # every mode, exactly as for a declared flat file above: read as "absent" it would
        # let the install displace the live document with something no reader can open.
        if not src.is_file() or platform_compat.is_link_or_junction(src):
            raise SourceComponentUnsound(
                f"{rel} in this snapshot is not a regular file.\n"
                "   Refusing to restore: a document that is a directory or a link cannot "
                "replace the live one."
            )
        if mc_for_merge is None or not (mc_for_merge / rel).is_file():
            _refuse_unless_valid_tree_document(src, rel, validator)

    # Component TREES carry databases too (`workspace/knowledge/knowledge.db`). Strictness
    # is per PATH: a database this product owns is strict wherever it lives, while an
    # incidental `.db` in a tree (a Windows `Thumbs.db`) may legitimately not be SQLite.
    for component, trees in COMPONENT_TREES.items():
        if not _want(components, component):
            continue
        for tree in trees:
            root = snap / tree
            if not root.exists() and not platform_compat.is_link_or_junction(root):
                continue  # absent from a selective bundle; nothing to validate
            if not root.is_dir() or platform_compat.is_link_or_junction(root):
                raise SourceComponentUnsound(
                    f"{tree} in this snapshot is not a directory.\n"
                    "   Refusing to restore: a declared component tree that is a file "
                    "or a link cannot replace the live one."
                )
            for src in sorted(root.rglob("*")):
                # Sidecars (`.db-wal`, `.db-shm`) do not match these suffixes, so they
                # need no separate exclusion.
                if not src.is_file() or not src.name.endswith((".db", ".sqlite3")):
                    continue
                rel = src.relative_to(snap).as_posix()
                if not _will_install(rel):
                    continue
                _refuse_unless_sound(src, rel, strict=is_product_tree_database(rel))


def _refuse_unless_json_object(src: Path, label: str, *, installed: bool) -> None:
    """Raise unless *src* parses as a JSON object.

    The consumers of these files read an unreadable one as EMPTY (`crons.json`'s loader
    falls back to "no jobs" on a parse error or an array), so installing a corrupt file
    silently discards content while the restore reports success.

    *installed* is decided by the caller from "will this reach a consumer". When it is
    False (merge with a live copy), the merger reports an unparseable file and writes
    nothing, so refusing would only fail every other component of the restore.

    Only structure is checked, not schema: field-level checks would duplicate each
    consumer's validation and refuse bundles those consumers accept.
    """
    try:
        parsed = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as e:
        if not installed:
            return
        raise SourceComponentUnsound(
            f"{label} in this snapshot could not be read as JSON ({e}).\n"
            "   Refusing to restore it over live state: its reader treats an unreadable "
            "file as an empty one, so this would discard content silently."
        ) from e
    # Everything past here is an INSTALL-path check, gated once so a later check cannot
    # forget the condition. The merge path is guarded by `_merge_crons`, whose
    # `_usable_cron_shape` check on BOTH sides is a superset of these; the install path has
    # no other guard.
    if not installed:
        return
    if not isinstance(parsed, dict):
        raise SourceComponentUnsound(
            f"{label} in this snapshot is a JSON {type(parsed).__name__}, not an "
            "object.\n"
            "   Refusing to restore it over live state: its reader expects an object and "
            "treats anything else as empty."
        )
    # Parsing succeeds well past the depth the recursive readers (the config cache's
    # deepcopy, the overlay deep-merge) can walk, so an over-deep document would install
    # and then make every later load raise RecursionError.
    if exceeds_nesting(parsed):
        raise SourceComponentUnsound(
            f"{label} in this snapshot nests deeper than {MAX_DOCUMENT_NESTING} levels.\n"
            "   Refusing to restore it over live state: its reader cannot walk a "
            "document that deep and would fail on every load."
        )
    # An object at the top is necessary and not sufficient: the readers iterate a named
    # list and call `.get` on each entry, so a `jobs` that is not a list of objects
    # reaches attribute access on a `str` and raises mid-restore.
    for key in _JSON_OBJECT_LISTS.get(src.name, ()):
        if key not in parsed:
            continue
        entries = parsed[key]
        if not isinstance(entries, list):
            raise SourceComponentUnsound(
                f"{label} in this snapshot has '{key}' as a JSON "
                f"{type(entries).__name__}, not a list.\n"
                "   Refusing to restore it over live state: its reader iterates that "
                "key and would fail partway through."
            )
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise SourceComponentUnsound(
                    f"{label} in this snapshot has '{key}[{i}]' as a JSON "
                    f"{type(entry).__name__}, not an object.\n"
                    "   Refusing to restore it over live state: its reader reads fields "
                    "off each entry and would fail partway through."
                )


def _refuse_unless_sound(src: Path, label: str, *, strict: bool) -> None:
    """Raise unless *src* is a sound SQLite database.

    *strict* decides what an unopenable file means: a refusal for a database this product
    declares by name, and nothing at all for a `.db` found inside an operator's own tree,
    which may legitimately not be SQLite.
    """
    # SQLite treats a ZERO-BYTE file as a valid, empty database: it opens, and
    # `integrity_check` answers `ok`. So the check below cannot see the difference between
    # a healthy database and a snapshot that captured nothing, and replace mode would
    # install empty memory over live memory and report success. Size is the only place
    # that distinction is visible, so it is read before the file is opened.
    try:
        if src.stat().st_size == 0:
            raise SourceComponentUnsound(
                f"{label} in this snapshot is EMPTY (zero bytes). SQLite opens such a "
                "file as a valid empty database, so this would replace your live data "
                "with nothing and report success.\n"
                "   Refusing to restore it. Take a fresh snapshot."
            )
    except OSError as e:
        if not strict:
            return
        raise SourceComponentUnsound(
            f"{label} in this snapshot could not be read ({e}).\n"
            "   Refusing to restore it over live state."
        )
    try:
        with closing(_facade().sqlite3.connect(str(src))) as conn:
            result = conn.execute("PRAGMA integrity_check;").fetchone()[0]
    except _facade().sqlite3.Error as e:
        if not strict:
            return  # not a database; not this code's business
        raise SourceComponentUnsound(
            f"{label} in this snapshot: integrity check failed — it cannot be "
            f"opened as a database ({e}).\n"
            "   Refusing to restore it over live state."
        ) from e
    if result != "ok":
        raise SourceComponentUnsound(
            f"{label} in this snapshot: integrity check failed ({result}).\n"
            "   Refusing to restore it over live state."
        )


def _allocate_rollback_dir(mc: Path) -> Path:
    """Create a rollback directory that is this restore's alone.

    The timestamp is second-granular, so two restores inside one second would otherwise
    share a directory. That is not a naming nicety: the tree saves below refuse to write
    into an existing destination on purpose — one rollback set holding files from two
    restores rolls back to neither generation — so a shared directory turned the second
    restore into an uncaught `FileExistsError` instead of a clean refusal.

    `mkdir` without `exist_ok` is the allocation: it is atomic, so the winner of a race
    gets the name and the loser moves to the next suffix rather than both proceeding.
    """
    ts = _facade().datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for attempt in range(1, 64):
        name = f"pre-restore-{ts}" if attempt == 1 else f"pre-restore-{ts}-{attempt}"
        candidate = mc / name
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise SourceComponentUnsound(
        f"could not allocate a rollback directory under {mc} — "
        f"'pre-restore-{ts}' and 63 suffixed variants all exist.\n"
        "   Refusing to restore without somewhere to save the current state."
    )


def _discard_partial_rollback(*dirs: Path | None) -> None:
    """Remove rollback directories a refused phase one only partly filled.

    Only ever called before any live mutation, so the directories hold copies and
    nothing else. One that cannot be removed is named, so it is not left unexplained.
    """
    for d in dirs:
        if d is None:
            continue
        try:
            shutil.rmtree(str(d))
            print(f"  Removed partial rollback copy: {d}/ (nothing was changed)")
        except FileNotFoundError:
            pass
        except OSError:
            print(
                f"  ⚠️  Partial rollback copy left at: {d}/ (nothing was changed; "
                "it is safe to delete)"
            )


def _do_replace(
    snap: Path, mc: Path, components: list[str] | None, *, allow_unpinned: bool = False
) -> None:
    """Replace the selected components, with a complete rollback set taken first.

    Two phases. Phase one copies every tree this run will mutate into a fresh rollback
    directory and mutates nothing; phase two performs every mutation. A refusal in phase
    one leaves the data home untouched, and a failure in phase two reverts from a rollback
    set known to be complete. The core-file swap must not run before phase one finishes.
    """
    facade = _facade()
    # Before anything is created or copied: a destination tree root that does not resolve
    # inside the data home would have the restore write outside it.
    facade._refuse_unsafe_destination_roots(mc, components)
    backup = facade._allocate_rollback_dir(mc)
    print("🔄 Replace mode — backing up current state...")

    # `memory` names two subtrees of workspace/ plus memory_stores/. When `workspace` is
    # also selected its own pass covers the two subtrees, and doing both would save the
    # INCOMING memory over the saved original -- so those are dropped from this list
    # exactly when a `workspace` tree contains them. memory_stores/ is under no other
    # component's tree and stays on the list whatever else was selected.
    mem_roots: list[tuple[str, Path]] = []
    if _want(components, "memory"):
        covered_by_workspace = (
            COMPONENTS["workspace"].trees if _want(components, "workspace") else ()
        )
        unsafe_now = []
        for tree in COMPONENTS["memory"].trees:
            if any(
                PurePosixPath(tree).is_relative_to(PurePosixPath(other))
                for other in covered_by_workspace
            ):
                continue
            if tree == MEMORY_STORES_DIR_NAME and not _bundle_carries_named_stores(snap):
                # Not saved, not cleared, not on the recovery target list: the archive is
                # silent about this tree because its writer did not know it, not because
                # the source had none, and replacing on that silence would erase every
                # crew's private memory. Said out loud, because a replace that leaves one
                # tree at the newer generation is a mixed result the operator should see.
                print(
                    f"  ↩️  {tree}/ is not carried by this archive (written before named "
                    "memory stores were backed up) — the live named stores are left as "
                    "they are"
                )
                continue
            d = mc / tree
            if safe_tree_root(d, what="destination root", home=mc) is None:
                # REFUSED, not skipped: the preflight cleared this set moments ago, so the
                # root moved mid-run. A `continue` would drop the tree from `mem_roots`,
                # neither saved nor restored, while the run reports "Replace complete."
                # Safe to raise: this runs before phase one, so nothing has been mutated.
                unsafe_now.append(f"memory:{tree}")
            else:
                mem_roots.append((tree, d))
        if unsafe_now:
            raise UnsafeComponentRoot(
                "these destination trees stopped resolving inside the data home after the "
                "pre-flight check passed: " + ", ".join(unsafe_now) + ". Nothing has been "
                "changed. A path that was safe moments ago and is not now was replaced "
                "mid-run (usually a symlink), so restoring past it would leave memory "
                "split between the old and new versions while reporting success."
            )

    # Still before phase one. A store open elsewhere would survive the removal below as an
    # unlinked file and lose every later write, so every store's lifetime lock is taken
    # EXCLUSIVELY here and held until the replace -- or its rollback -- is done: a store that
    # is open refuses the replace at once, and a store opened while this is held waits for
    # the replace to finish and then opens what it put there. Live and archived store
    # names both, so a store the archive introduces is held before it can be found.
    store_names: set[str] = set()
    barrier = ExitStack()
    try:
        if any(tree == MEMORY_STORES_DIR_NAME for tree, _ in mem_roots):
            barrier.enter_context(memory_store_namespace_lock(mc / MEMORY_STORES_DIR_NAME))
            for root in (mc / MEMORY_STORES_DIR_NAME, snap / MEMORY_STORES_DIR_NAME):
                if root.is_dir():
                    store_names.update(
                        p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")
                    )
        barrier.enter_context(
            facade.hold_stores_for_replace(mc / MEMORY_STORES_DIR_NAME, store_names)
        )
    except BaseException as exc:
        barrier.close()
        backup.rmdir()
        if isinstance(exc, StoresInUse):
            raise NamedStoresInUse(exc.names) from exc
        raise
    store_backup: Path | None = None
    phase_one_done = False
    try:
        # ── Phase one: the rollback set. No live state is mutated in this block. ──
        #
        # `_backup_tree_or_refuse` reports a skipped entry as FATAL, so a tree that cannot be
        # copied whole raises here rather than being rmtree'd later with an incomplete backup.
        for tree, d in mem_roots:
            if d.is_dir():
                if tree == MEMORY_STORES_DIR_NAME:
                    # Private memory stays behind the same agent fence as its live copy.
                    # Only root-level host state stays in place; V1 backups inside a
                    # store must be saved because the mutation removes that directory.
                    local_backups = mc.resolve() / tree / MEMBER_BACKUPS_DIR_NAME
                    if local_backups.resolve() != local_backups.absolute():
                        raise UnsafeComponentRoot("named store rollback directory is redirected")
                    platform_compat.make_owner_only_dir(local_backups)
                    store_backup = cast(Path, facade._allocate_rollback_dir(local_backups))

                    def ignore_host_local_root(directory: str, contents: list[str]) -> set[str]:
                        if Path(directory) != d:
                            return set()
                        return {
                            name
                            for name in contents
                            if is_host_local_store_state((MEMORY_STORES_DIR_NAME, name))
                        }

                    _backup_tree_or_refuse(
                        d,
                        store_backup,
                        allow_unpinned=allow_unpinned,
                        ignore=ignore_host_local_root,
                    )
                    print(f"  Previous state saved to: {store_backup}/")
                    continue
                # `tree` is NESTED (`workspace/memory`) and the pinned primitive does not
                # create a missing parent chain.
                (backup / tree).parent.mkdir(parents=True, exist_ok=True)
                _backup_tree_or_refuse(d, backup / tree, allow_unpinned=allow_unpinned)
        for comp in _WHOLE_TREE_COMPONENTS:
            if not _want(components, comp):
                continue
            for dirname in COMPONENTS[comp].trees:
                d = mc / dirname
                if dirname in _LOCKED_DOCUMENT_TREES:
                    # Saved in phase two instead, inside the lock hold that replaces it
                    # (`_save_locked_document_to`): a copy taken here could miss a team
                    # write committed before the install. Only the document is ever saved;
                    # the directory and its lock file are never touched.
                    continue
                # Saved only when phase two will REPLACE it (the archive carries the tree);
                # otherwise a rollback could overwrite a dashboard write to an unopened tree.
                if d.is_dir() and (snap / dirname).is_dir():
                    _backup_tree_or_refuse(d, backup / dirname, allow_unpinned=allow_unpinned)
        phase_one_done = True

        # Every relative path phase two can write. Recovery needs it because a target that did
        # not exist before the restore has nothing saved for it, so putting saved entries back
        # would leave that creation standing.
        targets: list[str] = []
        for comp in _CORE_FILE_COMPONENTS:
            if _want(components, comp):
                targets.extend(COMPONENTS[comp].files)
        for comp in _WHOLE_TREE_COMPONENTS:
            if _want(components, comp):
                targets.extend(
                    _LOCKED_DOCUMENT_TREES.get(tree, tree) for tree in COMPONENTS[comp].trees
                )
        targets.extend(tree for tree, _ in mem_roots)

        # Grows as phase two touches each target; recovery reads it to tell a creation from a
        # target the phase never reached.
        installed: set[str] = set()
        try:
            # A failed replace puts the previous files back, so the endorsed copies of
            # the keystone files it restored go back with them.
            with loosening_provenance.endorsements_restored_on_error():
                facade._do_replace_mutations(
                    snap,
                    mc,
                    backup,
                    components,
                    mem_roots,
                    installed,
                    allow_unpinned=allow_unpinned,
                )
        except BaseException as e:
            # `BaseException`, not `Exception`: `PinnedPathRefusal` and `KeyboardInterrupt`
            # both fire MID-mutation, and a Ctrl-C would otherwise leave live state half
            # replaced with no rollback. Always re-raised; the previous state is put back
            # first. Phase one is outside this try: it mutates nothing.
            failed = facade._restore_everything_from_rollback(
                backup,
                mc,
                targets,
                installed,
                allow_unpinned=allow_unpinned,
                store_backup=store_backup,
            )
            if failed:
                # The revert is part of the outcome, not a side effect of it. Re-raising the
                # original error alone would let the caller summarise this as "you are back
                # where you started", which is the one thing that must not be said when some
                # of the previous state now exists only in the rollback directory.
                raise RollbackIncomplete(e, failed, backup, store_backup=store_backup) from e
            raise
    except BaseException:
        # A refusal in phase one leaves live state untouched, so the half-written
        # rollback copies hold nothing the operator needs. Remove them rather than
        # leave one `pre-restore-<ts>/` per refused attempt in the data home.
        if not phase_one_done:
            _discard_partial_rollback(backup, store_backup)
        raise
    finally:
        barrier.close()

    try:
        backup.rmdir()
    except OSError:
        print(f"  Previous state saved to: {backup}/")
    print("✅ Replace complete.")


def _component_payload_absent(snap: Path, component: str) -> bool:
    """Does *snap* carry nothing this component could actually restore?

    Declared-but-hollow: the manifest says a component rode and the bundle holds none of its
    data, so replace would clear live state with nothing to put back.

    A DERIVED index is not payload. A tree counts only when it holds at least one FILE: the
    staging walk can leave an empty directory (``memory_stores/`` holding only sandbox
    runtime state), which is nothing to restore. Derived from `COMPONENTS`, never a
    hand-written list.
    """
    spec = COMPONENTS.get(component)
    if spec is None:
        return False  # not a component this function knows how to judge; do not refuse on it
    for rel in getattr(spec, "files", ()) or ():
        if rel in _DERIVED_INDEXES:
            continue
        if (snap / rel).exists():
            return False
    for rel in getattr(spec, "trees", ()) or ():
        root = snap / rel
        if root.is_dir() and any(p.is_file() for p in root.rglob("*")):
            return False
        if root.exists() and not root.is_dir():
            return False  # a non-directory at a tree's name is judged by the soundness check
    return True


def _drop_derived_indexes_absent_from_bundle(
    snap: Path, mc: Path, backup: Path, installed: set[str]
) -> None:
    """Remove a live derived index the archive does not carry, saving it first.

    Replace means the destination ends up matching the archive. `_backup_and_copy` skips a
    file the archive lacks, so a stale live index would keep indexing the PREVIOUS memory.
    The redaction pass drops `memory_index.db` from off-host bundles, so this is the
    ordinary case. Removing it lets the rebuild warning fire. Scoped to derived indexes: a
    missing payload database is refused elsewhere, not deleted.
    """
    for rel in sorted(_DERIVED_INDEXES):
        if (snap / rel).is_file():
            continue  # the archive carries it; the ordinary copy path applies
        live = mc / rel
        if not (live.is_file() or live.is_symlink() or platform_compat.is_link_or_junction(live)):
            continue
        # Recorded immediately before the move, never earlier: the recovery leg reads
        # membership as "this run reached this path", and the move below IS the save it
        # needs. Adding the name before a save is known is what let recovery delete an
        # occupant it had never saved.
        installed.add(rel)
        if platform_compat.is_link_or_junction(live) and not live.is_symlink():
            # A junction is a directory reparse point; `shutil.move` on it is not the
            # pairing this repo uses. Remove the link itself -- there is nothing to save,
            # because the link's target is not ours and stays where it is.
            platform_compat.unlink_link_or_junction(live)
        else:
            shutil.move(str(live), str(backup / rel))
        print(f"  ↩️  {rel} is not in the archive — moved aside so it can be rebuilt")


def _rmtree_or_raise(path: Path) -> None:
    """Remove a tree, read-only files included, and raise when any of it is left.

    Windows refuses to unlink a read-only file, and git writes its objects that way;
    ``rmtree_force`` clears the bit but never raises, so a surviving tree raises here.
    POSIX keeps the descriptor-walking ``shutil.rmtree``: the mode never blocks an unlink
    there, and the hook's retry by full path could follow a swapped symlink.
    """
    if platform_compat.IS_POSIX:
        shutil.rmtree(path)
    elif not platform_compat.rmtree_force(path):
        raise OSError(f"could not remove {path}")


def _clear_store_directories(root: Path) -> None:
    """Remove everything under ``memory_stores/`` that a bundle can carry, keep the rest.

    What stays is exactly `is_host_local_store_state`'s answer for a direct child: the
    historical host credential and runtime-log filenames, and the member backup directory --
    the last of which holds the lifetime locks the replace is holding. Everything else is a store
    directory (or something an operator left there) that the archive's copy replaces.
    """
    for entry in sorted(root.iterdir()):
        if is_host_local_store_state((MEMORY_STORES_DIR_NAME, entry.name)):
            continue
        if entry.is_dir() and not platform_compat.is_link_or_junction(entry):
            _rmtree_or_raise(entry)
        elif platform_compat.is_link_or_junction(entry) and not entry.is_symlink():
            platform_compat.unlink_link_or_junction(entry)
        else:
            entry.unlink()


def _do_replace_mutations(
    snap: Path,
    mc: Path,
    backup: Path,
    components: list[str] | None,
    mem_roots: list[tuple[str, Path]],
    installed: set[str],
    *,
    allow_unpinned: bool = False,
) -> None:
    """Every mutation replace mode performs, so one handler can revert all of them.

    *installed* accumulates every declared path this run begins writing, and is recorded
    BEFORE the write rather than after, so a target interrupted mid-write is still known
    to have been reached. Recovery needs that: a file is saved by moving it aside at the
    moment of its own mutation, so "nothing saved" is ambiguous until you know whether the
    phase ever got there.

    A memory tree is CLEARED unconditionally and refilled only when the archive carries it,
    because replace means the destination ends up matching the archive. Do not read
    `_trees_absent_from_bundle` as covering that: it refuses only bundles with no component
    map, and a v3 bundle may legitimately declare `memory` without carrying every tree of it.
    """
    facade = _facade()
    for comp in _CORE_FILE_COMPONENTS:
        if _want(components, comp):
            facade._backup_and_copy(
                mc, backup, snap, comp, allow_unpinned=allow_unpinned, installed=installed
            )
            print(f"  ✅ {comp}")

    if _want(components, "memory"):
        # After the copy, not before: the loop above is what installs the index when the
        # archive HAS it, and this only has to answer for the case where it does not.
        _drop_derived_indexes_absent_from_bundle(snap, mc, backup, installed)

    for comp in _WHOLE_TREE_COMPONENTS:
        if not _want(components, comp):
            continue
        kept_live = False
        for dirname in COMPONENTS[comp].trees:
            d = mc / dirname
            # Required by `test_each_trees_loop_is_guarded`: the pre-flight runs in the
            # CALLER, a whole backup phase earlier, so this loop calls the chokepoint itself.
            if safe_tree_root(d, what="destination root", home=mc) is None:
                raise UnsafeComponentRoot(f"{comp}:{dirname} no longer resolves inside {mc}")
            sd = snap / dirname
            if dirname in _LOCKED_DOCUMENT_TREES:
                # A bundle WITHOUT the document (or without the tree) removes the live one,
                # so this is not gated on `sd.is_dir()`. Only the document moves, under its
                # owner's lock; the directory is never swapped.
                rel = _LOCKED_DOCUMENT_TREES[dirname]
                if not (snap / rel).is_file() and _records_omission_of(_recorded_skips(snap), rel):
                    # Except where the bundle's own manifest says it could not carry the
                    # document (a sandboxed run, an unreadable tree): its absence then says
                    # nothing about the live document, and removing that would lose it.
                    print(
                        f"  ↩️  {dirname}: kept the live "
                        f"{_safe_name(PurePosixPath(rel).name)}; this bundle did not carry it "
                        "(MANIFEST.json lists it as skipped)."
                    )
                    kept_live = True
                    continue
                save = _save_locked_document_to(backup, rel)
                # The rollback copy is taken by the installer inside its lock hold, so
                # `installed` is recorded only once the mutation has run: a failure
                # before it leaves the live document exactly as it was, with nothing
                # saved and nothing for recovery to undo.
                if (snap / rel).is_file():
                    _install_locked_document(dirname, snap, mc, save_existing=save)
                else:
                    _remove_locked_document(dirname, mc, save_existing=save)
                installed.add(rel)
                continue
            if sd.is_dir():
                installed.add(dirname)
                if d.is_dir():
                    _rmtree_or_raise(d)
                # Skips are fatal: the live tree is gone. `must_create` refuses a root
                # recreated between the rmtree and the copy.
                facade._copytree_safe(
                    sd,
                    d,
                    allow_unpinned=allow_unpinned,
                    must_create=True,
                    on_skip=pinned_fs.fatal_skip_reporter(f"restore of {dirname!r}"),
                )
        if not kept_live:
            print(f"  ✅ {comp}")

    # Scoped to memory's own trees as `_do_replace` selected them: the two workspace/
    # subtrees only when `workspace` is not also selected (that pass has already replaced
    # them, and repeating the work here would save the INCOMING memory over the saved
    # original), memory_stores/ whenever the archive is one that carries it.
    for tree, d in mem_roots:
        sd = snap / tree
        # CLEARED UNCONDITIONALLY, then filled only if the archive carries the tree:
        # replace means the destination matches the archive. Phase one's rollback copy
        # keeps the removed state recoverable.
        keep_root = tree == MEMORY_STORES_DIR_NAME and d.is_dir()
        if d.is_dir() or platform_compat.is_link_or_junction(d):
            installed.add(tree)
            if platform_compat.is_link_or_junction(d):
                # Not `Path.unlink`, which raises on a Windows junction.
                platform_compat.unlink_link_or_junction(d)
            elif keep_root:
                # memory_stores/ is cleared ENTRY BY ENTRY, keeping its host-local half: the
                # lifetime locks `_do_replace` holds live under `.member-backups/`. The
                # archive never carries those entries (`_never_ships`), so no collision.
                _clear_store_directories(d)
            else:
                _rmtree_or_raise(d)
        if sd.is_dir():
            installed.add(tree)
            # The pinned primitive needs the parent chain to exist (a fresh machine has no
            # `workspace/`).
            d.parent.mkdir(parents=True, exist_ok=True)
            facade._copytree_safe(
                sd,
                d,
                allow_unpinned=allow_unpinned,
                # The kept root is the one destination that legitimately exists; every
                # child the archive brings is still refused if a name occupies it.
                must_create=not keep_root,
                on_skip=pinned_fs.fatal_skip_reporter(f"restore of {tree!r}"),
            )
    if mem_roots:
        print("  ✅ memory trees")


def _restore_everything_from_rollback(
    backup: Path,
    mc: Path,
    targets: list[str],
    installed: set[str],
    *,
    allow_unpinned: bool = False,
    store_backup: Path | None = None,
) -> list[str]:
    """Undo the mutation phase, target by target, using *targets* as the granularity.

    The recovery half of replace-mode atomicity. Undoing every target the mutation phase
    reached returns the replaced state to one coherent generation regardless of how far
    the pass got. Recovering only the item that failed is what leaves memory half-old and
    half-new.

    **Granularity is the invariant, and it is exactly *targets*.** Every entry is a
    declared relative path, and recovery touches nothing else. Walking the rollback
    DIRECTORY instead looks equivalent and is not: memory's trees are nested
    (``workspace/memory``), so ``backup`` contains a partial ``workspace/`` holding only
    those subtrees. Treating that directory as one unit clears the live ``workspace``
    whole and puts the partial copy back — deleting unrelated workspace data the restore
    never touched. Restoring `workspace/memory` restores `workspace/memory`.

    Three cases per target, and the third is why *installed* exists:

    * **Saved, and this run reached it** — put it back, clearing only that path.
    * **Not saved, and this run installed it** — it did not exist before, so the copy the
      restore created is REMOVED. That is what "no pre-restore state" restores to.
    * **This run never reached it** — LEFT ALONE, whether or not phase one saved a copy.
      Whole-tree rollback copies are taken before any mutation, so a later target can have
      a saved copy even though the restore never opened it. Putting that old copy back
      would overwrite a dashboard write that landed after phase one. Core files are saved
      by moving them aside at their own mutation point, but the same ledger rule applies.

    Locked documents (a team roster and the like) are handled first, outside the ledger
    rule: their commit is not a single rename, so no save/marker ordering closes every
    interrupt window.

    Best-effort per target, and it says so per target: a recovery that aborts on its
    first problem strands the rest, and by this point the operator's own data is what is
    at stake. Whatever cannot be undone is named, and this function never deletes the
    rollback directory.
    """
    facade = _facade()
    if not backup.is_dir():
        print(f"⚠️  No rollback directory at {backup}; nothing to put back.")
        # Reported as a failed revert, not as success with a warning: the caller's summary
        # line is what the operator acts on, and "put back" would be false here.
        return list(sorted(set(targets)))
    print(f"↩️  Restoring the previous state from {backup} ...")
    failed: list[str] = []
    locked_documents = {rel: tree for tree, rel in _LOCKED_DOCUMENT_TREES.items()}
    for rel in sorted(set(targets)):
        # A locked document is outside the ownership guard below (see the docstring).
        # Saved -> the copy goes back VERBATIM, unvalidated, so a document the reader
        # refused cannot strand later targets; not saved but installed -> remove it.
        if rel in locked_documents:
            saved = backup / rel
            try:
                if saved.is_file():
                    _restore_locked_document(locked_documents[rel], saved, mc)
                elif rel in installed:
                    _remove_locked_document(locked_documents[rel], mc)
            except (OSError, pinned_fs.PinnedPathRefusal) as e:
                failed.append(f"{rel} ({e})")
            continue
        # A saved copy proves only that the tree existed at phase one, not that this run
        # touched it; restoring it could delete a concurrent dashboard write. The
        # mutation ledger is the ownership proof.
        if rel not in installed:
            continue
        saved = store_backup if rel == MEMORY_STORES_DIR_NAME and store_backup else backup / rel
        target = mc / rel
        try:
            if platform_compat.is_link_or_junction(saved):
                # FIRST, because both tests below DEREFERENCE: a relative symlink moved into
                # the rollback directory stops resolving, so it would match no branch.
                # `_backup_and_copy` moves symlinked core FILES aside on purpose.
                target.parent.mkdir(parents=True, exist_ok=True)
                if platform_compat.is_link_or_junction(target):
                    platform_compat.unlink_link_or_junction(target)
                elif target.is_dir():
                    _rmtree_or_raise(target)
                elif target.exists():
                    target.unlink()
                # MOVED back, not copied: the rollback holds the only copy, and a move also
                # reinstates a Windows junction, whose target cannot be read portably.
                shutil.move(str(saved), str(target))
            elif saved.is_dir():
                # A contained root can still BE a link, and rmtree raises on one, so links
                # go through `unlink_link_or_junction` (which also handles a Windows
                # junction) and rmtree is kept for real directories.
                keep_root = (
                    rel == MEMORY_STORES_DIR_NAME
                    and target.is_dir()
                    and not platform_compat.is_link_or_junction(target)
                )
                if platform_compat.is_link_or_junction(target):
                    platform_compat.unlink_link_or_junction(target)
                elif keep_root:
                    # The rollback copy lives inside the kept host-local half, alongside
                    # the held lock inodes; neither may be removed during recovery.
                    _clear_store_directories(target)
                elif target.is_dir():
                    _rmtree_or_raise(target)
                target.parent.mkdir(parents=True, exist_ok=True)
                # The plain copy is lossless for TREES: `_backup_tree_or_refuse` refuses a
                # link, so a saved tree is links-free. (Saved core files can be links; the
                # branch above handles them.)
                facade._copytree_safe(
                    saved,
                    target,
                    # The `--allow-unpinned-staging` opt-in must reach the rollback too, or
                    # it refuses to put back what the forward path was allowed to change.
                    allow_unpinned=allow_unpinned,
                    must_create=not keep_root,
                    on_skip=pinned_fs.fatal_skip_reporter(f"rollback of {rel!r}"),
                )
            elif saved.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                # Routed through the pinned primitive, not `shutil.copy2`. copy2 opens the
                # destination BY NAME and FOLLOWS a link sitting there, so a link planted at
                # a core file's name between the save and this recovery would send the
                # restored bytes to whatever it points at. The directory branch above
                # already refuses a link; this is the same hazard in the sibling branch, and
                # recovery is the worst place to have it -- it runs precisely when the
                # operator's state is already half-replaced.
                #
                # An existing destination is REPLACED here, unlike the merge path: this is
                # putting back what the restore moved aside, so `skip_existing` would leave
                # the failed generation in place. `copy_file_pinned` opens
                # O_CREAT|O_EXCL|O_NOFOLLOW, so the old name is removed first and a link at
                # that name is refused rather than followed.
                if platform_compat.is_link_or_junction(target):
                    platform_compat.unlink_link_or_junction(target)
                elif target.is_file():
                    target.unlink()
                pinned_fs.copy_file_pinned(
                    str(saved),
                    str(target),
                    on_skip=pinned_fs.fatal_skip_reporter(f"rollback of {rel!r}"),
                )
            elif rel in installed and (
                target.exists() or platform_compat.is_link_or_junction(target)
            ):
                # Nothing saved, so delete only what this run created. `rel in installed` is
                # recorded before the save, and a DIRECTORY at a core file's path is never
                # saved, so the type is the evidence: this run would have created a FILE.
                # A directory there is left and reported as a failure, not deleted.
                if target.is_dir() and not platform_compat.is_link_or_junction(target):
                    if rel in CORE_FILES_FLAT:
                        failed.append(
                            f"{rel} (a directory is standing where this component's FILE "
                            "belongs, and no copy of it was saved -- refusing to delete it, "
                            "because nothing here proves this run created it)"
                        )
                        continue
                    if rel == MEMORY_STORES_DIR_NAME:
                        _clear_store_directories(target)
                        continue
                    _rmtree_or_raise(target)
                else:
                    # Covers a link or junction as well as a plain file, so it goes through
                    # the helper: `Path.unlink` cannot remove a Windows junction, and the
                    # helper falls through to `unlink` for an ordinary file anyway.
                    platform_compat.unlink_link_or_junction(target)
        # `PinnedPathRefusal` is not an OSError. Recorded per target: aborting the loop
        # would strand every remaining target.
        except (OSError, pinned_fs.PinnedPathRefusal) as e:
            failed.append(f"{rel} ({e})")
    if failed:
        print("⚠️  Could not undo these: " + ", ".join(failed))
        locations = ", ".join(str(p) for p in (backup, store_backup) if p is not None)
        print(
            f"   The saved copies are still in {locations} — recover them by hand before "
            "re-running."
        )
    else:
        print("↩️  Previous state restored.")
    return failed
