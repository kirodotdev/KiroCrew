"""Dev Fleet's per-worktree git facts cache for the fleet view.

A fleet poll reads git facts for every worktree. This module keeps them on keys
read with plain file reads and stats, so a poll with nothing changed runs no git.
It is display only: removal and discard re-read git through ``repository``.
"""

from __future__ import annotations

import asyncio
import os
import stat
import time
from pathlib import PurePosixPath
from typing import Any

from kiro_crew.apps.builtins.dev_fleet import repository
from kiro_crew.executors import subprocess_executor
from kiro_crew.platform_compat import open_file_no_reparse

#: The fleet view's per-worktree git facts, path -> entry with keys ``local``
#: (the worktree's own key), ``base`` (the base ref's key), ``info`` (a
#: ``_git_info`` result), ``ahead`` (a ``_git_ahead`` result, ``None`` until a
#: caller needs it), ``base_oid`` (the base commit ``ahead`` was counted on) and
#: ``dirty_at`` (when ``info["dirty"]`` was last read).
#: Display only. The removal and discard paths never read it: they run
#: ``_real_dirty`` / ``_dirty_split`` fresh, because those decide what is deleted.
_GIT_FACTS_CACHE: dict[str, dict[str, Any]] = {}
#: Seconds a cached entry's ``dirty`` stands before ``git status`` runs again (the
#: entry's ``dirty_at``). Before this cache a fleet pass took 330-370 s, so 300 s
#: keeps ``dirty`` as fresh as it was. ``git status`` is ~150 ms CPU per worktree:
#: with 176 worktrees that is ~9% of a core; 600 s halves it at the cost of freshness.
_DIRTY_TTL = 300.0
#: One read of the base ref for every branch at once, redone when the base ref moves:
#: ``key`` (remote, base, base key), ``oid`` and ``behind`` (refname -> commits behind).
_BASE_SNAPSHOT: dict[str, Any] = {"key": None, "oid": None, "behind": {}}
#: Old base oid -> whether it is an ancestor of the snapshot's base oid.
_BASE_ANCESTORS: dict[str, bool] = {}


#: The ``.git`` file, HEAD, a loose ref and commondir are each one short line.
_MAX_READ = 4096


class _Unsafe(Exception):
    """A git file this module will not read; the caller falls back to sandboxed git."""


def _read_text(p: str) -> str | None:
    """A small regular file's stripped text, ``None`` when it does not exist.

    The ``.git`` data is agent-writable, so this never follows a final symlink,
    never blocks on a FIFO and never reads a device or a large file: any of
    those raise ``_Unsafe``.
    """
    try:
        # Refuses a final link (POSIX O_NOFOLLOW, Windows reparse point) in the open
        # itself; nonblocking so a FIFO cannot hold the executor thread.
        fd = open_file_no_reparse(p, nonblocking=True)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as e:
        raise _Unsafe(p) from e
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _Unsafe(p)
        data = os.read(fd, _MAX_READ + 1)
    except OSError as e:
        raise _Unsafe(p) from e
    finally:
        os.close(fd)
    if len(data) > _MAX_READ:
        raise _Unsafe(p)
    try:
        return data.decode("utf-8").strip()
    except UnicodeDecodeError as e:
        raise _Unsafe(p) from e


def _stat_sig(p: str) -> tuple[int, int, int] | None:
    try:
        st = os.lstat(p)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def _inside(p: str, root: str) -> bool:
    """Whether resolved ``p`` is ``root`` or under it (``root`` already resolved)."""
    try:
        return os.path.commonpath([os.path.realpath(p), root]) == root
    except ValueError:  # another drive on Windows
        return False


def _ref_sig(common: str, ref: str) -> tuple:
    """What ``ref`` resolves to: a loose ref's oid, else the packed-refs file's stat."""
    # A refname is always '/'-separated; its parts join with the OS separator.
    p = os.path.join(common, *PurePosixPath(ref).parts)
    # An absolute or '..' ref, or a linked directory, would read outside the repo.
    if not _inside(p, common):
        raise _Unsafe(ref)
    loose = _read_text(p)
    if loose is not None:
        return ("loose", loose)
    return ("packed", _stat_sig(os.path.join(common, "packed-refs")))


