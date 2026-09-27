"""Cross-process serialization for workspace ``cli.json`` overlays."""

from __future__ import annotations

import errno
import os
import stat
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from kiro_crew import pinned_fs, platform_compat
from kiro_crew.security import canonical_path_refusal

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


_SETTINGS_DIR_OUTSIDE_WORK_DIR = (
    "workspace CLI settings directory resolves outside the work directory or loops"
)
_SETTINGS_DIR_SENSITIVE = (
    "workspace CLI settings directory is on a sensitive path or cannot be verified"
)


def _refuse_sensitive_settings_path(path: str | None) -> None:
    """Raise before a workspace CLI settings path can be opened or created beneath."""
    if path is None or canonical_path_refusal(path) is not None:
        raise OSError(_SETTINGS_DIR_SENSITIVE)


def _refuse_sensitive_settings_descriptor(
    settings_fd: int, *, by_name_fallback: Path | None = None
) -> None:
    """Raise when the held settings directory is sensitive or cannot be identified."""
    opened_path = pinned_fs.fd_real_path(settings_fd)
    if opened_path is None and by_name_fallback is not None:
        opened_path = os.fspath(by_name_fallback)
    _refuse_sensitive_settings_path(opened_path)


def _resolved_work_dir(work_dir: Path) -> Path:
    """Resolve the trusted *work_dir* once; a loop in it is refused by the first probe below it."""
    return Path(os.path.realpath(work_dir))


def _normalize_windows_link_target(target: str | PureWindowsPath) -> PureWindowsPath:
    r"""Return a Windows link target without NT device-path spelling.

    ``os.readlink`` may return either extended DOS spelling or the NT object
    manager spelling. The normalization is lexical: it never probes the path.
    An extended UNC target becomes an ordinary UNC target so the caller can
    compare its drive and components with the trusted work directory.
    """
    text = str(target)
    for prefix in ("\\\\?\\UNC\\", "\\??\\UNC\\"):
        if text.startswith(prefix):
            return PureWindowsPath("\\\\" + text[len(prefix) :])
    for prefix in ("\\\\?\\", "\\??\\"):
        if text.startswith(prefix):
            return PureWindowsPath(text[len(prefix) :])
    return PureWindowsPath(text)


def _target_parts_within_work_dir(target: str, work_dir_real: Path) -> tuple[bool, tuple[str, ...]]:
    """Return whether *target* is absolute and its lexically admitted parts.

    Relative targets are returned for in-place processing. Absolute targets
    are admitted only when they contain no parent component and name a path
    inside the resolved work directory. No filesystem operation occurs here.
    """
    if os.name == "nt":
        windows_target = _normalize_windows_link_target(target)
        if windows_target.drive.startswith("\\\\"):
            raise OSError(_SETTINGS_DIR_OUTSIDE_WORK_DIR)
        if windows_target.root and not windows_target.drive:
            raise OSError(_SETTINGS_DIR_OUTSIDE_WORK_DIR)
        if windows_target.drive and not windows_target.is_absolute():
            raise OSError(_SETTINGS_DIR_OUTSIDE_WORK_DIR)
        if not windows_target.is_absolute():
            return False, windows_target.parts
        if ".." in windows_target.parts:
            raise OSError(_SETTINGS_DIR_OUTSIDE_WORK_DIR)
        work_path = PureWindowsPath(str(work_dir_real))
        target_parts = tuple(os.path.normcase(part) for part in windows_target.parts)
        work_parts = tuple(os.path.normcase(part) for part in work_path.parts)
        if target_parts[: len(work_parts)] != work_parts:
            raise OSError(_SETTINGS_DIR_OUTSIDE_WORK_DIR)
        return True, windows_target.parts[len(work_path.parts) :]

    posix_target = Path(target)
    if not posix_target.is_absolute():
        return False, posix_target.parts
    if ".." in posix_target.parts:
        raise OSError(_SETTINGS_DIR_OUTSIDE_WORK_DIR)
    try:
        return True, posix_target.relative_to(work_dir_real).parts
    except ValueError as exc:
        raise OSError(_SETTINGS_DIR_OUTSIDE_WORK_DIR) from exc


