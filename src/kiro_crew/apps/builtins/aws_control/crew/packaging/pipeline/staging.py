"""Staging ownership: the claim, the proof, and the only recursive deletes the builder makes.

The run marker that claims a staging tree, the rules that decide whether a tree is one this
build wrote (by path and through a held descriptor), and the private-aside disposal that
deletes exactly the tree those rules verified. The checks live beside the deletes they
authorise, because a rule missing from one delete site is data loss.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import stat
import uuid
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import IO

from kiro_crew.atomic_write import read_json_or

from . import destination as _destination
from . import hashing as _hashing
from . import pinned as _pinned
from .contract import (
    _BUILD_WRITES_EMPTY,
    _STAGING_OWNED_TOP_LEVEL,
    PLAN_FILENAME,
    PLAN_VERSION,
    ExportRefused,
)
from .pinned import _NOFOLLOW_READ_FLAGS


def _is_shape_this_build_never_writes(p: "Path") -> bool:
    """True for anything that is not a plain file or a plain directory.

    Both replacement checks in ``build_bundle`` decided ownership with ``p.is_file()``,
    which is False for an empty directory, a FIFO, a socket, a device node and a link
    to a directory. Every one of those therefore passed the scan that exists to refuse
    unowned content, and was then deleted by the ``shutil.rmtree`` that follows.
    Measured before this existed: an empty directory and a FIFO both survived the scan
    and were removed.

    A symlink is judged BEFORE ``is_file()``, which follows links. This build writes
    plain files and directories only, so a link is a shape it never produced no matter
    what its target looks like or what the entry is called.
    """
    if _pinned._is_redirecting_entry(p):
        # ``is_symlink()`` was the test here and it is too narrow: a Windows JUNCTION is a
        # reparse point that is not reported as a symlink, and ``shutil.rmtree`` traverses one
        # on Windows rather than unlinking it as it does a symlink. So a junction planted
        # inside the output directory turned the recursive delete loose on its target.
        return True
    return not p.is_file() and not p.is_dir()


#: First line of the staging marker. Its job is to tell OUR marker apart from any other
#: file that happens to sit at that path, because the previous check was
#: ``staging_marker.is_file()`` and every plain file satisfies that -- an operator's own
#: note beside their own ``<name>.staging`` directory authorised a recursive delete of it.
#:
#: What this is NOT: authentication. Anyone who can write to ``out_dir.parent`` can write
#: this line too. The threat it removes is COLLISION, which is the one that happens by
#: accident; against an adversary who already has write access to that directory a forged
#: marker is not the shortest path to harm, since they can delete the staging tree
#: themselves. Stated here rather than implied so nobody reads the token as a secret.
_STAGING_MARKER_TOKEN = "kiro-crew-bundle-staging-marker/1"

#: Identifies THIS run, not just this builder.
#:
#: The token alone said "a kiro-crew build made this", which two concurrent builds against the
#: same --out both satisfy -- so each read the other's marker as its own and deleted the other's
#: staging tree with the recursive delete the marker authorises. The loser then promoted a
#: half-built bundle or crashed on a missing file.
#:
#: pid plus randomness, because pid alone repeats: a container that reruns the builder can see
#: the same pid, and a stale marker from a killed run would then look like this run's own.
_RUN_ID = f"{os.getpid()}-{uuid.uuid4().hex[:16]}"

_STAGING_MARKER_BODY = (
    _STAGING_MARKER_TOKEN + "\n" + _RUN_ID + "\n"
    "Written by kiro-crew's crew bundle builder so a later run can tell this staging\n"
    "directory apart from one you created. Safe to delete when no build is running.\n"
)


def _rmtree_pinned(parent_fd: int, name: str) -> None:
    """Recursively delete ``name`` reached through ``parent_fd``, never by re-resolving a path.

    ``shutil.rmtree(path)`` re-resolves ``path`` from its string, so a parent or intermediate
    component swapped for a link after a descriptor was pinned is followed and the recursive
    delete lands wherever the link names -- outside ``--out`` and irreversible. This opens
    ``name`` ``O_NOFOLLOW`` relative to ``parent_fd`` (a name swapped for a link fails its own
    open and REFUSES rather than being followed), then removes the whole tree through directory
    descriptors: each child is unlinked, or for a subdirectory recursed into and ``rmdir``-ed,
    every step ``dir_fd``-relative, so no path is resolved after the pin. ``name`` is a single
    leaf under ``parent_fd``.
    """
    if not _pinned._dir_fd_supported():
        # This reaches every deleted path through a directory descriptor, which the platform
        # must support; the disposal callers only enter the pinned path where it does, so this
        # is a fail-closed floor rather than a reachable branch.
        raise ExportRefused(
            "a pinned recursive delete needs directory-descriptor support, which this "
            "platform lacks; refusing rather than delete through a re-resolved path."
        )
    fd = os.open(
        name, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd
    )
    try:
        with os.scandir(fd) as it:
            entries = list(it)
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                _rmtree_pinned(fd, entry.name)
            else:
                os.unlink(entry.name, dir_fd=fd)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent_fd)


def _write_marker_exclusive(path: Path, *, ours: bool = False) -> None:
    """Create the staging marker at ``<out>.staging.owned``, refusing a planted link.

    The mechanism is in :func:`_write_nofollow`; this names the payload and keeps the call
    site readable. It is a separate function because the marker's BODY is what
    ``_marker_is_ours`` reads back, so the two belong beside each other.

    *ours* is passed through from the caller's own ownership check. On the resume path OUR
    marker legitimately exists and must be replaced; on a fresh build any existing file is
    a stranger's and is refused. The caller is the only place that knows which case it is,
    because it is the one that ran ``_marker_is_ours`` before touching staging.
    """
    _destination._write_nofollow(path, _STAGING_MARKER_BODY, exclusive=not ours)


def _marker_lines_are_this_run(fh: "IO[str]") -> bool:
    """Whether an open marker names this builder AND this run.

    Both lines, because either alone is the wrong question. Without the token any file
    passes; without the run id a CONCURRENT build's marker passes, and the recursive delete
    the marker authorises then removes a staging tree another build is still writing.

    A marker from an earlier run of this same builder is deliberately NOT ours. That is a
    behaviour change: such a marker does NOT authorise the delete, which is how a crashed run's
    residue got cleaned up automatically. It now has to be removed by hand, and the refusal
    says so -- the alternative is being unable to tell a crashed run's leftovers from a live
    run's working directory, and only one of those is safe to delete.
    """
    return fh.readline().strip() == _STAGING_MARKER_TOKEN and fh.readline().strip() == _RUN_ID


def _marker_is_ours(path: Path) -> bool:
    """True only for a marker this builder wrote, read without following a link.

    ``is_file()`` was the whole check and it is true of any plain file, so the ownership
    proof that authorises ``shutil.rmtree`` was satisfied by a file the operator put
    there. The token has to be present, and the read has to refuse a symlink for the same
    reason the write does: a link here would let the answer come from a file outside the
    directory being judged.

    Reads through the shared no-reparse opener where ``dir_fd`` is unsupported (Windows),
    which refuses a redirect at the marker path in the open itself. The token check still
    holds there; what is lost is the anchoring of the components ABOVE the marker, and losing
    it on the platform whose links behave differently anyway is the same trade the rest of
    this module already makes.
    """
    if not _pinned._dir_fd_supported():
        # The redirect refusal is the OPEN, because this branch has no anchoring to lose the
        # race with: a by-name read follows a symlink AND a junction, so a marker path
        # someone planted a redirect over would be read through to its target. The verdict
        # matches the anchored branch below, where ``O_NOFOLLOW`` answers ELOOP and this
        # function returns False -- a redirect at the marker path is not a marker this run
        # wrote, on either platform.
        fd = _pinned._open_leaf_no_reparse(path)
        if fd is None:
            return False
        try:
            with os.fdopen(fd, "r", encoding="utf-8", errors="replace", newline="") as fh:
                return _marker_lines_are_this_run(fh)
        except (OSError, UnicodeError):
            return False
    try:
        parent_fd = _pinned._open_dir_nofollow_pinned(path.parent)
    except OSError:
        # No parent directory, so no marker -- the ordinary first build into a path whose
        # parent does not exist yet. This open sat OUTSIDE the guard below, so
        # `--out new/nested/bundle` raised an unhandled FileNotFoundError out of a function
        # whose entire job is to answer yes or no. A file where the parent should be
        # (NotADirectoryError), a permission failure, and a parent component swapped to a link
        # (the pinning walk fails its own open) all get the same answer for the same reason:
        # none of them is a marker this run wrote.
        return False
    try:
        fd = os.open(path.name, os.O_RDONLY | _NOFOLLOW_READ_FLAGS, dir_fd=parent_fd)
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
        return False
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EMLINK}:
            return False  # a symlink at the marker path is not our marker
        # Any other open failure (EACCES on a marker that exists, an I/O error) means we
        # CANNOT confirm this marker is one this run wrote. This function's contract is a
        # bool -- "is this our marker?" -- and the safe answer to "cannot tell" is False:
        # a marker we cannot read is treated as not-ours, which makes the caller refuse to
        # reuse the staging tree rather than delete on an unverified marker. Re-raising the
        # raw OSError instead would escape a bool-returning function as a foreign type.
        return False
    finally:
        os.close(parent_fd)
    # ``os.open(O_RDONLY)`` SUCCEEDS on a directory and it is ``fdopen`` in text mode that
    # fails, with an ``IsADirectoryError`` raised BEFORE the file object it would return owns
    # the descriptor -- so the ``with`` below reaches no close for it and each such answer
    # strands one fd. Taking the verdict on the DESCRIPTOR settles both halves at once: the
    # answer is still simply "no", and the fd is released here instead of by a wrapper that
    # was never built. A raw traceback escaping a bool-returning function is the other half.
    if _pinned._dir_fd_closed(fd):
        return False
    try:
        with os.fdopen(fd, "r", encoding="utf-8", errors="replace", newline="") as fh:
            return _marker_lines_are_this_run(fh)
    except UnicodeError:
        return False


def _refuse_unless_this_build_wrote_it(d: Path, flag: str, crew_name: str) -> None:
    """Refuse ``d`` unless every rule says this build produced it. Raises ``ExportRefused``.

    Three rules, and the reason they live in ONE function is that they did not. ``--out``
    applied all three; the ``<out>.previous`` path added later applied the first two and
    was reported as a defect for exactly the case the third one catches -- a directory of
    the operator's own regular files that happen to use bundle names. Each site is about to
    run a RECURSIVE DELETE, so a rule missing from one of them is data loss.

    1. NAMES: nothing at the top level this build does not write.
    2. SHAPES: nothing anywhere that is not a plain file or directory. The name rule reads
       the CONTAINER while the delete is recursive, so ``skills`` being an owned name let
       ``skills/notes.txt`` through, and ``p.is_file()`` was False for an empty directory,
       a FIFO, a socket and a link to a directory -- each invisible, then deleted.
    3. THE MANIFEST'S OWN DIGEST: names and shapes are both satisfied by a directory
       someone else assembled. A bundle this build wrote carries a manifest whose digest
       covers every file except the manifest, and the plan is written after that digest is
       taken, so re-deriving while skipping the plan reproduces the recorded value exactly
       when nothing has been added, moved or edited.

    ``flag`` names the path in the operator's own vocabulary, so the message points at
    something they can act on rather than at an internal name.
    """
    if _pinned._is_redirecting_entry(d):
        # The ANCHOR, before anything relative to it. ``d.exists()``/``is_dir()``/``iterdir()``
        # and ``bundle_digest(d)`` below all FOLLOW a symlinked or junctioned ``d``, so a
        # redirected root would have its TARGET verified for ownership and then the recursive
        # delete keyed to this verdict would run through the link -- the tree under the anchor
        # was checked while the anchor itself was not. Refuse the root first: everything else
        # in this function is relative to it, and a verdict about a root you did not verify is
        # a verdict about the wrong tree.
        raise ExportRefused(
            f"{flag} {d} is a symlink or reparse point. Its ownership cannot be verified "
            f"because every check here would follow it to another tree, and a recursive "
            f"delete keyed to that verdict would run through the link. Point {flag} at a real "
            f"directory."
        )
    if d.exists() and not d.is_dir():
        raise ExportRefused(
            f"{flag} {d} exists and is not a directory. `exists()` is true for a plain "
            f"file and the scans below would then raise instead of refusing. Move that "
            f"file, or point --out elsewhere."
        )
    # ``iterdir`` on an unreadable existing ``d`` raises ``PermissionError``, which is NOT an
    # ``ExportRefused``; every caller keys its staging/marker cleanup to ``ExportRefused``, so
    # a raw ``OSError`` escaping here skips that cleanup and leaks the staging tree and its
    # ownership marker -- and the marker is what authorises the next run's recursive delete.
    # "Unreadable" is refused, in the same category as "not owned", not left to crash: convert
    # the enumeration failure into ``ExportRefused`` so the existing cleanup runs.
    try:
        strangers = sorted(p.name for p in d.iterdir() if p.name not in _STAGING_OWNED_TOP_LEVEL)
    except OSError as exc:
        raise ExportRefused(
            f"{flag} {d} exists but could not be listed ({exc}); refusing rather than leave "
            f"it unverified. Fix its permissions or point {flag} elsewhere."
        ) from exc
    if strangers:
        raise ExportRefused(
            f"{flag} {d} holds files this build does not own "
            f"({', '.join(strangers[:5])}"
            + (f", and {len(strangers) - 5} more" if len(strangers) > 5 else "")
            + "). Building replaces the whole directory, so it would delete them. "
            "Point --out at a fresh or previous bundle directory."
        )
    wrong_shape = sorted(
        p.relative_to(d).as_posix()
        for p in _pinned._walk_no_reparse(d)
        if _is_shape_this_build_never_writes(p)
    )
    if wrong_shape:
        raise ExportRefused(
            f"{flag} {d} holds entries of a shape this build never writes "
            f"({', '.join(wrong_shape[:5])}"
            + (f", and {len(wrong_shape) - 5} more" if len(wrong_shape) > 5 else "")
            + "). Building replaces the whole directory, so it would delete them, and a "
            "link, a FIFO or a device node is not something a previous bundle left "
            "behind. Point --out at a fresh or previous bundle directory."
        )
    entries = [p for p in _pinned._walk_no_reparse(d) if p.is_file()]
    # DIRECTORIES are verified too, by whether they lead anywhere this build wrote.
    #
    # Every check above this line either looks at the top level only (``d.iterdir()``) or at
    # SHAPE, and a plain directory passes both. ``entries`` then filters to ``is_file()``, so
    # a directory was never compared against anything at all: ``<out>/skills/notes/`` -- an
    # operator's own empty directory under a name this build does write -- passed the whole
    # scan and was removed by the ``rmtree`` below. The digest check could not catch it
    # either, because a digest is taken over file content and an empty directory contributes
    # none.
    #
    # A directory this build produced has a file under it, with ONE exception measured here:
    # ``skills/`` is created even when the plan selects no skills, so the top-level names this
    # build writes are owned whether or not anything is under them. Below that level the rule
    # holds, and below that level is where the loss was: ``<out>/skills/notes/``.
    owned_dir_paths = {parent for p in entries for parent in p.relative_to(d).parents}
    empty_dirs = sorted(
        rel.as_posix()
        for rel in (
            p.relative_to(d)
            for p in _pinned._walk_no_reparse(d)
            if p.is_dir() and not p.is_symlink()
        )
        if rel not in owned_dir_paths and rel.as_posix() not in _BUILD_WRITES_EMPTY
    )
    if empty_dirs:
        raise ExportRefused(
            f"{flag} {d} holds directories with no file this build would have written "
            f"({', '.join(empty_dirs[:5])}"
            + (f", and {len(empty_dirs) - 5} more" if len(empty_dirs) > 5 else "")
            + "). Building replaces the whole directory, so it would delete them, and an "
            "empty directory is not something a previous bundle left behind. Point --out at "
            "a fresh or previous bundle directory."
        )
    non_plan = [p for p in entries if p.relative_to(d).as_posix() != PLAN_FILENAME]
    manifest_path = d / "manifest.json"
    if not non_plan and entries:
        # A directory holding ONLY the plan file is the normal state between the `plan`
        # verb and the `build` verb, so it must be accepted -- refusing it would break the
        # documented two-step workflow. Ownership is proven by the plan's own IDENTITY, not
        # its filename or a version number: ``plan_version`` is generic (any JSON carrying it
        # passes), so a foreign ``curation-plan.json`` that merely says ``plan_version`` would
        # be treated as this build's staging tree and the directory deleted recursively. The
        # plan records which crew it is for, so the crew it names must also match the crew
        # being built; only then is it a plan this tool wrote for this build.
        body = read_json_or(
            d / PLAN_FILENAME,
            None,
            logger=logging.getLogger(__name__),
            what="curation plan",
        )
        recognised = (
            isinstance(body, dict)
            and body.get("plan_version") == PLAN_VERSION
            and body.get("crew") == crew_name
        )
        if not recognised:
            raise ExportRefused(
                f"{flag} {d} holds a single {PLAN_FILENAME} that this tool did not write for "
                f"crew {crew_name!r} (it must carry plan_version {PLAN_VERSION} and name this "
                f"crew). A version number is not an ownership claim and the name alone is not "
                f"proof of origin, and building replaces the directory recursively. Point "
                f"--out at a fresh directory or at a complete previous bundle."
            )
    if non_plan and not manifest_path.is_file():
        raise ExportRefused(
            f"{flag} {d} has bundle-shaped contents but no manifest.json, so it is not a "
            "directory this build produced and replacing it would delete files of "
            "unknown origin. Point --out at a fresh directory or at a complete previous "
            "bundle."
        )
    if non_plan:
        try:
            decoded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(decoded, dict):
                # A manifest that PARSES but is not an object: ``[]`` decodes fine and then
                # ``.get`` raises AttributeError, which is not in the tuple below. Measured: a
                # rebuild over such a bundle exited as a traceback, and it happens after the
                # staging tree and its ownership marker exist, so the operator is left with
                # both and no message naming either.
                raise ExportRefused(
                    f"{flag} {d} has a manifest.json that decodes to "
                    f"{type(decoded).__name__}, not an object, so the bundle it claims to "
                    f"describe cannot be verified before a recursive replace."
                )
            recorded = decoded.get("digest")
        except (OSError, ValueError) as exc:
            raise ExportRefused(
                f"{flag} {d} has a manifest.json that cannot be read ({exc}), so the "
                "bundle it claims to describe cannot be verified before a recursive "
                "replace."
            ) from None
        if recorded != _hashing.bundle_digest(d, also_skip=frozenset({PLAN_FILENAME})):
            raise ExportRefused(
                f"{flag} {d} does not match the bundle its manifest describes, so it "
                "holds at least one file this build did not write (a nested stray such "
                "as skills/notes.txt, or an edited file). Building replaces the "
                "directory recursively and would delete it. Point --out at a fresh "
                "directory."
            )


class _CapturedTree:
    """What one dir-fd-relative walk of a captured tree found.

    Every field is read THROUGH the held descriptor -- ``os.scandir(fd)``, ``entry.stat`` and
    ``os.open(..., dir_fd=fd)`` -- never by re-resolving the tree's name, so a parent component
    swapped after the capture cannot steer any read to a decoy. Regular-file bytes are hashed
    inline so the digest needs no second by-name pass, and the top-level ``manifest.json`` and
    plan are stashed whole for the ownership rules.
    """

    __slots__ = ("top_names", "files", "dirs", "specials", "digest_rows", "manifest", "plan")

    def __init__(self) -> None:
        self.top_names: list[str] = []
        self.files: list[str] = []
        self.dirs: list[str] = []
        self.specials: list[str] = []
        self.digest_rows: list[list[str]] = []
        self.manifest: "bytes | None" = None
        self.plan: "bytes | None" = None


def _read_regular_leaf_fd(dir_fd: int, name: str) -> "bytes | None":
    """Raw bytes of a single leaf opened ``O_NOFOLLOW`` relative to ``dir_fd``.

    ``name`` is one component under the held descriptor, so a leaf swapped for a link fails its
    own open and yields ``None`` with no path re-resolved. Returns ``None`` on a redirect, a
    special file, or a read error -- the same shape ``_read_bytes_openat`` gives, but reached
    through a descriptor the caller already holds rather than by walking a path from a root.

    The caller reads the entry's shape from its dirent before calling, and the held directory
    fd pins the directory but not the NAME inside it, so a regular file swapped for a directory
    between that stat and this open is still open to the same ``fdopen`` strand every leaf read
    here is: hence the same ``_dir_fd_closed`` verdict, taken on the descriptor.
    """
    try:
        fd = os.open(name, os.O_RDONLY | _NOFOLLOW_READ_FLAGS, dir_fd=dir_fd)
    except OSError:
        return None
    if _pinned._dir_fd_closed(fd):
        return None
    try:
        with os.fdopen(fd, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _inspect_captured_tree_fd(
    dir_fd: int, also_skip: frozenset[str], *, read_files: bool
) -> "_CapturedTree":
    """Walk the captured tree through ``dir_fd`` and collect the facts the ownership rules need.

    Mirrors ``_walk_no_reparse`` + ``bundle_digest``, but every ``scandir``, ``stat`` and read
    is descriptor-relative: none names an absolute path, so the swap the ownership check is
    exposed to -- a parent replaced after the tree was captured -- cannot reach any of them.
    Fails closed on a directory that exists but cannot be listed, or an entry that cannot be
    stat'd, the same refusal ``_walk_no_reparse`` gives, so a tree it silently omits part of is
    refused rather than verified. ``read_files`` hashes regular files for the digest and stashes
    the top-level ``manifest.json`` / plan; a caller that only needs names and shapes (the
    staging check) passes ``False`` and reads nothing.
    """
    if not _pinned._dir_fd_supported():
        # Every read here is directory-descriptor-relative, which the platform must support;
        # the disposal callers only reach this where it does, so this is a fail-closed floor.
        raise ExportRefused(
            "inspecting a captured tree needs directory-descriptor support, which this "
            "platform lacks; refusing rather than re-resolve the tree by name."
        )
    found = _CapturedTree()

    def _descend(fd: int, prefix: str) -> None:
        if (
            not _pinned._dir_fd_supported()
        ):  # fail-closed floor; the enclosing guard already refused
            raise ExportRefused("directory-descriptor support is required to walk a captured tree")
        try:
            with os.scandir(fd) as it:
                entries = list(it)
        except OSError as exc:
            raise ExportRefused(
                f"a directory inside the captured tree could not be listed ({exc}); refusing "
                f"rather than verify a tree it silently omits part of."
            ) from exc
        for entry in entries:
            rel = f"{prefix}{entry.name}"
            if prefix == "":
                found.top_names.append(entry.name)
            try:
                mode = entry.stat(follow_symlinks=False).st_mode
            except OSError as exc:
                raise ExportRefused(
                    f"an entry inside the captured tree could not be inspected ({exc}); "
                    f"refusing rather than verify a tree of unknown shape."
                ) from exc
            if stat.S_ISDIR(mode):
                found.dirs.append(rel)
                sub = os.open(
                    entry.name,
                    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=fd,
                )
                try:
                    _descend(sub, f"{rel}/")
                finally:
                    os.close(sub)
                continue
            if not stat.S_ISREG(mode):
                # A symlink, FIFO, socket or device: a shape this build never writes. Collected,
                # not read -- the ownership check refuses on it before any digest read runs.
                found.specials.append(rel)
                continue
            found.files.append(rel)
            if not read_files:
                continue
            data = _read_regular_leaf_fd(fd, entry.name)
            if prefix == "" and entry.name == "manifest.json":
                found.manifest = data
            if prefix == "" and entry.name == PLAN_FILENAME:
                found.plan = data
            if rel == "manifest.json" or rel in also_skip:
                # The manifest carries the digest and ``also_skip`` holds the plan added after a
                # prior bundle was built: the two entries that leave the signed set on purpose,
                # the same exclusions ``bundle_digest`` makes.
                continue
            if data is None:
                raise ExportRefused(
                    f"the captured file {rel} could not be read as a regular file through a "
                    f"no-follow descriptor; refusing to verify a digest over bytes reached by "
                    f"following a redirect."
                )
            found.digest_rows.append([rel, hashlib.sha256(data).hexdigest()])

    _descend(dir_fd, "")
    # ``bundle_digest`` appends rows in ``_walk_no_reparse`` order, which is a sort of the
    # tree's paths; the recursion above visits in ``scandir`` order, so sort by the same key to
    # reproduce that value byte-for-byte.
    found.digest_rows.sort(key=lambda row: row[0])
    return found


def _open_captured_dir_fd(parent_fd: int, moved_rel: str, label: Path, flag: str) -> int:
    """Open the captured tree as an ``O_NOFOLLOW`` directory descriptor through the pinned parent.

    ``moved_rel`` is ``<private>/<name>`` under ``parent_fd``: the private directory is this
    build's own exclusive creation and ``<name>`` was renamed in relative to ``parent_fd``, so
    the tree is reached through the held descriptor rather than by re-resolving ``label``'s
    absolute path. A captured entry that is a link or is not a directory fails this open
    and is refused -- the shape refusal the ownership check opens with, kept here because this
    is where the descriptor is obtained.
    """
    if not _pinned._dir_fd_supported():
        raise ExportRefused(
            f"{flag} {label} cannot be opened as a pinned directory descriptor because this "
            f"platform lacks directory-descriptor support; refusing rather than re-resolve it."
        )
    try:
        return os.open(
            moved_rel,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise ExportRefused(
            f"{flag} {label} is a symlink, is not a directory, or changed shape after it was "
            f"captured ({exc}). Its ownership cannot be verified through the held descriptor, "
            f"so a recursive delete keyed to that verdict is refused. Point {flag} at a real "
            f"directory."
        ) from exc


def _verify_build_wrote_captured_fd(
    parent_fd: int, moved_rel: str, flag: str, crew_name: str, *, label: Path
) -> None:
    """Ownership check of ``_refuse_unless_this_build_wrote_it``, read through the pinned parent.

    Same three rules -- owned top-level names, no shape this build never writes, and the
    manifest's own digest -- run on the entry the rename captured, reached only through a
    descriptor opened ``O_NOFOLLOW`` under ``parent_fd``. A parent swapped after the capture
    cannot make this inspect a decoy while the sweep deletes the captured inode, because nothing
    here re-resolves ``label``'s path; ``label`` supplies the operator-facing path for messages
    only. Raises ``ExportRefused`` on any rule.
    """
    dir_fd = _open_captured_dir_fd(parent_fd, moved_rel, label, flag)
    try:
        tree = _inspect_captured_tree_fd(dir_fd, frozenset({PLAN_FILENAME}), read_files=True)
    finally:
        os.close(dir_fd)

    strangers = sorted(n for n in tree.top_names if n not in _STAGING_OWNED_TOP_LEVEL)
    if strangers:
        raise ExportRefused(
            f"{flag} {label} holds files this build does not own "
            f"({', '.join(strangers[:5])}"
            + (f", and {len(strangers) - 5} more" if len(strangers) > 5 else "")
            + "). Building replaces the whole directory, so it would delete them. "
            "Point --out at a fresh or previous bundle directory."
        )
    wrong_shape = sorted(tree.specials)
    if wrong_shape:
        raise ExportRefused(
            f"{flag} {label} holds entries of a shape this build never writes "
            f"({', '.join(wrong_shape[:5])}"
            + (f", and {len(wrong_shape) - 5} more" if len(wrong_shape) > 5 else "")
            + "). Building replaces the whole directory, so it would delete them, and a "
            "link, a FIFO or a device node is not something a previous bundle left "
            "behind. Point --out at a fresh or previous bundle directory."
        )
    owned_dir_paths: set[str] = set()
    for rel in tree.files:
        # ``rel`` is a POSIX-separated name the descriptor walk produced (``.as_posix()``
        # form), so its ancestor directories are parsed with ``PurePosixPath`` rather than a
        # raw ``"/"`` split -- the same reason the source-component parse above uses it, and it
        # keeps these names canonical against ``tree.dirs`` on every platform.
        for ancestor in PurePosixPath(rel).parents:
            if ancestor.name:  # skip the ``.`` root PurePosixPath yields last
                owned_dir_paths.add(ancestor.as_posix())
    empty_dirs = sorted(
        d for d in tree.dirs if d not in owned_dir_paths and d not in _BUILD_WRITES_EMPTY
    )
    if empty_dirs:
        raise ExportRefused(
            f"{flag} {label} holds directories with no file this build would have written "
            f"({', '.join(empty_dirs[:5])}"
            + (f", and {len(empty_dirs) - 5} more" if len(empty_dirs) > 5 else "")
            + "). Building replaces the whole directory, so it would delete them, and an "
            "empty directory is not something a previous bundle left behind. Point --out at "
            "a fresh or previous bundle directory."
        )
    non_plan = [rel for rel in tree.files if rel != PLAN_FILENAME]
    if not non_plan and tree.files:
        # A directory holding ONLY the plan file is the normal state between the plan verb and
        # the build verb. Ownership is proven by the plan's own identity: it must carry this
        # tool's plan_version and name the crew being built, because a version number alone is
        # generic and a filename alone is not proof of origin.
        recognised = False
        if tree.plan is not None:
            try:
                body = json.loads(tree.plan.decode("utf-8"))
                recognised = (
                    isinstance(body, dict)
                    and body.get("plan_version") == PLAN_VERSION
                    and body.get("crew") == crew_name
                )
            except (ValueError, UnicodeDecodeError):
                recognised = False
        if not recognised:
            raise ExportRefused(
                f"{flag} {label} holds a single {PLAN_FILENAME} that this tool did not write "
                f"for crew {crew_name!r} (it must carry plan_version {PLAN_VERSION} and name "
                f"this crew). A version number is not an ownership claim and the name alone is "
                f"not proof of origin, and building replaces the directory recursively. Point "
                f"--out at a fresh directory or at a complete previous bundle."
            )
    if non_plan and "manifest.json" not in tree.files:
        raise ExportRefused(
            f"{flag} {label} has bundle-shaped contents but no manifest.json, so it is not a "
            "directory this build produced and replacing it would delete files of "
            "unknown origin. Point --out at a fresh directory or at a complete previous "
            "bundle."
        )
    if non_plan:
        if tree.manifest is None:
            raise ExportRefused(
                f"{flag} {label} has a manifest.json that cannot be read, so the bundle it "
                "claims to describe cannot be verified before a recursive replace."
            )
        try:
            decoded = json.loads(tree.manifest.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ExportRefused(
                f"{flag} {label} has a manifest.json that cannot be read, so the bundle it "
                "claims to describe cannot be verified before a recursive replace."
            ) from None
        if not isinstance(decoded, dict):
            raise ExportRefused(
                f"{flag} {label} has a manifest.json that decodes to "
                f"{type(decoded).__name__}, not an object, so the bundle it claims to "
                f"describe cannot be verified before a recursive replace."
            )
        payload = json.dumps(tree.digest_rows, ensure_ascii=False, separators=(",", ":"))
        computed = "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
        if decoded.get("digest") != computed:
            raise ExportRefused(
                f"{flag} {label} does not match the bundle its manifest describes, so it "
                "holds at least one file this build did not write (a nested stray such "
                "as skills/notes.txt, or an edited file). Building replaces the "
                "directory recursively and would delete it. Point --out at a fresh "
                "directory."
            )


def _verify_captured_is_staging_fd(
    parent_fd: int,
    moved_rel: str,
    *,
    label: Path,
    expected_identity: "tuple[int, int] | None" = None,
) -> None:
    """Confirm a captured tree is THIS build's own staging, read through the pinned parent.

    The moved-entry counterpart of the staging leftover check: when a retained
    descriptor identity is available, the captured tree must be that exact inode;
    only then are its top-level names and shapes checked. A tree swapped in before
    capture is moved (not deleted), fails here, and is left where it came from.
    Raises ``ExportRefused`` on any leftover or identity mismatch.
    """
    dir_fd = _open_captured_dir_fd(parent_fd, moved_rel, label, "the staging path")
    try:
        captured = os.fstat(dir_fd)
        if (
            expected_identity is not None
            and (
                captured.st_dev,
                captured.st_ino,
            )
            != expected_identity
        ):
            raise ExportRefused(
                f"the staging path {label} is no longer the directory this build opened "
                f"(its inode changed before cleanup). It has NOT been deleted."
            )
        tree = _inspect_captured_tree_fd(dir_fd, frozenset(), read_files=False)
    finally:
        os.close(dir_fd)
    leftover = sorted(
        rel
        for rel, is_special in (
            *((f, False) for f in tree.files),
            *((d, False) for d in tree.dirs),
            *((s, True) for s in tree.specials),
        )
        if PurePosixPath(rel).parts[0] not in _STAGING_OWNED_TOP_LEVEL or is_special
    )
    if leftover:
        raise ExportRefused(
            f"the staging path {label} changed between the ownership check and its cleanup and "
            f"now holds files this build did not write ({', '.join(leftover[:5])}). It has NOT "
            f"been deleted. Move it, or point --out elsewhere."
        )


def _dispose_via_private_aside(
    target: Path,
    verify: Callable[[int, str], None],
    settle: Callable[[str, int], None],
    *,
    resolved_parent: "Path | None" = None,
) -> bool:
    """Recursively delete ``target`` through a run-private aside, all relative to a pinned parent.

    ``shutil.rmtree(target)`` re-resolves ``target`` from its path string, so a swap of
    ``target`` OR of a PARENT component between the ownership check and the delete lands the
    recursive delete on whatever the path names then, and that delete is irreversible.
    ``resolved_parent`` is the parent resolved once at validation; this opens it by descriptor,
    walking every component ``O_NOFOLLOW`` and HOLDING the descriptor across the whole
    operation, and reaches ``target``, the private aside, and the caller's disposal destination
    as single leaves under it. A component swapped for a link since validation fails its own
    no-follow open and REFUSES here rather than being followed; a component swapped after this
    open is defeated, because every mutation goes through the held descriptor rather than
    re-resolving the name between two mutation points. Binding the ownership check and the
    delete to one held descriptor removes both windows:

    1. Create a private directory UNDER the pinned parent with ``os.mkdir(dir_fd=...)`` and mode
       ``0o700`` -- this build is the only writer of a name no other process chose, so nothing
       can pre-plant or swap it, and it cannot be relocated by a parent-name swap because it is
       created relative to the held descriptor.
    2. ``os.rename`` ``target`` into that private directory with ``src_dir_fd``/``dst_dir_fd``
       set to the pinned parent. ``rename`` acts on the entry under that descriptor, not a
       re-resolved path: a concurrent swap either loses the race (``target`` already gone) or
       moves the swapped tree into the private directory, where nothing outside can reach it.
    3. ``verify`` the MOVED tree -- the exact entry the rename captured -- reached through
       ``parent_fd`` as ``(parent_fd, moved_rel)``, never by re-resolving a path, so a parent
       swapped after the capture cannot make it inspect a decoy while the sweep deletes the
       captured inode. If it is not one this build wrote, rename it BACK ``dir_fd``-relative (a
       swapped-in tree the operator owns is returned untouched) and refuse; only a verified tree
       is disposed of.
    4. ``settle`` acts on the moved entry ``dir_fd``-relative to the pinned parent (the caller
       renames it to its destination; a purge leaves it for the sweep below). The private
       directory is then removed through the pinned parent by ``_rmtree_pinned``, which reaches
       every deleted path through a directory descriptor and refuses a redirect -- so the
       recursive delete cannot be steered outside the pinned parent, and it is the same inode
       step 3 verified.

    Best-effort at the edges: if ``target`` is already gone (step 2 raises
    ``FileNotFoundError``) there is nothing to dispose of and the private dir is removed; a
    partially-created private dir is cleaned on any failure.

    Returns ``True`` when the rename captured ``target``, ``verify`` and ``settle`` ran on it,
    and the private-dir sweep completed. Returns ``False`` in two cases: the already-gone case,
    where nothing was captured or verified (the early ``return``), and a sweep that did not
    complete. For the purge's no-op ``settle`` that sweep IS the delete, so ``False`` there means
    the captured tree is still under the ``.smc-purge-*`` aside, undeleted. The staging purge is
    the one caller that reads the return; the rename-to-destination and post-promotion callers
    ignore it, since their tree already reached its destination (or was already gone) and an
    unswept private dir is only best-effort residue for them.

    ``resolved_parent`` defaults to ``target.parent.resolve()`` for a direct caller with no
    earlier reading to pin; the transaction passes the value it resolved at validation so the
    pin reflects that moment rather than a fresh resolve at disposal time.
    """
    if resolved_parent is None:
        resolved_parent = target.parent.resolve()
    # Bound before anything that can raise: the final ``return`` reads this, and binding it up
    # front means no error path -- a failed parent open, a failed ``mkdir`` -- can reach that
    # read with the name unbound. It flips to ``True`` only if the private-dir sweep fails.
    sweep_failed = False
    try:
        parent_fd = _pinned._open_dir_nofollow_pinned(resolved_parent, already_resolved=True)
    except OSError as exc:
        # A component of the parent changed to a link or stopped being an openable directory
        # since --out was validated. Refuse rather than let a re-resolved path steer the
        # recursive delete onto whatever the swapped component now names.
        raise ExportRefused(
            f"cannot dispose of {target}: a component of its parent changed to a link or is no "
            f"longer an openable directory since --out was validated ({exc}). Nothing was "
            f"deleted. Point --out elsewhere."
        ) from exc
    try:
        private_name = f".smc-purge-{uuid.uuid4().hex}"
        private = target.parent / private_name
        target_name = target.name
        moved_rel = f"{private_name}/{target_name}"
        # exist_ok False (our exclusive name), created relative to the held parent descriptor.
        os.mkdir(private_name, mode=0o700, dir_fd=parent_fd)
        cleanup_private = True
        try:
            moved = private / target_name
            try:
                os.rename(target_name, moved_rel, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            except FileNotFoundError:
                # target vanished (a concurrent process removed or moved it first); nothing to
                # dispose of, and the empty private dir is cleaned in the finally below. Report
                # "not captured" rather than success: a MOVED tree still exists wherever it was
                # moved to, and this call neither captured nor verified it.
                return False
            try:
                verify(parent_fd, moved_rel)
            except BaseException:
                # ANY exception out of ``verify`` -- not only ``ExportRefused`` -- must restore
                # the captured tree before it propagates, or the ``finally`` below sweeps the
                # private aside and takes the operator's verified bundle with it. ``verify``
                # now inspects the tree through the pinned descriptor, so it can raise an
                # ``OSError`` from the walk as well as ``ExportRefused``; and a
                # ``KeyboardInterrupt`` or ``SystemExit`` during verification destroys the
                # bundle just as thoroughly as a ``ValueError``. So the handler is
                # ``BaseException``: restore the moved tree to where it came from, and if that
                # restore fails, RETAIN the private aside (do not let the finally sweep it) and
                # name where the tree now sits. A failed restore is not a licence to delete a
                # tree this build did not certify. There is no correct recursive delete of a
                # tree left unverified.
                try:
                    os.rename(moved_rel, target_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                except OSError as restore_exc:
                    cleanup_private = False
                    raise ExportRefused(
                        f"verification of the tree moved aside from {target} did not complete "
                        f"and restoring it failed ({restore_exc}). It has NOT been deleted -- "
                        f"it is at {moved}. Nothing was removed; move it back or remove it by "
                        f"hand."
                    ) from restore_exc
                raise
            # Disposal is the caller's, because only the caller knows what a verified tree is
            # FOR: the previous bundle is deleted, the operator's current one is kept as the
            # rollback copy. What must not vary is which entry the disposal acts on -- the one
            # the rename captured and ``verify`` just cleared, reached through the pinned parent,
            # never a path resolved again.
            try:
                settle(moved_rel, parent_fd)
            except BaseException:
                # Disposal raised, and the MOVED tree is still in the private aside -- for the
                # rename-to-destination settle this is the operator's current bundle, verified
                # moments ago. The sweep below would recursively delete it. Same discipline as
                # the verify-failure path above: put it back where it came from,
                # ``dir_fd``-relative, and if that cannot be done, RETAIN the aside and name
                # where the tree sits rather than deleting a tree this build did not create.
                # ``BaseException`` because the obligation not to delete the operator's tree
                # holds regardless of why disposal failed -- a cancelled build included -- and
                # it re-raises, so nothing is swallowed.
                try:
                    os.rename(moved_rel, target_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                except OSError as restore_exc:
                    cleanup_private = False
                    raise ExportRefused(
                        f"the tree at {target} was moved aside, disposing of it failed, and "
                        f"restoring it failed too ({restore_exc}). It has NOT been deleted -- it "
                        f"is at {moved}. Move it back or remove it by hand."
                    ) from restore_exc
                raise
        finally:
            if cleanup_private:
                # Reach the delete through the pinned parent, never by re-resolving
                # ``private``'s path: a bare ``shutil.rmtree(private)`` would follow a parent
                # component swapped after the pin. Best-effort AS A DELETE MECHANISM -- a private
                # dir that cannot be swept is never chased outside the parent -- but WHETHER it
                # completed is recorded, because for the purge caller this sweep is the disposal
                # itself and a silent failure would leave the staging tree undeleted in the aside
                # while the caller reports success.
                try:
                    _rmtree_pinned(parent_fd, private_name)
                except OSError:
                    sweep_failed = True
    finally:
        os.close(parent_fd)
    # The early ``return False`` (target already gone) exits before this line. On the ordinary
    # exit the purge caller's no-op ``settle`` left the captured tree in the private dir, so an
    # incomplete sweep means it is still there, undeleted: report that rather than success.
    return not sweep_failed


def _purge_via_private_aside(
    target: Path,
    verify: Callable[[int, str], None],
    *,
    resolved_parent: "Path | None" = None,
) -> bool:
    """Delete ``target`` through the private aside: capture, verify, then sweep via the pin.

    The verified tree is removed by the ``_rmtree_pinned`` sweep of the private directory in
    ``_dispose_via_private_aside``, so the settle step has nothing to do -- the no-op settle
    leaves the tree in the private dir for that sweep to delete.

    Returns ``_dispose_via_private_aside``'s result: ``False`` when ``target`` was already gone
    (nothing captured or verified) or when the sweep did not complete (the captured tree is
    still under the aside). Only ``_purge_staging_best_effort`` reads it; the transaction-path
    and post-promotion ``<out>.previous`` purges ignore it, so leftover scratch never turns a
    bundle that already landed into a refusal.
    """
    return _dispose_via_private_aside(
        target,
        verify,
        lambda moved_rel, pfd: None,
        resolved_parent=resolved_parent,
    )


def _unlink_out_leaf_best_effort(leaf: Path, resolved_parent: Path) -> None:
    """Best-effort unlink of a single ``--out``-derived leaf, reached through a pinned parent.

    The staging marker and the report live BESIDE ``--out`` in a directory this build does not
    own. A bare ``leaf.unlink()`` re-resolves the leaf's path string, so a parent component
    swapped for a link since ``--out`` was validated steers the unlink outside the validated
    parent. This opens ``resolved_parent`` ``O_NOFOLLOW`` and unlinks the leaf ``dir_fd``
    relative to it, never by re-resolving the name.

    Leave-residue is the deny-by-default failure: if the parent cannot be pinned (a component
    changed to a link, or is not an openable directory), the leaf is LEFT rather than
    deleted on a guess of where it now is -- deleting on that guess is the escape this closes.
    Best-effort like the cleanup it sits among: it runs inside failure handlers and on the
    ordinary exit, so a missing leaf or an unpinnable parent is swallowed rather than raised.
    A later reader sees a leftover marker as the residue a swapped parent forced, not a bug.
    """
    try:
        parent_fd = _pinned._open_dir_nofollow_pinned(resolved_parent, already_resolved=True)
    except OSError:
        return  # parent unpinnable -> leave residue, do not guess where the leaf is
    try:
        os.unlink(leaf.name, dir_fd=parent_fd)
    except OSError:
        pass  # missing, a directory, or otherwise not removable through the pin: leave it
    finally:
        os.close(parent_fd)


def _purge_staging_best_effort(
    staging: Path,
    resolved_parent: Path,
    *,
    staging_fd: "int | None" = None,
) -> bool:
    """Attempt identity-checked teardown of this build's staging tree.

    Returns ``True`` only when the private-aside capture, verification, and sweep
    completed. ``False`` means this call did not delete the tree: it was already gone or moved
    away, verification refused it (it is restored where it was), or the sweep did not complete
    (it is under a ``.smc-purge-*`` directory beside ``staging``).

    A bare ``shutil.rmtree`` of ``staging`` with ``ignore_errors=True`` re-resolves ``staging``'s
    path string, so a parent swapped between a failure and its cleanup steers the recursive
    delete outside ``--out`` -- the failure path then deletes as irreversibly as the success
    path. This captures
    ``staging`` into a run-private aside under a parent pinned ``O_NOFOLLOW``, confirms the
    captured tree holds only names and shapes this build writes, and deletes only then. When
    ``staging_fd`` is retained, the captured tree must also match that descriptor's device and
    inode; a bundle-shaped replacement therefore fails verification and is restored untouched.

    Best-effort, like the ``ignore_errors=True`` it replaces: it runs inside a failure handler,
    so it must not raise a NEW error over the exception already in flight. A refusal (a
    swapped-in tree) or a pin-open failure is swallowed, and a sweep that cannot complete comes
    back as ``False``; either way the scratch tree is left for the next run rather than masking
    the real failure.
    """
    expected_identity: "tuple[int, int] | None" = None
    if staging_fd is not None:
        try:
            opened = os.fstat(staging_fd)
        except OSError:
            return False
        expected_identity = (opened.st_dev, opened.st_ino)
    try:
        # ``True`` only when this call captured the tree, verified its identity, and the sweep
        # deleted it. A target that was already gone returns ``False``: nothing was captured
        # or verified, and a tree moved away by another process still exists where it went.
        return _purge_via_private_aside(
            staging,
            lambda parent_fd, moved_rel: _verify_captured_is_staging_fd(
                parent_fd,
                moved_rel,
                label=staging,
                expected_identity=expected_identity,
            ),
            resolved_parent=resolved_parent,
        )
    except Exception:
        # Swallow everything an OSError-scoped ``ignore_errors=True`` would, plus the ownership
        # ``ExportRefused``: this is teardown of the build's own scratch, and leaving it is safe
        # (the next run's ownership check handles a residue). A ``BaseException`` -- a cancel --
        # is left to propagate, as it is not the cleanup's to swallow.
        return False
