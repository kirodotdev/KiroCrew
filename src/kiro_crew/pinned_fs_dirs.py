"""Directories under a pinned ancestor: creating one and opening it
(:func:`create_and_open_dir_at`, :func:`create_and_open_dir_pinned`) and opening an
existing descendant (:func:`open_pinned_descendant_dir`), each by descriptor.
"""

from __future__ import annotations

import errno
import os
import stat as _stat
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from kiro_crew.pinned_fs_primitives import (
    _CREATE_ATTEMPTS,
    PinnedPathRefusal,
    dir_flags,
    is_reparse_point,
    pin_parent,
    supports_pinned_walk,
)


def create_and_open_dir_at(
    parent_fd: int,
    name: str,
    *,
    what: str,
    refusal: type[Exception] = PinnedPathRefusal,
) -> int:
    """Create directory *name* under the open *parent_fd* if absent, and open it.

    Both calls are relative to the caller's descriptor, so no path is resolved again:
    a parent swapped for a link after the caller opened it cannot steer the create
    elsewhere. The open is ``O_NOFOLLOW`` + ``O_DIRECTORY``, so a link (or a non
    directory) planted at *name* itself is refused with *refusal*. *name* must be one
    component. Returns the directory fd; the caller owns it.
    """
    if not name or name in (".", "..") or os.sep in name or (os.altsep and os.altsep in name):
        raise refusal(f"refusing to open the {what}: {name!r} is not a single path component")
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    try:
        return os.open(name, dir_flags(), dir_fd=parent_fd)
    except (NotADirectoryError, OSError) as exc:
        if isinstance(exc, NotADirectoryError) or exc.errno in (errno.ELOOP, errno.EMLINK):
            raise refusal(
                f"refusing to open the {what}: {name!r} is a link or not a directory"
            ) from exc
        raise


