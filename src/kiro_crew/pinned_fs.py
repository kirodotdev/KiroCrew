"""Descriptor-pinned filesystem staging: open once, then never trust a name again.

Every function here exists because guarding a path by NAME does not guard the open
that follows it. A name is validated, then re-opened, and in the window between the
two anything running as this user -- which in this product includes an agent -- can
swap the final component, or an ancestor DIRECTORY, for a link pointing somewhere
else. The validated path and the opened inode are then not the same thing.

The discipline is one sentence: resolve once, open once, and address everything
downstream through the descriptor you already hold. A descriptor cannot be
re-pointed, so a component that is open is fixed; a component reached by name is
not.

Why this module exists rather than a check at each call site: the
validated-by-name path uses are many and easy to miss one at a time -- source root,
each ancestor, each file, the destination tree, the pre-restore backup pass. The
mechanism belongs in one place with one set of invariants, and callers become thin
consumers of it.

``kiro_crew.eval.bench.safepath`` imports the mechanical half from here, so the
benchmark harness and the product share one set of invariants. The docstrings
explaining WHY each flag is load-bearing live with them.

Two things this module deliberately does NOT do:

* It holds no policy. It does not know which locations are protected, and it does
  not decide whether a refusal should abort a command or be reported and skipped.
  Callers pass their own refusal type (so an existing CLI error contract does not
  change) and their own ``on_skip`` reporter (so user-facing wording stays theirs).
* It does not silently substitute a weaker mechanism. Where a platform cannot pin
  (see :func:`supports_pinned_walk`), the caller is told so and decides; nothing
  here falls back to a by-name walk on its own.
"""

from __future__ import annotations

import errno
import importlib as _importlib
import os
import shutil
import stat
import stat as _stat
import sys as _sys
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Callable

import kiro_crew._facade_mirror as _facade_mirror
from kiro_crew.atomic_write import atomic_write, atomic_write_at
from kiro_crew.pinned_fs_dirs import (
    create_and_open_dir_at,
    create_and_open_dir_pinned,
    open_pinned_descendant_dir,
)
from kiro_crew.pinned_fs_files import (
    copy_file_pinned,
    is_regular_at,
    lstat_by_name,
    read_file_pinned,
    stat_at,
    unlink_pinned,
    unlink_verified,
    unlink_verified_by_name,
    write_file_pinned,
    write_new_file_pinned,
)
from kiro_crew.pinned_fs_primitives import (
    SKIP_IDENTITY_CHANGED,
    SKIP_NOT_REGULAR,
    SKIP_SYMLINK,
    SKIP_TOO_LARGE,
    SKIP_UNREADABLE_ENTRY,
    SKIP_VANISHED,
    PinnedPathRefusal,
    SkipReporter,
    _noop_skip,
    dir_flags,
    fatal_skip_reporter,
    fd_real_path,
    is_reparse_point,
    omits_wanted_data,
    open_fenced_for_read,
    pin_parent,
    refuse_hardlink_alias,
    supports_pinned_tree_walk,
    supports_pinned_walk,
)
from kiro_crew.platform_compat import open_file_no_reparse, pin_directory

__all__ = [
    "PUT_BACK_FAILED",
    "PUT_BACK_NAME_TAKEN",
    "PinnedPathRefusal",
    "PinnedTree",
    "REMOVAL_FAILED",
    "REMOVAL_IDENTITY_CHANGED",
    "REMOVAL_STAGE_FAILED",
    "REMOVAL_UNVERIFIABLE",
    "SKIP_NOT_REGULAR",
    "SKIP_SYMLINK",
    "SKIP_UNREADABLE_ENTRY",
    "SKIP_VANISHED",
    "SkipReporter",
    "StagedRemoval",
    "TreeRemoval",
    "close_all",
    "copy_file_pinned",
    "create_and_open_dir_pinned",
    "dir_flags",
    "drain_verified_chain",
    "fatal_skip_reporter",
    "fd_real_path",
    "is_reparse_point",
    "is_regular_at",
    "omits_wanted_data",
    "stat_at",
    "open_dir_pinned",
    "open_in_pinned_parent",
    "open_pinned_descendant_dir",
    "open_verified_chain",
    "pin_parent",
    "put_back_no_clobber",
    "refuse_hardlink_alias",
    "remove_dir_verified",
    "remove_tree_pinned",
    "scan_tree_pinned",
    "stage_tree_pinned",
    "supports_pinned_tree_walk",
    "supports_pinned_walk",
    "unlink_verified",
    "unlink_verified_by_name",
]


def open_in_pinned_parent(
    resolved_parent: str,
    name: str,
    *,
    flags: int,
    mode: int,
    what: str,
    refusal: type[Exception] = PinnedPathRefusal,
) -> int:
    """Open *name* under *resolved_parent* with the parent chain pinned.

    *name* is opened as given, so a link at the final name is refused by
    ``O_NOFOLLOW`` in *flags*. See :func:`pin_parent` for what pinning buys.
    """
    dir_fd = pin_parent(resolved_parent, what=what, refusal=refusal)
    try:
        return os.open(name, flags, mode, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)


def open_dir_pinned(
    path: str | Path,
    *,
    what: str,
    refusal: type[Exception] = PinnedPathRefusal,
) -> int:
    """Open a DIRECTORY with its whole ancestor chain pinned, final component included.

    Its absence is what makes a root-only check unsound:
    ``os.open(str(src), O_DIRECTORY | O_NOFOLLOW)``
    refuses a link at the root's own name but reaches that name by walking every
    ancestor BY NAME, so swapping a validated ancestor for a link to a credential
    directory redirects the whole traversal and the ``O_NOFOLLOW`` on the final
    component never fires -- what it finds there is a perfectly ordinary directory.

    Here the parent chain is resolved once and pinned component by component, and
    the root's own name is then opened relative to the pinned parent. Nothing in the
    subtree is ever addressed by a path again.
    """
    as_given = Path(path)
    resolved_parent = os.path.realpath(as_given.parent or Path("."))
    try:
        return open_in_pinned_parent(
            resolved_parent,
            as_given.name,
            flags=dir_flags(),
            mode=0o700,
            what=what,
            refusal=refusal,
        )
    except OSError as exc:
        # Same translation as `create_and_open_dir_pinned`. Without it the raw errno
        # leaks: `O_NOFOLLOW` refuses the link correctly, but a direct caller (the data
        # home, a backup root, a restore destination) would get an
        # `ELOOP`/`ENOTDIR` traceback instead of the one refusal type every other path on
        # this surface produces and the CLI boundary contains.
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise refusal(
                f"refusing to use the {what}: {as_given.name!r} is a symbolic link or "
                "not a directory, so working through it would follow whatever it points "
                "at. Remove it and re-run."
            ) from exc
        raise