def _settings_dir_within_work_dir(work_dir: Path) -> Path:
    """Resolve the workspace settings folder without touching an outside target.

    The trusted work directory is resolved once. From there, each component is
    inspected with ``lstat`` and a link is expanded from its ``readlink`` text.
    Absolute, remote, or escaping targets are rejected lexically before their
    targets are probed. Missing components stay as named so the locked walk can
    create them. More than 40 link expansions is treated as a loop.

    A process that can swap a component between two probes in one pass can make
    one later ``lstat`` follow that replacement. The pinned walk still refuses
    the changed chain before any write is redirected, and such a process already
    has the access that the one probe could expose.
    """
    work_dir_real = _resolved_work_dir(work_dir)
    prefix = work_dir_real
    pending = [".kiro", "settings"]
    link_expansions = 0

    while pending:
        name = pending.pop(0)
        if name in ("", "."):
            continue
        if name == "..":
            if prefix == work_dir_real:
                raise OSError(_SETTINGS_DIR_OUTSIDE_WORK_DIR)
            prefix = prefix.parent
            continue
        if Path(name).name != name:
            raise OSError(_SETTINGS_DIR_OUTSIDE_WORK_DIR)

        candidate = prefix / name
        try:
            info = os.lstat(candidate)
        except FileNotFoundError:
            for remaining in pending:
                if remaining in ("", ".", "..") or Path(remaining).name != remaining:
                    raise OSError(_SETTINGS_DIR_OUTSIDE_WORK_DIR)
            return candidate.joinpath(*pending)
        except OSError as exc:
            raise OSError(_SETTINGS_DIR_OUTSIDE_WORK_DIR) from exc

        is_link = (
            stat.S_ISLNK(info.st_mode)
            or platform_compat.lstat_is_name_surrogate(info)
            or platform_compat.is_link_or_junction(candidate)
        )
        if is_link:
            link_expansions += 1
            if link_expansions > 40:
                raise OSError(_SETTINGS_DIR_OUTSIDE_WORK_DIR)
            try:
                target = os.readlink(candidate)
            except OSError as exc:
                raise OSError(_SETTINGS_DIR_OUTSIDE_WORK_DIR) from exc
            absolute, target_parts = _target_parts_within_work_dir(target, work_dir_real)
            if absolute:
                prefix = work_dir_real
            pending[:0] = list(target_parts)
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise OSError("workspace CLI settings path component is not a directory")
        prefix = candidate

    return prefix


def admitted_workspace_cli_settings_dir(work_dir: Path) -> Path | None:
    """Return the admitted settings folder, or ``None`` without probing its target."""
    try:
        return _settings_dir_within_work_dir(work_dir)
    except OSError:
        return None


def workspace_cli_settings_fence_key(work_dir: Path) -> str:
    """Return the admitted folder, or a named fallback beneath the resolved work dir.

    Admission refusal must not resolve the refused settings link. Failure to
    resolve the trusted work directory still raises, so starts for one work
    directory cannot silently use different fence keys.
    """
    admitted = admitted_workspace_cli_settings_dir(work_dir)
    if admitted is not None:
        return os.fspath(admitted)
    return os.path.join(os.path.realpath(work_dir), ".kiro", "settings")


def _settings_chain(work_dir_real: Path, settings_dir: Path) -> tuple[str, ...]:
    """The directory names from the resolved work dir down to the resolved settings folder."""
    return settings_dir.relative_to(work_dir_real).parts