def _git_facts_key(path: str, remote: str, base: str) -> tuple[tuple, tuple] | None:
    """``(local key, base key)`` over every input ``_git_info`` and ``_git_ahead`` read.

    Blocking — executor only. Plain file reads and stats, so a poll with nothing
    changed spawns no git. The local key covers the HEAD file (branch or
    detached oid), the oid the branch ref points at, and the index file's stat
    (staging, commits and git's own index refresh all rewrite it). The base key
    covers the oid ``<remote>/<base>`` points at. ``None`` means "cannot
    fingerprint" (no checkout, a reftable repo, an unreadable gitdir): the
    caller then runs git, uncached.

    Every path must sit in the managed repo's own layout: the gitdir is its
    ``.git`` or ``.git/worktrees/<name>``, the commondir is that ``.git``, and
    refs stay under it. Anything else, or any file ``_read_text`` refuses, is
    ``None`` too, so git (sandboxed, timed) reads it instead.
    """
    try:
        return _git_facts_key_checked(path, remote, base)
    except (_Unsafe, repository.RepoUnavailable, ValueError):  # ValueError: NUL in a path
        return None


def _git_facts_key_checked(path: str, remote: str, base: str) -> tuple[tuple, tuple] | None:
    expected = os.path.realpath(os.path.join(repository._repo(), ".git"))
    dotgit = os.path.join(path, ".git")
    if os.path.isdir(dotgit):
        gitdir = dotgit
    else:
        line = _read_text(dotgit)
        if not line or not line.startswith("gitdir:"):
            return None
        gitdir = os.path.join(path, line[len("gitdir:") :].strip())
    gitdir = os.path.realpath(gitdir)
    if gitdir != expected and os.path.dirname(gitdir) != os.path.join(expected, "worktrees"):
        raise _Unsafe(gitdir)
    rel = _read_text(os.path.join(gitdir, "commondir"))
    common = os.path.realpath(os.path.join(gitdir, rel)) if rel else gitdir
    if common != expected:
        raise _Unsafe(common)
    if os.path.exists(os.path.join(common, "reftable")):
        return None
    head = _read_text(os.path.join(gitdir, "HEAD"))
    if not head:
        return None
    branch_sig = _ref_sig(common, head[5:].strip()) if head.startswith("ref: ") else None
    local = (head, branch_sig, _stat_sig(os.path.join(gitdir, "index")))
    return local, _ref_sig(common, f"refs/remotes/{remote}/{base}")


async def _base_snapshot(remote: str, base: str, base_key: tuple) -> dict[str, Any]:
    """The base ref's oid and every local branch's behind count, in ONE git call.

    The base ref moves many times a day, and a behind count per worktree per move
    is 176 git calls. ``for-each-ref``'s ``ahead-behind`` atom (git 2.41+)
    counts them all in one walk. On older git, or any failure, ``behind`` is empty
    and callers count per worktree as before.
    """
    key = (remote, base, base_key)
    if _BASE_SNAPSHOT["key"] == key:
        return _BASE_SNAPSHOT
    ref = f"refs/remotes/{remote}/{base}"
    out = await repository._git(
        repository._repo(),
        "for-each-ref",
        f"--format=%(objectname) %(refname) %(ahead-behind:{ref})",
        "refs/heads/",
        ref,
    )
    oid, behind = None, {}
    for ln in (out or "").splitlines():
        parts = ln.split(" ")
        if len(parts) == 4 and parts[1] == ref:
            oid = parts[0]
        elif len(parts) == 4 and parts[3].isdigit():
            behind[parts[1]] = int(parts[3])
    # Kept under this key even when it failed, so a git without the atom costs one
    # call per base move, not one per worktree per poll.
    _BASE_ANCESTORS.clear()
    _BASE_SNAPSHOT.update(key=key, oid=oid, behind=behind if oid else {})
    return _BASE_SNAPSHOT


async def _base_moved_forward(old_oid: str | None, new_oid: str | None) -> bool:
    """Whether the base only gained commits since ``old_oid``. One git call per old oid."""
    if not old_oid or not new_oid:
        return False
    if old_oid == new_oid:
        return True
    if old_oid not in _BASE_ANCESTORS:
        ok = await repository._git(
            repository._repo(), "merge-base", "--is-ancestor", old_oid, new_oid
        )
        _BASE_ANCESTORS[old_oid] = ok is not None
    return _BASE_ANCESTORS[old_oid]


