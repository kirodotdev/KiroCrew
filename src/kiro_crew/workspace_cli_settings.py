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

import logging
import os
import stat
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from kiro_crew import pinned_fs, platform_compat

logger = logging.getLogger(__name__)

CLI_SETTINGS_LOCK_NAME = ".kirocrew-cli-settings.lock"
_EXCLUDE_COMMENT = "# Kiro Crew session settings (local only)"
_READ_LIMIT = 1 << 20
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
        text = handle.read(_READ_LIMIT + 1)
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


@contextmanager
def workspace_cli_settings_lock(
    work_dir: Path, *, timeout: float = CLI_SETTINGS_LOCK_TIMEOUT_SECS
) -> Iterator[Path]:
    """Yield a workspace ``cli.json`` path while its verified lock is held."""
    stack = ExitStack()
    try:
        settings_dir = work_dir / ".kiro" / "settings"
        settings_dir.mkdir(parents=True, exist_ok=True)
        lock_path = settings_dir / CLI_SETTINGS_LOCK_NAME
        if platform_compat.is_link_or_junction(lock_path):
            raise OSError("workspace CLI settings lock is a symlink or junction")
        lock_fd = stack.enter_context(platform_compat.open_lock_file(lock_path))
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
        try:
            _exclude_from_git(work_dir)
        except OSError as exc:
            # Best effort: a failed exclude must never block the overlay itself.
            logger.debug("could not add CLI settings overlay to git exclude: %s", exc)
        yield settings_dir / "cli.json"
    finally:
        stack.close()