def create_and_open_dir_pinned(
    path: str | Path,
    *,
    what: str,
    must_create: bool = False,
    refusal: type[Exception] = PinnedPathRefusal,
) -> int:
    """Create a directory through its PINNED parent and return its descriptor.

    With *must_create* a name that already exists is REFUSED rather than accepted. Review
    found the hole that needs: a replace removes the live tree and stages the archive into
    its place, so if the gateway recreates that root in between, accepting it lets files
    the archive does not contain survive a "replace" that reports success. The children
    already refused a pre-existing name -- only the root was exempt, and this makes it
    consistent. Merge callers leave it false, because meeting an existing directory is
    what merging IS.

    It does NOT report whether our own `mkdir` created the directory, which would be
    the natural way to stamp the archive's mode and timestamps on only in that case.
    Such a flag cannot be trusted.

    Sampling `dst.exists()` beforehand is a name-based check with a window after it, and
    `mkdir` succeeding does not close that window either: the descriptor comes from a
    SEPARATE `open`, so a directory replaced between the two leaves the flag true while
    describing an object the caller does not hold -- and the archive's metadata then
    lands on somebody else's directory. Nothing repairs it: a stat taken after the
    `mkdir` observes the replacement just as happily, POSIX has no atomic
    create-and-open for a directory, and under a same-user threat model no mode trick
    helps. So the metadata is simply not applied to directories.

    ``Path(p).mkdir(parents=True)`` creates every missing component by name, so a link
    already sitting at an ancestor is followed and the directories are created inside
    whatever it points at -- a write through an attacker-controlled path, which is
    strictly worse than a read through one. Review flagged the by-name creation; the
    probe that settled it showed a link AT the final component is already refused by
    ``O_NOFOLLOW`` but an ancestor link is not.

    So the parent chain is pinned first and only the final component is created,
    relative to that descriptor. What remains is this module's documented and
    deliberate residual, stated in :func:`pin_parent`: a component that was already a
    link when the parent was resolved is followed by that resolution, because refusing
    every symlinked ancestor would break a destination under ``/tmp`` on macOS. The
    parent must therefore already exist -- callers create their own tree roots.

    Creating the directory and opening it are two syscalls, and a removal landing
    between them is an interleaving no ordering closes. The pair is therefore
    re-attempted, :data:`_CREATE_ATTEMPTS` times, against the SAME pinned parent
    descriptor -- a retry resolves no name, so it cannot be steered anywhere the first
    attempt could not go, and ``must_create`` plus the ``O_NOFOLLOW`` refusal are both
    re-asked on every attempt. Tolerating only the ``FileExistsError`` half handled a
    concurrent writer and not a concurrent remover (GH-12043), and this is a caller
    that needs the directory: a snapshot's staging root, a restore destination, a pod
    home. Exhaustion, and a removal of the pinned PARENT (which no attempt can
    survive), raise ``FileNotFoundError`` carrying the whole path rather than the bare
    relative name ``openat`` was given.

    The re-attempt covers a directory THIS call created and no other. When the
    ``mkdir`` instead MET one that was already there -- the merge case, where
    ``must_create`` is false and the destination legitimately holds the caller's files
    -- a removal in the window is reported, not re-attempted: the contents are already
    unrecoverable, and re-creating the name would hand back an empty directory that an
    additive restore would stage into and report success over. Review caught that as
    the retry's one unsafe case, and it is why the loop tracks which of the two
    happened rather than retrying on the errno alone.
    """
    as_given = Path(path)
    parent_fd = pin_parent(
        os.path.realpath(as_given.parent or Path(".")), what=what, refusal=refusal
    )
    try:
        lost: FileNotFoundError | None = None
        for _ in range(_CREATE_ATTEMPTS):
            ours = True
            try:
                os.mkdir(as_given.name, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                if must_create:
                    raise refusal(
                        f"refusing to use the {what}: {as_given.name!r} already exists, and "
                        "this operation replaces its destination rather than merging into "
                        "it. Something recreated that directory after it was removed, so "
                        "staging into it would leave files the archive does not contain "
                        "while reporting a replacement. Remove it and re-run with the "
                        "gateway stopped."
                    ) from None
                ours = False
            except FileNotFoundError as exc:
                # The PINNED PARENT is gone, not the directory being created. Nothing
                # can be made inside an unlinked directory, so every further attempt
                # would land here too -- reported at once, and with the whole path,
                # because the errno carries only the relative name the syscall was
                # given.
                raise FileNotFoundError(
                    errno.ENOENT,
                    f"the directory holding the {what} was removed, so "
                    f"{as_given.name!r} cannot be created in it",
                    str(as_given),
                ) from exc
            try:
                return os.open(as_given.name, dir_flags(), dir_fd=parent_fd)
            except FileNotFoundError as exc:
                if not ours:
                    # This call did NOT create the directory: the `mkdir` above met one
                    # that was already there, holding whatever the caller was about to
                    # merge into, and it is gone. Re-creating it would hand back an
                    # EMPTY directory, and a merge (`must_create=False`) would then
                    # stage the archive into it and report success while the files it
                    # was merging with are unrecoverably gone. This is the retry's one
                    # unsafe case. So it is reported instead -- the same outcome as
                    # with no retry, with the path the errno omits and a sentence
                    # saying what was lost.
                    raise FileNotFoundError(
                        errno.ENOENT,
                        f"the {what} already existed when this call met it and was "
                        "removed before it could be opened, so whatever it held is "
                        "gone; refusing to re-create it empty, because an additive "
                        "restore would then report success over the loss",
                        str(as_given),
                    ) from exc
                # The directory this attempt CREATED is gone. Nothing of the caller's
                # was in it -- it was empty and unopened -- so re-running the pair
                # costs nothing and loses nothing. That is the mirror of the
                # ``FileExistsError`` tolerated above: a concurrent writer is handled
                # and, without this, a concurrent REMOVER was not (GH-12043). Both are
                # the same interleaving seen from opposite sides, and everything here
                # runs as the same user as the agent, which is the premise the pinning
                # exists for.
                #
                # The pair is run again rather than repaired in place, because no
                # ordering of two syscalls closes a window between them. Every attempt
                # goes through the ONE descriptor pinned above this loop, so a retry
                # cannot be steered: nothing is re-resolved by name, ``must_create``
                # and this created-it-ourselves test are re-asked on each attempt, and
                # a link that appears at the name between two attempts is refused by
                # the ``O_NOFOLLOW`` below exactly as it is on the first.
                lost = exc
                continue
            except OSError as exc:
                # A link (or a plain file) at the destination's own name. O_NOFOLLOW already
                # refuses it -- but as a raw OSError, so a restore would end in a traceback
                # instead of the refusal every other path on this surface produces.
                # Translated here so callers have one type to contain.
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise refusal(
                        f"refusing to use the {what}: {as_given.name!r} is a symbolic link "
                        "or not a directory, so creating the tree there would write through "
                        "whatever it points at. Remove it and re-run."
                    ) from exc
                raise
        # Exhaustion is a ``FileNotFoundError``, not a *refusal*: a refusal on this
        # surface means the destination is a link or an occupied name a caller must
        # remove, and callers word it that way -- the prompt handler maps one to
        # "your prompt root is a link". A directory that keeps being removed is an
        # operational failure, so it stays in the class the kernel reported and gains
        # the FULL path the kernel could not name.
        raise FileNotFoundError(
            errno.ENOENT,
            f"the {what} {as_given} was removed between its creation and its open, "
            f"{_CREATE_ATTEMPTS} attempts in a row",
            str(as_given),
        ) from lost
    finally:
        os.close(parent_fd)


@contextmanager
def open_pinned_descendant_dir(
    root: str | Path,
    rel_dir_parts: Iterable[str],
    *,
    what: str,
    create: bool = False,
    refusal: type[Exception] = PinnedPathRefusal,
) -> Iterator[int | None]:
    """Walk *rel_dir_parts* below *root* one descriptor at a time, yielding the leaf.

    A caller that holds a root it trusts and a relative chain of DIRECTORY components
    it has already validated for containment (no ``..``, no absolute, no separator
    tricks -- e.g. an art path already through a lexical gate) needs to reach the leaf
    directory WITHOUT re-resolving any component by name, because between a
    containment check and the open every ancestor is swappable by a same-uid process
    when the tree is agent-writable. This is the multi-component generalisation of
    :func:`create_and_open_dir_pinned`: that one pins the chain ABOVE a single final
    directory and creates only that one; this one starts from an already-open root and
    walks (optionally creating) a whole relative chain below it, refusing a link at
    EVERY component, the root included.

    Yields, for the life of the ``with`` block:

    * on a platform that can pin (:func:`supports_pinned_walk`), the leaf directory's
      DESCRIPTOR (``int``). Every component from *root* down is opened
      ``O_RDONLY|O_DIRECTORY|O_NOFOLLOW`` relative to the previous one's fd, so a
      component that is (or becomes) a symlink fails the open and is refused rather
      than followed, and a component reached once is fixed. With *create* each missing
      component is ``mkdir``-ed ``dir_fd``-relative first, then opened the same way --
      the create tolerates an existing directory, the open still refuses a link that
      replaced it. Use it as ``dir_fd=`` for the leaf's own contents (``os.open`` a
      file under it, or :func:`atomic_write`'s ``parent_dir_fd``); the whole fd chain
      is closed on exit;
    * ``None`` on a platform that cannot pin (Windows: no ``dir_fd`` support). There
      the chain is validated by ``lstat`` -- the root and every component are refused
      when a symlink, a reparse point, or (with *create* off, or once created) a
      non-directory -- and, with *create*, a missing component is ``mkdir``-ed by name.
      The caller then addresses the leaf BY NAME (``root`` joined with
      *rel_dir_parts*), which is the same residual by-name posture
      :func:`write_file_pinned` and the removal helpers document for this platform:
      the ancestor-swap window between the ``lstat`` and the by-name use is not closed
      here because the platform cannot pin a directory at all, and no supported
      configuration relies on it. The ``lstat`` refusal of a PLANTED link -- the leg
      that needs no race -- is kept.

    Empty *rel_dir_parts* means the leaf IS *root*: the pinned arm yields *root*'s own
    ``O_NOFOLLOW`` descriptor (a linked root is refused), and the by-name arm yields
    ``None`` after ``lstat``-refusing a linked root.

    Any refusal raises *refusal* (default :class:`PinnedPathRefusal`); a caller that
    prefers a soft outcome catches it. ``create=False`` plus a missing component is a
    refusal, not a create.
    """
    parts = tuple(rel_dir_parts)
    root_path = Path(root)
    if not supports_pinned_walk():
        # By-name (Windows) arm: lstat the root and every component, refusing a link,
        # a reparse point, or a non-directory; create missing components when asked.
        current = root_path
        try:
            rst = current.lstat()
        except OSError as exc:
            raise refusal(f"refusing to use the {what}: {current} cannot be stat-ed") from exc
        if is_reparse_point(current) or not _stat.S_ISDIR(rst.st_mode):
            raise refusal(f"refusing to use the {what}: {current} is a link or not a directory")
        for part in parts:
            current = current / part
            try:
                lst = current.lstat()
            except FileNotFoundError:
                if not create:
                    raise refusal(f"refusing to use the {what}: {current} is missing") from None
                current.mkdir(0o700)
                continue
            except OSError as exc:
                raise refusal(f"refusing to use the {what}: {current} cannot be stat-ed") from exc
            if is_reparse_point(current) or not _stat.S_ISDIR(lst.st_mode):
                raise refusal(f"refusing to use the {what}: {current} is a link or not a directory")
        yield None
        return

    # Pinned (POSIX) arm: open the root O_NOFOLLOW, then walk the chain fd-to-fd.
    open_fds: list[int] = []
    try:
        try:
            parent = os.open(root_path, dir_flags())
        except OSError as exc:
            raise refusal(
                f"refusing to use the {what}: {root_path} is a link or not a directory"
            ) from exc
        open_fds.append(parent)
        for part in parts:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=parent)
                except FileExistsError:
                    pass
            try:
                child = os.open(part, dir_flags(), dir_fd=parent)
            except OSError as exc:
                raise refusal(
                    f"refusing to use the {what}: {part!r} on the way to it is a link "
                    "or not a directory"
                ) from exc
            open_fds.append(child)
            parent = child
        yield parent
    finally:
        for fd in open_fds:
            with suppress(OSError):
                os.close(fd)
