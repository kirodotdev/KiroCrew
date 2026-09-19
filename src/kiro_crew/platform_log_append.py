"""Confined, serialized JSONL append for the decision log.

The configured home is the trusted anchor (resolved once). Its log directory
and file must not be links. POSIX uses pinned directory-relative opens. Windows
holds the anchor and the log directory open with list access and read-only
sharing until the write ends, so a data-write or delete open of either (the
handle a junction swap needs) is a sharing violation while we hold them, and then
checks the leaf handle's final path against the pinned directory, so a swap that
raced the pins is refused rather than written through.
Creating the log directory and opening the file inside it cannot be one syscall,
so that pair is re-attempted when the directory is removed between them, and an
exhausted retry reports the whole path rather than the bare ``openat`` leaf.
No worker is spawned here: lock contention and write retries share a deadline.
"""

from __future__ import annotations

import ctypes
import errno
import os
import stat
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from ctypes import wintypes
from pathlib import Path

from kiro_crew import pinned_fs, platform_compat

#: Observation must not occupy a caller indefinitely on lock contention/retries.
_APPEND_TIMEOUT_SECONDS = 0.5
#: Times the create-and-pin of the log directory plus the leaf open is re-attempted
#: when that directory is removed between them. There is no atomic "create this
#: directory and open a file in it", so the removal lands INSIDE the sequence and no
#: ordering closes it -- the answer is to run the sequence again, and a small count
#: is the right bound because each attempt is a handful of syscalls with NO sleep
#: between them: this is a lost race to redo at once, not a resource to wait on.
#: Three, so a single interleaving costs one retry and a caller that keeps losing
#: still reports rather than spinning.
_CREATE_ATTEMPTS = 3
_DIR_MODE = 0o700
_FILE_MODE = 0o600

# CreateFile rights/dispositions. A directory pin asks for LIST_DIRECTORY, not
# just attributes: only data/delete access takes part in Windows sharing, so an
# attributes-only handle would pin nothing. With read-only sharing, a later
# write or delete open of the pinned directory fails with ERROR_SHARING_VIOLATION.
_WIN_LIST_DIRECTORY = 0x1
_WIN_GENERIC_READ_WRITE = 0xC0000000
_WIN_SHARE_READ = 0x1
_WIN_SHARE_READ_WRITE = 0x3
_WIN_OPEN_EXISTING = 3
_WIN_OPEN_ALWAYS = 4
_WIN_BACKUP_SEMANTICS = 0x02000000
_WIN_OPEN_REPARSE_POINT = 0x00200000
_WIN_REPARSE_ATTRIBUTE = 0x400


def _win_open(path: Path, *, directory: bool) -> int:  # pragma: no cover - Windows
    """Open the object itself, transferring the native handle only on success."""
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(
        str(path),
        _WIN_LIST_DIRECTORY if directory else _WIN_GENERIC_READ_WRITE,
        _WIN_SHARE_READ if directory else _WIN_SHARE_READ_WRITE,
        None,
        _WIN_OPEN_EXISTING if directory else _WIN_OPEN_ALWAYS,
        _WIN_BACKUP_SEMANTICS | _WIN_OPEN_REPARSE_POINT,
        None,
    )
    if handle is None or handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
    try:
        fd = msvcrt.open_osfhandle(  # type: ignore[attr-defined]
            handle, (os.O_RDONLY if directory else os.O_RDWR) | getattr(os, "O_BINARY", 0)
        )
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    try:
        info = os.fstat(fd)
        if info.st_file_attributes & _WIN_REPARSE_ATTRIBUTE:  # type: ignore[attr-defined]
            raise OSError(errno.ELOOP, "log path is a reparse point")
        if directory and not stat.S_ISDIR(info.st_mode):
            raise NotADirectoryError("log ancestor is not a directory")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _pin_log_dir(
    directory: Path, stack: ExitStack, *, create: bool, anchor: Path | None = None
) -> int | None:
    """Pin the log directory under its resolved anchor; return a POSIX dir fd or None.

    Only the immediate log directory may be created. The configured home must
    exist; resolving that anchor allows intentional home aliases and macOS /tmp.
    The log directory itself is never resolved before its no-link open. On POSIX
    the returned descriptor is what every later name is opened relative to; on
    Windows the directory handle is held open with read-only sharing (so a swap
    is a sharing violation while it is held) and callers work by path.

    *anchor* is that resolved anchor when the CALLER has already computed it, and
    it is not an optimisation. :func:`_open_log` re-attempts this function when the
    log directory is removed under it, and every attempt must target the anchor
    that was resolved ONCE before the first one: re-resolving per attempt lets an
    actor who swaps the anchor's name for a link between two attempts have the
    retry create the log directory -- and write the row -- inside whatever that
    link points at. Passing the resolution in is what makes a retry unable to
    redirect the write; with it, an anchor component that became a link is refused
    by ``pin_parent``'s ``O_NOFOLLOW`` walk instead.
    """
    if anchor is None:
        anchor = directory.parent.resolve(strict=True)
    pinned = anchor / directory.name
    if platform_compat.IS_POSIX:
        anchor_fd = pinned_fs.pin_parent(str(anchor), what="decision log")
        stack.callback(os.close, anchor_fd)
        if create:
            try:
                os.mkdir(pinned.name, _DIR_MODE, dir_fd=anchor_fd)
            except FileExistsError:
                pass
        dir_fd = os.open(pinned.name, pinned_fs.dir_flags(), dir_fd=anchor_fd)
        stack.callback(os.close, dir_fd)
        return dir_fd
    # pragma: no cover - Windows; exercised by the Windows test lane
    pin = _win_open(anchor, directory=True)
    stack.callback(os.close, pin)
    if create:
        try:
            pinned.mkdir(mode=_DIR_MODE)
        except FileExistsError:
            pass
    pin = _win_open(pinned, directory=True)
    stack.callback(os.close, pin)
    return None