async def _refresh_dirty(path: str, ent: dict[str, Any], started: float) -> None:
    """Refresh cached dirt on its own clock, retaining the value on failure."""
    if started - ent["dirty_at"] >= _DIRTY_TTL:
        # No key sees an unstaged edit or a new untracked file. Optional locks
        # stay off so this read does not refresh the index and move the key.
        st = await repository._git(path, "--no-optional-locks", "status", "--porcelain")
        if st is not None:
            ent["info"]["dirty"] = len(st) > 0
        ent["dirty_at"] = started


async def _git_facts(path: str) -> tuple[dict, tuple | None]:
    """``(_git_info, key)`` for the fleet view, re-run only when a key changes.

    * The worktree's own key moved (commit, checkout, staging): ``_git_info``
      runs again, all of it.
    * Only the base ref moved: the behind count comes from one shared
      ``_base_snapshot`` call, so only an expired dirty read runs in this
      worktree (a detached HEAD also runs one ``rev-list --count``).

    Keys are read before AND after git runs, and the result is cached only when
    the two reads match. So a change that lands mid-read is never cached under
    the new key, and ``git status`` rewriting the index (its own refresh) only
    means the next poll reads again. A failed read is never cached.

    ``dirty`` is the exception: no key sees an unstaged edit or a new untracked
    file, so a cached entry re-reads ``git status`` once per ``_DIRTY_TTL``.

    The returned key covers both the worktree and the base ref (for caches over
    ``<base>..HEAD``, like the context log); it is ``None`` whenever nothing was
    cached.
    """
    remote = await repository._upstream_remote()
    base = repository.BASE_BRANCH
    loop = asyncio.get_running_loop()

    async def read_keys() -> tuple[tuple, tuple] | None:
        return await loop.run_in_executor(subprocess_executor(), _git_facts_key, path, remote, base)

    keys = await read_keys()
    if keys is None:
        _GIT_FACTS_CACHE.pop(path, None)
        return await repository._git_info(path), None
    local, base_key = keys
    local = (remote, base, *local)
    snap = await _base_snapshot(remote, base, base_key)
    ent = _GIT_FACTS_CACHE.get(path)
    started = time.monotonic()
    if ent is not None and ent["local"] == local and ent["base"] == base_key:
        await _refresh_dirty(path, ent, started)
        return dict(ent["info"]), (*local, base_key)
    head = local[2]
    behind = snap["behind"].get(head[5:].strip()) if head.startswith("ref: ") else None
    if ent is not None and ent["local"] == local and behind is None:
        # A detached HEAD has no branch in the snapshot: count just this one.
        out = await repository._git(path, "rev-list", "--count", f"HEAD..{remote}/{base}")
        behind = int(out) if out and out.isdigit() else None
    if ent is not None and ent["local"] == local and behind is not None:
        await _refresh_dirty(path, ent, started)
        info = {**ent["info"], "behind": behind}
        # A forward move only adds base commits, so "nothing left to ship"
        # (ahead == 0) stays true; any other count is recounted when needed.
        keep = ent["ahead"] == 0 and await _base_moved_forward(ent["base_oid"], snap["oid"])
        ahead = 0 if keep else None
        dirty_at = ent["dirty_at"]
    else:
        info = await repository._git_info(path)
        ahead = None
        dirty_at = started
    if info.get("head_oid") is None or await read_keys() != keys:
        _GIT_FACTS_CACHE.pop(path, None)
        return info, None
    _GIT_FACTS_CACHE[path] = {
        "local": local,
        "base": base_key,
        "info": dict(info),
        "ahead": ahead,
        "base_oid": snap["oid"],
        "dirty_at": dirty_at,
    }
    return info, (*local, base_key)


async def _git_ahead_cached(path: str) -> int | None:
    """``_git_ahead`` (``git cherry``), counted once per ``_git_facts`` entry.

    ``git cherry`` costs over a second in a worktree far behind the base, so the
    fleet view asks for it only on the rows that need it. Uncached when
    ``_git_facts`` holds no entry for ``path``.
    """
    ent = _GIT_FACTS_CACHE.get(path)
    if ent is None:
        return await repository._git_ahead(path)
    if ent["ahead"] is None:
        ahead = await repository._git_ahead(path)
        if _GIT_FACTS_CACHE.get(path) is ent:
            ent["ahead"] = ahead
        return ahead
    return ent["ahead"]


def _git_facts_retain(paths: set[str]) -> None:
    """Keep cached facts only for the worktrees in ``paths`` (the discovered set)."""
    for p in [p for p in _GIT_FACTS_CACHE if p not in paths]:
        del _GIT_FACTS_CACHE[p]
