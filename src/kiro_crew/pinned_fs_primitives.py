"""The pieces every pinned operation is built from.

The neutral refusal type and the skip reasons a walk reports, the platform probes
(:func:`supports_pinned_walk`, :func:`supports_pinned_tree_walk`), the directory open
flags, pinning a resolved parent (:func:`pin_parent`), the hardlink-alias refusal, a
descriptor's real path, and the reparse-point test. Every name is reached as
``kiro_crew.pinned_fs.<name>``; the facade binds or serves it.
"""

from __future__ import annotations

import errno
import os
import stat as _stat
from pathlib import Path, PurePath
from typing import Callable

from kiro_crew.platform_compat import open_file_no_reparse


class PinnedPathRefusal(Exception):
    """Raised instead of completing an operation that could not be pinned.

    Neutral on purpose. A caller with its own refusal taxonomy passes that type as
    ``refusal=`` so its existing error contract is unchanged -- the benchmark
    harness keeps raising its ``UnsafePathError``, and a snapshot refusal stays
    something the CLI boundary already knows how to contain.
    """


# Named by the path callers import it from: a traceback or a log line names
# ``kiro_crew.pinned_fs.PinnedPathRefusal``, not this module.
PinnedPathRefusal.__module__ = "kiro_crew.pinned_fs"


#: Reason codes handed to an ``on_skip`` reporter. The primitive classifies; the
#: caller words the message, so user-facing output stays the caller's own.
SKIP_SYMLINK = "symlink"
SKIP_VANISHED = "vanished"
SKIP_NOT_REGULAR = "not_regular"
SKIP_TOO_LARGE = "too_large"
SKIP_IDENTITY_CHANGED = "identity_changed"
#: The entry is still there and this process may not read its metadata or its bytes
#: -- a platform-protected path, or one an ACL denies. Distinct from SKIP_VANISHED,
#: which says the name is gone: a caller that must not omit anything silently has to
#: tell those apart. Spelled for the ENTRY rather than reusing the neighbouring
#: module's SKIP_UNREADABLE, whose value is a different string for a different
#: subject (an unreadable trash BATCH).
SKIP_UNREADABLE_ENTRY = "unreadable_entry"

#: The reason codes that mean "carrying this was never the intent". A symlink and a
#: non-regular entry are screened on EVERY run, by design, and an archive that screened
#: one is complete -- there is nothing the operator asked for that it lacks. These two
#: codes are therefore reserved for a FIRST look: an entry that was regular when the
#: walk judged it and is a link or a FIFO by the time it is opened has been swapped,
#: and every site that can observe that reports ``SKIP_IDENTITY_CHANGED`` instead.
#: Private: the one question a caller may ask is :func:`omits_wanted_data`, so no
#: caller can build its own partial enumeration from these.
_NEVER_ARCHIVED = frozenset({SKIP_SYMLINK, SKIP_NOT_REGULAR})

#: The reason codes that mean the archive lacks something it WAS asked to carry: the
#: bytes were refused, truncated, swapped for another inode mid-copy, or the name went
#: away between the listing and the copy. Listed so a test can assert the two sets
#: together cover every ``SKIP_*`` code in this module.
_OMITS_WANTED_DATA = frozenset(
    {SKIP_UNREADABLE_ENTRY, SKIP_TOO_LARGE, SKIP_VANISHED, SKIP_IDENTITY_CHANGED}
)

#: Times :func:`create_and_open_dir_pinned` re-runs its ``mkdir``-then-``open`` pair
#: when the directory is removed between the two. There is no atomic "create this
#: directory and open it", so a removal lands INSIDE the sequence and no ordering of
#: the two calls closes that window -- the answer is to run the sequence again. A
#: small count is the right bound because an attempt is two syscalls with NO sleep
#: between them: this is a lost race to redo at once, not a resource to wait on.
#: Three, so a single interleaving costs one retry, a second one is still absorbed,
#: and a caller that keeps losing reports rather than spinning: past that point the
#: removals are not a race being lost but something removing the directory
#: repeatedly, which no number of attempts fixes and an operator has to see.
#:
#: :mod:`kiro_crew.platform_log_append` holds a private bound of the same name and
#: the same value for its own create-then-open pair, and the two stay separate
#: deliberately. They govern different sequences with different costs: that one
#: re-runs a directory create plus a FILE open for best-effort observation, where
#: losing the race costs one log row, and this one re-runs a directory create plus
#: the open OF that directory for a caller whose data must land. Sharing one
#: constant would mean tuning either operation silently retunes the other, and the
#: equal value today is two similar judgements rather than one decision.
_CREATE_ATTEMPTS = 3