@contextmanager
def pinned_log_dir(directory: Path) -> Iterator[int | None]:
    """Hold *directory* pinned for a scan: a no-follow dir fd on POSIX, ``None`` on Windows.

    For the retention sweep: names are listed and unlinked relative to this
    descriptor, so a directory link swapped in under the log directory's name
    cannot redirect the deletion into another tree. Raises if the directory does
    not exist; the sweep has nothing to do then.
    """
    with ExitStack() as stack:
        yield _pin_log_dir(directory, stack, create=False)


def _pin_and_open_leaf(path: Path, anchor: Path, stack: ExitStack) -> int:
    """ONE attempt at create-and-pin the log directory under *anchor*, then open the leaf.

    Every descriptor it opens is registered on *stack*, which is what lets
    :func:`_create_and_open` throw a losing attempt away without leaking one: a
    leaked directory descriptor pins its inode for the life of the process.
    """
    directory = path.parent
    parent_fd = _pin_log_dir(directory, stack, create=True, anchor=anchor)
    if parent_fd is not None:
        flags = os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK
        # Darwin can return ENOENT when nonexclusive O_CREAT loses a race.
        # Create exclusively, then open the winner under the same pin. If the
        # winner's leaf is gone again by then, that ENOENT reaches
        # :func:`_create_and_open` like any other lost interleaving and the
        # bounded retry creates a fresh log the same exclusive, pinned way.
        try:
            fd = os.open(path.name, flags | os.O_CREAT | os.O_EXCL, _FILE_MODE, dir_fd=parent_fd)
        except FileExistsError:
            fd = os.open(path.name, flags, dir_fd=parent_fd)
        stack.callback(os.close, fd)
        return fd
    # pragma: no cover - Windows; exercised by the Windows test lane
    pinned = anchor / directory.name
    fd = _win_open(pinned / path.name, directory=False)
    stack.callback(os.close, fd)
    # The pins make a directory swap a sharing violation while they are
    # held; this makes one that landed before them a refusal. The leaf
    # was opened by path, so ask the kernel where that handle really is.
    if _win_normalized(_win_final_path(fd)) != _win_normalized(str(pinned / path.name)):
        raise OSError(errno.ELOOP, "decision log path was redirected")
    return fd


def _create_and_open(path: Path, anchor: Path, stack: ExitStack) -> int:
    """Create-and-pin plus leaf open, re-attempted when the log directory is removed.

    Creating the log directory and opening a file inside it cannot be one
    syscall, so an actor removing that directory BETWEEN the two is a real
    interleaving and not a hypothetical: everything here runs as the same user as
    the agent, which is the premise the pinning exists for. The mirror case is
    tolerated in the same spirit -- :func:`_pin_log_dir` swallows the
    ``FileExistsError`` from a writer that created the directory first -- and
    without the removal half the failure reaches the caller as ``ENOENT`` naming
    the bare leaf: the ``openat`` argument, ``'day.jsonl'``, a filename with no
    directory component that reads as a process-working-directory bug and is
    neither (GH-12034).

    So the sequence is run again rather than repaired in place, because no
    ordering of two syscalls closes a window between them. Each attempt owns its
    descriptors on its own stack and a losing one is closed before the next
    begins; only the attempt that succeeds transfers its pins to the caller's
    *stack*, which still closes each exactly once.

    Exhaustion raises ``FileNotFoundError`` -- the same class, so every caller
    that already contains this still does -- carrying the FULL path as
    ``filename`` and a message that says what happened. A bare-leaf ``filename``
    is unreadable on its own, so reporting the path is half of what this buys.
    """
    lost: FileNotFoundError | None = None
    for _ in range(_CREATE_ATTEMPTS):
        attempt = ExitStack()
        try:
            fd = _pin_and_open_leaf(path, anchor, attempt)
        except FileNotFoundError as exc:
            # The directory, or a leaf another creator had just made, is gone
            # again. Release this attempt's pins before the bounded redo.
            attempt.close()
            lost = exc
            continue
        except BaseException:
            attempt.close()
            raise
        stack.push(attempt.pop_all())
        return fd
    raise FileNotFoundError(
        errno.ENOENT,
        f"the decision log directory {path.parent} was removed while this append was "
        f"creating it and opening {path.name!r} inside it, {_CREATE_ATTEMPTS} attempts "
        "in a row",
        str(path),
    ) from lost


