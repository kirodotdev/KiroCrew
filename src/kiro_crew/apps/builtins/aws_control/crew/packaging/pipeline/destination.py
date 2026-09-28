"""Writes to paths derived from ``--out``, judged by shape and never through a link.

The UNC screen on ``--out``, the parent check before a ``mkdir``, and the one no-follow
writer every plan, marker, report and staged leaf goes through. The writer decides SHAPE
only; whether a file at the path is this build's to replace is its caller's question.
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path, PurePosixPath

from . import pinned as _pinned
from .contract import ExportRefused
from .pinned import _NOFOLLOW_READ_FLAGS


def _refuse_unc_out(out_dir: Path) -> None:
    """Refuse a UNC-shaped ``--out`` before any path derived from it is touched.

    A screen that must ``lstat`` its subject to judge it cannot be the outermost one on
    Windows, because the touch IS the probe: ``lstat`` on a ``\\\\host\\share`` path reaches
    that host over SMB and carries an NTLM exchange before any check has run. So the purely
    local shape test -- read off the string, reaching nothing -- runs FIRST, and only a path
    that survives it earns a filesystem question. ``--out`` is author-supplied, the same class
    as the agent-spec and plan paths that already gate this way; every path this build touches
    (``out_dir``, its parent, the staging tree, the marker, the report) is derived from it, so
    the first ``_is_redirecting_entry`` or ``_refuse_unusable_parent`` below would otherwise be
    the probe. Guarded here rather than only at the CLI so the API surface is covered too.
    """
    if os.name != "nt":
        return
    try:
        from kiro_crew.hooks import is_unc_shape, unc_probe_allowed
    except ImportError as exc:
        raise ExportRefused(
            f"cannot judge whether --out {out_dir} names a UNC path, because "
            f"kiro_crew.hooks is not importable here ({exc}). Building there could reach a "
            f"host over SMB before any check runs, so it is refused rather than touched "
            f"unchecked. Point --out at a local directory."
        ) from exc
    _raw_out = str(out_dir)
    if is_unc_shape(_raw_out) and not unc_probe_allowed(_raw_out):
        raise ExportRefused(
            f"--out {out_dir} is a UNC path outside the trusted roots. Building there would "
            f"reach that host over SMB before this build could check anything about it, and a "
            f"Windows SMB touch carries an NTLM exchange. Point --out at a local directory."
        )


def _refuse_unusable_parent(path: Path, *, what: str) -> None:
    """Refuse before ``mkdir`` when a component of the destination cannot hold a directory.

    ``mkdir(parents=True)`` raises a bare ``NotADirectoryError`` (or ``FileExistsError``)
    when an existing component of the path is a FILE. That escapes as a traceback from a CLI
    whose every other refusal is an ``ExportRefused`` naming the flag at fault, so the
    operator gets a stack trace where they should get "point --out somewhere else".

    ``_is_redirecting_entry`` rather than ``is_dir()``: a junction reports as a directory on
    Windows, and creating directories through one writes wherever it names.
    """
    for ancestor in (path.parent, *path.parent.parents):
        if _pinned._is_redirecting_entry(ancestor):
            raise ExportRefused(
                f"cannot write {what}: {ancestor} on the way to {path} is a link or "
                f"junction, and creating directories through it would write outside the "
                f"path you named. Point --out at a plain directory."
            )
        if ancestor.exists():
            if not ancestor.is_dir():
                raise ExportRefused(
                    f"cannot write {what}: {ancestor} exists and is not a directory, so "
                    f"{path} cannot be created under it. Point --out elsewhere."
                )
            return


def _is_plain_file_no_follow(parent_fd: int, name: str) -> bool:
    """True only if *name* under *parent_fd* is a regular file, judged without following.

    ``os.lstat`` with ``dir_fd`` does not dereference a final symlink, so a symlink at the
    name reports as a link and returns False. This gates the ``exists_ok`` "already there"
    return: an ``O_EXCL`` open reports EEXIST for a symlink too, so the regular-file shape has
    to be re-established before that collision is treated as a benign re-run rather than a
    planted link. Any lstat error (the entry vanished in a race) is treated as not-a-plain-
    file, so the caller refuses rather than assuming.
    """
    try:
        st = os.lstat(name, dir_fd=parent_fd)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode)


def _write_bytes_nofollow(
    path: Path,
    data: bytes,
    *,
    mode: int = 0o600,
    exclusive: bool = False,
    exists_ok: bool = False,
    staging_fd: "int | None" = None,
    rel: "str | None" = None,
) -> bool:
    """Write *data* to *path* without following a link that is already there.

    Returns ``True`` when *data* was written and ``False`` only in the *exists_ok*
    exclusive case below, where a regular file was already claimed at *path*.

    Call sites all write to a path DERIVED from ``--out`` in a directory this build does not
    own -- the staging marker, the machine-readable report, and every staged bundle leaf. A
    plain ``write_bytes``/``write_text`` at any of them follows a link an adversary can
    pre-plant and truncates its target, which is the defect this closes. Writes RAW BYTES so a
    caller carrying a byte-exact signed artifact (the carried plan) gets it verbatim.

    What it does NOT do is decide ownership. The first version unlinked whatever was at the
    path, trading a symlink-follow for deleting an operator's file; the second refused any
    existing path, which broke rebuilding over the same ``--out`` -- the report from our own
    previous run legitimately sits there. Both were wrong in the same way: this function
    cannot tell whose file it is looking at, so it must not act on a guess.

    So the rule is narrow and about SHAPE. ``O_NOFOLLOW`` refuses a symlink, ``EISDIR``
    refuses a directory, and a regular file is truncated in place -- which is what pointing
    ``--out`` at an existing bundle already means. Nothing leaves the directory the operator
    named, which is the property that was actually missing.

    *exclusive* adds ``O_EXCL`` for a caller that has separately established the path should
    not exist yet. The staging marker uses it: a stranger's file there authorises a
    recursive delete, so that path needs more than shape, and its caller checks ownership
    before anything is created.

    *exists_ok* (only meaningful with *exclusive*) turns the ONE ambiguous case -- a regular
    file already at *path* -- from a refusal into a ``False`` return, while a symlink or a
    directory there is still refused. This is for a caller whose "already created" is a normal
    outcome, not a race lost: the plan command re-run on an already-planned crew. The check is
    still the atomic ``O_EXCL`` open, not a separate ``is_file()`` before it, so two runs
    racing on the same plan path cannot both believe they created it.

    Falls back to a plain write where ``dir_fd`` is unsupported, which is Windows.
    """
    if not _pinned._dir_fd_supported():
        # The shape refusals still apply here; only the mechanism differs. A directory at
        # this path reports IsADirectoryError on POSIX but PermissionError (EACCES) on
        # Windows, where opening a directory for writing is simply denied, so the shape is
        # judged BEFORE the write rather than translated out of whichever errno the platform
        # chose. Without this the Windows run raised a bare PermissionError and escaped the
        # module's contract to refuse cleanly.
        if _pinned._is_redirecting_entry(path):
            raise ExportRefused(
                f"{path} is a symlink. This build writes its own files there and will "
                f"not write through a link to somewhere else. Remove it, or point "
                f"--out elsewhere."
            )
        if path.is_dir():
            raise ExportRefused(
                f"{path} is a directory. This build needs that exact path for a file it "
                f"writes, and it will not delete a directory to get it. The path is "
                f"derived from --out; move it, or point --out elsewhere."
            )
        if exclusive and path.exists():
            if exists_ok and path.is_file() and not _pinned._is_redirecting_entry(path):
                # A regular file already claims the name. For a caller whose "already there"
                # is normal (the plan re-run), that is not a race lost -- report it as not
                # written. A symlink/dir was already refused above, so only a plain file
                # reaches here.
                return False
            raise ExportRefused(
                f"{path} already exists and this build did not write it. The path is "
                f"derived from --out, and building would replace it. Move it, or point "
                f"--out elsewhere."
            )
        # Spelled with an explicit call so this line is not textually identical to any other
        # write in the file. Two identical spellings made a source-substring mutation test land
        # on whichever came first in the file, which was this one -- a branch no POSIX run
        # takes, so the test passed while proving nothing.
        if not path.parent.is_dir():
            # The same refusal the descriptor branch gives, because the guard was added there
            # only and this branch reached the write with an absent parent -- raising a
            # bare FileNotFoundError on the one platform no local test runs. The Windows shard
            # caught it, which is the argument for having that shard.
            raise ExportRefused(
                f"cannot write {path.name}: its directory {path.parent} is not there, or is "
                f"not a directory this build can open. The path is derived from --out, so "
                f"point --out at a directory that exists."
            )
        path.write_bytes(data)
        return True
    flags = os.O_WRONLY | os.O_CREAT | _NOFOLLOW_READ_FLAGS
    flags |= os.O_EXCL if exclusive else os.O_TRUNC
    if staging_fd is not None and rel is not None:
        # The leaf lives under a directory this build CREATED and holds a descriptor for
        # (the staging root). Resolve it relative to that retained descriptor, walking each
        # sub-component ``O_NOFOLLOW``, so a swap of the staging root -- or any component
        # under it -- for another directory since the descriptor was opened cannot redirect
        # the write: the descriptor names the inode ``mkdir`` created, not whatever the path
        # string resolves to now. ``rel`` is the leaf's path relative to ``staging_fd``.
        parts = PurePosixPath(rel).parts
        parent_fd = os.dup(staging_fd)
        try:
            for comp in parts[:-1]:
                nxt = os.open(
                    comp,
                    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent_fd,
                )
                os.close(parent_fd)
                parent_fd = nxt
            leaf_name = parts[-1]
        except OSError as exc:
            os.close(parent_fd)
            raise ExportRefused(
                f"cannot write {rel} under the staging tree: a component changed to a link "
                f"or is not an openable directory since staging was created ({exc}). Nothing "
                f"was written. Re-run the build."
            ) from exc
        try:
            try:
                fd = os.open(leaf_name, flags, mode, dir_fd=parent_fd)
            except IsADirectoryError as exc:
                raise ExportRefused(
                    f"the staged path {rel} is a directory where this build writes a file; "
                    f"refusing rather than delete it. Re-run the build."
                ) from exc
            except FileExistsError as exc:
                if exists_ok and _is_plain_file_no_follow(parent_fd, leaf_name):
                    return False
                raise ExportRefused(
                    f"the staged path {rel} already exists under staging and this build did "
                    f"not write it. Re-run the build."
                ) from exc
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise ExportRefused(
                        f"the staged path {rel} is a symlink; this build writes its own file "
                        f"there and will not write through a link. Re-run the build."
                    ) from exc
                raise
            # Spelled ``fh.write(bytes(data))`` so this raw write is not a textual substring
            # of the by-name branch's ``fh.write(data)``, which a source-substring mutation
            # test anchors on and asserts is unique. Both write RAW BYTES with no translation.
            with os.fdopen(fd, "wb") as fh:
                fh.write(bytes(data))
        finally:
            os.close(parent_fd)
        return True
    try:
        parent_fd = _pinned._open_dir_nofollow_pinned(path.parent)
    except OSError as exc:
        # Refused, not raised. The write genuinely cannot proceed without a parent, but this
        # module's contract is to refuse with a message naming what an operator should do --
        # and every path here is derived from --out, so the operator can act on it. A redirect
        # at a parent component also arrives here (its own no-follow open fails), so a swapped
        # parent is refused rather than followed outside --out.
        raise ExportRefused(
            f"cannot write {path.name}: its directory {path.parent} is not there, is not a "
            f"directory this build can open, or a component of it changed to a link ({exc}). "
            f"The path is derived from --out, so point --out at a directory that exists."
        ) from exc
    try:
        try:
            fd = os.open(path.name, flags, mode, dir_fd=parent_fd)
        except IsADirectoryError as exc:
            raise ExportRefused(
                f"{path} is a directory. This build needs that exact path for a file it "
                f"writes, and it will not delete a directory to get it. The path is "
                f"derived from --out; move it, or point --out elsewhere."
            ) from exc
        except FileExistsError as exc:
            if exists_ok and _is_plain_file_no_follow(parent_fd, path.name):
                # O_EXCL reports EEXIST for ANY existing entry, a symlink included -- it
                # detects the entry before O_NOFOLLOW would fire. So the "already planned"
                # return is gated on an lstat proving a genuine regular file; a symlink or a
                # directory falls through to the refusals below rather than being swallowed.
                return False
            if _pinned._is_redirecting_entry(path):
                raise ExportRefused(
                    f"{path} is a symlink. This build writes its own files there and will "
                    f"not write through a link to somewhere else. Remove it, or point "
                    f"--out elsewhere."
                ) from exc
            raise ExportRefused(
                f"{path} already exists and this build did not write it. The path is "
                f"derived from --out, and building would replace it. Move it, or point "
                f"--out elsewhere."
            ) from exc
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise ExportRefused(
                    f"{path} is a symlink. This build writes its own files there and will "
                    f"not write through a link to somewhere else. Remove it, or point "
                    f"--out elsewhere."
                ) from exc
            # Any other write failure (ENOSPC, EACCES, EIO) is a genuine failure to WRITE, not
            # an ambiguous "unreadable read to interpret" -- so it is propagated deliberately.
            # Every caller is inside build_bundle's transaction, whose ``except BaseException``
            # rollback removes the staging tree and marker, so a propagated OSError aborts the
            # build cleanly rather than leaking. Converting it to ExportRefused here would only
            # relabel a real I/O failure; the honest report is the OSError.
            raise
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    finally:
        os.close(parent_fd)
    return True


def _write_nofollow(
    path: Path,
    text: str,
    *,
    mode: int = 0o600,
    exclusive: bool = False,
    exists_ok: bool = False,
    staging_fd: "int | None" = None,
    rel: "str | None" = None,
) -> bool:
    """Write *text* (UTF-8) to *path* without following a link that is already there.

    Thin wrapper over :func:`_write_bytes_nofollow`: the payload is encoded once, with
    ``newline=""`` semantics (no CRLF translation), so the shape refusals, the descriptor-
    relative no-follow open, and the byte-exact write all live in one place. See that function
    for the ownership rule, the *exists_ok* return, and why the write must not follow a
    planted link.
    """
    return _write_bytes_nofollow(
        path,
        text.encode("utf-8"),
        mode=mode,
        exclusive=exclusive,
        exists_ok=exists_ok,
        staging_fd=staging_fd,
        rel=rel,
    )
