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

Why this module exists rather than a check at each call site: two closed pull
requests (#2446, #2447) tried to add snapshot components while hardening staging in
the same change. Each review round named one more validated-by-name path use --
source root, each ancestor, each file, the destination tree, the pre-restore backup
pass -- and neither converged. The mechanism belongs in one place with one set of
invariants, and callers become thin consumers of it.

The mechanical half of this was first written for the benchmark harness in
``kiro_crew.eval.bench.safepath``, which now imports it from here. Its invariants
survived several rounds of review there and are preserved verbatim; the docstrings
explaining WHY each flag is load-bearing came with them.

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
import os
import shutil
import stat as _stat
from collections.abc import Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Callable

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
    "stat_at",
    "open_dir_pinned",
    "open_in_pinned_parent",
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
]


class PinnedPathRefusal(Exception):
    """Raised instead of completing an operation that could not be pinned.

    Neutral on purpose. A caller with its own refusal taxonomy passes that type as
    ``refusal=`` so its existing error contract is unchanged -- the benchmark
    harness keeps raising its ``UnsafePathError``, and a snapshot refusal stays
    something the CLI boundary already knows how to contain.
    """


#: Reason codes handed to an ``on_skip`` reporter. The primitive classifies; the
#: caller words the message, so user-facing output stays the caller's own.
SKIP_SYMLINK = "symlink"
SKIP_VANISHED = "vanished"
SKIP_NOT_REGULAR = "not_regular"

#: ``(reason_code, by_name_path)``. The path is for the message only -- it is never
#: re-opened, because re-opening it is the bug this module exists to prevent.
SkipReporter = Callable[[str, str], None]


def _noop_skip(_reason: str, _path: str) -> None:
    return None


def fatal_skip_reporter(what: str, *, refusal: type[Exception] = PinnedPathRefusal) -> SkipReporter:
    """A reporter that REFUSES instead of recording, for paths where a skip loses data.

    Skipping is the right answer while producing an archive: the entry is omitted, the
    omission is recorded, and nothing of the operator's is touched. It is the wrong answer
    on every path that has already moved or deleted the original -- there the skip means
    the live copy is gone AND the replacement was never written, so the operation
    "succeeds" having destroyed data.

    That distinction cost three separate review findings on this change (a backup pass
    whose skips preceded an ``rmtree``, a restore source skipped after the live file was
    moved aside, and a destination subtree that could not be opened). They were three
    instances of one rule, so the rule is now a parameter a caller passes rather than a
    condition each site re-implements: archive paths keep the recording reporter, mutating
    paths pass this one, and which kind a call site is becomes visible at the call site.
    """

    def _refuse(reason: str, path: str) -> None:
        raise refusal(
            f"refusing to continue the {what}: {Path(path).name!r} could not be copied "
            f"({reason}). This path has already moved or removed what it is replacing, so "
            "skipping would finish with the original gone and the replacement missing. "
            "Resolve that entry -- a hardlink alias or a symbolic link is the usual cause "
            "-- and re-run."
        )

    return _refuse


def supports_pinned_walk() -> bool:
    """Whether this platform can open relative to a directory descriptor.

    ``O_NOFOLLOW`` is part of the requirement, not an extra: a pinned walk without
    it would open each ancestor happily through whatever link sits there, which is
    the hole being closed. Found by the Windows-simulation tests, which delete
    ``os.O_NOFOLLOW`` and would otherwise have taken this path and crashed.
    """
    return (
        hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW") and os.open in os.supports_dir_fd
    )


