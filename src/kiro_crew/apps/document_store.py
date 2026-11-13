"""Pinned text transactions for server-selected resources.

Callers own authorization and resource selection. All cooperating writers,
including owner file editors, must use ``locked_path`` or ``write``. Advisory
locks do not serialize arbitrary programs that ignore this protocol. Platforms
without descriptor-relative publication fail closed; there is no by-name fallback.
Run these synchronous transactions in a worker thread when called from async code.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from weakref import WeakValueDictionary

from kiro_crew import pinned_fs, platform_compat
from kiro_crew.atomic_write import (
    atomic_write,
    fsync_open_dir,
    on_event_loop,
    pinned_parent_replace_supported,
)

_REVISION_RE = re.compile(r"[0-9a-fA-F]{64}\Z")
_LOCK_PREFIX = ".kiro-document-"
DOCUMENT_MAX_BYTES = 1024 * 1024
_thread_locks: WeakValueDictionary = WeakValueDictionary()
_thread_locks_guard = threading.Lock()


class DocumentStoreError(Exception):
    """A stable refusal code, without document content or sensitive paths."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class DocumentSnapshot:
    content: str
    revision: str


def _snapshot(data: bytes) -> DocumentSnapshot:
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DocumentStoreError("invalid_encoding") from exc
    return DocumentSnapshot(content, hashlib.sha256(data).hexdigest())


def _relative_parts(relative: str | Path) -> tuple[str, ...]:
    if not isinstance(relative, (str, Path)):
        raise DocumentStoreError("invalid_path")
    value = os.fspath(relative)
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise DocumentStoreError("invalid_path")
    parts = tuple(value.split("/"))
    if (
        Path(value).is_absolute()
        or PureWindowsPath(value).drive
        or any(part in ("", ".", "..") or part.startswith(_LOCK_PREFIX) for part in parts)
    ):
        raise DocumentStoreError("invalid_path")
    return parts


def _payload(content: str, operation: str, expected_revision: str | None, max_bytes: int) -> bytes:
    if operation not in ("create", "update"):
        raise DocumentStoreError("invalid_operation")
    valid_revision = isinstance(expected_revision, str) and bool(
        _REVISION_RE.fullmatch(expected_revision)
    )
    if (operation == "create" and expected_revision is not None) or (
        operation == "update" and not valid_revision
    ):
        raise DocumentStoreError("invalid_revision")
    if not isinstance(content, str):
        raise DocumentStoreError("invalid_content")
    if len(content) > max_bytes:
        raise DocumentStoreError("document_too_large")
    try:
        data = content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise DocumentStoreError("invalid_content") from exc
    if len(data) > max_bytes:
        raise DocumentStoreError("document_too_large")
    return data


@contextmanager
def _storage_errors() -> Iterator[None]:
    try:
        yield
    except DocumentStoreError:
        raise
    except pinned_fs.PinnedPathRefusal as exc:
        raise DocumentStoreError("unsafe_path") from exc
    except FileNotFoundError as exc:
        raise DocumentStoreError("not_found") from exc
    except FileExistsError as exc:
        raise DocumentStoreError("conflict") from exc
    except NotImplementedError as exc:
        raise DocumentStoreError("unsupported_platform") from exc
    except OSError as exc:
        code = (
            "unsafe_path"
            if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EISDIR)
            else "storage_unavailable"
        )
        raise DocumentStoreError(code) from exc


def _verify_regular(fd: int) -> os.stat_result:
    existing = os.fstat(fd)
    if not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1:
        raise DocumentStoreError("unsafe_path")
    return existing


@contextmanager
def _open_regular(parent_fd: int, name: str) -> Iterator[int]:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
    try:
        _verify_regular(fd)
        yield fd
    finally:
        os.close(fd)


def _read_fd(fd: int, max_bytes: int) -> bytes:
    if os.fstat(fd).st_size > max_bytes:
        raise DocumentStoreError("document_too_large")
    with os.fdopen(os.dup(fd), "rb") as stream:
        data = stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise DocumentStoreError("document_too_large")
    return data


@contextmanager
def _lock_directory(root: Path | None) -> Iterator[int]:
    if root is None:
        from kiro_crew.config.loader import data_home

        root = data_home() / "cache" / "document-locks"
    if not root.is_absolute() or ".." in root.parts:
        raise DocumentStoreError("invalid_path")
    fd = pinned_fs.pin_parent(root.anchor, what="document lock directory")
    try:
        for part in root.parts[1:]:
            child = pinned_fs.create_and_open_dir_at(fd, part, what="document lock directory")
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