def _pinned_settings_dir_fd(work_dir_real: Path, settings_dir: Path) -> int:
    """Return a settings-directory descriptor reached without re-opening an ancestor.

    The walk follows the chain the resolution produced, one ``openat`` per component with
    ``O_NOFOLLOW``, so a component replaced by a link after the resolution is refused rather
    than followed.
    """
    parent_fd = pinned_fs.pin_parent(
        str(work_dir_real),
        what="workspace CLI settings directory",
        refusal=OSError,
    )
    try:
        for name in _settings_chain(work_dir_real, settings_dir):
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

    The settings directory is the one ``<work_dir>/.kiro/settings`` resolves to, when that stays
    inside the resolved work dir; a chain that leaves it or loops is refused, and so is a folder
    on a sensitive path or one whose real path cannot be verified, before anything beneath it is
    created or opened. The directories
    from the work dir down to it are then reached by name as resolved, never through a link: a
    component swapped for a link after the resolution is refused. Windows creates and pins each
    directory before creating the next beneath it because descriptor-relative directory walks
    are unavailable; the held folder is checked before and after the lock file opens, so a
    reparse point set on it is refused. The work directory is resolved once, and every later
    resolution is compared with it and refused on difference.
    """
    stack = ExitStack()
    try:
        settings_dir = _settings_dir_within_work_dir(work_dir)
        _refuse_sensitive_settings_path(os.fspath(settings_dir))
        work_dir_real = _resolved_work_dir(work_dir)
        if not settings_dir.is_relative_to(work_dir_real):
            raise OSError("workspace CLI settings directory changed while it was opened")
        lock_path = settings_dir / CLI_SETTINGS_LOCK_NAME
        settings_fd: int | None = None
        settings_pin_fd: int | None = None
        if pinned_fs.supports_pinned_walk():
            work_dir.mkdir(parents=True, exist_ok=True)
            if _settings_dir_within_work_dir(work_dir) != settings_dir:
                raise OSError("workspace CLI settings directory changed while it was opened")
            settings_fd = _pinned_settings_dir_fd(work_dir_real, settings_dir)
            stack.callback(os.close, settings_fd)
            _refuse_sensitive_settings_descriptor(settings_fd)
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
            held: list[tuple[int, Path]] = []
            level = work_dir_real
            for name in _settings_chain(work_dir_real, settings_dir):
                if held and _settings_dir_within_work_dir(work_dir) != settings_dir:
                    raise OSError("workspace CLI settings directory changed while it was opened")
                level = level / name
                try:
                    level.mkdir(exist_ok=True)
                except FileExistsError as exc:
                    raise OSError(
                        "workspace CLI settings directory is linked or not a directory"
                    ) from exc
                try:
                    level_fd = platform_compat.pin_directory(level)
                    stack.callback(os.close, level_fd)
                except OSError as exc:
                    if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
                        raise OSError(
                            "workspace CLI settings directory is linked or not a directory"
                        ) from exc
                    raise
                held.append((level_fd, level))
                # A held directory can take a reparse point in place while it is empty, so
                # every held level is checked again once the next exists in it, after which
                # it cannot take one.
                if not all(_is_pinned_directory(fd, path) for fd, path in held):
                    raise OSError("workspace CLI settings directory changed while it was opened")
            if not held:
                held.append((platform_compat.pin_directory(settings_dir), settings_dir))
                stack.callback(os.close, held[0][0])
            settings_pin_fd = held[-1][0]
            _refuse_sensitive_settings_descriptor(settings_pin_fd, by_name_fallback=settings_dir)
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
    and holds every directory from the work dir down to the resolved settings folder open until
    the lock is released.
    """
    with locked_workspace_cli_settings(work_dir, timeout=timeout) as settings:
        yield settings.cli_json


def effort_ownership_stamp_matches(document: dict[str, Any], file_mtime: int | None) -> bool:
    """Whether *document*'s ownership stamp names the file version *file_mtime* was read from."""
    stamp = document.get(EFFORT_OWNED_STAMP_KEY)
    return type(stamp) is int and file_mtime is not None and stamp == file_mtime


def stamp_effort_ownership(document: dict[str, Any]) -> int | None:
    """Stamp a *document* carrying an ownership record; return the ``mtime_ns`` that publishes it.

    A document without a record under :data:`EFFORT_OWNED_KEY` has nothing for a
    stamp to validate, so a stamp it carries is dropped and ``None`` leaves the
    published file's mtime natural.

    A natural rewrite can be dated one clock tick behind ``time.time()``, and FAT
    rounds mtimes down to an even second. An even second at least three seconds
    back is stored exactly and remains below any later natural rewrite.
    """
    if EFFORT_OWNED_KEY not in document:
        document.pop(EFFORT_OWNED_STAMP_KEY, None)
        return None
    stamp = (int(time.time()) - 3) // 2 * 2
    document[EFFORT_OWNED_STAMP_KEY] = stamp
    return stamp * 1_000_000_000


def _without_effort_ownership_stamp(document: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in document.items() if key != EFFORT_OWNED_STAMP_KEY}


def owned_document_unchanged(
    document: dict[str, Any], read_document: dict[str, Any], file_mtime: int | None
) -> bool:
    """Whether a write of *document* would republish the owned file it was read from.

    *read_document* is the file version *file_mtime* was read from, as read. When
    its record is valid, its stamp names that version, and *document* equals it
    apart from the stamp, the write has nothing to change: skipping it keeps the
    file's bytes and its mtime, so the record stays valid without a restamp. A
    file with no record, or with a void one, is never kept this way; that write
    goes through and drops or claims the record under the writer's own rule.
    """
    return (
        EFFORT_OWNED_KEY in read_document
        and effort_ownership_stamp_matches(read_document, file_mtime)
        and _without_effort_ownership_stamp(document)
        == _without_effort_ownership_stamp(read_document)
    )


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