def supports_pinned_tree_walk() -> bool:
    """Whether a whole TREE can be walked without ever re-opening a name.

    Strictly more than :func:`supports_pinned_walk`: descending a tree also needs to
    list and stat through a descriptor. Without ``os.listdir`` on an fd the walk would
    have to re-list by name, which reintroduces exactly the ancestor swap the pinned
    open just refused.

    Note which stat is probed. ``os.lstat`` is NOT a member of
    ``os.supports_dir_fd`` even on Linux -- the capability belongs to ``os.stat``, and
    ``lstat(p, dir_fd=fd)`` is documented as ``stat(p, dir_fd=fd,
    follow_symlinks=False)``. Probing ``os.lstat`` reports False on a platform that
    fully supports the walk, which would have made every snapshot on Linux refuse and
    demand the by-name opt-in. The walk below calls ``os.stat`` with
    ``follow_symlinks=False`` so the call and the probe are the same function.
    """
    return supports_pinned_walk() and os.listdir in os.supports_fd and os.stat in os.supports_dir_fd


def dir_flags() -> int:
    """Open flags every pinned directory walk uses: read-only, a directory, never a link.

    Public because a second module needs the same flags to walk a tree the same way, and
    reaching for a private was the beginning of the divergence this module exists to end.
    Called rather than captured at import: the Windows-simulation tests delete
    ``os.O_NOFOLLOW`` at runtime, and a frozen constant would keep offering a flag the
    platform no longer has.
    """
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def pin_parent(
    resolved_parent: str,
    *,
    what: str,
    refusal: type[Exception] = PinnedPathRefusal,
) -> int:
    """Return a descriptor for *resolved_parent*, refusing a component that is now a link.

    One ``openat`` per component, each relative to the previous component's
    descriptor and each carrying ``O_NOFOLLOW``. Two properties come out of that:

    * a component that became a symlink after *resolved_parent* was computed fails
      ``O_NOFOLLOW`` and is refused -- this is the check-to-use swap, and it is the
      reason a single ``os.open(parent, O_DIRECTORY)`` is not enough: that call
      follows such a link silently and then pins its target;
    * once a component is open, its descriptor cannot be re-pointed, so everything
      already traversed is fixed.

    *resolved_parent* must be resolved by the CALLER, once, before this runs.
    Resolving it here would re-follow whatever an ancestor points at by now, which
    is the exact mistake that made an earlier version of this defensible-looking and
    useless.

    The descriptor is returned OPEN and the caller must close it. Handing it back
    rather than doing one open inside is what lets a durable write create its
    temporary file and rename it over the destination through the same pinned
    directory, so the swap cannot be redirected between the two steps.

    Not closed: a component swapped BEFORE *resolved_parent* was computed is
    followed by that resolution. Refusing every symlinked ancestor would close it
    and would also break paths under ``/tmp`` on macOS, where ``/tmp`` is itself a
    link.
    """
    parts = PurePath(resolved_parent).parts
    if not parts:  # pragma: no cover - a resolved path always has parts
        raise refusal(f"refusing to open the {what}: empty parent path")

    if os.path.isabs(resolved_parent):
        dir_fd = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY)
        rest = parts[1:]
    else:  # pragma: no cover - realpath returns absolute paths
        dir_fd = os.open(".", os.O_RDONLY | os.O_DIRECTORY)
        rest = parts

    try:
        for component in rest:
            try:
                nxt = os.open(component, dir_flags(), dir_fd=dir_fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise refusal(
                        f"refusing to write the {what}: the directory {component!r} on "
                        "the way to it became a symbolic link after the path was "
                        "checked. A parent swapped for a link redirects the write "
                        "however carefully the final name is opened, so it is refused."
                    ) from exc
                raise
            os.close(dir_fd)
            dir_fd = nxt
    except BaseException:
        os.close(dir_fd)
        raise
    return dir_fd


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

    This is the one the preserved staging branches did not have, and its absence is
    the finding that closed #2446: ``os.open(str(src), O_DIRECTORY | O_NOFOLLOW)``
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
        # Same translation as `create_and_open_dir_pinned`. Review found this sibling
        # still leaking the raw errno: `O_NOFOLLOW` refuses the link correctly, but a
        # direct caller (the data home, a backup root, a restore destination) got an
        # `ELOOP`/`ENOTDIR` traceback instead of the one refusal type every other path on
        # this surface produces and the CLI boundary contains.
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise refusal(
                f"refusing to use the {what}: {as_given.name!r} is a symbolic link or "
                "not a directory, so working through it would follow whatever it points "
                "at. Remove it and re-run."
            ) from exc
        raise


def refuse_hardlink_alias(
    fd: int,
    *,
    what: str,
    name: str,
    refusal: type[Exception] = PinnedPathRefusal,
) -> None:
    """Reject a descriptor that is one of several names for the same inode.

    A hardlink is invisible to every path-based guard: it shares the target's inode,
    so ``realpath`` yields the alias's own name, ``is_symlink()`` is False, and
    ``O_NOFOLLOW`` has no link to refuse. A planted alias therefore let an O_TRUNC
    write destroy a protected file, and let a read hand back its bytes.

    Checked on the DESCRIPTOR rather than the path, which is what makes it
    race-free: this fd already refers to the inode being judged.

    The cost is honest and small: a file that legitimately has more than one link --
    a dedup-ing backup tool, a deliberate alias -- is refused. Copy it instead.

    Closes *fd* before raising, so a caller's ``except BaseException: os.close(fd)``
    must not run for this refusal.
    """
    links = os.fstat(fd).st_nlink
    if links > 1:
        os.close(fd)
        raise refusal(
            f"refusing to use the {what}: {name!r} has {links} hard links, so it is "
            "another name for a file this command was not pointed at. A path guard "
            "cannot see that -- the alias shares the target's inode -- so it is "
            "refused on the open descriptor instead. Remove the extra link or use a "
            "different path."
        )


def fd_real_path(fd: int) -> str | None:
    """Real filesystem path of an OPEN descriptor, or ``None`` when unreadable.

    The descriptor-pinned twin of ``realpath``: the name it returns is the
    kernel's own answer for the inode already held open, so it carries no
    symlink component left to swap -- which is what makes it usable as the
    containment witness after an ``O_NOFOLLOW`` open (validate the path the
    descriptor REALLY refers to, not the path it was opened by).

    Platform routes: ``/proc/self/fd`` on Linux, ``fcntl.F_GETPATH`` on macOS,
    ``GetFinalPathNameByHandleW`` on Windows. Every route FAILS CLOSED --
    ``None``, never a fallback to the mutable pathname -- including the whole
    Windows branch: a host where the handle's final path cannot be read gives
    callers nothing to validate, and each caller decides whether that refusal
    aborts (a containment check) or degrades (an advisory diagnostic).
    """
    if os.name == "nt":
        try:
            import ctypes
            import msvcrt

            win_dll = getattr(ctypes, "WinDLL", None)
            get_osfhandle = getattr(msvcrt, "get_osfhandle", None)
            if not callable(win_dll) or not callable(get_osfhandle):
                return None
            kernel32 = win_dll("kernel32", use_last_error=True)
            get_final_path = kernel32.GetFinalPathNameByHandleW
            get_final_path.argtypes = [
                ctypes.c_void_p,
                ctypes.c_wchar_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
            ]
            get_final_path.restype = ctypes.c_uint32
            buffer = ctypes.create_unicode_buffer(32768)
            length = get_final_path(
                ctypes.c_void_p(get_osfhandle(fd)),
                buffer,
                len(buffer),
                0,
            )
            if length == 0 or length >= len(buffer):
                return None
            path = buffer.value
            if path.startswith("\\\\?\\UNC\\"):
                return "\\\\" + path[8:]
            if path.startswith("\\\\?\\"):
                return path[4:]
            return path
        except (AttributeError, ImportError, OSError, ValueError):
            return None

    try:
        return os.readlink(f"/proc/self/fd/{fd}")  # Linux
    except OSError:
        pass
    try:
        import fcntl

        if hasattr(fcntl, "F_GETPATH"):  # macOS
            buf = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024))
            return buf.split(b"\x00", 1)[0].decode()
    except (OSError, ValueError, ImportError):
        pass
    return None


