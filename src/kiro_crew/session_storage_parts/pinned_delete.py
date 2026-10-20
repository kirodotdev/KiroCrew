"""Deleting a batch's listed files without following a swapped directory component.

An owner module of :mod:`kiro_crew.session_storage`, the one import path: callers,
tests and documents reach every name here as ``kiro_crew.session_storage.<name>``.

Where the platform can delete by descriptor (``_FD_SAFE_DELETE``),
:func:`_delete_listed_files` removes only the files the manifest lists, checked against the
approval's digest when one is supplied, through a walk pinned by descriptor; elsewhere a
batch is removed whole (:func:`_coarse_remove`), renamed aside to an unguessable name
first when an approved identity is supplied. Renaming a batch aside and back, and the
progress reporting of a delete, live here too.
"""

from __future__ import annotations

import os
import shutil
import stat
import uuid
from contextlib import suppress
from pathlib import Path, PurePosixPath, PureWindowsPath

from kiro_crew import pinned_fs, platform_compat
from kiro_crew.session_storage_parts.batch_manifest import _manifest_rels, _rels_digest
from kiro_crew.session_storage_parts.store_layout import (
    MANIFEST_NAME,
    SKIP_IDENTITY_CHANGED,
    SKIP_INCOMPLETE,
    SKIP_UNLISTED_FILES,
    SKIP_UNREADABLE,
    BatchIdentity,
    EmptyProgress,
    logger,
)

# How many deleted files pass between two progress callbacks. Per file would be six
# figures of calls for one batch of the size this reporting exists for.
_PROGRESS_EVERY_FILES = 64


#: True when this platform can delete a file by (directory fd, name) and can open a
#: directory refusing to follow a link. Both are what make a per-file delete safe;
#: Windows has neither, so it takes the coarse path below.
#:
#: The pinned-walk half is asked of :mod:`kiro_crew.pinned_fs` rather than restated here.
#: That module exists because spelling this mechanism per call site does not converge, so
#: a second spelling is the failure it exists
#: to end. What is added on top is only what THIS path needs beyond walking a tree: the
#: three mutating calls it makes relative to a descriptor.
_FD_SAFE_DELETE = (
    pinned_fs.supports_pinned_tree_walk()
    # `os.link` is here for the manifest RECOVERY: putting the manifest back must fail
    # rather than replace, and `rename` cannot express that. A platform without it takes
    # the coarse path instead of reaching a recovery it cannot perform safely.
    and {os.unlink, os.rmdir, os.rename, os.link} <= os.supports_dir_fd
    and os.scandir in os.supports_fd
)

#: Flags for every directory this module opens on the delete path, from the same source as
#: the walk. Called rather than captured at import: the Windows-simulation tests delete
#: ``os.O_NOFOLLOW`` at runtime, and a frozen constant would keep offering a flag the
#: platform has dropped.
_dir_open_flags = pinned_fs.dir_flags


#: Descriptor bookkeeping for the pinned walks below, from the same source as the walk
#: itself. Aliases rather than second copies: a leaked descriptor pins its inode for the
#: life of the process, and the drain's "pop as you go" rule exists so a failure part-way
#: cannot leave an already-closed number for a later cleanup to close again.
_close_all = pinned_fs.close_all
_drain = pinned_fs.drain_verified_chain

#: The pinned traversal and the verified chain-open live in :mod:`kiro_crew.pinned_fs`
#: with the rest of the mechanism, because they are
#: consumer-agnostic - "walk a tree without ever re-resolving a name" and "open a
#: component only as the inode a scan recorded" say nothing about batches, manifests or
#: approval. What stays in this module is the trash-specific part: which map
#: authorises the delete, and what a refusal means to the user.
_scan_tree = pinned_fs.scan_tree_pinned
_open_chain = pinned_fs.open_verified_chain


def _plain_parts(rel: str) -> tuple[str, ...] | None:
    """The components of *rel*, or None if it is not a plain path under a batch.

    A manifest is a file on disk: a tampered or corrupted one can name an absolute
    path or walk out with ``..``. Every consumer of a manifest entry goes through
    here first, so "is this name usable" is answered in ONE place rather than at each
    call site - the size read below skipped this check when it was written inline in
    the delete, and stat'd whatever the manifest said.

    Absoluteness is asked of the PATH rather than of any spelling of a separator.
    Enumerating those was the earlier bug: ``PurePosixPath("//tmp/victim").parts`` is
    ``("//", "tmp", "victim")`` - a POSIX root of two slashes, which a check against
    ``"/"`` misses - and an absolute path passed to ``os.open`` ignores ``dir_fd``
    entirely, so the open leaves the batch. Both flavours are consulted because the
    coarse path joins these names with the local ``Path``: on Windows ``..\\x`` is a
    parent reference and ``C:\\x`` is absolute, neither of which POSIX parsing sees.

    A DRIVE is rejected whether or not the path is absolute, which is not the same
    question. ``C:.ssh/id_rsa`` is drive-RELATIVE - ``is_absolute()`` is False - yet
    joining it onto the batch on Windows replaces the anchor, because pathlib lets a
    right-hand side that carries a drive take over, and it then resolves against that
    drive's working directory. So the size read would stat a path outside the batch and
    report its existence and size. Asking for ``.drive`` covers every spelling of that,
    including ``c:y``, which a check for a component ending in a colon does not see.

    That also refuses a POSIX file legitimately named ``a:b``, since Windows parsing
    reads the same two characters as a drive. Accepted deliberately: this store names
    its own files, so nothing it writes looks like that, and the cost of being wrong is
    one batch reported incomplete and kept rather than a path escaping the batch.

    An embedded NUL is refused with no such trade, because no file name can contain one:
    a JSON manifest CAN carry it (``"\\u0000"``), and every ``os`` call then raises
    ``ValueError``, which is not ``OSError`` and so was not caught where a bad name is
    meant to be skipped - it escaped mid-loop and left the batch half-deleted.
    """
    if PurePosixPath(rel).is_absolute() or PureWindowsPath(rel).is_absolute():
        logger.warning("refusing a staged path that is absolute")
        return None
    if PureWindowsPath(rel).drive:
        logger.warning("refusing a staged path that names a drive")
        return None
    parts = PurePosixPath(rel).parts
    if not parts or any(
        part in ("", ".", "..") or "\\" in part or "\x00" in part for part in parts
    ):
        logger.warning("refusing a staged path that is not a plain name")
        return None
    return parts