def stage_tree_pinned(
    src: str | Path,
    dst: str | Path,
    *,
    what: str,
    ignore: Callable[[str, list[str]], set[str]] | None = None,
    on_skip: SkipReporter = _noop_skip,
    skip_existing: bool = False,
    must_create: bool = False,
    skip_unreadable: bool = False,
    refusal: type[Exception] = PinnedPathRefusal,
) -> None:
    """Copy a tree with BOTH traversals pinned end to end.

    A by-name walk's link screens protect only each final component: swapping an
    allowlisted ancestor DIRECTORY for a link to a credential directory mid-walk
    redirects every deeper open through the link, and the per-file ``O_NOFOLLOW``
    never fires because what it finds inside the replaced tree is a plain regular
    file. Here every directory is opened ``O_NOFOLLOW|O_DIRECTORY`` relative to its
    PARENT's descriptor and every file is opened relative to its pinned parent, so
    the directory that was validated is exactly the directory used. Both roots go
    through :func:`open_dir_pinned`, which pins the chain ABOVE each root too.

    The DESTINATION is pinned for a reason, not for symmetry. The first version of
    this function pinned only the source, which was defensible while the only
    destination was a private temporary directory -- and wrong the moment a restore
    used it, because then the destination IS the live data home and an ancestor
    swapped there lands the archive's bytes outside it. Caught in review.

    With *skip_existing* an occupied destination name is reported and skipped rather
    than refused, which is what a merge that must not overwrite needs. Without it an
    occupied name raises, because in a staging directory this process just created,
    the only thing that can be sitting there is something planted.

    Symlinks and non-regular files are reported through *on_skip* and skipped;
    entries that vanish mid-walk are reported and skipped; *ignore* sees
    ``(directory_by_name, contents)`` exactly as ``shutil.copytree``'s does. Every
    other error propagates, so a staging pass never silently ships without files it
    failed to read.

    *skip_unreadable* is the one exception, and it is off by default. With it, an
    entry whose metadata or bytes this process may not read is reported as
    ``SKIP_UNREADABLE_ENTRY`` and skipped -- a file, a directory whose open is
    refused, and the tree's own root alike, since a tolerance that covered only
    files still ended the operation on a directory. It belongs ONLY to a caller that records
    every skip somewhere the operator will read it: a data home can hold a
    platform-protected path that no retry will make readable, and ending the walk
    there leaves them with no backup at all instead of one that declares the gap. A
    RESTORE must never set it -- there the unreadable name is the archive's own
    content, so skipping it drops data the operator asked to have put back.

    Refuses outright on a platform that cannot pin a tree. Callers that must still
    function there are expected to say so explicitly rather than have this module
    quietly hand them a by-name walk -- see :func:`supports_pinned_tree_walk`.
    """
    if not supports_pinned_tree_walk():
        raise refusal(
            f"refusing to stage the {what}: this platform cannot open a directory "
            "relative to a descriptor, so the traversal would have to re-open every "
            "component by name and could be redirected by an ancestor swapped "
            "mid-walk. Staging by name is a caller's decision to declare, not this "
            "helper's to make silently."
        )

    def _walk(src_fd: int, dst_fd: int, by_name: str) -> None:
        try:
            names = os.listdir(src_fd)
        except PermissionError:
            # The directory opened and its LISTING is refused. Local static modes
            # cannot produce this -- an O_RDONLY open already required read -- but a
            # filesystem that re-validates on each call can, when the grant changes
            # between the open and the read. It is the same omission as a directory
            # whose open was refused, and a caller that records that gap records this
            # one: nothing under the directory is copied, and the reporter says so.
            # The PERMISSION class only, as at every other tolerated site: any other
            # errno here says the storage is failing and still ends the walk.
            if not skip_unreadable:
                raise
            on_skip(SKIP_UNREADABLE_ENTRY, by_name)
            return
        skipped = set(ignore(by_name, names)) if ignore else set()
        for entry in sorted(names):
            if entry in skipped:
                continue
            path = os.path.join(by_name, entry)
            try:
                st = os.stat(entry, dir_fd=src_fd, follow_symlinks=False)
            except FileNotFoundError:
                on_skip(SKIP_VANISHED, path)
                continue
            except PermissionError:
                # Unclassifiable, so there is nothing to copy and nothing to descend
                # into. Reported under its own reason rather than as a vanished entry:
                # the name is still there, and a caller recording the omission has to
                # be able to say which of the two it was.
                #
                # The PERMISSION class only. An EIO or an ENOTCONN says the storage
                # is failing, and a backup that quietly omits files because the disk
                # is dying is the worst possible artefact -- those still end the walk.
                if not skip_unreadable:
                    raise
                on_skip(SKIP_UNREADABLE_ENTRY, path)
                continue
            if _stat.S_ISLNK(st.st_mode):
                on_skip(SKIP_SYMLINK, path)
            elif _stat.S_ISDIR(st.st_mode):
                child_src = _open_child_dir(
                    src_fd,
                    entry,
                    path,
                    on_skip,
                    skip_unreadable=skip_unreadable,
                    after_stat=True,
                )
                if child_src is None:
                    continue
                try:
                    try:
                        os.mkdir(entry, 0o700, dir_fd=dst_fd)
                    except FileExistsError:
                        # Only a merge legitimately meets an existing destination
                        # directory. Anywhere else the destination tree is one this
                        # process just created, so a name already occupying it is a
                        # planted link or file -- and swallowing that would make the
                        # pinned open below refuse the subtree and the whole restore
                        # report success with the archive's subtree missing. That is
                        # a silent partial, so it is a refusal rather than a skip.
                        if not skip_existing:
                            raise refusal(
                                f"refusing to stage into {path!r}: a name already "
                                "occupies that directory in a destination tree this "
                                "operation created, so it is a link or a file planted "
                                "there rather than a directory to merge into. Writing "
                                "past it would silently omit everything below it."
                            )
                    child_dst = _open_child_dir(dst_fd, entry, path, on_skip)
                    if child_dst is None:
                        # A SOURCE entry that stopped being a directory is skipped --
                        # there is nothing left to copy. A DESTINATION that stopped
                        # being one is different: the archive's subtree still exists
                        # and now has nowhere to go, so continuing would report success
                        # with that subtree missing. A merge is the
                        # one caller that may legitimately meet a foreign destination
                        # tree, so it keeps the skip.
                        if not skip_existing:
                            raise refusal(
                                f"refusing to stage into {path!r}: the destination "
                                "directory stopped being a plain directory after it was "
                                "created, so the archive's contents below it could not "
                                "be written. Continuing would report success with that "
                                "subtree missing."
                            )
                        continue
                    try:
                        _walk(child_src, child_dst, path)
                        # Applied AFTER the contents, through the destination's OWN
                        # descriptor, and ONLY to a directory this walk created.
                        #
                        # `shutil.copytree` preserved directory mode and timestamps; the
                        # walk that replaced it did not, so a restored 0755 directory came
                        # back 0700 with a fresh mtime. Fixing that unconditionally then
                        # broke the merge case: a live 0700 directory had the ARCHIVE's
                        # 0755 stamped onto it, which both clobbers the user's metadata and
                        # loosens permissions from an untrusted source. Both caught in
                        # review, one round apart.
                        #
                        # The archive-is-untrusted half is not a new rule -- it is why a
                        # security file is forced to 0600 rather than given the archive's
                        # mode. That rule was applied to files and not carried to
                        # directories.
                        #
                        # The archive's directory mode and mtime are NOT applied.
                        # Gating them on `created` from the `mkdir` cannot be trusted:
                        # the descriptor comes from a separate `open`, so a directory
                        # replaced between the two leaves `created=True` describing an
                        # object we do not hold, and the archive's metadata then
                        # lands on somebody else's directory.
                        #
                        # There is no sound repair. An identity check cannot help -- a
                        # stat taken after the mkdir observes the REPLACEMENT just as
                        # happily as our own directory -- and POSIX has no atomic
                        # create-and-open for a directory to make the flag mean what it
                        # says. Under a same-user threat model no mode trick closes it
                        # either.
                        #
                        # So the fidelity is given up on purpose: a restored tree keeps its
                        # default directory mode and a current mtime. That is the
                        # right trade -- applying untrusted archive metadata to a live
                        # directory is the hazard this module exists to refuse.
                    finally:
                        os.close(child_dst)
                finally:
                    os.close(child_src)
            elif _stat.S_ISREG(st.st_mode):
                try:
                    copy_file_pinned(
                        path,
                        dir_fd=src_fd,
                        name=entry,
                        dst_dir_fd=dst_fd,
                        dst_name=entry,
                        skip_existing=skip_existing,
                        skip_unreadable=skip_unreadable,
                        # The inode this walk just judged regular. Handing it over is
                        # what lets the copy tell a swap from a first-look screen and
                        # report it under the reason retention treats as an omission.
                        expected_src_ident=(st.st_dev, st.st_ino),
                        on_skip=on_skip,
                    )
                except FileNotFoundError:
                    on_skip(SKIP_VANISHED, path)
                except FileExistsError as exc:
                    # The destination name was taken between this walk starting and the
                    # publish. Without `skip_existing` that is not a merge -- the caller
                    # believes the tree it is writing into is free -- so it is a real
                    # condition, and must not escape as a bare traceback out of a restore.
                    # Translated to this module's refusal type, the same
                    # way ELOOP/ENOTDIR already are, so callers have one type to contain
                    # and the message says which name is occupied.
                    raise refusal(
                        f"refusing to stage {path!r}: its destination name was created by "
                        "something else while this operation was running, so writing it "
                        "would either overwrite that file or leave the tree half-applied. "
                        "Nothing was written for this entry. Re-run with the gateway "
                        "stopped."
                    ) from exc
            else:
                on_skip(SKIP_NOT_REGULAR, path)

    try:
        root_src = open_dir_pinned(src, what=what, refusal=refusal)
    except (OSError, refusal) as exc:
        # A SOURCE root that was swapped or removed after the caller's listing-time
        # screen is omitted, not fatal -- the same treatment every other unusable source
        # entry gets, and it now reaches MANIFEST.json rather than only the console. The
        # refusal type is caught alongside the raw errno because `open_dir_pinned`
        # translates ELOOP/ENOTDIR (so DIRECT callers get no tracebacks); this call site
        # is the one place that wants the softer outcome.
        if isinstance(exc, OSError) and exc.errno not in (
            errno.ELOOP,
            errno.ENOTDIR,
            errno.ENOENT,
        ):
            # A ROOT refused for permission is the same omission as a refused entry
            # inside it, and it reaches the recorder the same way, so a caller that
            # declares the gap declares this one too. Every other errno still ends
            # the walk. Without this the tolerance was file-shaped only: a tree whose
            # own directory cannot be opened still ended the operation.
            if skip_unreadable and isinstance(exc, PermissionError):
                on_skip(SKIP_UNREADABLE_ENTRY, str(src))
                return
            raise
        on_skip(SKIP_SYMLINK, str(src))
        return
    try:
        # No parents=True here, deliberately. Creating a missing ancestor chain by name
        # is exactly what this replaced: every caller's destination parent already
        # exists (a staging directory this process made, the data home, or a backup
        # directory the caller created), so a missing parent means the caller is
        # pointing somewhere it has not validated, and that should surface rather than
        # be materialised through whatever the path resolves to.
        # Whether the ROOT is ours to stamp comes from the kernel, not from a name.
        # `not dst.exists()` is a check with a window after it: a forced restore removes
        # the workspace, the gateway recreates it before the mkdir, and the live directory
        # is then stamped with the archive's metadata. This module's first invariant:
        # no write, and no decision governing a write, may be made through a path name.
        root_dst = create_and_open_dir_pinned(
            dst,
            what=f"{what} destination",
            # Stated by the caller, NOT derived. Deriving it from `skip_existing` is the
            # obvious guess and it is wrong: the snapshot's own staging destination already
            # exists when the walk starts, and it has no `skip_existing` either, so that
            # derivation would refuse every snapshot.
            # Only a REPLACE knows it removed the tree it is about to write.
            must_create=must_create,
            refusal=refusal,
        )
        try:
            _walk(root_src, root_dst, str(src))
            # No metadata write here either. `root_is_ours` comes from the same
            # mkdir-then-open pair as the per-child flag and is unsound for the same
            # reason, so the root is treated exactly like its children.
        finally:
            os.close(root_dst)
    finally:
        os.close(root_src)


