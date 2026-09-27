"""Cross-process serialization for workspace ``cli.json`` overlays."""

from __future__ import annotations

import errno
import os
import stat
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew import pinned_fs, platform_compat

CLI_SETTINGS_LOCK_NAME = ".kirocrew-cli-settings.lock"
#: The effort levels Kiro Crew wrote into a workspace ``cli.json``, by model.
EFFORT_OWNED_KEY = "kirocrew.effortOwned"
#: The whole-second mtime the publishing writer gave the file. The record under
#: :data:`EFFORT_OWNED_KEY` counts only while this equals the mtime of the file
#: the reader used, so any rewrite that does not carry the stamp voids it.
EFFORT_OWNED_STAMP_KEY = "kirocrew.effortOwnedStamp"
#: The STARTUP ceiling. A native launch cannot wait long for this file, and a
#: launch that loses the lock has a safe fallback (authored agents).
CLI_SETTINGS_LOCK_TIMEOUT_SECS = 2.0
#: The ceiling for an operator action that is NOT on the startup path. Projection
#: holds this lock across a sub-second critical section, so a ceiling this far
#: above it makes losing the lock a stuck holder rather than routine contention --
#: which is what lets a caller keep a two-valued result instead of a third state
#: for a failure that only a stuck holder produces. Callers using it MUST be off
#: the event loop: waiting this long on it would freeze every session.
CLI_SETTINGS_LOCK_ACTION_TIMEOUT_SECS = 30.0
#: ``stat.FILE_ATTRIBUTE_REPARSE_POINT``, which the type stubs declare for Windows only.
_FILE_ATTRIBUTE_REPARSE_POINT: int = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


@dataclass(frozen=True)
class LockedCliSettings:
    """The workspace ``cli.json`` and its settings directory while the lock is held.

    ``settings_fd`` is the settings directory the lock opened without following links, or
    ``None`` on the by-name floor. The lock owns and closes it.
    """

    cli_json: Path
    settings_dir: Path
    settings_fd: int | None

    def holds_named_settings_dir(self) -> bool:
        """Whether the settings directory reached by name is still the one the lock opened."""
        return self.settings_fd is not None and _is_pinned_directory(
            self.settings_fd, self.settings_dir
        )


def _settings_dir_within_work_dir(work_dir: Path) -> Path:
    """Return the settings directory below the resolved work dir, refusing a link at either part."""
    work_dir_real = Path(os.path.realpath(work_dir))
    kiro_dir = work_dir_real / ".kiro"
    settings_dir = kiro_dir / "settings"
    if platform_compat.is_link_or_junction(kiro_dir) or platform_compat.is_link_or_junction(
        settings_dir
    ):
        raise OSError("workspace CLI settings directory is linked")
    return settings_dir


