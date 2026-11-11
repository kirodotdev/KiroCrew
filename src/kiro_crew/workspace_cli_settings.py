"""Cross-process serialization for workspace ``cli.json`` overlays.

kiro-cli reads workspace settings only from ``.kiro/settings/cli.json`` in the
folder it starts in, so the overlay must live in the work dir. When that work
dir is inside a user's git repository, the overlay and its lock would show up in
``git status`` as changes the user never made. When the work dir is a git
work-tree top, :func:`_exclude_from_git` keeps them out by adding both paths to
the repository's local ``info/exclude`` -- git's per-clone ignore list, which is
never committed. A file the repository already TRACKS is not hidden by that
list; that case is a known limit.
"""

from __future__ import annotations

import errno
import logging
import os
import stat
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from kiro_crew import atomic_write as atomic_write_module
from kiro_crew import pinned_fs, platform_compat
from kiro_crew.bounded_read import bounded_read
from kiro_crew.config.paths import kiro_home
from kiro_crew.security import canonical_path_refusal

logger = logging.getLogger(__name__)

CLI_SETTINGS_LOCK_NAME = ".kirocrew-cli-settings.lock"
_INTO_KIRO_HOME = "workspace CLI settings folder is a link into the global kiro settings"
_EXCLUDE_COMMENT = "# Kiro Crew session settings (local only)"
_READ_LIMIT = 1 << 20
#: What the overlay reader says about a ``cli.json`` shape it refuses, after the
#: file it names.
CLI_JSON_REFUSED = (
    "is a link, a hardlink, not a regular file inside the settings directory, or cannot be "
    "read; left unchanged"
)
_EXCLUDE_LINES = (
    "/.kiro/settings/cli.json",
    f"/.kiro/settings/{CLI_SETTINGS_LOCK_NAME}",
)
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


def _settings_chain(work_dir_real: Path, settings_dir: Path) -> tuple[str, ...]:
    """The directory names from the resolved work dir down to the resolved settings folder."""
    return settings_dir.relative_to(work_dir_real).parts


def _pinned_settings_dir_fd(work_dir_real: Path, settings_dir: Path) -> int:
    """Return a settings-directory descriptor reached without re-opening an ancestor.

    The walk follows the chain the resolution produced, one ``openat`` per component with
    ``O_NOFOLLOW``, so a component replaced by a link after the resolution is refused rather
    than followed.

    Not ``pinned_fs.open_pinned_descendant_dir``: that opens its root by name, so an ancestor
    of the work dir swapped for a link is followed, while ``pin_parent`` here pins every
    component from the filesystem root. It also creates each folder 0o700, and these are the
    user's own ``.kiro`` folders, which keep the mode the umask gives them.
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

    A link set on the held directory itself keeps its file id, so it is refused by its reparse
    tag: on Windows an empty folder can take one without being renamed or deleted. A reparse
    point that keeps its own contents, such as a OneDrive cloud-files folder, is the directory.
    """
    named = pinned_fs.lstat_by_name(settings_dir)
    pinned = os.fstat(settings_fd)
    return (
        named is not None
        and stat.S_ISDIR(named.st_mode)
        and not platform_compat.lstat_is_name_surrogate(named)
        and (named.st_dev, named.st_ino) == (pinned.st_dev, pinned.st_ino)
    )