def _open_child_dir(
    parent_fd: int,
    entry: str,
    by_name: str,
    on_skip: SkipReporter,
    *,
    skip_unreadable: bool = False,
    after_stat: bool = False,
) -> int | None:
    """Open a child directory through *parent_fd*, or report why it was skipped.

    Returns ``None`` for the two races worth tolerating -- the entry vanished, or it
    stopped being a plain directory between the stat and this open. ``ELOOP`` and
    ``ENOTDIR`` are exactly the swap the pinned open exists to refuse, so they are
    reported rather than raised. WHICH reason depends on *after_stat*: a SOURCE walk
    that stat-ed a directory and now meets a link has watched a swap, and everything
    under that directory is data the bundle was asked to carry, so it reports
    ``SKIP_IDENTITY_CHANGED`` -- the side of the retention split that holds the prune.
    A destination, or a caller that has not looked yet, reports ``SKIP_SYMLINK``, the
    screen. Every other error propagates.

    Extracted because the source and destination sides need identical handling, and
    the version of this code that had it on one side only let the swap the source
    skipped escape the destination as a raw ``OSError``.

    *skip_unreadable* adds one more tolerated case, for a SOURCE side that records
    what it omitted: a directory whose open is REFUSED for permission is reported as
    ``SKIP_UNREADABLE_ENTRY`` rather than ending the walk. It stays off for the
    destination, where the same failure means the copy has nowhere to go.
    """
    try:
        return os.open(entry, dir_flags(), dir_fd=parent_fd)
    except FileNotFoundError:
        on_skip(SKIP_VANISHED, by_name)
        return None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            on_skip(SKIP_IDENTITY_CHANGED if after_stat else SKIP_SYMLINK, by_name)
            return None
        if skip_unreadable and isinstance(exc, PermissionError):
            on_skip(SKIP_UNREADABLE_ENTRY, by_name)
            return None
        raise