@contextmanager
def _open_log(path: Path) -> Iterator[int]:
    """Keep all required pins alive until the file descriptor is closed.

    The anchor is resolved ONCE, here, above the retry in :func:`_create_and_open`,
    and every attempt is made against that one resolution. Two things come out of
    that: a home that is genuinely absent raises from this resolution rather than
    being re-attempted and then reported as a removed log directory, and an actor
    who puts a link at the anchor's name between two attempts cannot have the
    retry write the row inside whatever it points at.
    """
    with ExitStack() as stack:
        fd = _create_and_open(path, path.parent.parent.resolve(strict=True), stack)
        _regular_single_link(fd)
        yield fd


def _win_final_path(fd: int) -> str:  # pragma: no cover - Windows
    """The kernel's own path for an open handle (``GetFinalPathNameByHandleW``)."""
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    handle = msvcrt.get_osfhandle(fd)  # type: ignore[attr-defined]
    size = 1024
    while True:
        buf = ctypes.create_unicode_buffer(size)
        needed = kernel.GetFinalPathNameByHandleW(handle, buf, size, 0)
        if needed == 0:
            raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
        if needed < size:
            return buf.value
        size = needed + 1


def _win_normalized(path: str) -> str:  # pragma: no cover - Windows
    """Drop the device prefix and fold case so two spellings of one path compare."""
    if path.startswith("\\\\?\\UNC\\"):
        path = "\\\\" + path[8:]
    elif path.startswith("\\\\?\\"):
        path = path[4:]
    return os.path.normcase(os.path.normpath(path))


def _regular_single_link(fd: int) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise OSError("decision log must be a regular file with one link")


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("decision log append deadline expired")
    return remaining


class LogFull(OSError):
    """The file would pass *max_bytes* with this record. Nothing was written."""


def append_line(path: Path, line: bytes, *, max_bytes: int | None = None) -> None:
    """Append one complete JSONL record, or raise without joining later rows.

    *max_bytes*, when given, is a ceiling on the FILE: a record that would carry
    it past that size raises :class:`LogFull` inside the lock, before any byte is
    written, so a day-file is bounded by size as well as by age.

    The same file lock spans the EOF check, all short writes, and rollback.
    A failed append truncates only its counted suffix, never a pre-existing tail
    or another writer's growth. If rollback also fails, the torn tail is left in
    place and the next append terminates it before writing, so one torn row
    costs one unparseable line and not the record after it. Locks are advisory
    on POSIX: unrelated writers must honor this protocol to serialize. The
    deadline starts once the log is OPEN and from there bounds contention and
    retries; it does not bound the open, nor a stalled filesystem syscall.
    """
    if not line.endswith(b"\n") or b"\n" in line[:-1]:
        raise ValueError("append requires exactly one newline-terminated record")
    with _open_log(path) as fd:
        # Set the deadline adjacent to what it governs, AFTER the open: the budget
        # bounds the lock wait and the write retries, and a create-and-pin it
        # cannot cancel does not get to spend it. Charged from above the open
        # instead, an open slower than the whole budget -- a loaded worker, a
        # scanner on a fresh directory -- raises right here with the leaf ALREADY
        # created, leaving a day file that exists and holds nothing for the next
        # reader of this JSONL log to fail on (GH-12077).
        deadline = time.monotonic() + _APPEND_TIMEOUT_SECONDS
        with platform_compat.file_lock(fd, exclusive=True, timeout=_remaining(deadline)):
            _regular_single_link(fd)
            start = os.lseek(fd, 0, os.SEEK_END)
            payload = line
            if start:
                os.lseek(fd, start - 1, os.SEEK_SET)
                if os.read(fd, 1) != b"\n":
                    # A torn tail (a writer that died mid-row, an outside edit) is
                    # closed off first so THIS record still lands on a line of its
                    # own. The torn row stays as one unparseable line; it does not
                    # swallow the next one.
                    payload = b"\n" + line
            if max_bytes is not None and start + len(payload) > max_bytes:
                raise LogFull(f"decision log is at its {max_bytes}-byte ceiling; row not written")
            os.lseek(fd, start, os.SEEK_SET)
            written = 0
            try:
                while written < len(payload):
                    _remaining(deadline)
                    try:
                        count = os.write(fd, payload[written:])
                    except InterruptedError:
                        continue
                    if count <= 0:
                        raise OSError("decision log write made no progress")
                    written += count
            except BaseException:
                # Never remove bytes we did not append, including a stale tail.
                if written and os.fstat(fd).st_size == start + written:
                    os.ftruncate(fd, start)
                raise