def _read_small(path: Path) -> str:
    """Read a regular file as text without following a final symlink.

    Opened non-blocking and refused unless regular, so a FIFO or device planted
    at a git path cannot hang spawn prep.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError(f"{path} is not a regular file")
    with os.fdopen(fd, encoding="utf-8", errors="surrogateescape") as handle:
        text = bounded_read(handle, _READ_LIMIT + 1)
    if len(text) > _READ_LIMIT:
        # A partial read would misjudge the trailing newline; leave it alone.
        raise OSError(f"{path} is larger than {_READ_LIMIT} characters")
    return text


def _git_common_dir(work_dir: Path) -> Path | None:
    """Return the git common dir when *work_dir* is a work-tree top, else None.

    Only ``work_dir/.git`` is consulted: a session pointed at the user's repo
    has it there, while a Crew run dir or a subfolder does not, so nothing is
    written into a repository further up. Resolved from the files git itself
    reads, without running git: a ``.git`` directory, or a ``.git`` file (linked
    worktree, submodule) naming the git dir, whose optional ``commondir`` names
    the shared dir. ``info/exclude`` lives in the common dir, which is the one
    git honors for every linked worktree.
    """
    dot_git = work_dir / ".git"
    if dot_git.is_dir():
        git_dir = dot_git
    elif dot_git.is_file():
        text = _read_small(dot_git).strip()
        if not text.startswith("gitdir:"):
            return None
        git_dir = Path(text[len("gitdir:") :].strip())
        if not git_dir.is_absolute():
            git_dir = work_dir / git_dir
    else:
        return None
    try:
        named = _read_small(git_dir / "commondir").strip()
    except FileNotFoundError:
        return git_dir
    common = Path(named)
    return common if common.is_absolute() else git_dir / common


def _exclude_from_git(work_dir: Path) -> None:
    """Add the overlay and its lock to the work tree's ``info/exclude``.

    Appends only the lines that are missing and never rewrites the user's own
    lines. Does nothing unless *work_dir* is a git work-tree top.
    """
    common = _git_common_dir(work_dir)
    # The work dir is agent-writable, so a planted ``.git`` file can name any
    # directory. Only write where a real git dir already keeps its ``info/``
    # (``git init`` creates it): never create directories on that path.
    if (
        common is None
        or not (common / "HEAD").is_file()
        or not (common / "objects").is_dir()
        or not (common / "info").is_dir()
        or (common / "info").is_symlink()
    ):
        return
    info = common / "info"
    exclude = info / "exclude"
    try:
        existing = _read_small(exclude)
    except FileNotFoundError:
        existing = ""
    present = {line.strip() for line in existing.splitlines()}
    missing = [line for line in _EXCLUDE_LINES if line not in present]
    if not missing:
        return
    lead = "\n" if existing and not existing.endswith("\n") else ""
    text = lead + "\n".join([_EXCLUDE_COMMENT, *missing]) + "\n"
    fd = os.open(
        exclude,
        os.O_WRONLY
        | os.O_APPEND
        | os.O_CREAT
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
        0o644,
    )
    with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape") as handle:
        handle.write(text)


def _resolves_into_kiro_home(settings_dir: Path, work_dir_real: Path) -> bool:
    """Whether a ``.kiro`` or ``settings`` link makes *settings_dir* a kiro home's own.

    *settings_dir* is the folder ``<work_dir>/.kiro/settings`` already resolved to
    inside *work_dir_real*, so no link target outside the work dir is probed here.
    That folder holds the user's own kiro-cli defaults. Both homes are checked: the
    user's own kiro-cli reads ``~/.kiro`` even when ``KIRO_HOME`` moves this one. A
    work dir with no link, or a link to any other folder, is left alone.
    """
    if settings_dir == work_dir_real / ".kiro" / "settings":
        return False
    for home in (kiro_home(), Path(os.path.expanduser("~")) / ".kiro"):
        home_real = Path(os.path.realpath(home))
        if settings_dir in (home_real, home_real / "settings"):
            return True
    return False


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
    resolution is compared with it and refused on difference. A ``.kiro`` or ``.kiro/settings``
    link into the global kiro settings is refused too, before anything is created.
    """
    stack = ExitStack()
    try:
        settings_dir = _settings_dir_within_work_dir(work_dir)
        _refuse_sensitive_settings_path(os.fspath(settings_dir))
        work_dir_real = _resolved_work_dir(work_dir)
        if not settings_dir.is_relative_to(work_dir_real):
            raise OSError("workspace CLI settings directory changed while it was opened")
        if _resolves_into_kiro_home(settings_dir, work_dir_real):
            raise OSError(_INTO_KIRO_HOME)
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
                    0o600,
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
                    level_fd = platform_compat.pin_directory(level, links_only=True)
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
                held.append(
                    (platform_compat.pin_directory(settings_dir, links_only=True), settings_dir)
                )
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
        try:
            _exclude_from_git(work_dir)
        except OSError as exc:
            # Best effort: a failed exclude must never block the overlay itself.
            logger.debug("could not add CLI settings overlay to git exclude: %s", exc)
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


def pinned_cli_settings_fd(settings: LockedCliSettings) -> int | None:
    """Return the descriptor both cli.json reads and replacements can use."""
    if settings.settings_fd is None or not atomic_write_module.pinned_parent_replace_supported():
        return None
    return settings.settings_fd