# ---------------------------------------------------------------------------
# Verified removal
#
# Everything below owns ONE mechanism: removing something reached through a pinned
# descriptor, where the removal itself must not address a name that could have been
# swapped since it was checked. It lives here rather than at its call sites because
# spelling it once per site -- the interior directories of a trash batch, the batch
# directory, the coarse fallback -- is exactly the per-site respelling this module's own
# docstring names as the failure to avoid.
#
# The policy stays with the caller. Nothing here logs, and nothing here decides whether a
# refusal aborts or is reported and skipped: each function returns what happened and the
# caller words it. That is the same split the walk above already uses.
# ---------------------------------------------------------------------------

#: Reason codes returned by :func:`remove_dir_verified` and :func:`remove_tree_pinned`.
#: The caller words the message; these only classify.
#:
#: Named for the REMOVAL rather than borrowing the ``SKIP_`` prefix its first consumer
#: uses. That consumer has its own ``SKIP_UNREADABLE``/``SKIP_IDENTITY_CHANGED`` for the
#: batch it is skipping, and both families end up in the same log line -- one pair of names
#: meaning two things, with one of them a DIFFERENT string, is how a later reader matching
#: on the value gets it wrong.
REMOVAL_IDENTITY_CHANGED = "removal_identity_changed"
REMOVAL_UNVERIFIABLE = "removal_unverifiable"
REMOVAL_FAILED = "removal_failed"
REMOVAL_STAGE_FAILED = "removal_stage_failed"


def close_all(fds: Iterable[int]) -> None:
    """Close every descriptor in *fds*, tolerating one that is already closed.

    A close that raises must not abandon the rest: a leaked directory descriptor pins its
    inode for the life of the process, and a tree removal opens one per directory.

    Public for the same reason :func:`dir_flags` is -- a second module walking trees the
    same way needs it, and reaching for a private is how the divergence starts.
    """
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass


@dataclass(frozen=True)
class PinnedTree:
    """What one pinned traversal saw, keyed by components relative to the scan root.

    Three maps rather than one because the removal treats them differently: a directory is
    removed with ``rmdir`` after its children, a link is unlinked without ever being
    followed, and a file is unlinked. The values are inodes, which is the part that matters
    -- see :func:`scan_tree_pinned`.
    """

    dirs: dict[tuple[str, ...], int]
    files: dict[tuple[str, ...], int]
    links: dict[tuple[str, ...], int]


@dataclass(frozen=True)
class StagedRemoval:
    """The outcome of one identity-verified directory removal.

    ``staged_name`` is set only when the entry was LEFT under the staging name, which is
    the case a human has to be told about: the object there is not the one that was
    approved, so the name it came from is not this code's to write to either. A removal
    that failed but WAS renamed back therefore reports no staging name -- that is the same
    fact, and a second field carrying it would be a second place for it to be wrong.
    """

    removed: bool
    reason: str | None = None
    staged_name: str | None = None
    error: OSError | None = None


@dataclass(frozen=True)
class TreeRemoval:
    """The outcome of :func:`remove_tree_pinned`.

    ``survivors`` counts entries still present in the closing scan. It is reported rather
    than raised on so a caller can keep a partly-emptied directory listed instead of
    claiming success.

    No ``error`` field, unlike :class:`StagedRemoval`: that one carries the exception
    because its caller RE-RAISES it, and a whole-tree caller does not. It reports the
    reason code and keeps the directory, so an exception object here would be surface
    nothing reads.
    """

    removed: bool
    survivors: int = 0
    reason: str | None = None
    staged_name: str | None = None


def scan_tree_pinned(dir_fd: int, *, device: int) -> PinnedTree:
    """One pinned traversal of the tree under *dir_fd*: its directories, files and links.

    Records the inode of every entry, keyed by components relative to the scan root, plus
    every LINK's key separately. Children are opened relative to the descriptor with
    ``O_NOFOLLOW``, so no path is resolved and a directory that is really a link is not
    descended into. The inodes come from the directory block itself
    (``os.DirEntry.inode``), so recording them costs no extra syscall.

    The inodes are the point. ``O_NOFOLLOW`` refuses a LINK, but a real directory RENAMED
    into a scanned name is not a link and satisfies ``O_DIRECTORY`` -- so moving a live
    directory onto a scanned directory's name redirects later unlinks at live files that
    happen to share a name, and pinning the ROOT does not cover that, because the root's
    own inode is unchanged by a rename inside it. Every directory a removal later opens
    must be the inode this scan recorded. A map read at removal time cannot serve: it
    reports the impostor.

    Refuses an entry on another ``device``, which is how a mount arriving underneath is
    caught: it is not a link either.

    ITERATIVE, with an explicit stack, because a recursive walk of a deeply nested tree
    raises ``RecursionError`` -- which is not ``OSError``, so it escapes callers that turn
    a failed read into a refusal.

    What it costs in descriptors, stated accurately because the obvious guess is wrong: a
    directory's child directories are ALL opened before any of them is visited, so what is
    held at once is the queued frontier, not the current path. A wide tree can therefore
    exhaust descriptors as easily as a deep one. Both surface as ``EMFILE`` -- an ``OSError``
    every caller here already treats as "cannot read this, keep it" -- so the failure is
    contained either way; only the arithmetic differs. Files and links cost nothing, which is
    what makes this bearable in practice: the trees this walks are wide in FILES and narrow
    in directories.

    Opening children eagerly is deliberate rather than incidental. Each child's inode is
    taken from the directory block and cross-checked against the ``fstat`` of the descriptor
    opened a moment later, and that pair is what catches a directory renamed into a scanned
    name between the listing and the open. Deferring the open until the child is visited
    would put every sibling's processing inside that window, trading a documented containment
    property for a descriptor bound. The bound is the thing worth giving up.

    Raises rather than returning a short list: this feeds decisions about deleting the only
    copy of something, so an incomplete answer must not read as "nothing unaccounted for".
    """
    dirs: dict[tuple[str, ...], int] = {}
    files: dict[tuple[str, ...], int] = {}
    links: dict[tuple[str, ...], int] = {}
    # (key prefix, descriptor, whether this function opened it and must close it)
    stack: list[tuple[tuple[str, ...], int, bool]] = [((), dir_fd, False)]
    try:
        while stack:
            here, fd, owned = stack.pop()
            try:
                with os.scandir(fd) as entries:
                    listing = list(entries)
                for entry in listing:
                    key = here + (entry.name,)
                    if entry.is_symlink():
                        links[key] = entry.inode()
                        continue
                    if not entry.is_dir(follow_symlinks=False):
                        files[key] = entry.inode()
                        continue
                    child = os.open(entry.name, dir_flags(), dir_fd=fd)
                    try:
                        info = os.fstat(child)
                        if info.st_dev != device:
                            raise OSError(
                                f"refusing a scanned directory on another device: {entry.name!r}"
                            )
                        # The name was listed by `scandir` and opened a moment later, which
                        # is a check-to-use window like any other -- and this one produces
                        # the map every later verification is made of. A rename in between
                        # means the inode recorded here belongs to the replacement, so the
                        # map blesses it. `entry.inode()` is what the listing saw; the
                        # descriptor is what was opened. They have to agree.
                        if info.st_ino != entry.inode():
                            raise OSError(
                                "refusing a scanned directory that changed between the "
                                f"listing and the open: {entry.name!r}"
                            )
                    except OSError:
                        close_all((child,))
                        raise
                    dirs[key] = info.st_ino
                    stack.append((key, child, True))
            finally:
                if owned:
                    close_all((fd,))
    finally:
        # Whatever is still queued when an error unwinds this: the loop closes each
        # descriptor as it finishes with it, so only the unvisited remainder is left.
        close_all(fd for _key, fd, owned in stack if owned)
    return PinnedTree(dirs=dirs, files=files, links=links)