def omits_wanted_data(reason: str) -> bool:
    """Whether *reason* means the archive lacks something it was asked to carry.

    The single question a retention or completeness decision needs to ask, answered
    HERE rather than at each consumer. A consumer that names one code has to be found
    and edited every time a code is added, and until someone does it silently keeps
    the old answer: a guard that names ``SKIP_UNREADABLE_ENTRY`` lets
    ``SKIP_TOO_LARGE``, ``SKIP_VANISHED`` and ``SKIP_IDENTITY_CHANGED`` past it into a
    prune that deletes the last complete backup.

    An UNKNOWN reason answers True, and that direction is the point. This is asked by
    code deciding whether to DELETE an older archive, so the two wrong answers are not
    symmetric: keeping a surplus bundle costs disk, and pruning on a reason nobody
    classified costs the operator's last complete backup. A new code is therefore
    incomplete-by-default the moment it is reported, before anyone has thought about
    it, and the completeness test is what tells the author to choose a side.
    """
    return reason not in _NEVER_ARCHIVED


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

    The distinction is easy to lose one site at a time -- a backup pass whose skips
    precede an ``rmtree``, a restore source skipped after the live file was moved aside,
    a destination subtree that cannot be opened -- so the rule is a parameter a caller
    passes rather than a condition each site re-implements: archive paths keep the
    recording reporter, mutating paths pass this one, and which kind a call site is
    becomes visible at the call site.
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
    demand the by-name opt-in. The tree walk, :func:`stage_tree_pinned`, calls ``os.stat`` with
    ``follow_symlinks=False`` so the call and the probe are the same function.
    """
    return supports_pinned_walk() and os.listdir in os.supports_fd and os.stat in os.supports_dir_fd


def dir_flags() -> int:
    """Open flags every pinned directory walk uses: read-only, a directory, never a link.

    Public because a second module needs the same flags to walk a tree the same way, and
    reaching for a private was the beginning of the divergence this module exists to end.
    Called rather than captured at import: the Windows-simulation tests delete
    ``os.O_NOFOLLOW`` at runtime, and a frozen constant would keep offering a flag the
    platform then lacks.
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
    is the exact mistake that makes this check defensible-looking and useless.

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
                        "the way to it is not usable: it either became a symbolic "
                        "link after the path was checked, was one all along because "
                        "this path was handed in unresolved - which the walk cannot "
                        "tell apart - or is not a directory at all. Each of those "
                        "redirects or blocks the write however carefully the final "
                        "name is opened, so it is refused."
                    ) from exc
                raise
            os.close(dir_fd)
            dir_fd = nxt
    except BaseException:
        os.close(dir_fd)
        raise
    return dir_fd


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


# The one implementation every consumer binds, named by its import path:
# ``kiro_crew.pinned_fs.fd_real_path``.
fd_real_path.__module__ = "kiro_crew.pinned_fs"


def open_fenced_for_read(
    resolved: Path | str,
    *,
    fence: Callable[[str], bool],
    refusal: type[Exception] = OSError,
) -> int:
    """Open *resolved* for reading and return a descriptor validated as an inode.

    *resolved* is a path the caller has already canonicalised and judged with
    *fence* (``True`` means refuse). A by-name open after that judgement is a
    check-to-open window: the artifact directory and the agents directories are
    agent-writable, so the name can be re-pointed at a credential file between
    the two. The open refuses a link at the final component on every platform
    (:func:`kiro_crew.platform_compat.open_file_no_reparse`), the descriptor
    must be a regular file with a single link (a hardlink to a credential file
    has a benign ``realpath``, so the link count is the only tell), and the
    kernel's own path for the opened inode is read back with
    :func:`fd_real_path`. *fence* is asked again exactly when that path differs
    from *resolved*: a matching path is the question the caller already
    answered, and on the event loop every extra call is a resolver-pool
    submission. A missing kernel path fails closed.

    The caller owns the returned descriptor. Every refusal closes it first and
    raises *refusal*; a missing file surfaces as the ordinary
    ``FileNotFoundError`` from the open.
    """
    resolved_str = os.fspath(resolved)
    try:
        fd = open_file_no_reparse(resolved_str, nonblocking=True)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise refusal(f"refusing to read through a link: {resolved_str}") from exc
        raise

    try:
        opened = os.fstat(fd)
        if not _stat.S_ISREG(opened.st_mode):
            raise refusal(f"refusing to read a non-regular file: {resolved_str}")
        if opened.st_nlink != 1:
            raise refusal(f"refusing to read a hardlinked file: {resolved_str}")
        fd_real = fd_real_path(fd)
        if fd_real is None:
            raise refusal(f"refusing to read an unverifiable file: {resolved_str}")
        if os.path.normcase(fd_real) != os.path.normcase(resolved_str):
            if fence(fd_real):
                raise refusal(f"refusing to read sensitive path: {fd_real}")
    except BaseException:
        os.close(fd)
        raise
    return fd


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