def read_pinned_cli_json(
    settings_fd: int, cli_json: Path, *, max_bytes: int = _READ_LIMIT
) -> tuple[bytes, os.stat_result, os.stat_result] | None:
    """Read cli.json relative to a pinned settings directory without following links.

    Returns the bytes, at most ``max_bytes + 1`` so a caller can tell a file at
    the cap from one past it, with the opened file's metadata from before and
    after the read, so a caller can tell whether the version it read changed
    under it. An absent file reads as ``None``.

    Raises ``OSError`` for a link, a hardlink, a non-regular file, a file whose
    opened descriptor names a sensitive path or cannot be resolved, and one that
    cannot be read.
    """
    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    refusal = f"workspace cli.json overlay {cli_json} {CLI_JSON_REFUSED}"
    try:
        fd = os.open(
            "cli.json",
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=settings_fd,
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise OSError(refusal) from exc
    return _read_opened_cli_json(fd, refusal, max_bytes, settings_dir=None)


def read_named_cli_json(
    cli_json: Path, settings_dir: Path, *, max_bytes: int = _READ_LIMIT
) -> tuple[bytes, os.stat_result, os.stat_result] | None:
    """Read cli.json by name, for platforms without descriptor-relative opens.

    Returns what :func:`read_pinned_cli_json` returns and refuses what it refuses.
    The opened file must also sit in ``settings_dir``, the lock's resolved folder.
    A cloud-files or dedup tag on the file is admitted, as it is on the folders the
    lock holds. On Windows the handle is opened without delete sharing, so the name
    keeps this file until the read is done.
    """
    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    refusal = f"workspace cli.json overlay {cli_json} {CLI_JSON_REFUSED}"
    try:
        fd = platform_compat.open_file_no_reparse(cli_json, nonblocking=True, links_only=True)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise OSError(refusal) from exc
    return _read_opened_cli_json(fd, refusal, max_bytes, settings_dir=settings_dir)


def named_cli_json_metadata(cli_json: Path) -> os.stat_result | None:
    """The metadata :func:`read_named_cli_json` reports for the file now at ``cli_json``.

    Read from an open handle, as the reader reads it: on Windows a by-name stat and
    a handle stat do not report the same fields, so a version taken one way never
    matches one taken the other.
    """
    try:
        fd = platform_compat.open_file_no_reparse(cli_json, nonblocking=True, links_only=True)
    except FileNotFoundError:
        return None
    try:
        return os.fstat(fd)
    finally:
        os.close(fd)


def _read_opened_cli_json(
    fd: int, refusal: str, max_bytes: int, *, settings_dir: Path | None
) -> tuple[bytes, os.stat_result, os.stat_result]:
    """Validate the descriptor opened on cli.json, then read it; closes ``fd``."""
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise OSError(refusal)
        opened_path = pinned_fs.fd_real_path(fd)
        if opened_path is None or canonical_path_refusal(opened_path) is not None:
            raise OSError(refusal)
        if settings_dir is not None and not _opened_within(opened_path, settings_dir):
            raise OSError(refusal)
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "rb") as handle:
        raw = bounded_read(handle, max_bytes + 1)
        return raw, opened, os.fstat(handle.fileno())


def _opened_within(opened_path: str, settings_dir: Path) -> bool:
    """Whether the kernel's name for an opened file is directly in ``settings_dir``."""
    opened = platform_compat.strip_extended_length_prefix(opened_path)
    folder = platform_compat.strip_extended_length_prefix(str(settings_dir))
    try:
        return os.path.normcase(os.path.dirname(opened)) == os.path.normcase(folder)
    except ValueError:
        return False


def publish_locked_cli_settings(
    settings: LockedCliSettings,
    content: str | bytes,
    *,
    write: Callable[..., None],
) -> None:
    """Publish cli.json through the directory held by the settings lock where supported.

    Where pinned replace is supported the write goes through the held
    descriptor, and ``OSError`` is raised when the folder reached by name is no
    longer the one the lock holds after the write; the caller checks it before.
    Elsewhere it writes by name.
    """
    settings_fd = pinned_cli_settings_fd(settings)
    if settings_fd is None:
        write(settings.cli_json, content)
        return
    write(
        settings.cli_json,
        content,
        parent_dir_fd=settings_fd,
    )
    if not settings.holds_named_settings_dir():
        raise OSError("workspace CLI settings directory changed while the overlay was written")