def _listed_bytes(batch: Path, rels: list[str]) -> int:
    """Best-effort size of the manifest's files plus the manifest itself.

    Statting the named files rather than walking the tree, for the same reason the
    delete does not walk it - and only names that pass :func:`_plain_parts`, so a
    tampered entry cannot make this measure a file outside the batch.
    """
    total = 0
    for rel in rels:
        parts = _plain_parts(rel)
        if parts is None:
            continue
        try:
            total += batch.joinpath(*parts).lstat().st_size
        # ValueError as well as OSError: a name the validator somehow still admits must
        # cost this ONE file, never the whole read. `os` raises ValueError, not OSError,
        # for a name it cannot even pass to the kernel.
        except (OSError, ValueError):
            continue
    try:
        total += (batch / MANIFEST_NAME).lstat().st_size
    except OSError:
        pass
    return total


def _open_absolute_nofollow(path: Path) -> tuple[int, int]:
    """Open *path* and its parent, walking from the filesystem root.

    Returns ``(parent fd, path fd)``. The parent is returned because removing the
    directory itself must also happen by descriptor: ``rmtree`` and ``rmdir`` take a
    PATH, which re-resolves the whole prefix, so the last step of the delete would
    reopen the batch through exactly the ancestors this walk exists to pin.

    The walk itself is :func:`kiro_crew.pinned_fs.pin_parent` - one ``openat`` per
    component, each ``O_NOFOLLOW`` - rather than a second copy of it here. ``O_NOFOLLOW``
    constrains only the LAST component, so opening the batch by its path left every
    ancestor to be re-resolved by the kernel: the trash root and the directories above it
    are writable by the same user, and one swapped to a link after validation is followed,
    after which every unlink through the returned descriptors points outside the trash.

    *path* must already be RESOLVED, which is both what ``pin_parent`` requires and what
    makes demanding it safe rather than brittle: a resolved path contains no links, so
    ``O_NOFOLLOW`` can only fail because something changed underneath - the case worth
    failing on - and not on an install whose data home legitimately sits behind a symlinked
    home directory, which a naive "no links anywhere" rule would refuse outright.
    """
    if path.parent == path:
        raise OSError(f"refusing to open {path}: it has no parent")
    parent = pinned_fs.pin_parent(str(path.parent), what="trash batch", refusal=OSError)
    try:
        fd = os.open(path.name, _dir_open_flags(), dir_fd=parent)
    except OSError:
        _close_all((parent,))
        raise
    return parent, fd


