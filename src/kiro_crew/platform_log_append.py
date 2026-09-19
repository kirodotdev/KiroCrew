"""Confined, serialized JSONL append for the decision log.

The configured home is the trusted anchor (resolved once). Its log directory
and file must not be links. POSIX uses pinned directory-relative opens. Windows
holds the anchor and the log directory open with list access and read-only
sharing until the write ends, so a data-write or delete open of either (the
handle a junction swap needs) is a sharing violation while we hold them, and then
checks the leaf handle's final path against the pinned directory, so a swap that
raced the pins is refused rather than written through.
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


def _pin_log_dir(directory: Path, stack: ExitStack, *, create: bool) -> int | None:
    """Pin the log directory under its resolved anchor; return a POSIX dir fd or None.

    Only the immediate log directory may be created. The configured home must
    exist; resolving that anchor allows intentional home aliases and macOS /tmp.
    The log directory itself is never resolved before its no-link open. On POSIX
    the returned descriptor is what every later name is opened relative to; on
    Windows the directory handle is held open with read-only sharing (so a swap
    is a sharing violation while it is held) and callers work by path.
    """
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


@contextmanager
def _open_log(path: Path) -> Iterator[int]:
    """Keep all required pins alive until the file descriptor is closed."""
    directory = path.parent
    with ExitStack() as stack:
        parent_fd = _pin_log_dir(directory, stack, create=True)
        if parent_fd is not None:
            fd = os.open(
                path.name,
                os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK,
                _FILE_MODE,
                dir_fd=parent_fd,
            )
            stack.callback(os.close, fd)
        else:  # pragma: no cover - Windows; exercised by the Windows test lane
            pinned = directory.parent.resolve(strict=True) / directory.name
            fd = _win_open(pinned / path.name, directory=False)
            stack.callback(os.close, fd)
            # The pins make a directory swap a sharing violation while they are
            # held; this makes one that landed before them a refusal. The leaf
            # was opened by path, so ask the kernel where that handle really is.
            if _win_normalized(_win_final_path(fd)) != _win_normalized(str(pinned / path.name)):
                raise OSError(errno.ELOOP, "decision log path was redirected")
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
    deadline bounds contention and retries, not a stalled filesystem syscall.
    """
    if not line.endswith(b"\n") or b"\n" in line[:-1]:
        raise ValueError("append requires exactly one newline-terminated record")
    deadline = time.monotonic() + _APPEND_TIMEOUT_SECONDS
    with _open_log(path) as fd:
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
