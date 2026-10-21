"""Removing batches from the trash for good: the empty a user asks for, and emptied batches.

An owner module of :mod:`kiro_crew.session_storage`, the one import path: callers,
tests and documents reach every name here as ``kiro_crew.session_storage.<name>``.

:func:`staged_targets` approves what an empty would destroy, :func:`empty_trash` deletes
the batches the user approved, and :func:`_remove_emptied_batch` removes a batch that a
restore or a rolled-back move left holding nothing worth keeping.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from kiro_crew import pinned_fs
from kiro_crew.session_storage_parts.batch_manifest import (
    _header_names_this_batch,
    _manifest_rels,
    _read_manifest,
    _rels_digest,
    _summarize_manifest,
)
from kiro_crew.session_storage_parts.crew_staging import (
    _discard_crew_staging,
    _unlisted_crew_staging,
)
from kiro_crew.session_storage_parts.file_moves import _batch_dir, _mutation_lock, _unlisted_files
from kiro_crew.session_storage_parts.pinned_delete import (
    _FD_SAFE_DELETE,
    _close_all,
    _delete_listed_files,
    _open_absolute_nofollow,
    _rename_back,
    _scan_tree,
    _stage_batch_by_name,
)
from kiro_crew.session_storage_parts.store_inventory import invalidate_scan_cache, list_trash
from kiro_crew.session_storage_parts.store_layout import (
    MANIFEST_NAME,
    SKIP_IDENTITY_CHANGED,
    SKIP_OUTSIDE_ROOT,
    SKIP_UNLISTED_FILES,
    SKIP_UNREADABLE,
    BatchIdentity,
    EmptyProgress,
    EmptySkip,
    SessionStorageError,
    logger,
    trash_root,
)


def _discard_restored_batch(batch: Path, expect: tuple[int, int] | None) -> None:
    """Remove a fully-restored batch, unless it holds files nothing listed.

    Deleting the directory wholesale would destroy an interrupted move's staged
    files — on the one path that exists to be safe. Those keep the batch alive
    instead, so a user can still see it and decide.

    *expect* is the batch's identity as the CALLER recorded it, before the restore read
    anything inside it. It is not re-taken here on purpose: this function is reached only
    after the restore has read the manifest and moved every listed file out, so an identity
    sampled at this point would already be a substitute's if an ancestor had been swapped
    during any of that. Taken at the top of the operation instead, the same comparison
    refuses a swap that landed at ANY point in it. ``None`` means the caller could not read
    it, which refuses.

    NOTHING here writes, and a batch this declines to remove stays exactly as it was found -
    including its manifest, whose entries all describe files the restore has already moved
    back OUT. That stale listing is a decided residual, not an oversight: clearing it would
    mean writing to a path that a refusal has just given evidence about - a swapped ancestor,
    an identity that could not be read - and ``atomic_write`` REPLACES its destination, so the
    tidying would land wherever the path now leads. See the caller for why the write is not
    hoisted above this call either. `empty_trash` clears the batch on the user's own say-so.
    """
    try:
        leftovers = _unlisted_files(batch)
    except SessionStorageError as exc:
        # Not knowing is treated exactly like finding leftovers: keep the batch.
        logger.warning("keeping trash batch %r: %s", batch.name, exc)
        return
    if _unlisted_crew_staging(batch):
        # Removing the batch would leave that staging to the prune, which deletes it.
        logger.warning("keeping trash batch %r: it holds unlisted crew-log staging", batch.name)
        return
    if leftovers:
        logger.warning(
            "keeping trash batch %r: %d staged file(s) are absent from its manifest, "
            "so removing it would delete the only copy",
            batch.name,
            len(leftovers),
        )
        return
    _remove_emptied_batch(batch, "the restored batch", expect=expect)


def _approve_emptied_batch(
    batch: Path,
    root_fd: int,
    tree: pinned_fs.PinnedTree,
) -> str | None:
    """Re-ask "is this the batch, and does it hold anything unlisted" of a DESCRIPTOR.

    The trash-specific half of :func:`_remove_emptied_batch`, passed to ``pinned_fs`` as its
    approval hook. Both questions were already answered by path before the removal was
    reached - which is exactly the answer that cannot be relied on, because resolving the
    batch's parent FOLLOWS an ancestor that is already a symbolic link. Pinning the walk
    then reaches the link's target faithfully, and the removal would empty that.

    So the two questions are asked again here, of the descriptor the removal will actually
    address, from ONE read of ONE file:

    * the pinned root must BE the directory the caller was looking at, compared by
      ``(st_dev, st_ino)`` against an identity the caller captured before its own by-path
      read - at the batch's creation on one path, before the leftover scan on the other.
      This is the check that makes the rest sound. Without it the only thing standing
      between a swapped ancestor and a deletion is whether the directory the link points at
      can produce a convincing manifest, and a tree an actor can write to can: they plant a
      header naming this batch and a listing covering its contents, and every other question
      here answers yes about the wrong directory. An inode cannot be forged by writing files.
    * the manifest is opened through ``root_fd``, its inode is checked on the OPEN handle
      against the one the scan recorded, and its header must claim THIS batch's
      directory name. The header is the link back to the caller's own selection: a directory
      substituted for the batch brings its own contents, and a directory that is not a
      trash batch at all brings none - neither can produce a header naming this name.
      :func:`_header_names_this_batch` is the same check the approval on the delete path
      makes, for the same reason.
    * NOTHING but the manifest may remain. Not "nothing unlisted" - nothing at all. Both
      callers reach here only when the batch should already be empty of files: the restore
      path has MOVED every listed file back out, and the staging path has a manifest with no
      entries at all. So a file at a listed path is not the listed file; it is something that
      arrived at that name afterwards, and it may be the only copy of whatever it is.
      Consulting the listing here would authorise deleting exactly that, on the strength of a
      name the manifest happens to mention. The stricter rule also needs no listing, which is
      why the entries this read produced are discarded.

    ONE read, not two, and that is the load-bearing part rather than an efficiency note.
    Asking :func:`_summarize_manifest` for the header and :func:`_manifest_rels` for a listing
    opens the file TWICE, and a manifest replaced in between passes the header check on the
    first file while the second answers for another. The header now comes from one handle
    whose inode was verified on that same handle, so there is no interval for a replacement to
    land in.

    Returns None to allow the removal, or a reason code to withhold it. The identity
    comparison against the caller's recorded inode happens BEFORE this, in
    :func:`_remove_emptied_batch`'s own hook -- it owns that answer because the caller also
    needs to know whether the path was verified, and a reason code cannot carry that.

    Links are refused too, not exempted. A symbolic link holds no data of its own, which is
    why the delete path removes scanned ones - but it is also the only entry a removal can
    take by NAME, with a scanned-inode check and an unlink that are two separate syscalls.
    Admitting one is admitting that window, so a batch holding a link is kept and left for
    `empty_trash`, which removes links through its own approval when a user asks for the
    empty.
    """
    scanned_ino = tree.files.get((MANIFEST_NAME,))
    if scanned_ino is None:
        # Nothing here vouches for this being the selected batch, and `list_trash` would not
        # offer a batch with no manifest either.
        logger.warning("keeping trash batch %r: it has no manifest to identify it", batch.name)
        return SKIP_UNREADABLE
    try:
        parsed = _read_manifest(batch, dir_fd=root_fd, expect_ino=scanned_ino)
    except UnicodeDecodeError:
        # `_read_manifest` reports an unreadable manifest as None, but it DECODES strictly,
        # so bytes that are not UTF-8 raise instead - and a ValueError, which neither its own
        # `except OSError` nor anything between here and the caller catches. Reached when the
        # file is corrupted after the by-path pre-screen read it, which is the very race this
        # pinned re-read exists for. A manifest that cannot be decoded cannot identify the
        # batch, so it withholds the removal like any other unreadable one instead of
        # aborting a restore that has already moved the user's files back.
        logger.warning("keeping trash batch %r: its manifest is not valid UTF-8", batch.name)
        return SKIP_UNREADABLE
    if parsed is None:
        logger.warning(
            "keeping trash batch %r: its manifest is not readable as the file that was scanned",
            batch.name,
        )
        return SKIP_UNREADABLE
    header, _entries = parsed
    # `_header_names_this_batch` takes a `_summarize_manifest` triple; it consults only the
    # header, and handing it the one this read already produced is what keeps this to one
    # open.
    if not _header_names_this_batch((header, 0, 0), batch.name):
        return SKIP_IDENTITY_CHANGED
    for key in tree.files:
        if key == (MANIFEST_NAME,):
            continue
        logger.warning(
            "keeping trash batch %r: it still holds %r, which nothing here may remove",
            batch.name,
            "/".join(key),
        )
        return SKIP_UNLISTED_FILES
    if tree.links:
        # A link is the ONE entry the removal takes by name: `unlink_verified` compares the
        # scanned inode and then unlinks, and POSIX offers no "unlink if this inode", so those
        # are two syscalls. Admitting a link therefore hands an actor a lever - plant one, let
        # this approval pass it, then put a file at that name in the window and the removal
        # deletes the file instead of the link. A file is refused above for the same reason and
        # a link is not different enough to treat differently here.
        #
        # It costs nothing that matters: neither of these two removals was asked for, and a
        # batch kept for this reason is still cleared by `empty_trash` on the user's own
        # say-so, which removes scanned links through its own approval.
        logger.warning(
            "keeping trash batch %r: it holds %d link(s), which the removal could only take "
            "by name",
            batch.name,
            len(tree.links),
        )
        return SKIP_UNLISTED_FILES
    return None


def _remove_emptied_batch(batch: Path, what: str, *, expect: tuple[int, int] | None) -> bool:
    """Remove a batch that holds nothing worth keeping, by descriptor rather than by name.

    Both callers reach this having already established that the batch holds no file the
    manifest does not list - a fully restored batch, and one no session was staged into.
    What is left is the removal, and ``shutil.rmtree(batch)`` would take a PATH, which
    the kernel re-resolves component by component. The trash root and the directories above
    it are writable by the same user, which in this product includes an agent, so one of
    them swapped to a symbolic link after the caller's own read is followed and the removal
    lands wherever the link points. Neither caller holds the mutation lock across that
    window, and the checks above them are all by path, so nothing else closed it.

    :func:`kiro_crew.pinned_fs.remove_tree_pinned` is the same mechanism the delete path
    uses, reached through the same module rather than respelled here: the parent chain is
    pinned one ``openat`` per component, the batch is opened through it with
    ``O_NOFOLLOW``, and every removal inside addresses a descriptor whose inode a scan
    already recorded.

    Pinning is only half of it, and the weaker half. It closes the window after the path is
    resolved; the swap that lands BEFORE the resolve is followed by ``Path.resolve()``
    itself, and the pinned walk then reaches the wrong tree correctly - faithfully pinned to
    a directory that is not this batch. What closes that is
    :func:`_approve_emptied_batch`, which compares the pinned root against *expect* - an
    identity the CALLER captured before its own by-path read - and then re-asks through the
    same descriptor the two questions the callers asked by path.

    *expect* being None means the caller could not read the batch's identity, and the removal
    is refused: with nothing to bind to, every remaining question would be answered about an
    unknown directory.

    A refusal LEAVES THE BATCH, and that is the safe direction on both paths: the batch is
    still listed, so a user can still see it and act. Reporting a success that did not
    happen is what a bare ``ignore_errors=True`` did. Nothing here writes to the batch on
    the way out either: every refusal below is evidence about the path, and answering it
    with a by-path write would be writing through the very redirection it just caught.

    Returns whether the batch is gone.
    """
    if not _FD_SAFE_DELETE:
        # No ``openat``/``O_NOFOLLOW``, so there is no descriptor to bind the removal to.
        # It goes through the same rename-verify-remove the explicit empty uses on this
        # platform, from the same owner: the batch is renamed aside inside the trash root,
        # its identity is checked THERE, and only the staged name is removed.
        #
        # Refusing outright looks like the safer half of the trade, on the reasoning that
        # nobody asked for these two removals. It is not: this
        # branch is the whole of Windows, so refusing leaves a batch behind after EVERY
        # restore and every rolled-back move, still listing the sessions it does not hold any more.
        # Tests read that as a failure and so would a user. The residual
        # accepted instead is the one `empty_trash` already accepts here and documents - an
        # actor who can observe the staging name inside the window can redirect the removal
        # through an ancestor swapped afterwards - and it is bounded by an identity check
        # that a same-named impostor cannot pass.
        if expect is None:
            # Nothing to bind to, so the verify half would be answered about an unknown
            # directory and the rename would only move the batch out of sight.
            logger.warning("keeping trash batch %r: its identity was never established", batch.name)
            return False
        staged, skip = _stage_batch_by_name(batch, expect=expect)
        if staged is None:
            logger.warning(
                "keeping trash batch %r: %s was not removed (%s)", batch.name, what, skip
            )
            return False
        try:
            shutil.rmtree(staged)
        except OSError as exc:
            # Back under the name the user saw: a batch left under a staging name is one
            # `list_trash` does not offer, so it would be neither visible nor restorable.
            logger.warning(
                "keeping trash batch %r: %s could not be removed: %s", batch.name, what, exc
            )
            _rename_back(staged, batch)
            return False
        return True
    try:
        # Only the PARENT is resolved, and the name is re-joined onto it. `Path.resolve()`
        # follows the FINAL component too, so a batch directory replaced by a symlink would
        # resolve to its target and every removal below would run there - deleting from
        # outside the trash on the strength of a name inside it. Re-joining the name leaves
        # the last component for the pinned ``O_NOFOLLOW`` open to refuse. This is the same
        # rule :func:`_approve_batch` follows, for the same reason.
        resolved = batch.parent.resolve() / batch.name
    except OSError as exc:
        logger.warning(
            "keeping trash batch %r: its location could not be read: %s", batch.name, exc
        )
        return False

    def _approve(root_fd: int, tree: pinned_fs.PinnedTree) -> str | None:
        if expect is None:
            # Nothing to bind to, so every remaining question would be answered about an
            # unknown directory.
            logger.warning("keeping trash batch %r: its identity was never established", batch.name)
            return SKIP_UNREADABLE
        opened = os.fstat(root_fd)
        if (opened.st_dev, opened.st_ino) != expect:
            logger.warning(
                "keeping trash batch %r: it is not the directory that was selected", batch.name
            )
            return SKIP_IDENTITY_CHANGED
        return _approve_emptied_batch(batch, root_fd, tree)

    try:
        outcome = pinned_fs.remove_tree_pinned(
            str(resolved),
            what=what,
            refusal=OSError,
            approve=_approve,
            # The manifest goes LAST, and comes back if the batch itself will not go.
            # `list_trash` omits a batch with no readable manifest, so removing it first and
            # then failing on the directory would leave a batch holding data that is neither
            # visible nor restorable - the same reason the delete path moves it aside rather
            # than unlinking it.
            keep_until_empty=MANIFEST_NAME,
        )
    except OSError as exc:
        # Includes the ancestor swap the pinned walk exists to catch. Warned rather than
        # raised: on both paths the caller has already finished the work the user asked for,
        # and a leftover batch is visible and restorable.
        logger.warning(
            "keeping trash batch %r: it could not be removed safely: %s", batch.name, exc
        )
        return False
    if not outcome.removed:
        logger.warning(
            "keeping trash batch %r: %s left %d entr(y/ies) behind%s",
            batch.name,
            outcome.reason or "the removal",
            outcome.survivors,
            f"; part of it is now {outcome.staged_name!r}" if outcome.staged_name else "",
        )
    return outcome.removed


def _approve_batch(path: Path) -> tuple[BatchIdentity, int] | None:
    """What *path* IS and what it holds, both read through ONE pinned descriptor.

    `list_trash()` reads each manifest by path and takes no lock, so its byte totals
    describe whatever answered to that name then. Recording an identity separately, by the
    same name, can straddle a swap: the approval would then carry the REPLACEMENT's
    identity with the original's numbers, and the delete - which checks the identity it was
    given - would destroy session data the user was never shown. Pinning once and taking
    both from that descriptor makes the pair describe one object or nothing.

    None means no identity, and a batch with no identity is dropped from the approved set
    rather than carried unchecked.
    """
    try:
        # Only the PARENT is resolved. `Path.resolve()` follows the final component too, so
        # a batch directory replaced by a symlink resolved to its TARGET and everything below
        # pinned that target - the approval would record another directory's identity under
        # this batch's id, and the delete, checking the identity it was handed, would destroy
        # session data from outside the trash. Re-joining the name onto the resolved parent
        # keeps the walk's `O_NOFOLLOW` on the component that matters, which refuses the link
        # instead of resolving through it.
        resolved = path.parent.resolve() / path.name
    except OSError:
        return None
    if not _FD_SAFE_DELETE:
        # No openat: the interior cannot be walked by descriptor and the coarse removal
        # cannot use such a map anyway. `lstat`, not `stat`, so a batch directory that is
        # itself a link records the LINK - which the delete then refuses - rather than
        # silently resolving through it.
        try:
            info = os.stat(path, follow_symlinks=False)
        except OSError:
            return None
        summary = _summarize_manifest(path)
        if not _header_names_this_batch(summary, path.name):
            return None
        if summary is None:
            return None
        return (
            BatchIdentity(
                info.st_dev, info.st_ino, None, rels_digest=_rels_digest(_manifest_rels(path))
            ),
            summary[2],
        )
    try:
        parent_fd, batch_fd = _open_absolute_nofollow(resolved)
    except OSError:
        return None
    try:
        info = os.fstat(batch_fd)
        # The listing FIRST, and through the pinned descriptor. Taken after the scan it
        # could be a manifest rewritten in between - recording the NEW listing against the
        # OLD inode map, which authorises exactly the file the digest exists to refuse.
        # Taken by path it could be a different directory's manifest altogether. Ordering
        # it first also fails closed: a rewrite after this point leaves the digest
        # describing the old listing, so the delete refuses.
        digest = _rels_digest(_manifest_rels(path, dir_fd=batch_fd))
        scanned = _scan_tree(batch_fd, device=info.st_dev)
        dirs, files, links = scanned.dirs, scanned.files, scanned.links
        summary = _summarize_manifest(path, dir_fd=batch_fd)
    except OSError:
        return None
    finally:
        _close_all((batch_fd, parent_fd))
    if not _header_names_this_batch(summary, path.name):
        return None
    if summary is None:
        return None
    return (
        BatchIdentity(
            info.st_dev,
            info.st_ino,
            dirs,
            files,
            links,
            rels_digest=digest,
        ),
        summary[2],
    )


def staged_targets(
    batch_ids: list[str] | None = None,
) -> tuple[list[str], int, dict[str, BatchIdentity]]:
    """Which staged batches an empty would destroy, and what they hold.

    Taken under the mutation lock, which is the whole point of it being here rather
    than a list comprehension at the caller. ``move_to_trash`` holds that lock for
    the length of a staging run, and :func:`list_trash` does not take it - so an
    unlocked read can see a batch whose directory and manifest header exist while its
    sessions are still being moved in. Selecting that id then makes the delete wait
    for staging to finish and destroy the FINISHED batch, including sessions appended
    after the user clicked. Under the lock a batch is either fully staged or not
    visible, so the set returned is one a user could actually have seen.

    Returns explicit ids even for "everything", because the caller must be able to
    hand the worker a fixed set: ``empty_trash(None)`` re-enumerates when it runs,
    which is later, and a staged batch is the only copy of its sessions.

    Each id is returned WITH the ``(st_dev, st_ino)`` of the directory it named at
    snapshot time. An id is only a name: the lock is released for the handoff, and a
    directory swapped in its place would be opened by that same name and deleted -
    session data the user was never shown and never approved. The delete re-checks the
    pair against the descriptor it actually opened, which is the object every later
    operation uses, so a replacement is refused rather than destroyed. A batch whose
    identity or manifest cannot be read here gets no entry in the identity map: no
    identity means no check, and deleting it unchecked is the one outcome worth
    avoiding. On a sweep its id is still returned in the list, so :func:`empty_trash`
    refuses it as a skip; a named selection holding one raises here instead.
    """
    with _mutation_lock():
        # Explicit ids go through the SAME resolver every other caller uses, before
        # anything is filtered. Filtering alone silently dropped an id that is not a
        # batch - a typo, or one already emptied - so the worker received an empty
        # list and reported success for a delete the user asked for and did not get.
        # This keeps the guard `empty_trash` has always applied through `_batch_dir`,
        # which the filter had quietly taken over from.
        for batch_id in batch_ids or []:
            _batch_dir(batch_id)
        wanted = None if batch_ids is None else set(batch_ids)
        chosen = [batch for batch in list_trash() if wanted is None or batch.batch_id in wanted]
        if wanted is not None:
            # A well-formed id whose batch is not staged - already emptied, or never
            # existed - passes `_batch_dir`, which does not require the directory to
            # exist. Filtering it out silently turned "destroy this" into a zero-byte
            # success. It is a refusal: the caller named something that is not there,
            # and on the pre-snapshot path the batch would at least have reported an
            # unreadable-batch skip once it reached the delete.
            missing = wanted - {batch.batch_id for batch in chosen}
            if missing:
                raise SessionStorageError(
                    f"{len(missing)} of the batches named are no longer staged"
                )
        identities: dict[str, BatchIdentity] = {}
        ids: list[str] = []
        total = 0
        unapprovable: list[str] = []
        for batch in chosen:
            # Identity AND size from one pinned descriptor. `batch.bytes` came from
            # `list_trash()`, which reads by path under no lock, so pairing it with a
            # separately-stat'd identity could describe two different directories.
            approved = _approve_batch(_batch_dir(batch.batch_id))
            if approved is None:
                # No identity means no check, so it must NOT be deleted. It is still returned
                # in the id list: `empty_trash` refuses an id that a supplied approval map
                # does not name, so the batch comes back as a skip with a reason. Dropping it
                # here instead left the user a success message above a batch still on screen,
                # which is the bug the missing-id refusal above exists to prevent.
                logger.warning(
                    "refusing to approve %r: its identity or its manifest could not be read",
                    batch.batch_id,
                )
                unapprovable.append(batch.batch_id)
                ids.append(batch.batch_id)
                continue
            recorded, staged_bytes = approved
            identities[batch.batch_id] = recorded
            ids.append(batch.batch_id)
            total += staged_bytes
        if unapprovable and wanted is not None:
            # Only for a NAMED selection, which is exactly the asymmetry the missing-id rule
            # above draws. Raising on the sweep would let one batch damaged by a crash
            # mid-append make the whole trash un-emptyable, and the delete path deliberately
            # skips rather than aborts for that reason. A named batch is different: the
            # caller asked for that batch, and it is not going to be deleted.
            raise SessionStorageError(
                f"{len(unapprovable)} of the batches named cannot be verified for deletion"
            )
        return ids, total, identities


def empty_trash(
    batch_ids: list[str] | None = None,
    on_progress: EmptyProgress | None = None,
    on_skip: EmptySkip | None = None,
    expect: dict[str, BatchIdentity] | None = None,
) -> int:
    """Delete staged batches for good; return the bytes freed.

    Serialized against other mutations, so a batch cannot be destroyed while a
    restore is mid-way through putting its files back.

    ``on_progress`` is called with the running total of bytes freed and ``on_skip``
    with a ``SKIP_*`` code for each batch kept rather than deleted, both from the
    calling thread. They are advisory: a caller that passes neither gets exactly the
    behaviour it always had, and nothing here waits on or trusts a callback.

    ``on_skip`` exists because keeping a batch is a REFUSAL a person needs told.
    Returning only the bytes freed made a batch that was deliberately kept
    indistinguishable from an empty one — "0 bytes freed, success" — with the reason
    reaching a log the user cannot read.

    ``expect`` maps a batch id to the ``(st_dev, st_ino)`` :func:`staged_targets` saw
    under the lock. A batch whose opened descriptor does not match is kept, because the
    id is only a name and a directory swapped in during the handoff would otherwise be
    deleted on the strength of the user having approved a different one. Omitted means
    unchecked, which is what a direct caller that never took a snapshot gets.
    """
    with _mutation_lock():
        try:
            return _empty_trash_locked(batch_ids, on_progress, on_skip, expect)
        finally:
            invalidate_scan_cache()


def _empty_trash_locked(
    batch_ids: list[str] | None = None,
    on_progress: EmptyProgress | None = None,
    on_skip: EmptySkip | None = None,
    expect: dict[str, BatchIdentity] | None = None,
) -> int:
    """Delete staged batches for real; return the bytes freed.

    This is the only call here that destroys data, and the only one that changes
    free space. Every path is resolved and confirmed to be inside the trash root
    first, so a tampered manifest or a symlinked batch directory cannot direct the
    delete outside it - and the removal itself then works through descriptors opened
    from the filesystem root, so nothing above the batch can be substituted between
    that check and the bytes going away.
    """

    def _skipped(code: str) -> None:
        if on_skip is not None:
            on_skip(code)

    try:
        root = trash_root().resolve()
    except OSError:
        return 0
    if batch_ids is None:
        targets = [_batch_dir(b.batch_id) for b in list_trash()]
    else:
        targets = [_batch_dir(batch_id) for batch_id in batch_ids]

    freed = 0
    for target in targets:
        try:
            resolved = target.resolve()
        except OSError:
            continue
        if resolved == root or not resolved.is_relative_to(root):
            logger.warning("refusing to empty %r: outside the trash root", target)
            _skipped(SKIP_OUTSIDE_ROOT)
            continue
        # A batch can hold files no manifest line mentions, left by an interruption
        # between the move and the append. The user was never shown them — the
        # batch may even list zero sessions — so "empty this batch" is not informed
        # consent for destroying them, and they are the only copy.
        try:
            leftovers = _unlisted_files(resolved)
        except SessionStorageError as exc:
            # Skip, not abort: one unreadable batch must not make the whole trash
            # un-emptyable. Skipping deletes nothing, which is the safe direction.
            logger.warning("refusing to empty %r: %s", target.name, exc)
            _skipped(SKIP_UNREADABLE)
            continue
        if leftovers:
            logger.warning(
                "refusing to empty %r: %d staged file(s) are absent from its "
                "manifest, so this would delete the only copy",
                target.name,
                len(leftovers),
            )
            _skipped(SKIP_UNLISTED_FILES)
            continue
        stray_crew = _unlisted_crew_staging(resolved)
        if stray_crew:
            logger.warning(
                "refusing to empty %r: crew logs of %d session(s) are staged but absent "
                "from its manifest, so this would delete the only copy",
                target.name,
                len(stray_crew),
            )
            _skipped(SKIP_UNLISTED_FILES)
            continue
        # An approval map that was SUPPLIED and does not name this batch is not the same as
        # no approval at all. `staged_targets` leaves out a batch whose identity it could not
        # read, and treating that as "nothing to check" would delete it unverified - the one
        # outcome the map exists to prevent. Refusing here is also what makes the omission
        # VISIBLE: the batch comes back as a skip with a reason instead of vanishing from the
        # job and leaving the user a success message above a batch still on screen.
        if expect is not None and target.name not in expect:
            logger.warning(
                "refusing to empty %r: it is not in the approved set, so it cannot be " "verified",
                target.name,
            )
            _skipped(SKIP_UNREADABLE)
            continue
        deleted, skip = _delete_listed_files(
            resolved, on_progress, freed, None if expect is None else expect.get(target.name)
        )
        freed += deleted
        if skip is not None:
            _skipped(skip)
        else:
            freed += _discard_crew_staging(target.name)
    return freed