def _remove_scanned_dirs(
    batch_fd: int,
    dirs: dict[tuple[str, ...], int],
    device: int,
    cache: dict[tuple[str, ...], int],
) -> None:
    """Remove the batch's directories, deepest first, each by ``(parent fd, name)``.

    Returns nothing: the fresh post-condition scan is what decides whether the batch is
    empty, so a second answer from here was bookkeeping no caller read. Replaces a recursive
    sweep that re-discovered
    children by name and descended into whatever it found: a directory swapped in after
    verification was recursed into and its links and empty directories removed, because
    discovery cannot tell an approved directory from a substituted one.

    Driven by the APPROVED map instead, so there is nothing to discover. Each parent is
    reached through :func:`_open_chain`, which admits a component only as the inode the
    approval recorded, and the removal is ``rmdir`` relative to that descriptor - so a
    directory the approval never named is not opened, not descended into, and not removed.

    Deepest first, by component count, because ``rmdir`` needs a directory to be empty and
    a parent cannot go before its children. Iterative for the same reason the scan is: a
    deep tree must not raise ``RecursionError``.

    The removal itself goes through a rename to an unguessable name, because ``rmdir``
    addresses a NAME and so does the check above it: an actor with write access to the
    parent could swap the name between the two and have an unapproved directory removed on
    another one's approval. That mechanism is
    :func:`kiro_crew.pinned_fs.remove_dir_verified`, which is where the batch directory and
    the whole-tree removal get it too - one owner rather than a copy per site. What stays
    here is the policy: this path logs and CONTINUES, because the post-condition scan below
    is what decides whether the batch is incomplete.
    """
    for key in sorted(dirs, key=len, reverse=True):
        try:
            # The FULL key, so the directory about to be removed is itself checked against
            # the approved inode - not merely the chain leading to it. A top-level staged
            # directory's parent IS the batch, so verifying only the parent verified
            # nothing about it, and `rmdir` addresses a NAME: an empty directory swapped
            # into that name would have been removed on the approval of another one.
            _open_chain(batch_fd, key, cache=cache, dirs=dirs, device=device)
            parent = _open_chain(batch_fd, key[:-1], cache=cache, dirs=dirs, device=device)
        except OSError as exc:
            logger.warning("could not reach staged directory %r: %s", key[-1], exc)
            continue
        outcome = pinned_fs.remove_dir_verified(
            parent,
            key[-1],
            expect=(device, dirs[key]),
        )
        if outcome.removed:
            continue
        if outcome.reason == pinned_fs.REMOVAL_IDENTITY_CHANGED:
            # The object under the staging name is NOT the approved directory, so the listed
            # name is not ours to write to either. An actor who swapped the directory and
            # then placed something at the listed name would have had a rename-back destroy
            # it. It stays under the unguessable name, logged, and the post-condition
            # reports the batch incomplete.
            logger.error(
                "refusing a staged directory that is not the one that was approved; it is "
                "now %r and is left there rather than renamed back",
                outcome.staged_name,
            )
        elif outcome.reason == pinned_fs.REMOVAL_UNVERIFIABLE:
            # Also not put back: what is under the staging name is unknown, so renaming it
            # onto the listed name would replace whatever is there now.
            logger.warning(
                "could not re-check staged directory %r; leaving it as %r: %s",
                key[-1],
                outcome.staged_name,
                outcome.error,
            )
        elif outcome.reason == pinned_fs.REMOVAL_STAGE_FAILED:
            logger.warning(
                "could not stage staged directory %r for removal: %s", key[-1], outcome.error
            )
        else:
            logger.warning("could not remove staged directory %r: %s", key[-1], outcome.error)
            if outcome.staged_name is not None:
                # The identity matched, so this IS the approved directory - but it could not
                # be put back, and a directory under a staging name is one nothing
                # recognises.
                logger.error(
                    "staged directory %r is now %r and could not be put back",
                    key[-1],
                    outcome.staged_name,
                )


def _unlink_debris(parent_fd: int, debris: str, expect_ino: int | None) -> None:
    """Unlink the moved-aside manifest, only while that name still holds it.

    The debris name lives in the trash root, and by the time this runs the batch removal has
    either succeeded or failed - so time has passed in a directory an actor may be able to
    write to. Unlinking by name alone would destroy whatever answers to it by then, which is
    the same trusted-a-name mistake every other removal on this path avoids.
    Best effort: a name that has stopped holding the manifest is left alone, not chased.
    """
    if expect_ino is None:
        return
    with suppress(OSError):
        if os.stat(debris, dir_fd=parent_fd, follow_symlinks=False).st_ino == expect_ino:
            os.unlink(debris, dir_fd=parent_fd)


def _inode_of(name: str, dir_fd: int) -> int | None:
    """Return *name*'s inode relative to *dir_fd*, or None if it cannot be read.

    Links are not followed: the question is always what this NAME holds, never what it
    points at.
    """
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_ino
    except OSError:
        return None


def _remove_pinned_batch(parent_fd: int, batch_fd: int, name: str) -> None:
    """Remove the batch directory, or raise ``OSError`` having removed nothing.

    The last `rmdir` had the same flaw the interior ones did, one level up: the final scan
    proves the batch is empty by DESCRIPTOR, and then the removal addressed a NAME. A late
    file plus a swap of the batch's name in that interval removed an empty replacement
    instead - and by then the manifest has already been moved aside, so the real batch was
    left holding data with nothing to list it, while the caller reported success.

    So the name is moved to one nothing can predict, its identity is checked against the
    descriptor the whole operation was pinned to, and only that name is removed - by
    :func:`kiro_crew.pinned_fs.remove_dir_verified`, the same primitive the interior
    directories use. Raising rather than reporting lets the caller's existing recovery run
    unchanged: it renames the manifest back through ``batch_fd``, so it lands in the REAL
    batch and the batch stays listed and restorable.
    """
    expected = os.fstat(batch_fd)
    outcome = pinned_fs.remove_dir_verified(
        parent_fd,
        name,
        expect=(expected.st_dev, expected.st_ino),
    )
    if outcome.removed:
        return
    if outcome.reason == pinned_fs.REMOVAL_STAGE_FAILED:
        # Nothing was moved, so there is nothing to report about a staging name. Raised as
        # the OSError the rename itself produced, which is what this function's callers
        # already handle.
        raise outcome.error or OSError(f"could not stage the batch for removal: {name!r}")
    if outcome.reason in (pinned_fs.REMOVAL_IDENTITY_CHANGED, pinned_fs.REMOVAL_UNVERIFIABLE):
        # NOT renamed back. Whatever is under the staging name is not the pinned batch, so
        # the batch's name is not ours to write to either - and POSIX rename REPLACES its
        # destination, so putting this back would destroy whatever now answers to that name.
        # It stays under the unguessable name, named in the log for a human to deal with.
        logger.error(
            "the batch name no longer denotes the pinned batch; what was there is now %r "
            "in the trash root and is left there rather than renamed back",
            outcome.staged_name,
        )
        raise OSError(f"the batch name no longer denotes the pinned batch: {name!r}")
    if outcome.staged_name is not None:
        # The identity matched, so this IS the pinned batch - but the rmdir failed AND it
        # could not be renamed back, so it is now under a name `list_trash` does not offer.
        logger.error(
            "staged directory %r is now %r and could not be put back", name, outcome.staged_name
        )
    raise outcome.error or OSError(f"could not remove the batch directory: {name!r}")