def open_verified_chain(
    root_fd: int,
    parts: tuple[str, ...],
    *,
    cache: dict[tuple[str, ...], int],
    dirs: Mapping[tuple[str, ...], int],
    device: int,
) -> int:
    """Open the directory named by *parts* under *root_fd*, or raise ``OSError``.

    Every component is opened with ``O_NOFOLLOW`` relative to the previous one, so a
    component that is (or becomes) a link fails the open instead of being followed, and
    each one is admitted only as the inode *dirs* recorded for that name on *device*. A
    rename landing after the scan is therefore refused rather than followed -- which
    ``O_NOFOLLOW`` alone cannot do, since a renamed real directory is not a link.

    Descriptors are cached because one tree has a handful of directories and can have tens
    of thousands of files inside them, so each directory is verified once and then
    addressed by the descriptor that was checked. The cache is the CALLER's, because
    dropping it between phases is what forces a re-check -- see
    :func:`drain_verified_chain`.
    """
    fd = root_fd
    key: tuple[str, ...] = ()
    for part in parts:
        key = key + (part,)
        cached = cache.get(key)
        if cached is not None:
            fd = cached
            continue
        expected = dirs.get(key)
        if expected is None:
            raise OSError(f"refusing a scanned directory the tree scan did not see: {part!r}")
        child = os.open(part, dir_flags(), dir_fd=fd)
        try:
            info = os.fstat(child)
        except OSError:
            close_all((child,))
            raise
        if (info.st_dev, info.st_ino) != (device, expected):
            close_all((child,))
            raise OSError(f"refusing a scanned directory that changed identity: {part!r}")
        cache[key] = child
        fd = child
    return fd


def drain_verified_chain(cache: dict[tuple[str, ...], int]) -> None:
    """Close and forget every descriptor in *cache*.

    Emptied as it goes rather than closed in a loop and cleared afterwards: a failure
    part-way would otherwise leave already-closed descriptors in the cache for a later
    cleanup to close a SECOND time, and a reused number makes that second close land on an
    unrelated file.
    """
    while cache:
        _key, fd = cache.popitem()
        try:
            os.close(fd)
        except OSError:
            pass