class LockedDocument:
    """One pinned resource while its shared serialization lock is held.

    Use the yielded object for the complete owner read/modify/write operation;
    re-entering the store for the same resource would acquire a second file lock.
    """

    def __init__(self, parent_fd: int, name: str, max_bytes: int):
        self._parent_fd = parent_fd
        self._name = name
        self._active = True
        self._max_bytes = max_bytes

    def _check_active(self) -> None:
        if not self._active:
            raise DocumentStoreError("transaction_closed")

    def read(self) -> DocumentSnapshot:
        self._check_active()
        with _storage_errors(), _open_regular(self._parent_fd, self._name) as fd:
            return _snapshot(_read_fd(fd, self._max_bytes))

    def write(
        self,
        content: str,
        *,
        operation: str,
        expected_revision: str | None = None,
    ) -> DocumentSnapshot:
        self._check_active()
        data = _payload(content, operation, expected_revision, self._max_bytes)
        with _storage_errors():
            if operation == "create":
                self._create(data)
            else:
                assert expected_revision is not None  # validated by _payload
                with _open_regular(self._parent_fd, self._name) as fd:
                    current = _snapshot(_read_fd(fd, self._max_bytes))
                    if current.revision != expected_revision.lower():
                        raise DocumentStoreError("conflict")
                    atomic_write(
                        self._name,
                        data,
                        fsync=True,
                        mode=stat.S_IMODE(os.fstat(fd).st_mode),
                        preserve_access_control_from=fd,
                        parent_dir_fd=self._parent_fd,
                    )
            fsync_open_dir(self._parent_fd, self._name)
            actual = self.read()
            if actual != _snapshot(data):
                raise DocumentStoreError("write_unverified")
            return actual

    def _create(self, data: bytes) -> None:
        try:
            with _open_regular(self._parent_fd, self._name):
                raise DocumentStoreError("conflict")
        except FileNotFoundError:
            pass
        # A private staged leaf plus no-replace rename keeps create atomic even
        # when a nonparticipating writer occupies the destination during staging.
        staged = f"{_LOCK_PREFIX}{os.urandom(16).hex()}.tmp"
        try:
            atomic_write(staged, data, fsync=True, mode=0o600, parent_dir_fd=self._parent_fd)
            platform_compat.rename_noreplace(
                staged, self._name, src_dir_fd=self._parent_fd, dst_dir_fd=self._parent_fd
            )
        finally:
            try:
                os.unlink(staged, dir_fd=self._parent_fd)
            except FileNotFoundError:
                pass


class DocumentStore:
    """CAS text storage beneath an existing, server-selected absolute root."""

    def __init__(
        self, root: Path, *, max_bytes: int = DOCUMENT_MAX_BYTES, lock_root: Path | None = None
    ):
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        self.root = Path(root)
        self.max_bytes = max_bytes
        self.lock_root = Path(lock_root) if lock_root is not None else None

    def read(self, relative: str | Path) -> DocumentSnapshot:
        with self.locked_path(relative) as document:
            return document.read()

    def write(
        self,
        relative: str | Path,
        content: str,
        *,
        operation: str,
        expected_revision: str | None = None,
    ) -> DocumentSnapshot:
        # Validate before opening the lock sidecar; malformed requests have no
        # filesystem effect, including no persistent lock file.
        _payload(content, operation, expected_revision, self.max_bytes)
        with self.locked_path(relative) as document:
            return document.write(content, operation=operation, expected_revision=expected_revision)

    @contextmanager
    def locked_path(self, relative: str | Path) -> Iterator[LockedDocument]:
        parts = _relative_parts(relative)
        if not self.root.is_absolute() or ".." in self.root.parts:
            raise DocumentStoreError("invalid_path")
        if not pinned_fs.supports_pinned_walk() or not pinned_parent_replace_supported():
            raise DocumentStoreError("unsupported_platform")
        with _storage_errors():
            # Do not realpath this chain: doing so would silently admit a planted
            # symlink. pin_parent walks every supplied component with O_NOFOLLOW.
            parent_fd = pinned_fs.pin_parent(str(self.root.joinpath(*parts[:-1])), what="document")
            try:
                with self._serialize(parent_fd, parts[-1], self.lock_root):
                    document = LockedDocument(parent_fd, parts[-1], self.max_bytes)
                    try:
                        yield document
                    finally:
                        document._active = False
            finally:
                os.close(parent_fd)

    @staticmethod
    @contextmanager
    def _serialize(parent_fd: int, name: str, lock_root: Path | None) -> Iterator[None]:
        parent = os.fstat(parent_fd)
        key = (parent.st_dev, parent.st_ino, name)
        with _thread_locks_guard:
            thread_lock = _thread_locks.get(key)
            if thread_lock is None:
                thread_lock = threading.Lock()
                _thread_locks[key] = thread_lock
        if not thread_lock.acquire(blocking=not on_event_loop()):
            raise DocumentStoreError("storage_unavailable")
        try:
            key_bytes = os.fsencode(f"{parent.st_dev}:{parent.st_ino}:{name}")
            lock_name = hashlib.sha256(key_bytes).hexdigest() + ".lock"
            with _lock_directory(lock_root) as lock_dir_fd:
                flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
                lock_fd = os.open(lock_name, flags, 0o600, dir_fd=lock_dir_fd)
                try:
                    _verify_regular(lock_fd)
                    with platform_compat.file_lock(lock_fd, required=True):
                        yield
                finally:
                    os.close(lock_fd)
        finally:
            thread_lock.release()