def _stage_batch_by_name(batch: Path, *, expect: tuple[int, int]) -> tuple[Path | None, str | None]:
    """Rename *batch* aside inside the trash root and verify its identity THERE.

    The half of rename-verify-remove that works on a platform with no ``openat``: there is
    no descriptor to bind a removal to, and checking the path and then handing that same
    path to a remover re-resolves it, so a swap in between is followed and an unapproved
    replacement destroyed. The rename is what makes the check meaningful rather than
    decorative. After it the approved name is gone, so nothing can be substituted
    at it; the identity is verified on the RENAMED directory, so a swap that landed before
    the rename is caught and the impostor is put back rather than removed; and the name a
    caller finally removes existed for microseconds and carries random characters.

    NOT airtight, and better said than implied: the caller's removal still resolves the
    staging path, so an actor who can OBSERVE that name inside the window can redirect it
    through an ancestor swapped afterwards. That residual is accepted on this platform
    because the alternative is worse in both directions - refusing the empty a user
    explicitly asked for, or leaving a batch behind after every restore, which no test and
    no user would read as success.

    Returns ``(staged path, None)`` when the caller may remove it, or ``(None, skip code)``
    with the batch already back under the name the user saw. One owner for three callers:
    the explicit empty, and the two cleanups that follow work which already succeeded.
    """
    staging = batch.parent / f".{batch.name}.removing-{uuid.uuid4().hex[:8]}"
    try:
        os.rename(batch, staging)
    except OSError as exc:
        logger.warning("refusing to remove trash batch %r: %s", batch.name, exc)
        return None, SKIP_UNREADABLE
    try:
        current = os.stat(staging, follow_symlinks=False)
    except OSError as exc:
        logger.warning("refusing to remove trash batch %r: %s", batch.name, exc)
        _rename_back(staging, batch)
        return None, SKIP_UNREADABLE
    if (current.st_dev, current.st_ino) != expect:
        logger.warning(
            "refusing to remove trash batch %r: it is not the directory that was selected",
            batch.name,
        )
        _rename_back(staging, batch)
        return None, SKIP_IDENTITY_CHANGED
    return staging, None


def _rename_back(staging: Path, batch: Path) -> None:
    """Put a staged-for-removal directory back under the name the user saw.

    Best effort by necessity - if this fails there is nothing further to try - but logged
    loudly, because a batch left under a staging name is one `list_trash` does not offer:
    visible nowhere, restorable never. The staging name carries the batch id so a human
    can finish the job by hand.
    """
    try:
        os.rename(staging, batch)
    except OSError:
        logger.error(
            "could not restore trash batch %r from its staging name %r; it is on disk but "
            "will not be listed until the directory is renamed back",
            batch.name,
            staging.name,
        )


def _coarse_remove(
    target: Path,
    rels: list[str],
    on_progress: EmptyProgress | None,
    base_bytes: int,
) -> tuple[int, str | None]:
    """``shutil.rmtree`` with an honest byte figure and a post-condition.

    The removal for platforms with neither ``openat`` nor ``O_NOFOLLOW``. The bytes are
    measured AFTER the attempt and the survivors subtracted, because ``ignore_errors``
    means a locked file - the normal Windows failure - leaves the tree standing while
    ``rmtree`` returns quietly, and the up-front figure would then be reported as freed
    with the bytes still on disk. Whether the directory actually went is the separate
    question :func:`_incomplete_if_present` answers.
    """
    before = _listed_bytes(target, rels)
    shutil.rmtree(target, ignore_errors=True)
    freed = before - _listed_bytes(target, rels)
    if on_progress is not None:
        on_progress(base_bytes + freed)
    return freed, _incomplete_if_present(target)