def is_reparse_point(path: str | Path) -> bool:
    """True for a symlink or a Windows junction.

    ``os.path.islink`` is False for a junction -- it is a reparse point but not a
    symlink -- so the tag is checked as well. Comparing ``realpath`` against
    ``abspath`` would be simpler and wrong: on Windows a temp directory is handed
    back as an 8.3 short path, which differs from its resolved form with nothing
    linked anywhere.
    """
    if os.path.islink(path):
        return True
    try:
        return bool(getattr(os.lstat(path), "st_reparse_tag", 0))
    except OSError:  # pragma: no cover - a component that vanished mid-walk
        return False


def copy_file_pinned(
    by_name: str,
    dst: str | None = None,
    *,
    dir_fd: int | None = None,
    name: str | None = None,
    dst_dir_fd: int | None = None,
    dst_name: str | None = None,
    skip_existing: bool = False,
    force_mode: int | None = None,
    on_skip: SkipReporter = _noop_skip,
) -> bool:
    """Copy one file's bytes from a descriptor pinned to a validated inode.

    Returns True when bytes were copied, False when the source was skipped.

    ``shutil.copy2`` cannot be used on a user-writable tree: it dereferences a
    hardlink into innocent-looking regular bytes, and a later tar-level hardlink
    screen never sees a link to reject -- so a hardlink to a credential planted
    inside an otherwise allowlisted directory would ride along as plain content.
    The order here is open first, judge the DESCRIPTOR second: ``O_NOFOLLOW`` where
    the platform has it, then ``fstat`` on the fd, so the inode that is validated is
    exactly the inode whose bytes are copied and no check-to-use window remains.
    Mode and timestamps are applied from that same ``fstat`` result rather than from
    a fresh by-name stat.

    BOTH ends can be pinned, and on a destination the caller does not own they MUST
    be. Pass *dir_fd* + *name* for a pinned source and *dst_dir_fd* + *dst_name* for
    a pinned destination; each side falls back to the by-name form when its pair is
    absent, which is only appropriate for a path this process just created. A
    destination reached by name is an ancestor swap away from landing the bytes
    somewhere else entirely -- that was a real gap in the first version of this
    module, caught in review, and it is why the by-name destination is now the
    exception rather than the only form.

    The destination is created with ``O_EXCL``, so anything already at that name is a
    planted link or alias rather than a file to overwrite and creation refuses it
    without a separate check. With *skip_existing* an occupied name is reported as
    skipped instead, which is what a merge that must not overwrite needs: exclusive
    creation makes "it did not exist" and "this call created it" one statement rather
    than two with a window between them.

    ``FileNotFoundError`` propagates so a caller can tolerate a source that vanished
    mid-walk; every other ``OSError`` propagates so real failures still abort.
    """
    if dst is None and dst_name is None:  # pragma: no cover - caller bug
        raise ValueError("copy_file_pinned needs either dst or dst_name")
    # O_NONBLOCK is not about performance. Opening a FIFO for reading BLOCKS until a
    # writer appears, so without it a single named pipe -- in an extracted archive, or
    # planted in a staged tree -- hangs the whole snapshot or restore forever with no
    # timeout and no message. Found when a mutation probe removed a caller's own
    # `is_file()` guard and the test run stalled until a watchdog killed it, which is
    # exactly how an operator would experience it. The fstat below still rejects the FIFO;
    # this only guarantees we reach that check. On a regular file the flag has no effect.
    src_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        if dir_fd is not None and name is not None:
            fd = os.open(name, src_flags, dir_fd=dir_fd)
        else:
            fd = os.open(by_name, src_flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            # A symlink final component that appeared after the listing-time link
            # screen -- refuse it the same way the screen would have.
            on_skip(SKIP_SYMLINK, by_name)
            return False
        raise
    try:
        st = os.fstat(fd)
        if not _stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            on_skip(SKIP_NOT_REGULAR, by_name)
            return False
        # The bytes are written to the FINAL name, opened O_CREAT|O_EXCL, and no name is
        # resolved again afterwards. Three designs have now been tried on these lines and
        # this is the only one that satisfies this module's own central rule -- once a
        # descriptor exists, no write may be derived from a path name:
        #
        #   * exclusive create at the final name, cleanup unlinks that name after an
        #     identity check -- rejected because the name can change between the check and
        #     the unlink (POSIX has no unlink-by-inode), so cleanup could delete a file
        #     another writer had published;
        #   * private temporary published with `os.link` -- rejected because the LINK
        #     re-resolves the temporary BY NAME. Proven exploitable: swapping the `.part`
        #     entry after the copy makes the publish install attacker bytes and report the
        #     core file as restored. A descriptor-based publish would fix it, and there is
        #     no portable one -- `linkat(AT_EMPTY_PATH)` is not exposed by Python and the
        #     `/proc/self/fd` form is Linux-only and privileged;
        #   * this one: the descriptor IS the destination from the first byte. There is no
        #     publish step to attack, and no hard link, which is also why the FAT/exFAT
        #     fallback and its overwrite hazard are simply gone rather than guarded.
        #
        # What the first design got wrong was the CLEANUP, not the create, and the fix is
        # to stop unlinking names: on failure the file is emptied through the descriptor we
        # hold and the failure is reported. `O_EXCL` proves we created this entry, so
        # truncating it cannot touch anyone else's file, and a reported empty file with the
        # previous version still in the backup is a far smaller harm than either publishing
        # attacker bytes or deleting a concurrent writer's file.
        #
        # EEXIST keeps its two answers: with `skip_existing` an occupied name is a skip,
        # otherwise it is raised for the caller to translate.
        dst_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        published = False
        try:
            if dst_dir_fd is not None and dst_name is not None:
                dst_fd = os.open(dst_name, dst_flags, 0o600, dir_fd=dst_dir_fd)
            else:
                dst_fd = os.open(str(dst), dst_flags, 0o600)
        except FileExistsError:
            if skip_existing:
                return False
            raise
        # The destination is finished through its OWN descriptor and never by name again:
        # `os.chmod(name, dir_fd=...)` re-resolves the final component, so a name swapped
        # between the write and the chmod would have the mode applied to the replacement,
        # while `fchmod`/`futimes` on the open descriptor cannot be redirected.
        #
        # On failure only the TEMPORARY is removed. That is the whole point of writing to
        # one: the final name is never touched by the failure path, so no cleanup of ours
        # can delete a file another writer published there. The descriptor is closed before
        # the unlink because Windows refuses to remove a file with an open handle -- the
        # earlier form unlinked first and left the fragment exactly where cleanup was meant
        # to remove it, which my own Windows shard caught.
        try:
            with os.fdopen(fd, "rb") as fsrc:
                fd = -1  # ownership passed to the file object
                # fdopen takes ownership and closes what it is given, so it gets a
                # duplicate: dst_fd itself has to outlive the write for the two
                # descriptor-based metadata calls below.
                with os.fdopen(os.dup(dst_fd), "wb") as fdst:
                    shutil.copyfileobj(fsrc, fdst)
            _apply_metadata(
                dst_fd,
                st,
                dst=dst,
                dst_dir_fd=dst_dir_fd,
                dst_name=dst_name,
                mode=force_mode,
            )
            published = True
        except BaseException:
            # No name is unlinked here. `O_EXCL` above proves this entry is ours, so the
            # partial content is emptied through the descriptor -- the one operation that
            # cannot be redirected at another writer's file. The caller's reporter is what
            # makes it visible; on a restore that reporter is fatal, so the operation stops
            # with the previous version still in the backup rather than continuing over a
            # truncated core file.
            if dst_fd >= 0:
                try:
                    os.ftruncate(dst_fd, 0)
                except OSError:
                    pass  # nothing further is safe to try, and the report still fires
                os.close(dst_fd)
                dst_fd = -1
            raise
        finally:
            if dst_fd >= 0:
                os.close(dst_fd)
        return published
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


def stat_at(dir_fd: int, name: str) -> os.stat_result | None:
    """`lstat` *name* relative to *dir_fd*, or ``None`` if it is not there.

    The descriptor-relative answer to "what is this, and is it there?". A plain
    ``Path.is_file()`` re-resolves the whole path, so between the question and the use of
    the answer the object can be replaced -- and a guard that inspects the replacement
    while the code acts on the original is worse than no guard, because it reports success.
    Review found three such guards in code that was already holding the right descriptor.

    Never follows a link: the caller wants to know what the NAME is, and a link is one of
    the answers it needs to be able to see.
    """
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except (FileNotFoundError, NotADirectoryError):
        return None


def is_regular_at(dir_fd: int, name: str) -> bool:
    """True when *name* under *dir_fd* is a plain file -- not a link, FIFO, or directory."""
    st = stat_at(dir_fd, name)
    return st is not None and _stat.S_ISREG(st.st_mode)


def _apply_metadata(
    dst_fd: int,
    st: os.stat_result,
    *,
    dst: str | Path | None,
    dst_dir_fd: int | None,
    dst_name: str | None,
    mode: int | None = None,
) -> None:
    """Copy mode and timestamps onto the destination, by descriptor where possible.

    The fallbacks name the DESTINATION, which is now the only thing they could name: the
    bytes are written straight to the final name under ``O_EXCL``. An earlier revision wrote
    to a private temporary and published it by link, so the fallbacks had to name that
    temporary instead -- that design is gone (the publish re-resolved a name, which is the
    one thing this module exists to avoid), and with it the parameter that carried the
    distinction.

    ``os.fchmod`` does not exist on Windows and ``os.utime`` only accepts a descriptor
    where ``os.utime in os.supports_fd``, so the fd form cannot be unconditional: an
    earlier revision called it always and crashed a Windows snapshot with
    ``AttributeError`` the moment it reached a core file. Caught in review.

    The fd form is preferred wherever it exists, because a name re-resolves: a final
    component swapped between the write and the chmod would have the mode applied to the
    replacement. Where it does not exist the by-name form is the only option available,
    and it is the same platform that already cannot pin a directory at all -- so this
    adds no exposure that the declared by-name traversal does not already carry.

    *mode* overrides the source's mode. Used for a restored security file, which must end
    up owner-only regardless of what the archive recorded.
    """
    want = _stat.S_IMODE(st.st_mode) if mode is None else mode
    fallback_name = dst_name
    fallback_path = str(dst) if dst else None
    if hasattr(os, "fchmod"):
        os.fchmod(dst_fd, want)
    elif dst_dir_fd is not None and fallback_name is not None:  # pragma: no cover - Windows
        os.chmod(fallback_name, want, dir_fd=dst_dir_fd)
    elif fallback_path is not None:  # pragma: no cover - Windows
        os.chmod(fallback_path, want)

    times = (st.st_atime_ns, st.st_mtime_ns)
    if os.utime in os.supports_fd:
        os.utime(dst_fd, ns=times)
    elif dst_dir_fd is not None and fallback_name is not None:  # pragma: no cover - Windows
        os.utime(fallback_name, ns=times, dir_fd=dst_dir_fd)
    elif fallback_path is not None:  # pragma: no cover - Windows
        os.utime(fallback_path, ns=times)


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

    This used to also report whether our own `mkdir` was what created the directory, so
    that the archive's mode and timestamps could be stamped on in that case only. That
    flag is gone, and the reasoning that justified it was wrong in an instructive way.

    It was correct that sampling `dst.exists()` beforehand is a name-based check with a
    window after it. What it missed is that `mkdir` succeeding does not close the window
    either: the descriptor comes from a SEPARATE `open`, so a directory replaced between
    the two leaves the flag true while describing an object the caller no longer holds --
    and the archive's metadata then lands on somebody else's directory. Review found
    that. Nothing repairs it: a stat taken after the `mkdir` observes the replacement
    just as happily, POSIX has no atomic create-and-open for a directory, and under a
    same-user threat model no mode trick helps. So the metadata is simply not applied to
    directories, and the flag has no remaining purpose.

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
    """
    as_given = Path(path)
    parent_fd = pin_parent(
        os.path.realpath(as_given.parent or Path(".")), what=what, refusal=refusal
    )
    try:
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
        try:
            return os.open(as_given.name, dir_flags(), dir_fd=parent_fd)
        except OSError as exc:
            # A link (or a plain file) at the destination's own name. O_NOFOLLOW already
            # refuses it -- the gap review found was that it escaped as a raw OSError, so
            # a restore ended in a traceback instead of the refusal every other path on
            # this surface produces. Translated here so callers have one type to contain.
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise refusal(
                    f"refusing to use the {what}: {as_given.name!r} is a symbolic link "
                    "or not a directory, so creating the tree there would write through "
                    "whatever it points at. Remove it and re-run."
                ) from exc
            raise
    finally:
        os.close(parent_fd)


def stage_tree_pinned(
    src: str | Path,
    dst: str | Path,
    *,
    what: str,
    ignore: Callable[[str, list[str]], set[str]] | None = None,
    on_skip: SkipReporter = _noop_skip,
    skip_existing: bool = False,
    must_create: bool = False,
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
        names = os.listdir(src_fd)
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
            if _stat.S_ISLNK(st.st_mode):
                on_skip(SKIP_SYMLINK, path)
            elif _stat.S_ISDIR(st.st_mode):
                child_src = _open_child_dir(src_fd, entry, path, on_skip)
                if child_src is None:
                    continue
                try:
                    try:
                        os.mkdir(entry, 0o700, dir_fd=dst_fd)
                    except FileExistsError:
                        # Only a merge legitimately meets an existing destination
                        # directory. Anywhere else the destination tree is one this
                        # process just created, so a name already occupying it is a
                        # planted link or file -- and swallowing that made the pinned
                        # open below refuse the subtree and the whole restore report
                        # success with the archive's subtree missing. Raised in
                        # review, and the same silent-partial shape this change fixes
                        # elsewhere, so it is now a refusal rather than a skip.
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
                        # with that subtree missing. Raised in review. A merge is the
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
                        # The archive's directory mode and mtime are NOT applied. They used
                        # to be, gated on `created` from the `mkdir`, and review showed the
                        # gate cannot be trusted: the descriptor comes from a separate
                        # `open`, so a directory replaced between the two leaves
                        # `created=True` describing an object we no longer hold, and the
                        # archive's metadata then lands on somebody else's directory.
                        #
                        # There is no sound repair. An identity check cannot help -- a
                        # stat taken after the mkdir observes the REPLACEMENT just as
                        # happily as our own directory -- and POSIX has no atomic
                        # create-and-open for a directory to make the flag mean what it
                        # says. Under a same-user threat model no mode trick closes it
                        # either.
                        #
                        # So the fidelity is given up on purpose: a restored tree keeps its
                        # default directory mode and a current mtime. That partly gives
                        # back a fidelity fix made earlier in this change, and it is the
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
                        on_skip=on_skip,
                    )
                except FileNotFoundError:
                    on_skip(SKIP_VANISHED, path)
                except FileExistsError as exc:
                    # The destination name was taken between this walk starting and the
                    # publish. Without `skip_existing` that is not a merge -- the caller
                    # believes the tree it is writing into is free -- so it is a real
                    # condition, but it escaped as a bare traceback out of a restore.
                    # Review found it. Translated to this module's refusal type, the same
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
        # refusal type is caught alongside the raw errno because `open_dir_pinned` now
        # translates ELOOP/ENOTDIR (review asked for that so DIRECT callers stop getting
        # tracebacks); this call site is the one place that wants the softer outcome.
        if isinstance(exc, OSError) and exc.errno not in (
            errno.ELOOP,
            errno.ENOTDIR,
            errno.ENOENT,
        ):
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
        # `not dst.exists()` was a check with a window after it: a forced restore removes
        # the workspace, the gateway recreates it before the mkdir, and the live directory
        # is then stamped with the archive's metadata. Review caught it, and it is the
        # third instance of this change's first invariant -- no write, and no decision
        # governing a write, may be made through a path name.
        root_dst = create_and_open_dir_pinned(
            dst,
            what=f"{what} destination",
            # Stated by the caller, NOT derived. Deriving it from `skip_existing` was the
            # obvious guess and it is wrong: the snapshot's own staging destination already
            # exists when the walk starts, and it has no `skip_existing` either, so that
            # derivation refused every snapshot. The pre-existing snapshot suite caught it.
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


def _open_child_dir(parent_fd: int, entry: str, by_name: str, on_skip: SkipReporter) -> int | None:
    """Open a child directory through *parent_fd*, or report why it was skipped.

    Returns ``None`` for the two races worth tolerating -- the entry vanished, or it
    stopped being a plain directory between the stat and this open. ``ELOOP`` and
    ``ENOTDIR`` are exactly the swap the pinned open exists to refuse, so they are
    reported as a skipped symlink rather than raised: the listing-time screen would
    have said the same thing a moment earlier. Every other error propagates.

    Extracted because the source and destination sides need identical handling, and
    the version of this code that had it on one side only let the swap the source
    skipped escape the destination as a raw ``OSError``.
    """
    try:
        return os.open(entry, dir_flags(), dir_fd=parent_fd)
    except FileNotFoundError:
        on_skip(SKIP_VANISHED, by_name)
        return None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            on_skip(SKIP_SYMLINK, by_name)
            return None
        raise


# ---------------------------------------------------------------------------
# Verified removal
#
# Everything below owns ONE mechanism: removing something reached through a pinned
# descriptor, where the removal itself must not address a name that could have been
# swapped since it was checked. It lives here rather than at its call sites because it
# was previously spelled once per site -- the interior directories of a trash batch, the
# batch directory, and the coarse fallback -- and a fourth consumer is what finally shows
# which parts are consumer-agnostic. That per-site respelling is the failure this module's
# own docstring names, from #2446 and #2447.
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


def unlink_verified(holder_fd: int, name: str, expect: tuple[int, int]) -> bool:
    """Unlink *name* under *holder_fd*, only if it is still ``(st_dev, st_ino)`` *expect*.

    The residual is irreducible and better stated than implied: POSIX has no
    unlink-by-inode, so the stat and the unlink are two syscalls addressing the same NAME.
    What CAN be done is refuse when the name no longer holds what the scan saw, which is
    what turns "delete whatever answers to this name" into "delete this object, or nothing".
    The remaining window needs a swap landing between two adjacent syscalls, and the
    directory holding the name was itself reached only through verified descriptors.
    """
    try:
        info = os.stat(name, dir_fd=holder_fd, follow_symlinks=False)
    except OSError:
        return False
    if (info.st_dev, info.st_ino) != expect:
        return False
    try:
        os.unlink(name, dir_fd=holder_fd)
    except OSError:
        return False
    return True


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
    ``O_NOFOLLOW`` and its inode checked against *expect_ino* before anything is read from
    it, and the copy reads that DESCRIPTOR rather than re-opening the name. Without that, a
    name swapped for a symbolic link is followed and whatever it points at -- a credential,
    say -- is linked or copied into the destination under a name the caller will treat as its
    own.

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
    try:
        src = os.open(src_name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=src_parent_fd)
    except OSError:
        return PUT_BACK_FAILED
    try:
        if os.fstat(src).st_ino != expect_ino:
            # The name no longer holds what was moved aside, so there is nothing here this
            # function may put anywhere. Refusing leaves the caller to report the name.
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
