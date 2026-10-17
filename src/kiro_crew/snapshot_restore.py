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

import json
import os
import shutil
import stat as _stat
from contextlib import ExitStack, closing
from datetime import timezone
from pathlib import Path, PurePosixPath
from typing import Callable, cast

from kiro_crew import pinned_fs, platform_compat
from kiro_crew.member_memory_backup import StoresInUse
from kiro_crew.memory_stores import (
    MEMBER_BACKUPS_DIR_NAME,
    MEMORY_STORES_DIR_NAME,
    is_host_local_store_state,
    memory_store_namespace_lock,
)
from kiro_crew.snapshot_archive import (
    _bundle_carries_named_stores,
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
            os.replace(partial, final)
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

    Split from the per-component check because the two answer different questions, and
    conflating them is what let live data go. Per COMPONENT, presence is "any declared path
    is there", which is right: a home that never wrote ``memory_index.db`` produces a memory
    bundle with only ``memory.db``, and demanding every file would refuse a sound bundle.
    Per TREE under ``--mode replace`` that leniency is destructive, because
    :func:`_replace_tree_root` clears the destination BEFORE it knows whether the archive
    has a replacement -- so one present file makes the whole component look carried while
    an absent tree is cleared from live state and the restore reports success.

    Both halves of the condition are load-bearing, and requiring only the first was wrong:
    a home that never used a tree produces a bundle without it, so "the archive lacks this
    tree" alone refuses sound bundles. What makes it a LOSS is live state to lose. Naming
    the live side keeps the refusal to exactly the case where clearing destroys something.

    Deliberately not applied to a complete bundle: there, a tree the archive lacks is a
    tree the source genuinely did not have, so clearing it is the point of replace. Only a
    bundle that ASSERTS it is partial while carrying no component map cannot tell those two
    apart, and that is the one case this speaks for.
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

    The manifest is normally what answers "does this bundle carry X", and a
    bundle that declares a component map is checked against it. This is the
    fallback for the one shape that has no map to check: a root marked partial
    whose manifest predates (or omits) the component list.

    Naming a component is not evidence the bundle holds it. Replace mode moves
    each live core file aside before it knows whether the archive has a
    replacement, and clears a component tree whether or not the archive carries
    one -- so an operator who names a component the bundle never held loses that
    component and is told the restore succeeded.

    Presence is "any declared path is there", not "all of them". A component
    legitimately ships without every file: a home that never wrote
    ``memory_index.db`` produces a memory bundle with only ``memory.db``, and
    requiring both would refuse a sound bundle.
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

    *installed*, when given, is the rollback ledger, and this function is the ONLY place
    that may add a core file to it: a name goes in immediately before that file's own first
    mutation and never before, because the recovery leg reads membership as "this run
    reached this path". Adding every file the component DECLARES up front is a different
    set -- a bundle may legitimately carry only some of them, and the loops below SKIP the
    rest. Recovery would then see a file with no saved copy that is nonetheless
    "installed", conclude the restore created it, and delete the operator's untouched
    file. Recording per file is what makes membership mean what the recovery leg already
    documents it to mean.

    Composing ``mc / f`` and handing it to ``shutil.copy2`` would reach the destination
    by name every time. Two consequences, both real: a component of the data home
    swapped for a link redirects the write out of the data home entirely, and a symlink
    left at the core file's own name is written THROUGH rather than refused -- the
    name-based ``islink`` check above skips the backup move and then the copy follows
    the link it just declined to move.

    Instead the data home is pinned once and each file is created relative to that
    descriptor with ``O_EXCL``. A name that is still occupied after the backup move
    is refused instead of written through, which is the symlink case above.
    """
    if not _facade()._staging_is_pinned(
        allow_unpinned=allow_unpinned, what=f"restore of {component!r}"
    ):
        for f in CORE_FILES.get(component, ()):
            # Validated before the live file is touched, and a symlink at the live name is
            # MOVED aside rather than skipped -- the same two properties the pinned branch
            # got earlier in this change. Review found this branch still carrying the old
            # behaviour: it skipped both the backup AND the replacement, so the archive's
            # file was never applied and the command reported success anyway. Moving a
            # symlink moves the link, never its target.
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
            # Not `copy2`: it opens the destination by name for writing and follows a
            # symlink planted there in the window after the live file was moved aside,
            # overwriting whatever it points at. copy_file_pinned uses
            # O_CREAT|O_EXCL|O_NOFOLLOW even without a directory descriptor, so a link at
            # the destination name is refused rather than written through.
            # `fatal_skip_reporter`, NOT `_report_skip`: the live file has ALREADY been
            # moved into the backup by this point, so a skip here is not an omission from
            # an archive -- it is the live file gone AND the archive's version never
            # applied, reported as success. That is the whole reason for a fatal
            # reporter: a skip is correct while PRODUCING an archive and is data loss on
            # any path that has already moved or deleted the original.
            # A collision here means something recreated the name after the live file was
            # moved aside. It is a real condition, not a skip, but it must not surface as a
            # traceback: the same escape exists on the pinned tree walk. The live bytes
            # are recoverable from the backup, which is what the message has to say.
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
                    # Checked BEFORE the live file is touched. The archive is untrusted
                    # input, so a member that is not a regular file -- a FIFO, a device
                    # node, a directory at a core filename -- is a real possibility, and
                    # the old order moved the live file aside first and only then found
                    # the source unusable: the original ended up in the backup and
                    # nothing was restored, reported as success. Raised in review; the
                    # same validate-before-mutate ordering the platform gate follows.
                    # Asked through src_fd, not by composing a path. The by-name form
                    # re-resolves the snapshot root, so a root swapped after pinning
                    # leaves this guard inspecting the replacement while the copy below
                    # acts on the descriptor -- the same class as the
                    # destination-ownership check.
                    if not pinned_fs.is_regular_at(src_fd, f):
                        continue
                    # Same point as the fallback branch: past the skip, so this file is
                    # about to be mutated and is recorded BEFORE the move. A file the
                    # bundle does not carry never reaches here, so recovery correctly reads
                    # it as never touched and leaves it alone.
                    if installed is not None:
                        installed.add(f)
                    # A symlink at a core file's name is MOVED aside like any other
                    # occupant, not skipped. The old code skipped the move and then let
                    # the copy write through the very link it had just declined to
                    # move; skipping the whole entry instead would be no better,
                    # because the archive's version of that file would then silently
                    # never be restored. Moving a symlink moves the LINK, never its
                    # target, so nothing outside the data home is touched.
                    #
                    # The move goes through both pinned descriptors rather than
                    # shutil.move on two composed paths: review pointed out that a
                    # by-name move re-resolves both ends, so an ancestor swapped
                    # between the check and the move would relocate something else.
                    # os.rename with src_dir_fd/dst_dir_fd cannot be redirected, and it
                    # is atomic within the data home, which a copy-then-delete is not.
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
                            # Owner-only applied through the destination DESCRIPTOR, in
                            # the same call that wrote the bytes. Two things wrong with
                            # the previous _lock_down_restored(mc / f) here, both raised
                            # in review: it reopened the freshly written file BY NAME, so
                            # a link swapped in at that instant had restrict_to_owner
                            # change the permissions of whatever it pointed at; and the
                            # mode cannot be inherited from the archive, which is
                            # untrusted input -- a hand-built tarball can record 0o777 on
                            # telemetry_salt. The reviewer's suggested fix was to drop
                            # the lockdown because "the copy already applies mode", which
                            # would have done exactly that: applied the ARCHIVE's mode.
                            force_mode=(
                                0o600
                                if component == "security" or f in _OWNER_ONLY_SETTINGS_FILES
                                else None
                            ),
                            # The live file was moved aside two lines up, so a skip here
                            # finishes with the original gone AND the archive's version
                            # never written. Review's third instance of that rule; it is
                            # now the reporter's job rather than a per-site check.
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
            finally:
                os.close(backup_fd)
        finally:
            os.close(dst_fd)
    finally:
        os.close(src_fd)


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

    Validation has to precede mutation, and for this path that is not a stylistic
    preference. Putting the incoming file where the live one was and only then checking it
    can report that the home is now sitting on a corrupt database, which is the outcome the
    check exists to prevent. A bundle arriving over the network from object storage is
    untrusted input no matter whose bucket held it, so it is validated at the point where
    refusing is still free.

    **The condition is "does this restore read or install the file", not "is this replace
    mode".** Replace installs everything it carries, so *mc_for_merge* is ``None`` and
    every declared entry is checked. Merge is per-file, because merge is not one behaviour:
    it installs some files, parses others in place, and leaves the rest alone — see
    `_merge_reads` for the three cases and why a single destination-existence test was the
    wrong proxy for all of them.

    Every incoming database for the SELECTED components is checked, not just the largest
    or the first.

    Unreadable counts as unsound. Tolerating a file named `.db` that SQLite cannot open
    is right when *creating* a snapshot (the operator's home is the source of truth and
    the file is copied verbatim), and wrong when consuming one: there the file is about
    to BECOME the operator's memory.
    """

    def _merge_reads(rel: str) -> bool:
        """Whether MERGE reads or installs *rel*, so validation has to cover it.

        A single "is the destination missing" test was a proxy, and it was wrong in two
        places, both of which merge genuinely consumes:

        * `crons.json` is PARSED when a local one exists (`_merge_crons` json-loads both
          sides) and copied when it does not. Either way merge reads it, so a malformed
          file is never harmless — skipping it because the destination exists is what let
          an unparseable file reach an unguarded `json.loads`.
        * `memory_index.db` is copied alongside `memory.db` exactly when the live
          `memory.db` is ABSENT, whatever the index's own destination looks like. Keying on
          the index's own path let a corrupt index overwrite a healthy one.

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
                # "Will this file reach a consumer?" -- not "is this a replace?". Merge
                # installs after all when the destination is ABSENT: the per-component
                # branch merges only `if dst.is_file()`, and its sibling `else` copies the
                # bundle's file in verbatim with no validation. An absent destination is the
                # FRESH MACHINE case, which is the scenario a backup exists for, so the one
                # path that skipped this check was the likeliest one to need it.
                #
                # Reproduced: a well-formed JSON ARRAY in the bundle, no live `crons.json`,
                # merge -> copied verbatim, rc=0, reported success -- and the cron loader's
                # `isinstance(data, dict) else []` branch then reports zero jobs. Every
                # schedule silently gone, nothing raised, nothing retried.
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

    # Component TREES carry databases too, and a tree is copied wholesale: the knowledge
    # store lives at `workspace/knowledge/knowledge.db`, inside a tree the memory
    # component declares. Checking only the top-level declared files leaves exactly the
    # same hole one directory down.
    #
    # Strictness is per PATH, not per location. A database this product owns is strict
    # wherever it lives: `workspace/knowledge/knowledge.db` is as much ours as
    # `memory.db`, so an unopenable one is a broken bundle, not an operator's stray file.
    # Leniency exists only for the INCIDENTAL contents of a tree, where a `.db` that is
    # not SQLite is ordinary — a Windows `Thumbs.db` is on this product's own ignore list
    # — and refusing those would block restores over files that were never databases.
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

    A database is not the only thing a restore can install broken. The consumers of these
    files treat an unreadable one as an EMPTY one — `crons.json`'s loader falls back to
    "no jobs" on both a parse error and a well-formed array — so installing a corrupt file
    silently discards the operator's content while the restore reports success. Silent
    emptiness is the worst failure available here: nothing raises, so nothing is retried.

    *installed* is what separates the two hazards, because only one of them is silent.
    Replace INSTALLS this file, so an unparseable one reaches the consumer and reads as
    empty — refusing is the only way the operator hears about it. Merge USUALLY does not
    install it: when a live copy exists, the per-component merger reads the bundle's copy,
    reports a file it cannot parse, and returns without writing live state, so the content
    is neither lost nor lost quietly. Refusing there would turn one unreadable component
    into a failed restore of every other one, which is the opposite of what an off-host
    backup is for.

    "Usually" is load-bearing, not "never". Merge's per-component branch merges only
    `if dst.is_file()`; its sibling `else` copies the bundle's file in VERBATIM. So an
    absent destination -- the fresh-machine case, which is the scenario a backup exists
    for -- does install, and is the one path that could skip this check. The caller
    therefore decides *installed* from "will this reach a consumer", not from "is this a
    replace". Reproduced before the fix: a well-formed JSON array, no live `crons.json`,
    merge copied it verbatim and reported success, and the cron loader then read zero jobs.

    Only structure is checked, not schema. Parsing proves the file survived transport and
    is the shape its consumer branches on; asserting field-level schema here would
    duplicate each consumer's own validation and refuse bundles those consumers accept.

    The shape checks below are gated on *installed* too, matching the parse branch,
    because the merger itself now guards the merge path: `_merge_crons` runs
    `_usable_cron_shape` over BOTH sides and returns without writing when either is
    misshapen, and that guard is a superset of this one -- it also rejects a non-string
    job name and a lone surrogate inside one.
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
    # Everything past here is an INSTALL-path check, in ONE place rather than a gate per
    # branch, so a check added later cannot forget to carry the condition.
    #
    # The merge path is guarded by the merger: `_merge_crons` runs `_usable_cron_shape`
    # over BOTH sides and returns without writing when either is misshapen, and that guard
    # is a superset of these -- it also rejects a non-string job name and a lone surrogate
    # inside one. It catches more than a file it cannot PARSE, so `{"jobs": ["x"]}` does
    # not flow past it, and refusing on the merge path would only turn one misshapen
    # component into a failed restore of every other one.
    #
    # The install path has no such guard -- it copies the file in and the consumer reads an
    # unusable one as empty -- so here this refusal is the only thing between a misshapen
    # bundle and silently discarded jobs.
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


def _do_replace(
    snap: Path, mc: Path, components: list[str] | None, *, allow_unpinned: bool = False
) -> None:
    """Replace the selected components, with a complete rollback set taken first.

    Two phases, and the boundary between them is the whole design. Phase one copies every
    tree this run will mutate into a fresh rollback directory and mutates nothing; phase
    two performs every mutation. A refusal in phase one therefore aborts with the data home
    untouched, and a failure in phase two can be reverted from a rollback set that is known
    to be complete -- the ordering an earlier revision got wrong by running the core-file
    swap loop first, which aborted with the new databases live and the old trees live.
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
                # REFUSED, not skipped. `_refuse_unsafe_destination_roots` already cleared
                # this exact set moments ago, so a root that fails here failed AFTER that
                # check -- something moved under us mid-run. A `continue` here would drop
                # the tree from `mem_roots` entirely: neither saved nor restored, with the
                # run still printing "Replace complete." Letting the preflight pass and
                # swapping `workspace` for an external link immediately after drops both
                # memory trees silently and exits 0, so an operator restoring after losing
                # a machine believes memory came back when only the databases did. That is
                # exactly the lie the hoisted preflight exists to end. Safe to raise here:
                # this runs before phase one, so no live state has been mutated yet.
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
                # `tree` is NESTED (`workspace/memory`), so the rollback destination's parent
                # does not exist in a freshly-allocated backup dir. The pinned primitive pins
                # an existing parent chain and does not create one -- callers create their own
                # tree roots -- so without this the copy fails with FileNotFoundError on the
                # intermediate component. The single-level trees below are unaffected because
                # their parent IS the backup dir.
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
                # Saved only when phase two will REPLACE it, which is exactly when the
                # archive carries the tree -- `_do_replace_mutations` leaves a wanted
                # component whose bundle half is absent standing untouched. Saving it anyway
                # let a rollback put that copy back over a tree this restore never opened,
                # deleting an artifact the dashboard wrote while it ran.
                if d.is_dir() and (snap / dirname).is_dir():
                    _backup_tree_or_refuse(d, backup / dirname, allow_unpinned=allow_unpinned)

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
            facade._do_replace_mutations(
                snap, mc, backup, components, mem_roots, installed, allow_unpinned=allow_unpinned
            )
        except BaseException as e:
            # `BaseException`, not `Exception`, and deliberately wider than a few named
            # classes. `PinnedPathRefusal` belongs here because it fires MID-mutation and
            # leaving it out leaves live state half replaced; `KeyboardInterrupt` has exactly
            # that property and is not an `Exception` at all, so a narrower handler misses it
            # entirely. A Ctrl-C after the memory component is replaced otherwise leaves
            # `memory.db` holding the archive's copy and `crons.json` still the live one, with
            # no rollback attempted. A restore is the one operation where an interrupt must not
            # be taken at face value -- the operator's own state is mid-swap.
            #
            # The exception is always re-raised, so an interrupt still terminates the command and
            # `SystemExit` still exits; what changes is that the previous state is put back first.
            # A second interrupt DURING the rollback cannot be defended against here, and the
            # rollback directory is what answers for it.
            #
            # Phase one is deliberately outside this try: a refusal there happens before any
            # mutation, so there is nothing to roll back and the clean refusal is the answer.
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
    finally:
        barrier.close()

    try:
        backup.rmdir()
    except OSError:
        print(f"  Previous state saved to: {backup}/")
    print("✅ Replace complete.")


def _component_payload_absent(snap: Path, component: str) -> bool:
    """Does *snap* carry nothing this component could actually restore?

    Declared-but-hollow is the shape this answers: the manifest says a component rode, and the
    bundle holds none of its data. Replace then clears the live state for it -- memory trees
    are cleared unconditionally, and a derived index is now removed too -- with nothing to put
    back, which is a partial erasure rather than a restore.

    A DERIVED index is not payload. A bundle carrying only `memory_index.db` has no memory to
    restore, and counting it would let precisely the reproduced case through.

    Files AND trees both count, so a component whose data is a directory is not called hollow
    just because it keeps no flat file. A tree counts only when it holds at least one FILE:
    the staging walk copies a directory whose every entry it excluded as an empty directory
    -- ``memory_stores/`` on a home whose only content there is the sandbox's precreated
    runtime state is the ordinary case -- and an empty directory is nothing to restore, so
    counting it would let a bundle with no memory at all pass this check on the strength of
    a directory entry. Derived from `COMPONENTS` / `CORE_FILES`, never a hand-written list: a
    component gaining a file later must not silently start passing this check on the
    strength of a stale enumeration.
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

    Replace means the destination ends up matching the archive. The memory-TREE loop already
    says so and clears unconditionally for it; a derived index is a FILE and never got the
    same treatment, so `_backup_and_copy` skipped it (`(snap / f).is_file()` is false) and
    left the live one in place. The result is the restored payload indexed by the PREVIOUS
    one: searches answer from memory that was just replaced.

    That gap is reachable precisely because the redaction pass DROPS `memory_index.db` from an
    off-host bundle -- so a redacted bundle is the ordinary way to arrive here, not an exotic
    one. The comment justifying that drop pointed at restore "telling the operator to rebuild
    it", which is true only when the live index is absent too: the warning tests the file
    after the restore, and a surviving stale index means no warning is printed at all.

    Removing it is what makes the absence real, so the existing warning fires and the index is
    rebuilt from the restored payload. Scoped to derived indexes ON PURPOSE -- a missing
    payload database is a different question with a different answer (refuse, not delete), and
    it is answered elsewhere.
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
            shutil.rmtree(str(entry))
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
        for dirname in COMPONENTS[comp].trees:
            d = mc / dirname
            # Required by `test_each_trees_loop_is_guarded`: a `.trees` loop either calls
            # the chokepoint or sits in a function that already refused every unsafe root,
            # and this one does neither -- the pre-flight runs in the CALLER, a whole backup
            # phase earlier. The literal-tuple branches this loop replaced were invisible to
            # that invariant; a loop over the declared trees is not.
            if safe_tree_root(d, what="destination root", home=mc) is None:
                raise UnsafeComponentRoot(f"{comp}:{dirname} no longer resolves inside {mc}")
            sd = snap / dirname
            if dirname in _LOCKED_DOCUMENT_TREES:
                # Replace means the destination matches the archive, so a bundle WITHOUT
                # the document -- a tree that lacks it OR no tree at all, the ordinary
                # shape of an export from a home that never made a team -- removes the
                # live one. Not gated on `sd.is_dir()` like the trees below: that guard
                # let the destination's own teams survive a reported full replace.
                # Either way only the document moves, under its owner's lock, and the
                # directory is never swapped.
                rel = _LOCKED_DOCUMENT_TREES[dirname]
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
                    shutil.rmtree(str(d))
                # rmtree just removed the live tree, so a skipped source entry here means
                # that file exists in neither place.
                #
                # `must_create` is what makes the removal mean something. Without it the
                # walk accepted a root recreated between the rmtree and the copy, so files
                # the archive does not contain survived a REPLACE that reported success.
                facade._copytree_safe(
                    sd,
                    d,
                    allow_unpinned=allow_unpinned,
                    must_create=True,
                    on_skip=pinned_fs.fatal_skip_reporter(f"restore of {dirname!r}"),
                )
        print(f"  ✅ {comp}")

    # Scoped to memory's own trees as `_do_replace` selected them: the two workspace/
    # subtrees only when `workspace` is not also selected (that pass has already replaced
    # them, and repeating the work here would save the INCOMING memory over the saved
    # original), memory_stores/ whenever the archive is one that carries it.
    for tree, d in mem_roots:
        sd = snap / tree
        # CLEARED UNCONDITIONALLY, then filled only if the archive carries the tree.
        # Clearing only when the archive had it meant a bundle without, say,
        # `workspace/knowledge` left the destination's own knowledge tree in place, so a
        # "replace" produced restored memory mixed with stale notes and still reported
        # success. Replace means the destination ends up matching the archive; a tree the
        # archive does not have is a tree the destination must not keep. The rollback copy
        # was taken in phase one, before any database was swapped, so the removed state is
        # still recoverable.
        #
        # `_trees_absent_from_bundle` does NOT cover this: it only refuses bundles carrying
        # no component map, which is the escape hatch for pre-seam archives. A v3 bundle
        # declares `memory` and legitimately may not carry every tree of it, and this is
        # the branch that has to answer for that.
        keep_root = tree == MEMORY_STORES_DIR_NAME and d.is_dir()
        if d.is_dir() or platform_compat.is_link_or_junction(d):
            installed.add(tree)
            if platform_compat.is_link_or_junction(d):
                # `unlink_link_or_junction`, not `Path.unlink`. Detecting with
                # `is_link_or_junction` and removing with plain unlink is the mispairing that
                # helper's own docstring warns against: a Windows junction is a DIRECTORY
                # reparse point, so `unlink` raises on it and the tree is never cleared --
                # the replace then fails mid-flight on a platform where this is the ordinary
                # shape of a linked tree.
                platform_compat.unlink_link_or_junction(d)
            elif keep_root:
                # memory_stores/ is cleared ENTRY BY ENTRY and its host-local half kept in
                # place: the lifetime locks `_do_replace` holds live under
                # `.member-backups/`, and removing that directory would let a store opened
                # mid-replace create a fresh, unheld lock beside the one being held. The
                # archive never carries those entries (`_never_ships`), so the copy below
                # meets no collision, and the local backups and execution logs stay where
                # the default store's own `<home>/backups/` already stays.
                _clear_store_directories(d)
            else:
                shutil.rmtree(str(d))
        if sd.is_dir():
            installed.add(tree)
            # Nested destination, same reason as the rollback save: the pinned primitive
            # requires the parent chain to exist and creates only the final directory. A
            # home that has no `workspace/` at all is the ordinary case for a restore onto
            # a fresh machine, which is exactly what this component is for.
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

    Locked documents (a team roster and the like) are OUT OF SCOPE for the "this run
    never reached it" rule: they keep main's rollback behavior and are handled first,
    before the ledger guard. Their commit is not a single rename, so no save/marker
    ordering closes every interrupt window between the save and the commit; the ledger
    guard governs only the single-rename targets where that window does not exist.

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
        # A locked document is OUT OF SCOPE for the ownership guard below: it keeps
        # exactly main's rollback behavior, byte for byte, and is handled here, before
        # the guard. Its commit is not a single rename, so no save/marker ordering closes
        # every interrupt window between the save and the commit; the guard applies only
        # to the single-rename targets where that window does not exist. Saved -> the copy
        # goes back VERBATIM (recovery puts the operator's state back, it does not
        # validate it -- the saved copy may be a document the reader already refused, and
        # refusing it here would strand every later target); not saved but installed by
        # this run -> remove what the run created; otherwise leave it alone. Nothing on
        # this branch raises anything but ``OSError``, which the loop names per target.
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
        # Phase one saves every whole tree before any mutation. A saved copy therefore
        # proves only that the tree existed at the phase-one snapshot, not that this run
        # later touched its live target. A dashboard write can land there while an earlier
        # component is being replaced; if that earlier replacement fails, restoring this
        # older saved copy would delete the concurrent write from a tree the restore never
        # opened. The mutation ledger is the ownership proof for both saved and unsaved
        # non-locked targets, so later targets are left exactly as they stand.
        if rel not in installed:
            continue
        saved = store_backup if rel == MEMORY_STORES_DIR_NAME and store_backup else backup / rel
        target = mc / rel
        try:
            if platform_compat.is_link_or_junction(saved):
                # FIRST, because both tests below DEREFERENCE. A core file that was a
                # relative symlink stops resolving the moment it is moved into the rollback
                # directory, so `is_dir()` and `is_file()` are both false and the saved link
                # matched no branch at all: the replacement at the live name was deleted as
                # an undone creation, the link stayed in the rollback directory, and the
                # recovery reported success. Reproduced -- the live name ended up not
                # existing at all.
                #
                # The directory branch below says the rollback directory is "links-free by
                # construction". That is true of TREES, which `_backup_tree_or_refuse` saves
                # with a fatal skip reporter. Core FILES are saved by `_backup_and_copy`,
                # which deliberately MOVES a symlinked core file aside and prints that it
                # did -- so a link here is a state this code creates on purpose, and the
                # claim was being applied to an input it was never about.
                target.parent.mkdir(parents=True, exist_ok=True)
                if platform_compat.is_link_or_junction(target):
                    platform_compat.unlink_link_or_junction(target)
                elif target.is_dir():
                    shutil.rmtree(str(target))
                elif target.exists():
                    target.unlink()
                # MOVED back rather than copied, unlike the two branches below. The save
                # moved the link itself, so the rollback holds the only copy; and a move is
                # the one operation that also reinstates a Windows junction, whose target
                # cannot be read portably. Moving a link moves the LINK, never its target.
                shutil.move(str(saved), str(target))
            elif saved.is_dir():
                # Clearing the live root before refilling it. A root that passed
                # containment can still BE a link -- one pointing elsewhere inside the
                # data home resolves within it -- and rmtree raises on a link, which at
                # this point would strand the recovery. Remove a link as a link and
                # reserve rmtree for real directories.
                #
                # Through `unlink_link_or_junction`, because a Windows junction is a
                # directory reparse point that `Path.unlink` cannot remove: recovery would
                # raise here and leave the operator's prior state stranded, which is the
                # one outcome this whole function exists to prevent.
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
                    shutil.rmtree(str(target))
                target.parent.mkdir(parents=True, exist_ok=True)
                # The plain staging copy is lossless HERE, which it would not be for the
                # save. `_backup_tree_or_refuse` reports a skipped entry as fatal, so a
                # TREE containing a link never reaches the rollback directory at all
                # -- whatever `backup` holds for a tree is links-free by construction, and
                # there is nothing for a link-preserving copy to preserve on the way back.
                #
                # Scoped to trees deliberately. Read as a claim about the whole rollback
                # directory it is false: core FILES are saved by a different function that
                # MOVES a symlink aside on purpose, and reading it that broadly leaves the
                # saved-link case with no branch to match.
                facade._copytree_safe(
                    saved,
                    target,
                    # The operator's opt-in has to reach HERE, not just the forward path.
                    # Without it this copy takes the default and refuses on a platform with
                    # no directory descriptors -- so an operator who passed
                    # `--allow-unpinned-staging` gets a replace that is allowed to MUTATE
                    # live state and a rollback that then refuses to put it back. The
                    # safety net becomes the one thing that will not run, at the only
                    # moment it matters.
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
                # Nothing saved, so the only justification for deleting is that this run
                # created it. `rel in installed` is NOT that evidence: the name is recorded
                # BEFORE the save is known to have happened (deliberately -- a crash
                # mid-write must still leave it known to have been reached), and the save
                # only actually happens on two branches, a symlink or a regular file. A core
                # file's path occupied by a DIRECTORY matches neither, so nothing is saved
                # and the name is recorded anyway.
                #
                # Reproduced: with a directory at `crons.json` holding operator data,
                # recovery deleted the directory and its contents, reported an empty failure
                # list, and printed "Previous state restored." Data loss announced as a
                # successful recovery.
                #
                # So the type is the evidence. A core file entry is one this run would have
                # created as a regular FILE; a directory standing there is something else's,
                # and deleting it is unrecoverable while leaving it is not. Recorded as a
                # failure so the operator is told rather than silently obeyed.
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
                    shutil.rmtree(str(target))
                else:
                    # Covers a link or junction as well as a plain file, so it goes through
                    # the helper: `Path.unlink` cannot remove a Windows junction, and the
                    # helper falls through to `unlink` for an ordinary file anyway.
                    platform_compat.unlink_link_or_junction(target)
        # `PinnedPathRefusal` alongside OSError, and NOT an OSError itself: recovery now
        # restores through the pinned primitives with a fatal reporter, so one target it
        # cannot put back raises a refusal. Recorded per target like any other failure --
        # aborting the loop here would strand every remaining target, which is the opposite
        # of recovery, and by this point the data at stake is the operator's own.
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