def _delete_listed_files(
    batch: Path,
    on_progress: EmptyProgress | None,
    base_bytes: int,
    expect_identity: BatchIdentity | None = None,
) -> tuple[int, str | None]:
    """Delete the files the manifest names, reporting bytes freed as it goes.

    Returns ``(bytes freed, skip code or None)``. A skip code means the batch is still
    there and the caller must say so: reporting bytes alone left a batch that survived
    looking exactly like an empty one - "0 bytes freed, success" - which is the silent
    refusal this module's skip codes exist to remove. The two ways that happens are the
    batch not opening at all, and the tree not being gone afterwards.

    Driven by the MANIFEST, not by a directory walk, and that is a safety property
    rather than a convenience. A walk has to decide per directory entry whether to
    descend, and on Windows a junction is not a symlink - ``os.path.islink`` reports
    False for one - so ``os.walk`` descends into it and would unlink the files it
    points at, outside the trash entirely. Naming the files means nothing is ever
    discovered by traversal.

    Each file is then removed by ``(directory fd, name)``, with every directory
    component opened ``O_NOFOLLOW`` from the batch down. That is what makes the
    per-file delete safe rather than merely validated: checking a path and then
    unlinking it re-resolves the prefix, so a component swapped to a link in between is
    followed and the same-named file outside the trash deleted. Nothing here resolves a
    path at unlink time, so there is no window to win.

    Removing the emptied directories is by descriptor too, bottom-up, so no step of
    this path ever resolves a path - not even the last one. Finishing with
    ``rmtree(batch)`` re-resolved the whole prefix, which reopened the batch through
    exactly the ancestors the walk above exists to pin. Callers reach here only after
    :func:`_unlisted_files` has confirmed the batch holds nothing the manifest omits,
    so "every listed file" is every file.

    Progress is per file rather than per batch because that is the point of naming the
    files: the store this exists for stages a single batch holding tens of thousands of
    sessions, where "one batch done" is one step from nothing to finished, minutes
    later. Where the platform can neither open a directory refusing to follow a link
    nor delete relative to one - Windows has neither - the coarse ``rmtree`` is taken
    instead, with the bytes measured after the attempt and the survivors subtracted,
    and progress degrades to one report per batch. A smoother bar is not worth a weaker
    delete.

    ``base_bytes`` is what earlier batches in the same call already freed, so the
    number handed to ``on_progress`` is always the total for the whole operation and
    never a figure that restarts per batch.
    """
    # BEFORE the manifest is read, and before the platform branch, because both paths
    # destroy data on the strength of what it says. A symlink or an NTFS junction at this
    # name was not written here - the product writes it with `atomic_write` - so its
    # entries describe something else, and on the coarse path `rmtree` would remove the
    # link while a locked staged file survived, leaving a batch that `list_trash()` omits
    # with data still inside it.
    #
    # `is_link_or_junction` rather than `is_symlink()`: on Windows a junction reports False
    # for the latter, and the coarse path is the Windows path.
    #
    # The descriptor path checks this again from its pinned scan. That is not duplication:
    # this one is computed from a path and so cannot see a link planted after it, while the
    # scan's view can - and only this one runs on the coarse path at all.
    if platform_compat.is_link_or_junction(batch / MANIFEST_NAME):
        logger.warning(
            "refusing to empty %r: its manifest is a link, so its listing is not this "
            "batch's own",
            batch.name,
        )
        return 0, SKIP_UNREADABLE
    rels = _manifest_rels(batch)
    # The listing is WHAT the delete is allowed to remove, so it has to be the listing the
    # approval was computed from. Its inode is not enough: rewritten in place the manifest
    # keeps that inode, and every other identity check still passes because none of the
    # FILES changed - only which of them the delete now believes it may unlink. A file
    # already sitting in the batch, unlisted, is refused today; add it to the listing after
    # the approval and it would be deleted as if the user had approved it.
    if expect_identity is not None and expect_identity.rels_digest is not None:
        if _rels_digest(rels) != expect_identity.rels_digest:
            logger.warning(
                "refusing to empty %r: its manifest lists different files than the ones "
                "that were selected",
                batch.name,
            )
            return 0, SKIP_IDENTITY_CHANGED
    if not _FD_SAFE_DELETE:
        if expect_identity is None:
            return _coarse_remove(batch, rels, on_progress, base_bytes)
        # Rename aside inside the trash root, verify the identity THERE, remove only the
        # staged name - see :func:`_stage_batch_by_name` for why the rename is what makes
        # the check meaningful, and for the residual this platform accepts. Shared with the
        # two cleanup paths rather than respelled here.
        staging, skip = _stage_batch_by_name(
            batch, expect=(expect_identity.dev, expect_identity.ino)
        )
        if staging is None:
            return 0, skip
        freed, skip = _coarse_remove(staging, rels, on_progress, base_bytes)
        if skip is not None:
            # A tree that would not go must not be left under a staging name: `list_trash`
            # would not offer it, so the batch would be neither visible nor restorable. Put
            # the name back, which is where the user last saw it.
            _rename_back(staging, batch)
        return freed, skip

    try:
        parent_fd, batch_fd = _open_absolute_nofollow(batch)
    except OSError as exc:
        # A refusal, not a silent success: the batch is still on disk and still on the
        # user's screen, and an open that fails here is exactly the ancestor swap this
        # walk exists to catch.
        logger.warning("refusing to empty %r: %s", batch.name, exc)
        return 0, SKIP_UNREADABLE

    cache: dict[tuple[str, ...], int] = {}
    freed = 0
    files = 0
    cleared = False
    try:
        try:
            # The approved directory, or nothing. `batch` is a NAME, and the mutation lock
            # was released between the snapshot that showed the user what would go and this
            # open - so a directory moved into that name would be deleted on the strength of
            # consent given for a different one. Checked on the DESCRIPTOR rather than by
            # another stat of the path: the fd is the object every removal below actually
            # addresses, so a further swap after this point does not reach the data. Nothing
            # is deleted yet, so refusing here costs only the batch.
            #
            # Unconditional even when there is nothing to compare against, because the same
            # call supplies the device every directory INSIDE the batch is pinned to.
            try:
                opened = os.fstat(batch_fd)
            except OSError as exc:  # pragma: no cover - defensive
                logger.warning("refusing to empty %r: %s", batch.name, exc)
                return 0, SKIP_UNREADABLE
            if expect_identity is not None and (opened.st_dev, opened.st_ino) != (
                expect_identity.dev,
                expect_identity.ino,
            ):
                logger.warning(
                    "refusing to empty %r: it is not the directory that was selected",
                    batch.name,
                )
                return 0, SKIP_IDENTITY_CHANGED
            device = opened.st_dev
            # One traversal of the batch through its own descriptor, before anything is
            # removed: it records the inode of every directory inside it and names every
            # file actually present. The caller's `_unlisted_files` asks the same "is
            # anything here unaccounted for" question by PATH; this re-establishes it from
            # the pinned descriptor, so the answer the delete acts on is the one it can
            # bind to.
            try:
                scanned = _scan_tree(batch_fd, device=device)
            except OSError as exc:
                logger.warning("refusing to empty %r: %s", batch.name, exc)
                return 0, SKIP_UNREADABLE
            dirs, present, links = scanned.dirs, scanned.files, scanned.links
            # The interior has to match the APPROVAL, not merely be self-consistent. A map
            # built here records whatever is present by now - a directory swapped in during
            # the handoff included - so it cannot be the thing that authorises the delete.
            # `staged_targets` recorded the same map under the mutation lock; equality is
            # demanded in both directions, so a directory added, removed or replaced since
            # then is a refusal rather than something to reconcile. A concurrent restore
            # that removed a staged directory lands here too, and refusing is right: the
            # approval does not describe the batch any more.
            approved = None if expect_identity is None else expect_identity.dirs
            if approved is not None and dirs != approved:
                logger.warning(
                    "refusing to empty %r: its directories are not the ones that were " "selected",
                    batch.name,
                )
                return 0, SKIP_IDENTITY_CHANGED
            verify = dirs if approved is None else approved
            # The files and links get the same treatment, and for a sharper reason than
            # symmetry: the per-file identity check further down compares each name against
            # the scan taken HERE, which is self-consistent and authorises nothing. A listed
            # file replaced during the handoff would have its replacement's inode recorded,
            # matched, and unlinked - an unapproved file destroyed on consent given for a
            # different one. Comparing the whole map against the approval refuses the batch
            # instead. A concurrent restore that removed staged files lands here too, and
            # refusing is right for the same reason it is right for directories: the
            # approval does not describe the batch any more.
            if expect_identity is not None:
                for label, seen_now, was in (
                    ("files", present, expect_identity.files),
                    ("links", links, expect_identity.links),
                ):
                    if was is not None and seen_now != was:
                        logger.warning(
                            "refusing to empty %r: its %s are not the ones that were selected",
                            batch.name,
                            label,
                        )
                        return 0, SKIP_IDENTITY_CHANGED
            # A manifest that is a SYMLINK is not this batch's manifest. It matters here
            # rather than at read time because the link loop below unlinks every recorded
            # link, and the manifest is only excluded from the FILE loop - so a symlink at
            # that name is removed early, defeating the whole reason the real manifest goes
            # last. What that costs is not the link but the listing: `list_trash()` omits a
            # batch with no readable manifest, so a batch that then fails to empty (one
            # unwritable directory, one file held open) leaves the user data on disk they
            # can neither see nor restore.
            #
            # Refusing beats deferring it to the end. The product writes this file with
            # `atomic_write`, so a symlink here was not written by us, and the entries the
            # approval was computed from were read THROUGH it - they may not describe this
            # batch at all. Deleting on the strength of that is the one outcome worth
            # avoiding, and a batch kept costs the user a second attempt.
            if (MANIFEST_NAME,) in links:
                logger.warning(
                    "refusing to empty %r: its manifest is a symlink, so its listing is "
                    "not this batch's own",
                    batch.name,
                )
                return 0, SKIP_UNREADABLE
            listed = {p for p in (_plain_parts(rel) for rel in rels) if p is not None}
            # The batch's own manifest is accounted for by definition. A file named
            # `manifest.jsonl` deeper in the tree is not: it is data nothing lists, and
            # keeping it is the safe direction.
            unaccounted = [p for p in present if p != (MANIFEST_NAME,) and p not in listed]
            if unaccounted:
                logger.warning(
                    "refusing to empty %r: %d staged file(s) are absent from its manifest, "
                    "so this would delete the only copy",
                    batch.name,
                    len(unaccounted),
                )
                return 0, SKIP_UNLISTED_FILES
            for rel in rels:
                # No absolute path, no traversal, no empty component: a tampered manifest
                # does not get to name anything but a plain path under this batch.
                parts = _plain_parts(rel)
                if parts is None:
                    continue
                # Nor does it get to name the MANIFEST, which is the batch's own recovery
                # metadata: `list_trash()` omits a batch without a readable one, so
                # removing it here - before the sweep has proved every other file gone -
                # turns any surviving session data into files the user can neither see nor
                # restore. The manifest is removed once, at the end, below.
                if parts == (MANIFEST_NAME,):
                    logger.warning("refusing a staged entry that names the manifest")
                    continue
                try:
                    holder = _open_chain(
                        batch_fd, parts[:-1], cache=cache, dirs=verify, device=device
                    )
                    info = os.stat(parts[-1], dir_fd=holder, follow_symlinks=False)
                # ValueError as well as OSError, and it matters more here than anywhere: an
                # exception escaping this loop stops an IRREVERSIBLE operation partway, with
                # earlier files already unlinked. `os` raises ValueError, not OSError, for a
                # name it cannot hand to the kernel at all, so a single bad entry would abort
                # the batch instead of costing itself. One unusable name must skip its own
                # file and leave the batch reported incomplete and restorable.
                except (OSError, ValueError):
                    continue
                if stat.S_ISDIR(info.st_mode):
                    continue
                # The leaf is the one thing a descriptor cannot hold: POSIX has no
                # unlink-by-inode, so this addresses a NAME. What it can do is refuse a
                # name that has stopped denoting the object the pinned scan saw there - which
                # closes the interval between that scan and this unlink, and leaves only
                # the two syscalls between the stat above and the unlink below, plus a file
                # substituted before the scan under a name the manifest already lists.
                seen = present.get(parts)
                if seen is None or (info.st_dev, info.st_ino) != (device, seen):
                    logger.warning("refusing a staged file that is not the one that was scanned")
                    continue
                try:
                    os.unlink(parts[-1], dir_fd=holder)
                except (OSError, ValueError):
                    continue
                freed += info.st_size
                files += 1
                # Reporting per file would call back six figures of times for one batch;
                # this is often enough that a reader sees the number move.
                if on_progress is not None and files % _PROGRESS_EVERY_FILES == 0:
                    on_progress(base_bytes + freed)
            # Links recorded by the scan go too. Removing a link destroys nothing - the
            # thing it points at is untouched, and nothing here ever follows one - while
            # leaving them would make a batch holding one impossible to empty for good.
            #
            # But "it is only a link" is a statement about what the SCAN saw, not about
            # what this name holds now, and this is an unlink by name like any other. A
            # regular file moved onto a recorded link's name in between is data, and
            # deleting it would be the very loss the file loop's identity check exists to
            # prevent - so the same check applies here: still a link, and still the one
            # that was recorded.
            for key, seen_ino in links.items():
                try:
                    holder = _open_chain(
                        batch_fd, key[:-1], cache=cache, dirs=verify, device=device
                    )
                    info = os.stat(key[-1], dir_fd=holder, follow_symlinks=False)
                except (OSError, ValueError):
                    continue
                if not stat.S_ISLNK(info.st_mode) or (info.st_dev, info.st_ino) != (
                    device,
                    seen_ino,
                ):
                    logger.warning("refusing a staged link that is not the one that was scanned")
                    continue
                with suppress(OSError, ValueError):
                    os.unlink(key[-1], dir_fd=holder)
            # The manifest is not one of its own entries, and on a batch of this size it is
            # not a rounding error, so its size is counted here - but it is NOT removed yet.
            # It is the only thing that makes the batch restorable: `list_trash()` omits a
            # batch with no readable manifest, so deleting it while a listed file still
            # survived (an unwritable staged directory, a file held open) left the user with
            # data on disk they could neither see nor restore. It goes last, only once
            # nothing else is left.
            manifest_bytes = 0
            with suppress(OSError):
                manifest_bytes = os.stat(
                    MANIFEST_NAME, dir_fd=batch_fd, follow_symlinks=False
                ).st_size
            # The descriptor cache is dropped FIRST, so the removal phase re-opens every
            # directory and re-checks its inode. Reusing a descriptor cached during the
            # file phase would satisfy the check with the identity of the directory that
            # was there THEN, and the `rmdir` below addresses a name - so an empty
            # directory swapped in afterwards was removed on the strength of it.
            _drain(cache)
            # The directories the APPROVAL named, deepest first, each through a descriptor
            # chain that admits only the approved inode. Nothing is discovered by walking,
            # so a directory substituted after verification is not opened and not removed.
            _remove_scanned_dirs(batch_fd, dirs, device, cache)
            _drain(cache)
            # The post-condition, read the same way everything else here is: a fresh pinned
            # scan, and nothing may be left but the manifest. This replaces a recursive
            # sweep that answered "is it empty" while also deciding what to delete - two
            # jobs, and the deciding half could be pointed at a directory nobody approved.
            try:
                left = _scan_tree(batch_fd, device=device)
            except OSError as exc:
                logger.warning("emptying %r: could not confirm it is empty: %s", batch.name, exc)
                cleared = False
            else:
                left_dirs, left_files, left_links = left.dirs, left.files, left.links
                # By INODE, not by name. Everything after this point treats the survivor as
                # the batch's own manifest: it is renamed aside and, once the batch is gone,
                # the debris is unlinked. So a file substituted at that name after the first
                # scan would be accepted here and then destroyed - an unapproved file, whose
                # only copy this is, deleted on the strength of matching a name. The first
                # scan recorded what the manifest WAS, and that is what has to still be here.
                cleared = (
                    not left_dirs
                    and not left_links
                    and list(left_files) == [(MANIFEST_NAME,)]
                    and left_files[(MANIFEST_NAME,)] == present.get((MANIFEST_NAME,))
                )
                if not cleared and list(left_files) == [(MANIFEST_NAME,)]:
                    logger.warning(
                        "emptying %r: the file at its manifest's name is not the one that "
                        "was scanned, so it is left alone",
                        batch.name,
                    )
            if cleared:
                # The manifest and the batch directory have to go TOGETHER, and `rmdir`
                # cannot run while the manifest is still in there. Unlinking it first left a
                # window that a file created after the sweep turned into silent loss: the
                # rmdir then failed on a non-empty directory, and the batch - now without a
                # manifest - vanished from `list_trash()` with that data inside it,
                # unreachable and unrestorable.
                #
                # So it is MOVED to the trash root under a debris name instead of deleted.
                # From there the batch can be removed, and if that fails the manifest is
                # renamed straight back, leaving the batch listed and restorable exactly as
                # it was. `list_trash()` enumerates directories only, so the debris name is
                # never mistaken for a batch, and a crash between the two renames leaves one
                # small file rather than an unreadable batch.
                # The suffix is RANDOM, like the coarse path's staging name and for the same
                # reason: `os.rename` replaces an existing destination silently on POSIX, so
                # a deterministic name is one an actor with write access to the trash root
                # can plant a file at and have this destroy its only copy. A name nothing
                # can predict cannot be squatted.
                debris = f".{batch.name}.{MANIFEST_NAME}.removing-{uuid.uuid4().hex[:8]}"
                moved_aside = True
                try:
                    os.rename(MANIFEST_NAME, debris, src_dir_fd=batch_fd, dst_dir_fd=parent_fd)
                except OSError as exc:
                    logger.warning("emptying %r left its manifest in place: %s", batch.name, exc)
                    cleared = False
                    moved_aside = False
                # The post-condition verified the manifest's inode, and this rename addresses
                # its NAME - two syscalls apart, the same irreducible interval the leaf unlink
                # has, because POSIX has no rename-by-inode either. What CAN be checked is
                # what landed: if the debris is not the file that was verified, the rename
                # moved something else, and the unlink at the end of this block would destroy
                # it. So it is left as debris rather than removed, and both names are logged.
                # The real manifest was already replaced by then - that loss is not this
                # code's to undo - but it does not have to add a second one.
                if moved_aside:
                    landed = _inode_of(debris, parent_fd)
                    if landed != present.get((MANIFEST_NAME,)):
                        logger.error(
                            "emptying %r moved a file that is not its manifest aside as %r; "
                            "leaving it in the trash root rather than deleting it (inode %r, "
                            "expected %r)",
                            batch.name,
                            debris,
                            landed,
                            present.get((MANIFEST_NAME,)),
                        )
                        cleared = False
                        moved_aside = False
                if moved_aside:
                    try:
                        _remove_pinned_batch(parent_fd, batch_fd, batch.name)
                    except OSError as exc:
                        logger.warning("emptying %r did not remove it: %s", batch.name, exc)
                        cleared = False
                        restored = False
                        # `landed` equalled this inode above, which is what let this branch be
                        # reached at all. Bound to a local, and the recovery skipped outright
                        # if it is unknown: without an identity to check, the debris name is
                        # just a name in a directory an actor may be able to write to.
                        debris_ino = present.get((MANIFEST_NAME,))
                        # Never `rename`: POSIX rename REPLACES its destination silently,
                        # which is the property the debris name above is chosen to be safe
                        # against - and this direction needs the opposite guarantee. If
                        # anything has created a `manifest.jsonl` in the batch since ours
                        # was moved aside, renaming over it would destroy the only copy of a
                        # file this code has never read.
                        #
                        # `pinned_fs.put_back_no_clobber` owns that: the debris name is opened
                        # `O_NOFOLLOW` and checked against `debris_ino` before anything is read
                        # from it, `os.link` first because it cannot clobber, and an
                        # `O_CREAT | O_EXCL` create-and-copy where the MOUNT has no hard links
                        # - which the `os.supports_dir_fd` probe cannot see, since it tests
                        # what the OS accepts and not what the filesystem does. The debris is
                        # unlinked only once the batch has its manifest back, so no window has
                        # neither.
                        back: str | None = pinned_fs.PUT_BACK_FAILED
                        if debris_ino is not None:
                            back = pinned_fs.put_back_no_clobber(
                                parent_fd,
                                batch_fd,
                                debris,
                                MANIFEST_NAME,
                                expect_ino=debris_ino,
                            )
                        restored = back is None
                        if back == pinned_fs.PUT_BACK_NAME_TAKEN:
                            # Something arrived at the name. NOT overwritten - that is the
                            # whole reason this is not a rename - and the batch is still
                            # listable, because whatever arrived is a manifest by name and
                            # `list_trash()` can read it. The debris stays for a human.
                            logger.error(
                                "emptying %r could not restore its manifest because another "
                                "file now holds that name; the manifest is %r in the trash "
                                "root",
                                batch.name,
                                debris,
                            )
                        elif not restored:
                            logger.error(
                                "emptying %r removed neither the batch nor its manifest; "
                                "the manifest is now %r in the trash root",
                                batch.name,
                                debris,
                            )
                        if restored:
                            _unlink_debris(parent_fd, debris, debris_ino)
                    else:
                        _unlink_debris(parent_fd, debris, present.get((MANIFEST_NAME,)))
                        freed += manifest_bytes
        finally:
            _drain(cache)
            _close_all((batch_fd,))
    finally:
        _close_all((parent_fd,))
    if on_progress is not None:
        on_progress(base_bytes + freed)
    return freed, None if cleared else SKIP_INCOMPLETE


def _incomplete_if_present(batch: Path) -> str | None:
    """``SKIP_INCOMPLETE`` if the batch survived its own delete, else None.

    For the COARSE path only. It calls ``rmtree(ignore_errors=True)``, which reports
    nothing, so a tree it could not remove - a file held open by another process, a
    permission it does not have - left the batch on screen while the job said it had
    succeeded. Whether the directory is actually gone is the one question that
    distinguishes those, and it costs a stat. The descriptor path needs no second look:
    its own sweep already knows whether anything was left.
    """
    try:
        if batch.exists():
            logger.warning("emptying %r did not remove it", batch.name)
            return SKIP_INCOMPLETE
    except OSError:
        return SKIP_INCOMPLETE
    return None