def _pinned_settings_dir_fd(resolved_work_dir: str) -> int:
    """Return a settings-directory descriptor reached without re-opening an ancestor."""
    parent_fd = pinned_fs.pin_parent(
        resolved_work_dir,
        what="workspace CLI settings directory",
        refusal=OSError,
    )
    try:
        for name in (".kiro", "settings"):
            try:
                os.mkdir(name, 0o777, dir_fd=parent_fd)
            except FileExistsError:
                pass
            try:
                child_fd = os.open(name, pinned_fs.dir_flags(), dir_fd=parent_fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
                    raise OSError(
                        "workspace CLI settings directory is linked or not a directory"
                    ) from exc
                raise
            os.close(parent_fd)
            parent_fd = child_fd
        return parent_fd
    except BaseException:
        os.close(parent_fd)
        raise


def _is_pinned_directory(settings_fd: int, settings_dir: Path) -> bool:
    """Whether ``settings_dir`` reached by name is still the directory ``settings_fd`` holds.

    A reparse point set on the held directory itself keeps its file id, so it is refused by its
    attribute: on Windows an empty folder can take one without being renamed or deleted.
    """
    named = pinned_fs.lstat_by_name(settings_dir)
    pinned = os.fstat(settings_fd)
    return (
        named is not None
        and stat.S_ISDIR(named.st_mode)
        and not getattr(named, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
        and (named.st_dev, named.st_ino) == (pinned.st_dev, pinned.st_ino)
    )


@contextmanager
def locked_workspace_cli_settings(
    work_dir: Path, *, timeout: float = CLI_SETTINGS_LOCK_TIMEOUT_SECS
) -> Iterator[LockedCliSettings]:
    """Yield the workspace ``cli.json`` and its settings directory while the verified lock is held.

    Windows creates and pins ``.kiro`` before creating ``settings`` beneath it because
    descriptor-relative directory walks are unavailable; the held ``settings`` is checked before
    and after the lock file opens, so a reparse point set on it is refused. The work directory is
    resolved once, and every later resolution is compared with it and refused on difference.
    """
    stack = ExitStack()
    try:
        settings_dir = _settings_dir_within_work_dir(work_dir)
        lock_path = settings_dir / CLI_SETTINGS_LOCK_NAME
        settings_fd: int | None = None
        settings_pin_fd: int | None = None
        if pinned_fs.supports_pinned_walk():
            work_dir.mkdir(parents=True, exist_ok=True)
            if _settings_dir_within_work_dir(work_dir) != settings_dir:
                raise OSError("workspace CLI settings directory changed while it was opened")
            resolved_work_dir = str(settings_dir.parent.parent)
            settings_fd = _pinned_settings_dir_fd(resolved_work_dir)
            stack.callback(os.close, settings_fd)
            try:
                lock_fd = platform_compat.open_create_or_existing(
                    CLI_SETTINGS_LOCK_NAME,
                    os.O_RDWR | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                    0o644,
                    dir_fd=settings_fd,
                )
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.EMLINK):
                    raise OSError("workspace CLI settings lock is a symlink or junction") from exc
                if exc.errno == errno.ENOENT:
                    raise OSError(
                        "workspace CLI settings directory or lock file was removed while the lock"
                        " was being opened"
                    ) from exc
                raise
            stack.callback(os.close, lock_fd)
        else:
            work_dir.mkdir(parents=True, exist_ok=True)
            kiro_dir = settings_dir.parent
            try:
                kiro_dir.mkdir(exist_ok=True)
            except FileExistsError as exc:
                raise OSError(
                    "workspace CLI settings directory is linked or not a directory"
                ) from exc
            try:
                kiro_pin_fd = platform_compat.pin_directory(kiro_dir)
                stack.callback(os.close, kiro_pin_fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
                    raise OSError(
                        "workspace CLI settings directory is linked or not a directory"
                    ) from exc
                raise
            if not _is_pinned_directory(kiro_pin_fd, kiro_dir):
                raise OSError("workspace CLI settings directory changed while it was opened")
            if _settings_dir_within_work_dir(work_dir) != settings_dir:
                raise OSError("workspace CLI settings directory changed while it was opened")
            try:
                settings_dir.mkdir(exist_ok=True)
            except FileExistsError as exc:
                raise OSError(
                    "workspace CLI settings directory is linked or not a directory"
                ) from exc
            try:
                settings_pin_fd = platform_compat.pin_directory(settings_dir)
                stack.callback(os.close, settings_pin_fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
                    raise OSError(
                        "workspace CLI settings directory is linked or not a directory"
                    ) from exc
                raise
            # A held ``.kiro`` can take a reparse point in place while it is empty, so it is
            # checked again once ``settings`` exists in it, after which it cannot take one.
            if not _is_pinned_directory(settings_pin_fd, settings_dir) or not _is_pinned_directory(
                kiro_pin_fd, kiro_dir
            ):
                raise OSError("workspace CLI settings directory changed while it was opened")
            if platform_compat.is_link_or_junction(lock_path):
                raise OSError("workspace CLI settings lock is a symlink or junction")
            lock_fd = stack.enter_context(platform_compat.open_lock_file(lock_path))
        pinned_settings_fd = settings_fd if settings_fd is not None else settings_pin_fd
        opened = os.fstat(lock_fd)
        named = pinned_fs.lstat_by_name(lock_path)
        if (
            platform_compat.is_link_or_junction(lock_path)
            or named is None
            or not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise OSError("workspace CLI settings lock changed while it was opened")
        if pinned_settings_fd is not None and not _is_pinned_directory(
            pinned_settings_fd, settings_dir
        ):
            raise OSError("workspace CLI settings directory changed while it was opened")
        stack.enter_context(
            platform_compat.file_lock(
                lock_fd,
                exclusive=True,
                timeout=timeout,
            )
        )
        current = pinned_fs.lstat_by_name(lock_path)
        if (
            platform_compat.is_link_or_junction(lock_path)
            or current is None
            or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise OSError("workspace CLI settings lock changed while it was acquired")
        if pinned_settings_fd is not None and not _is_pinned_directory(
            pinned_settings_fd, settings_dir
        ):
            raise OSError("workspace CLI settings directory changed while it was acquired")
        yield LockedCliSettings(
            cli_json=settings_dir / "cli.json",
            settings_dir=settings_dir,
            settings_fd=settings_fd,
        )
    finally:
        stack.close()


@contextmanager
def workspace_cli_settings_lock(
    work_dir: Path, *, timeout: float = CLI_SETTINGS_LOCK_TIMEOUT_SECS
) -> Iterator[Path]:
    """Yield a workspace ``cli.json`` path while its verified lock is held.

    Windows keeps the by-name floor because descriptor-relative directory walks are unavailable,
    and holds ``.kiro`` and ``settings`` open until the lock is released.
    """
    with locked_workspace_cli_settings(work_dir, timeout=timeout) as settings:
        yield settings.cli_json


def effort_ownership_stamp_matches(document: dict[str, Any], file_mtime: int | None) -> bool:
    """Whether *document*'s ownership stamp names the file version *file_mtime* was read from."""
    stamp = document.get(EFFORT_OWNED_STAMP_KEY)
    return type(stamp) is int and file_mtime is not None and stamp == file_mtime


def stamp_effort_ownership(document: dict[str, Any]) -> int:
    """Store a fresh ownership stamp in *document* and return the ``mtime_ns`` that publishes it.

    The previous second cannot equal the replacement inode's natural creation mtime,
    so a later rewrite that drops the timestamp is told apart from this one.
    """
    stamp = int(time.time()) - 1
    document[EFFORT_OWNED_STAMP_KEY] = stamp
    return stamp * 1_000_000_000


def carry_effort_ownership(document: dict[str, Any], file_mtime: int | None) -> int | None:
    """Prepare a rewrite of *document* that keeps a valid ownership record valid and a void one void.

    For a writer that changes other settings and only carries the effort keys through.
    A record whose stamp matches *file_mtime* is restamped, and the returned ``mtime_ns``
    must be passed to ``atomic_write`` so the published file still validates it. Any
    other record is dropped before the write, because no later Kiro Crew write may
    re-validate an entry Kiro Crew cannot prove it wrote; ``None`` then leaves the
    file's mtime natural.
    """
    if effort_ownership_stamp_matches(document, file_mtime):
        return stamp_effort_ownership(document)
    document.pop(EFFORT_OWNED_KEY, None)
    document.pop(EFFORT_OWNED_STAMP_KEY, None)
    return None