def _name_is_free(parent_fd: int, name: str) -> bool:
    """Whether *name* holds nothing under *parent_fd*, as far as one ``stat`` can say.

    Deliberately conservative in both directions: only a definite "nothing is there" answers
    True, so a name that cannot be read is treated as occupied rather than free. See
    :func:`remove_dir_verified` for what this check does and does not buy.
    """
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def remove_dir_verified(
    parent_fd: int,
    name: str,
    *,
    expect: tuple[int, int],
) -> StagedRemoval:
    """Remove the directory at *name* under *parent_fd*, only if it is *expect*.

    *expect* is ``(st_dev, st_ino)``. This is the single owner of rename-verify-remove.
    ``rmdir`` addresses a NAME and so does any check above it, so an actor with write
    access to the parent can swap the name between the two and have an unapproved
    directory removed on another one's approval. So:

    1. the name is renamed to ``.<name>.removing-<random>`` in the SAME parent, which is
       atomic and moves whatever holds the name somewhere only this call knows;
    2. the identity is re-checked THERE, through the parent descriptor, against *expect*;
    3. only that staging name is removed.

    The suffix is random because ``os.rename`` replaces an existing destination silently on
    POSIX: a predictable name is one that can be squatted.

    The rename BACK is the part that is easy to get wrong, so both halves are stated.

    On a MISMATCH it never happens: the object under the staging name is not the one that
    was approved, which means the name it came from is not this code's to write to either --
    and rename replaces its destination, so putting the impostor back would destroy whatever
    now answers to the original name.

    On a failed ``rmdir`` it happens only if the original name is FREE. It has to be put back
    where it can be found: this directory could not be removed because something is inside
    it, and that something is unaccounted-for content nobody listed -- leaving it under an
    unguessable name means nothing can point at it again. But POSIX rename REPLACES a
    directory destination when that destination is an empty directory, so renaming back
    blindly can silently remove one a concurrent writer created at the name. Checking the
    name is free first is a check-to-use pair, and this module says elsewhere that those are
    the mistake it exists to remove -- so what it does and does not buy is worth being exact
    about. There is no no-replace rename for directories in the stdlib (``renameat2``'s
    ``RENAME_NOREPLACE`` is Linux-only and unexposed), so the choice is between this and one
    of two unconditional losses. What the check removes is the DETERMINISTIC case, where
    something already holds the name by the time the removal fails; what is left is a race
    inside two adjacent syscalls whose worst outcome is an empty directory removed, which
    holds no data and is trivially remade. A name found occupied leaves the directory staged
    and reported.

    Never raises for these outcomes and never logs: the caller decides between
    log-and-continue and raise, and words the message. ``error`` carries the underlying
    ``OSError`` for a caller that re-raises.
    """
    try:
        staging = f".{name}.removing-{os.urandom(4).hex()}"
        os.rename(name, staging, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    except OSError as exc:
        return StagedRemoval(removed=False, reason=REMOVAL_STAGE_FAILED, error=exc)
    try:
        moved = os.stat(staging, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        # NOT restored. What is under the staging name is unknown, so putting it back means
        # renaming an unknown object onto the original name -- and POSIX rename REPLACES
        # its destination, which would destroy whatever is there now.
        return StagedRemoval(
            removed=False, reason=REMOVAL_UNVERIFIABLE, staged_name=staging, error=exc
        )
    if not _stat.S_ISDIR(moved.st_mode) or (moved.st_dev, moved.st_ino) != expect:
        # Also NOT restored, and this is the case that matters: an actor who swapped the
        # directory and then placed something at the original name would have had the
        # rename-back destroy it.
        return StagedRemoval(removed=False, reason=REMOVAL_IDENTITY_CHANGED, staged_name=staging)
    try:
        os.rmdir(staging, dir_fd=parent_fd)
    except OSError as exc:
        # The identity matched, so this IS the approved directory and the original name is
        # where it belongs -- but only while nothing else has taken that name. A rename-back
        # that itself fails leaves the staging name, and that -- not a second flag saying the
        # same thing -- is what the caller reports.
        left: str | None = staging
        if _name_is_free(parent_fd, name):
            try:
                os.rename(staging, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            except OSError:
                pass
            else:
                left = None
        return StagedRemoval(
            removed=False,
            reason=REMOVAL_FAILED,
            staged_name=left,
            error=exc,
        )
    return StagedRemoval(removed=True)


#: Outcomes of :func:`put_back_no_clobber`. ``None`` means the name is back.
PUT_BACK_NAME_TAKEN = "name_taken"
PUT_BACK_FAILED = "failed"


def put_back_no_clobber(
    src_parent_fd: int,
    dst_dir_fd: int,
    src_name: str,
    dst_name: str,
    *,
    expect_ino: int,
) -> str | None:
    """Recreate *dst_name* inside *dst_dir_fd* from *src_name*, refusing to replace anything.

    The undo half of "move an entry aside, remove the tree, put it back if the tree would
    not go". It has to be no-clobber: something may have arrived at *dst_name* while the
    entry was out, and that something is a file this code has never read.

    *src_name* is treated as UNTRUSTED, which is the part that is easy to skip. It is a name
    in a directory an actor may be able to write to, and the whole reason this function runs
    is that an earlier step already failed -- so time has passed. The name is opened
    ``O_NOFOLLOW | O_NONBLOCK``, screened for a regular file, and its inode checked against
    *expect_ino* before anything is read from it, and the copy reads that DESCRIPTOR rather
    than re-opening the name. Without ``O_NOFOLLOW``, a name swapped for a symbolic link is
    followed and whatever it points at -- a credential, say -- is linked or copied into the
    destination under a name the caller will treat as its own. ``O_NOFOLLOW`` alone is not
    enough: it does not refuse a FIFO, and opening one for reading blocks until a writer
    appears, so the hardening itself hangs. Both flags are needed, and neither is what
    decides -- the screen below is.

    First choice is ``os.link``, which cannot clobber, and it is called with
    ``follow_symlinks=False`` so a swap landing after the verification links the link itself
    rather than its target. What LANDED is then checked by inode too, because that link is
    still addressed by name.

    Where hard links are unsupported there is no second no-clobber RENAME in the stdlib --
    ``renameat2``'s ``RENAME_NOREPLACE`` is Linux-only and unexposed -- and the two obvious
    substitutes are each a documented loss:

    * ``rename`` after checking the name is free puts a check-to-use window between the look
      and the act, so a file arriving in between is replaced. That is the same
      trusted-a-name mistake this module exists to remove.
    * giving up leaves the tree holding data with nothing that lists it: unreachable and
      unrecoverable.

    ``O_CREAT | O_EXCL`` is the third option and has neither flaw. The create either wins or
    fails with ``EEXIST``, decided inside one syscall, so nothing can arrive in a window;
    and it needs no hard-link support, so it strands nothing. The cost is a copy rather than
    a link, paid only on a filesystem without links and only when a removal already failed.

    Note WHY the copy path cannot be skipped by probing first: ``os.link in
    os.supports_dir_fd`` tests whether the OS accepts ``dir_fd``, not what the MOUNT
    supports, so a filesystem without hard links passes that probe and then refuses the
    call. A guard built on the probe is a guard that fails exactly where it matters.

    Returns ``None`` when the name is back, :data:`PUT_BACK_NAME_TAKEN` when something else
    holds it (nothing was overwritten), or :data:`PUT_BACK_FAILED`.
    """
    # O_NONBLOCK for the same reason `copy_file_pinned` carries it: O_NOFOLLOW refuses a
    # symbolic link and does NOT refuse a FIFO, so a named pipe at this name blocks the open
    # until a writer appears -- forever, with no timeout and no message. That is not a wrong
    # answer, it is NO answer, and it is the one failure an operator cannot read off a log.
    # The flag has no effect on a regular file; it only guarantees the screen below is reached.
    src_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        src = os.open(src_name, src_flags, dir_fd=src_parent_fd)
    except OSError:
        return PUT_BACK_FAILED
    try:
        st = os.fstat(src)
        if not _stat.S_ISREG(st.st_mode) or st.st_ino != expect_ino:
            # The name no longer holds what was moved aside, so there is nothing here this
            # function may put anywhere. Refusing leaves the caller to report the name.
            # S_ISREG is checked as well as the inode because an inode number is reusable:
            # a FIFO created after the entry was unlinked can carry the number this call
            # recorded, and only the mode tells the two apart.
            return PUT_BACK_FAILED
        linked = False
        try:
            os.link(
                src_name,
                dst_name,
                src_dir_fd=src_parent_fd,
                dst_dir_fd=dst_dir_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            return PUT_BACK_NAME_TAKEN
        except (OSError, NotImplementedError):
            pass
        else:
            linked = True
        if linked:
            # The link was addressed by NAME on both ends, so what landed is checked before
            # the caller is told the entry is back.
            try:
                if os.stat(dst_name, dir_fd=dst_dir_fd, follow_symlinks=False).st_ino == expect_ino:
                    return None
            except OSError:
                return PUT_BACK_FAILED
            # NOT unlinked. The name holds something that is not what this call linked, so
            # it is a replacement that arrived in between -- and it may be the only copy of
            # whatever it is. The link this call made is no longer reachable through that
            # name, so there is nothing of ours left to clean up either; reporting the
            # failure is the whole remedy.
            return PUT_BACK_FAILED
        try:
            dst = os.open(
                dst_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=dst_dir_fd,
            )
        except FileExistsError:
            return PUT_BACK_NAME_TAKEN
        except OSError:
            return PUT_BACK_FAILED
        try:
            created = os.fstat(dst)
        except OSError:
            os.close(dst)
            return PUT_BACK_FAILED
        try:
            # From the VERIFIED descriptor, not from the name again.
            os.lseek(src, 0, os.SEEK_SET)
            while True:
                chunk = os.read(src, 1 << 20)
                if not chunk:
                    break
                while chunk:
                    chunk = chunk[os.write(dst, chunk) :]
        except OSError:
            # A half-written file is worse than none: it would carry SOME of the content and
            # silently drop the rest, which reads as a smaller batch rather than as a failure.
            #
            # Emptied FIRST, through the descriptor this call is still holding. That part
            # cannot touch anything else: `dst` is the file this call created, so even if the
            # name now leads elsewhere the truncate lands on our own inode. It means the
            # partial content is gone whether or not the unlink below happens.
            #
            # Then the name is removed, but only while it still holds that same file. The
            # check narrows a two-syscall window rather than closing it - POSIX has no
            # "unlink if this inode" - and the truncate above is what makes the residual
            # bounded: the worst case is a file that arrived at a private name this call
            # created moments earlier, inside a recovery path reached only after a removal
            # already failed. Leaving the name in place instead would satisfy the window
            # completely and leave a zero-length manifest at it, which a later read cannot
            # tell from a corrupted one and a later retry cannot overwrite.
            with suppress(OSError):
                os.ftruncate(dst, 0)
            unlink_verified(dst_dir_fd, dst_name, (created.st_dev, created.st_ino))
            return PUT_BACK_FAILED
        finally:
            os.close(dst)
    finally:
        os.close(src)
    return None


def remove_tree_pinned(
    resolved_path: str,
    *,
    what: str,
    approve: Callable[[int, PinnedTree], str | None],
    refusal: type[Exception] = PinnedPathRefusal,
    keep_until_empty: str | None = None,
) -> TreeRemoval:
    """Remove the directory *resolved_path* and everything in it, by descriptor throughout.

    The whole-tree counterpart of the pieces above, for callers whose answer to "what may
    be deleted here" is "all of it" -- a directory they have already established holds
    nothing worth keeping. What it replaces at those call sites is
    ``shutil.rmtree(path)``, which re-resolves the path: every ancestor is walked again by
    the kernel, so one of them swapped to a link after the caller's own checks is followed
    and the removal lands outside the tree entirely.

    The sequence: pin the parent chain one ``openat`` per component, open the target
    through it with ``O_NOFOLLOW``, scan it once, let *approve* look at what was found,
    remove links and files against the scanned inodes, remove directories deepest-first
    through :func:`remove_dir_verified`, re-scan to decide whether it is actually empty,
    and only then remove the target itself -- verified against the descriptor the whole
    operation was pinned to.

    *approve* is where a caller re-establishes, THROUGH THE PINNED DESCRIPTOR, whatever it
    believes about this directory. It is called once with ``(root_fd, tree)`` before
    anything is removed, and returning a reason string refuses the whole removal having
    touched nothing. It is REQUIRED, and not nullable. Resolving the parent does NOT by
    itself prove the opened directory is the caller's, because ``Path.resolve()`` follows an
    ancestor that is ALREADY a link -- so a swap landing before the resolve produces a
    perfectly pinned walk to the wrong tree, and pinning alone would then delete it. What
    defeats that is the caller asking a question only its own directory can answer, and
    asking it of the descriptor rather than of the path. A removal that asks nothing is the
    weaker mode, and there is no caller that wants it: leaving the parameter optional would
    only make the weak mode available to a future caller by omission.

    *keep_until_empty* names a top-level FILE to remove LAST, and it exists because the
    order is load-bearing rather than tidy. Some trees are only DISCOVERABLE through one
    entry -- an index, a manifest -- and removing that first means a later failure leaves
    the rest of the tree on disk with nothing that lists it: data neither visible nor
    recoverable, while the removal reported a partial success. So the named entry is skipped
    by the file pass, the closing scan must find nothing but it, and only then is it moved
    aside and the tree removed. If the tree still will not go, the entry goes back through
    :func:`put_back_no_clobber`, which cannot overwrite whatever arrived at that name and
    needs no hard-link support to succeed.

    Raises *refusal* on a platform that cannot pin (see
    :func:`supports_pinned_tree_walk`) rather than falling back to a by-name removal. That
    is this module's standing rule: the caller is told and decides. It also raises
    *refusal* when the target cannot be opened at all, which is precisely the ancestor swap
    the pinned walk exists to catch.

    Returns rather than raises for a tree that would not go: ``removed`` is False and
    ``survivors`` says how much is left, so a caller can keep the directory visible instead
    of reporting a success that did not happen.

    *resolved_path* must already be RESOLVED -- see :func:`pin_parent` for why demanding
    that is safe rather than brittle.
    """
    if not supports_pinned_tree_walk():
        raise refusal(
            f"refusing to remove the {what} by name: this platform cannot open relative to "
            "a directory descriptor, so the removal would re-resolve every ancestor"
        )
    target = Path(resolved_path)
    if target.parent == target:
        raise refusal(f"refusing to remove the {what}: {resolved_path!r} has no parent")
    parent_fd = pin_parent(str(target.parent), what=what, refusal=refusal)
    try:
        try:
            root_fd = os.open(target.name, dir_flags(), dir_fd=parent_fd)
        except OSError as exc:
            raise refusal(f"refusing to remove the {what}: {exc}") from exc
        try:
            pinned = os.fstat(root_fd)
            device = pinned.st_dev
            cache: dict[tuple[str, ...], int] = {}
            deferred_key: tuple[str, ...] | None = None
            deferred_ino: int | None = None
            try:
                tree = scan_tree_pinned(root_fd, device=device)
                withheld = approve(root_fd, tree)
                if withheld is not None:
                    # Before ANY removal, so a refusal costs nothing: the directory is
                    # exactly as it was found.
                    return TreeRemoval(
                        removed=False,
                        survivors=len(tree.dirs) + len(tree.files) + len(tree.links),
                        reason=withheld,
                    )
                # Resolved against the SCAN, not by asking the directory again: the entry
                # held back has to be the one this pass saw, so the identity re-checked
                # after the removal has something honest to compare with. Named but absent
                # means there is nothing to defer, which is not an error - a tree with no
                # index entry is simply one where the order does not matter.
                if keep_until_empty is not None and (keep_until_empty,) in tree.files:
                    deferred_key = (keep_until_empty,)
                    deferred_ino = tree.files[deferred_key]
                # Links first and never followed: a link is unlinked, so what it points at
                # is irrelevant -- but only if it is STILL the link the scan saw, because
                # the name could now hold a real file that is somebody's only copy.
                for key, ino in tree.links.items():
                    try:
                        holder = open_verified_chain(
                            root_fd, key[:-1], cache=cache, dirs=tree.dirs, device=device
                        )
                    except OSError:
                        continue
                    unlink_verified(holder, key[-1], (device, ino))
                for key, ino in tree.files.items():
                    if key == deferred_key:
                        # Held back on purpose - see `keep_until_empty`. Removing it now and
                        # then failing to remove the tree would leave data on disk that
                        # nothing lists.
                        continue
                    try:
                        holder = open_verified_chain(
                            root_fd, key[:-1], cache=cache, dirs=tree.dirs, device=device
                        )
                    except OSError:
                        continue
                    unlink_verified(holder, key[-1], (device, ino))
                # Dropped FIRST, so the directory phase re-opens every directory and
                # re-checks its inode. Reusing a descriptor cached during the file phase
                # would satisfy the check with the identity the directory had THEN, and
                # `rmdir` addresses a name.
                drain_verified_chain(cache)
                staged: str | None = None
                for key in sorted(tree.dirs, key=len, reverse=True):
                    try:
                        # The FULL key, so the directory about to go is itself checked
                        # against the scanned inode -- not merely the chain leading to it.
                        open_verified_chain(
                            root_fd, key, cache=cache, dirs=tree.dirs, device=device
                        )
                        holder = open_verified_chain(
                            root_fd, key[:-1], cache=cache, dirs=tree.dirs, device=device
                        )
                    except OSError:
                        continue
                    outcome = remove_dir_verified(
                        holder,
                        key[-1],
                        expect=(device, tree.dirs[key]),
                    )
                    if outcome.staged_name is not None:
                        staged = outcome.staged_name
                drain_verified_chain(cache)
                # The post-condition is a FRESH pinned scan, for the same reason the
                # removal is driven by the first one: asking "is it empty" of the same walk
                # that decided what to delete lets one answer stand in for the other.
                try:
                    left = scan_tree_pinned(root_fd, device=device)
                except OSError:
                    # Cannot confirm it is empty, so it is not treated as empty: removing
                    # the directory now would be doing it on an answer that was never read.
                    return TreeRemoval(
                        removed=False,
                        reason=REMOVAL_UNVERIFIABLE,
                        staged_name=staged,
                    )
                remaining = dict(left.files)
                if deferred_key is not None:
                    # By INODE, not by name. Everything after this treats the survivor as
                    # the entry that was deliberately kept: it is moved aside and, once the
                    # tree is gone, unlinked. A file substituted at that name after the
                    # first scan would otherwise be accepted here and then destroyed.
                    if remaining.pop(deferred_key, None) != deferred_ino:
                        return TreeRemoval(
                            removed=False,
                            survivors=len(left.dirs) + len(left.files) + len(left.links),
                            reason=REMOVAL_IDENTITY_CHANGED,
                            staged_name=staged,
                        )
                survivors = len(left.dirs) + len(remaining) + len(left.links)
                if survivors:
                    return TreeRemoval(
                        removed=False,
                        survivors=survivors,
                        reason=REMOVAL_FAILED,
                        staged_name=staged,
                    )
            finally:
                drain_verified_chain(cache)
            if deferred_key is None or deferred_ino is None:
                outcome = remove_dir_verified(
                    parent_fd,
                    target.name,
                    expect=(pinned.st_dev, pinned.st_ino),
                )
                return TreeRemoval(
                    removed=outcome.removed,
                    reason=outcome.reason,
                    staged_name=outcome.staged_name,
                )
            return _remove_with_deferred_entry(
                parent_fd,
                root_fd,
                target.name,
                deferred_key[0],
                deferred_ino,
                (pinned.st_dev, pinned.st_ino),
            )
        finally:
            close_all((root_fd,))
    finally:
        close_all((parent_fd,))


def _remove_with_deferred_entry(
    parent_fd: int,
    root_fd: int,
    name: str,
    entry: str,
    entry_ino: int,
    expect: tuple[int, int],
) -> TreeRemoval:
    """Move the deferred entry out, remove the now-empty tree, then delete the entry.

    The entry and the directory have to go TOGETHER, and ``rmdir`` cannot run while the
    entry is still in there. Unlinking it first leaves a window that a file created after
    the closing scan turns into silent loss: the ``rmdir`` then fails on a non-empty
    directory, and the tree - now without the entry that made it discoverable - is data on
    disk that nothing lists.

    So the entry is MOVED to the parent under an unguessable debris name instead of deleted.
    From there the tree can be removed, and if that fails the entry goes straight back,
    leaving it discoverable exactly as it was. A crash between the two renames leaves one
    small file rather than an unreadable tree.

    The way back is :func:`put_back_no_clobber`, never ``rename``: POSIX rename REPLACES its
    destination silently, which is the property the debris name is chosen to be safe
    against, and this direction needs the opposite - a file that arrived at the entry's name
    in the interval must not be clobbered. The debris is unlinked only once the entry is
    back, so no window has neither.
    """
    debris = f".{name}.{entry}.removing-{os.urandom(4).hex()}"
    try:
        os.rename(entry, debris, src_dir_fd=root_fd, dst_dir_fd=parent_fd)
    except OSError:
        return TreeRemoval(removed=False, reason=REMOVAL_STAGE_FAILED)
    landed: int | None = None
    try:
        landed = os.stat(debris, dir_fd=parent_fd, follow_symlinks=False).st_ino
    except OSError:
        pass
    if landed != entry_ino:
        # The rename moved something that is not the verified entry, so the unlink at the
        # end of this would destroy it. Left as debris, named for a human.
        return TreeRemoval(removed=False, reason=REMOVAL_IDENTITY_CHANGED, staged_name=debris)
    outcome = remove_dir_verified(parent_fd, name, expect=expect)
    if not outcome.removed:
        put_back_no_clobber(parent_fd, root_fd, debris, entry, expect_ino=landed)
        # The debris is KEPT here, and that is the whole point of this branch. The tree
        # survived, so the entry is what makes it discoverable, and the check available
        # before an unlink can only confirm that DEBRIS is still the file that was staged -
        # never that the name it was put back to still holds a good copy. A file replaced at
        # the entry's name after the put-back would leave the debris as the last readable
        # copy, and unlinking it would turn a batch that is merely stuck into one nothing
        # lists and nothing can restore, which is exactly the loss the deferral exists to
        # prevent. The cost is one small named file per stuck cleanup, reported below.
        return TreeRemoval(
            removed=False,
            reason=outcome.reason,
            # The directory's staging name first when there is one: the whole tree sits
            # under it, so it is the more urgent thing to name. Otherwise the debris, so no
            # retained copy is left unmentioned.
            staged_name=outcome.staged_name or debris,
        )
    unlink_verified(parent_fd, debris, (expect[0], landed))
    return TreeRemoval(removed=True)


# This module keeps the hand-written ``__all__``, the two openers under a pinned parent
# (:func:`open_in_pinned_parent`, :func:`open_dir_pinned`) and the tree operations:
# :func:`stage_tree_pinned` and the verified scan, removal and put-back of a tree. The
# primitives and the single-entry operations live in the owners below, and every name they
# define still resolves as ``kiro_crew.pinned_fs.<name>``. A write through this module
# (``monkeypatch.setattr``, ``mock.patch``) reaches every owner holding the name
# (``kiro_crew._facade_mirror``). No owner imports this module; an owner imports only the
# owners listed before it.
# New work goes to the owner of its responsibility:
#   ``pinned_fs_primitives`` -- refusal, skip reasons, platform probes, pin_parent, fd_real_path
#   ``pinned_fs_dirs`` -- creating and opening directories under a pinned ancestor
#   ``pinned_fs_files`` -- copying, writing, reading and unlinking one file
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.pinned_fs_primitives",
    "kiro_crew.pinned_fs_dirs",
    "kiro_crew.pinned_fs_files",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.pinned_fs_files import _apply_metadata  # noqa: F401
    from kiro_crew.pinned_fs_primitives import (  # noqa: F401
        _CREATE_ATTEMPTS,
        _NEVER_ARCHIVED,
        _OMITS_WANTED_DATA,
    )
