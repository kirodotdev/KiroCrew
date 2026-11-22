# SPDX-License-Identifier: Apache-2.0
"""The commits a worktree's own git folder names, for the removability check.

A worktree's ``HEAD`` reflog, its per-worktree refs and their reflogs, and its
``ORIG_HEAD`` and ``FETCH_HEAD`` live in its own git folder and go when it is
removed. :func:`kiro_crew.crewmate_worktree.is_provably_disposable` keeps the
worktree unless every commit they name is reached from somewhere that stays. The
files are read here, never through a link: the git folder sits in an
agent-writable tree, so a link planted there would make the gateway read whatever
it points at. A link anywhere on the way answers ``None``, and the caller keeps
the worktree.
"""

from __future__ import annotations

import os
from pathlib import Path

from kiro_crew.jsonl_util import UnreadableRecord, open_regular_nofollow, strict_records

#: Largest reflog, ``ORIG_HEAD`` or ``FETCH_HEAD`` file read; a bigger one keeps the worktree.
_MAX_FILE_BYTES = 64 * 1024 * 1024
#: The worktree's own files naming a commit outside any reflog.
_PSEUDO_REFS = ("ORIG_HEAD", "FETCH_HEAD")


def history_commits(wt, dest: str) -> set[str] | None:
    """Every commit the worktree *dest*'s own git folder names; ``None`` when it cannot be read.

    Both endpoints of every entry of its own reflogs (``logs/``, the only place git
    keeps an entry's old endpoint), and the commits ``ORIG_HEAD`` and every line of
    ``FETCH_HEAD`` name. ``None`` keeps the worktree: a repository whose refs are not
    stored as files (``reftable``, so no reflog files to read), a link in ``logs/``
    or in place of a file, or a file that cannot be read whole.
    """
    fmt = wt._run_git(["rev-parse", "--show-ref-format"], dest)
    if fmt.returncode != 0 or not _stores_refs_as_files(fmt.stdout.strip()):
        return None
    gitdir = wt._run_git(["rev-parse", "--absolute-git-dir"], dest)
    folder = gitdir.stdout.strip() if gitdir.returncode == 0 else ""
    if not folder:
        return None
    found: set[str] = set()
    try:
        for name in _PSEUDO_REFS:
            lines = _lines(Path(folder, name))
            if lines is not None:
                found.update(line.split(None, 1)[0] for line in lines if line.strip())
        logs = Path(folder, "logs")
        if logs.is_symlink():
            return None
        if not logs.is_dir():
            return found  # no reflog was ever written here
        for current, dirs, files in os.walk(logs, followlinks=False, onerror=_raise):
            if any(Path(current, d).is_symlink() for d in dirs):
                return None
            for name in files:
                lines = _lines(Path(current, name))
                for line in lines or ():
                    found.update(e for e in line.split(" ", 2)[:2] if e.strip("0"))
    except (OSError, UnreadableRecord):
        return None
    return found


def _stores_refs_as_files(answer: str) -> bool:
    """Whether ``rev-parse --show-ref-format`` printed *answer* for a repository whose refs are files.

    A git older than 2.45 does not know the option and prints it back; it knows
    no ref format but files, so that echo answers ``True``, as ``files`` does.
    Any other format keeps the worktree.
    """
    return answer in ("files", "--show-ref-format")


def _lines(path: Path) -> list[str] | None:
    """*path*'s lines when it is a regular file opened without following a link, ``None`` when absent.

    A link, a non-regular file or one past :data:`_MAX_FILE_BYTES` raises :class:`OSError`.
    """
    try:
        with open_regular_nofollow(path, max_bytes=_MAX_FILE_BYTES) as fh:
            return list(strict_records(fh, path))
    except FileNotFoundError:
        return None


def _raise(error: OSError) -> None:
    raise error
