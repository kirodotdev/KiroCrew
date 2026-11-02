"""Remove a temp tree from ``/tmp`` or ``/var/tmp`` once it is provably nobody's work.

``kirocrew tmp reclaim`` is the person's way to clean up what agent sessions left
in the system temp roots -- mostly fake ``HOME`` directories for test runs, each
carrying its own uv, npm and bytecode caches. An agent cannot do it from its own
shell: the deny rules refuse ``rm -rf`` on an absolute path, and the sandbox
shows it only the few processes inside its own user namespace, so nothing it
runs can tell whether a sibling session still uses a tree. The design record is
``docs/request-for-change/rfc-agent-temp-reclaim.md``; this module is its
Phase 1, the routine and the command. No agent surface reaches it yet.

REFUSE-SAFE IS THE WHOLE DESIGN. A path is removed only when every check below
proves it may go, and every doubt keeps it:

1. It is a plain directory or regular file, never a link.
2. It is a top-level entry of a temp root, ``/tmp/<name>``, owned by this user.
   Nothing deeper is accepted: a process can look things up below a directory
   it names, so with no directory between the temp root and the target, every
   such directory is inside the target or is the temp root itself. It is not a
   mount point and holds none: its device is the temp root's, and no entry of
   ``/proc/self/mountinfo`` is at or below it.
3. Nothing anywhere in it was written in the last :data:`IDLE_SECONDS`.
4. It holds no git repository.
5. No process uses it. No working directory, root, executable, open file,
   mapped file or ``TMPDIR``/``TMP``/``TEMP`` value is inside it, and no
   ``HOME``, ``PYTHONPATH`` entry, ``XDG_*`` or ``*_CACHE_DIR`` directory is
   inside it or holds it, because a process looks things up below those. A
   link the kernel prints with `` (deleted)`` counts under both readings. A
   relative or empty entry makes every tree unprovable: the process resolved it
   against the directory it started in, which ``/proc`` does not record. A
   process of this user whose ``/proc`` entries cannot be read makes every tree
   unprovable, because it could be the one using it. The exception is a process
   the kernel marks not dumpable (ssh-agent, gpg-agent, a setuid sudo): it is
   skipped like another user's process and named in the output. A tree that
   holds a socket file is always kept, because nothing in ``/proc`` ties a live
   socket to its file, so a socket such a process bound is covered too.
6. The removal is pinned to the inode the checks saw
   (:func:`pinned_fs.remove_tree_pinned`, or a verified unlink for a file).
   Right before it the mtimes are read again through the pinned descriptor and
   the process table is read again, and afterwards the path must still name
   that inode, so a swap, a rename, a write or a run started after the checks
   removes nothing. A failure keeps the path and prints why; it never stops the run.

Linux only: the in-use check reads ``/proc``, and without it nothing is proven.
"""

from __future__ import annotations

import os
import re
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

from kiro_crew import pinned_fs
from kiro_crew.agent_scratch import (
    _is_git_directory,
    _is_plain_dir,
    _regenerable,
    _search,
    _searched_venv,
    _tree_newest_mtime,
)

#: The roots a person's leftovers live in. Anything outside them is kept.
TEMP_ROOTS: tuple[str, ...] = ("/tmp", "/var/tmp")
#: A tree written more recently than this is kept as ``recent``.
IDLE_SECONDS = 30 * 60
#: The whole run stops starting new paths after this many seconds.
DEADLINE_SECONDS = 120.0
#: Environment variables whose value names one directory the process works in.
#: Also every ``XDG_*`` and ``*_CACHE_DIR`` not in :data:`_ENV_LIST_KEYS`.
_ENV_PATH_KEYS = frozenset({"HOME", "TMPDIR", "TMP", "TEMP"})
#: Environment variables whose value is a ``:``-separated list of directories.
_ENV_LIST_KEYS = frozenset({"PYTHONPATH", "XDG_DATA_DIRS", "XDG_CONFIG_DIRS"})
#: The two kinds of reference a process makes. A ``ROOT_REF`` is a directory the
#: process looks things up under, so a path inside it is in use too; a
#: ``PATH_REF`` protects itself and what is inside it, nothing above.
PATH_REF = "path"
ROOT_REF = "root"
#: A relative or empty environment entry. The process resolved it against the
#: directory it started in, which ``/proc`` does not record, so it could name
#: any tree and makes every tree unprovable.
UNPLACED_REF = "unplaced"
#: The kernel appends this to a link whose target was deleted. A live directory
#: can also be named with it, so both readings are recorded.
_DELETED = " (deleted)"

REMOVED = "removed"
KEPT = "kept"
WOULD_REMOVE = "would-remove"


@dataclass(frozen=True)
class Outcome:
    """What happened to one requested path."""

    path: str
    outcome: str
    reason: str = ""


@dataclass(frozen=True)
class ProcessView:
    """Every path the user's live processes name, read once per run.

    ``uses`` holds ``(pid, comm, path)`` for each working directory, root,
    executable, open or mapped file, and ``roots`` the same for each directory
    an environment entry names (see :data:`ROOT_REF`). ``unreadable`` counts
    this user's processes whose entries could not be read, and ``unplaced``
    names each environment entry that cannot be placed on disk; either one
    makes every tree unprovable.
    """

    uses: tuple[tuple[int, str, str], ...]
    unreadable: int
    available: bool = True
    #: ``"<comm> <pid>"`` for each unreadable process, so a refusal can name them.
    unreadable_names: tuple[str, ...] = ()
    #: ``"<comm> <pid>"`` for each of this user's processes skipped as not dumpable.
    not_inspected: tuple[str, ...] = ()
    #: ``(pid, comm, directory)`` for each directory an environment entry names.
    roots: tuple[tuple[int, str, str], ...] = ()
    #: ``"pid <n> (<comm>) has <NAME> entry '<value>'"`` for each relative or
    #: empty environment entry (see :data:`UNPLACED_REF`).
    unplaced: tuple[str, ...] = ()


def _env_paths(raw: bytes) -> list[tuple[str, str]]:
    """The directories one ``/proc/<pid>/environ`` blob names, as ``(kind, path)``.

    Only :data:`_ENV_LIST_KEYS` are split at ``:``; every other value is one
    directory, so ``TMPDIR=/tmp/run:cache`` stays whole. ``TMPDIR``, ``TMP`` and
    ``TEMP`` are :data:`PATH_REF`: they say where new temp files go, not that
    anything already under them is in use, and they are often ``/tmp`` itself.
    The rest are :data:`ROOT_REF`: ``HOME``, a ``PYTHONPATH`` entry, an ``XDG_*``
    or ``*_CACHE_DIR`` directory is where the process looks things up, so a
    package below it is in use before it is ever opened.

    Each absolute value is resolved, because it is whatever the parent wrote:
    ``/tmp//home/.``, or a link into the tree, must still match. A relative or
    empty entry is :data:`UNPLACED_REF`, with ``"<NAME> entry '<value>'"`` as
    its text. Python resolves ``PYTHONPATH=deps``, and reads an empty entry as
    the working directory, once at startup, and ``/proc`` shows only the
    working directory the process has now. An ``XDG_*`` entry that is not
    absolute is skipped, as the XDG specification tells programs to do.
    """
    out: list[tuple[str, str]] = []
    for item in raw.split(b"\0"):
        key, sep, value = item.partition(b"=")
        if not sep or not value:
            continue
        # fsdecode, not utf-8 with "replace": a byte that is not UTF-8 must still
        # name the same file, the way os.readlink and os.path.realpath read it.
        name = os.fsdecode(key)
        text = os.fsdecode(value)
        if name in _ENV_LIST_KEYS:
            parts, kind = text.split(os.pathsep), ROOT_REF
        elif name in _ENV_PATH_KEYS:
            parts, kind = [text], PATH_REF if name != "HOME" else ROOT_REF
        elif name.startswith("XDG_") or name.endswith("_CACHE_DIR"):
            parts, kind = [text], ROOT_REF
        else:
            continue
        for part in parts:
            if part.startswith("/"):
                out.append((kind, os.path.realpath(part)))
            elif not name.startswith("XDG_"):
                out.append((UNPLACED_REF, f"{name} entry {part!r}"))
    return out


def _status_uids(proc: Path) -> tuple[int, int]:
    """The real and effective uid from ``/proc/<pid>/status``.

    Not the owner of ``/proc/<pid>``: the kernel hands that to root for any
    process that is not dumpable (ssh-agent, gpg-agent, a setuid program), which
    would make this user's own process look like someone else's and skip it.
    ``status`` is world-readable and always names the real owner.
    """
    # Bytes, split only at LF: the Name line carries the process's own comm, which
    # may hold CR or U+2028, and text-mode reading turns CR into a line break.
    for line in (proc / "status").read_bytes().split(b"\n"):
        if line.startswith(b"Uid:"):
            fields = line.split()
            return int(fields[1]), int(fields[2])
    raise ProcessLookupError(str(proc))


def _dir_owner(proc: Path) -> int:
    """The owner of ``/proc/<pid>``: root for a process the kernel marks not dumpable."""
    return proc.stat().st_uid


def _readings(name: str) -> list[str]:
    """*name* as the kernel printed it, plus without :data:`_DELETED` when it ends so.

    The kernel marks a deleted target by appending the suffix, and a directory
    can carry the same text in its real name, so the two cannot be told apart.
    """
    return [name, name.removesuffix(_DELETED)] if name.endswith(_DELETED) else [name]


def _map_paths(raw: str) -> list[str]:
    """The file paths in one ``/proc/<pid>/maps`` text (column six, absolute only).

    The kernel writes a newline in a mapped path as ``\\012``, and a file can
    carry that text in its real name, so both readings are kept, each also
    with and without :data:`_DELETED`.
    """
    out: list[str] = []
    for line in raw.split("\n"):
        fields = line.split(None, 5)
        if len(fields) == 6 and fields[5].startswith("/"):
            name = fields[5]
            out.extend(_readings(name))
            if "\\012" in name:
                out.extend(_readings(name.replace("\\012", "\n")))
    return out


def _process_paths(proc: Path) -> list[tuple[str, str]]:
    """Every path one process names, as ``(kind, path)``.

    The working directory, root, executable, open and mapped files are
    :data:`PATH_REF`; environment entries carry their own kind (see
    :func:`_env_paths`). Raises ``PermissionError`` when the process cannot be read.
    """
    links: list[str] = []
    for link in ("cwd", "root", "exe"):
        try:
            links.append(os.readlink(proc / link))
        except FileNotFoundError:
            continue
    for fd in os.listdir(proc / "fd"):
        try:
            links.append(os.readlink(proc / "fd" / fd))
        except FileNotFoundError:
            continue
    paths = [(PATH_REF, p) for link in links for p in _readings(link)]
    maps = os.fsdecode((proc / "maps").read_bytes())
    paths.extend((PATH_REF, p) for p in _map_paths(maps))
    paths.extend(_env_paths((proc / "environ").read_bytes()))
    return paths


def read_processes(proc_root: Path = Path("/proc"), uid: int | None = None) -> ProcessView:
    """Read what every live process of *uid* (default: this user) names.

    A process counts as this user's when its real or effective uid is *uid*.
    This process itself is skipped: running the command from inside a tree, or
    with ``HOME`` pointing into it, says nothing about whether anything else
    uses it. A process of another uid is skipped, because the ownership check
    already limits removal to this user's own trees, and so is one of this
    user's whose ``/proc/<pid>`` the kernel gave to another owner because it is
    not dumpable; those are listed in ``not_inspected``. A process that vanishes
    mid-read is skipped. One of this user's that still cannot be read, which is
    what another user namespace does, is counted and named, and makes every
    tree unprovable.
    """
    if not (proc_root / "self").exists():
        return ProcessView(uses=(), unreadable=0, available=False)
    uid = os.getuid() if uid is None else uid
    me = os.getpid()
    uses: list[tuple[int, str, str]] = []
    roots: list[tuple[int, str, str]] = []
    unplaced: list[str] = []
    unreadable: list[str] = []
    skipped: list[str] = []
    for name in os.listdir(proc_root):
        if not name.isdigit() or int(name) == me:
            continue
        proc = proc_root / name
        try:
            if uid not in _status_uids(proc):
                continue
            comm = os.fsdecode((proc / "comm").read_bytes()).removesuffix("\n")
            dir_owner = _dir_owner(proc)
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError:
            unreadable.append(f"? {name}")
            continue
        if dir_owner != uid:
            # The kernel gives root a not-dumpable process's /proc entry (ssh-agent,
            # gpg-agent, a setuid sudo). Its paths cannot be read, and it is treated
            # like another user's process: skipped, and named in the output. A
            # process hidden by another user namespace keeps this user as owner and
            # still counts as unreadable below.
            skipped.append(f"{comm} {name}")
            continue
        try:
            paths = _process_paths(proc)
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError:
            unreadable.append(f"{comm} {name}")
            continue
        for kind, path in paths:
            if kind == UNPLACED_REF:
                unplaced.append(f"pid {name} ({comm}) has {path}")
            else:
                (roots if kind == ROOT_REF else uses).append((int(name), comm, path))
    return ProcessView(
        uses=tuple(uses),
        unreadable=len(unreadable),
        unreadable_names=tuple(sorted(unreadable)),
        not_inspected=tuple(sorted(skipped)),
        roots=tuple(roots),
        unplaced=tuple(unplaced),
    )


def _inside(path: str, tree: str) -> bool:
    return path == tree or path.startswith(tree.rstrip("/") + "/")


def _temp_root(resolved: Path, roots: Sequence[str]) -> Path | None:
    """The temp root *resolved* sits strictly inside, or ``None``."""
    for root in roots:
        real = Path(os.path.realpath(root))
        if resolved != real and _inside(str(resolved), str(real)):
            return real
    return None


def _holds_repository(tree: Path) -> bool:
    """:func:`agent_scratch.holds_git_repository`, with a top-level ``.cache/uv`` skipped.

    uv keeps a ``.git`` inside ``.cache/uv/sdists-v10``, so every fake ``HOME``
    that ran uv would read as a repository and could never be reclaimed. Only
    ``.cache/uv`` is skipped: a clone anywhere else under ``.cache`` is still
    found. Below the first two levels the sweep's own search runs unchanged
    (:func:`agent_scratch._search`), and every doubt answers True, as it does there.
    """
    try:
        if _level_holds_repository(tree, skip=None):
            return True
        cache = tree / ".cache"
        return _is_plain_dir(cache) and _level_holds_repository(cache, skip="uv")
    except OSError:
        return True


def _level_holds_repository(directory: Path, *, skip: str | None) -> bool:
    """One level of the search: *directory* itself, then each child the sweep searches.

    ``.cache`` is never searched from the top level here, because the caller
    searches it on its own with ``uv`` skipped. Raises ``OSError`` on an
    unreadable directory; the caller turns that into True.
    """
    if _is_git_directory(directory):
        return True
    for entry in os.scandir(directory):
        child = directory / entry.name
        if entry.name == ".git":
            return True
        if entry.name == skip or (skip is None and entry.name == ".cache"):
            continue
        if not _is_plain_dir(child):
            continue
        if _regenerable(child, top_level=False) and not _searched_venv(child, top_level=False):
            continue
        if _search(child, basetemp_at_top=False):
            return True
    return False


#: This process's mount table. Removal runs in this mount namespace, so a mount
#: another namespace sees is not one the unlinks could reach.
_MOUNTINFO = Path("/proc/self/mountinfo")


def _mount_points() -> list[str] | None:
    """Every mount point in :data:`_MOUNTINFO` (field 5), or ``None`` when unreadable.

    The kernel writes a space, tab, newline or backslash in a path as a
    three-digit octal escape (``\\040``), which is decoded here.
    """
    try:
        text = os.fsdecode(_MOUNTINFO.read_bytes())
    except OSError:
        return None
    points: list[str] = []
    # Only at LF: the kernel escapes that one, and str.splitlines would also cut a
    # path at CR, U+2028 or any other line boundary the kernel leaves as is.
    for line in text.split("\n"):
        fields = line.split(" ")
        if len(fields) > 4:
            points.append(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), fields[4]))
    return points


def _mount_doubt(resolved: Path, device: int, root: Path) -> str:
    """A reason to keep *resolved* when it is, or holds, a mount point, or ``""``.

    A mount owned by this user, such as ``rclone mount`` or ``sshfs -o
    idmap=user``, passes every other check, and the pinned walk takes its
    device from the target itself, so the whole mounted filesystem would go. A
    different device from the temp root is a mount; a bind mount of the same
    filesystem keeps the device, so the mount table is read too. A mount below
    the target counts as well: the pinned walk refuses another device but not
    a bind mount of this one.
    """
    try:
        if os.stat(root).st_dev != device:
            return "outside-scope: a mount point"
    except OSError as exc:
        return f"refused: {exc}"
    points = _mount_points()
    if points is None:
        return "unprovable: the mount table could not be read"
    tree = str(resolved)
    for point in points:
        if _inside(point, tree):
            return (
                "outside-scope: a mount point"
                if point == tree
                else f"outside-scope: holds mount point {point}"
            )
    return ""


def _user(path: Path, uid: int) -> bool:
    try:
        return os.lstat(path).st_uid == uid
    except OSError:
        return False


def _use_of(tree: str, view: ProcessView) -> str:
    """The first process using *tree*, as a reason, or ``""``.

    A path a process names is a use when it is inside *tree*. A directory it
    looks things up under (``view.roots``) is a use when either holds the
    other: ``PYTHONPATH=/tmp/deps`` imports from ``/tmp/deps/pkg`` before it
    opens a file there, and ``HOME=/tmp/h/home`` lives inside ``/tmp/h``.
    """
    for pid, comm, path in view.uses:
        if _inside(path, tree):
            return f"in-use: pid {pid} ({comm})"
    for pid, comm, root in view.roots:
        if _inside(tree, root) or _inside(root, tree):
            return f"in-use: pid {pid} ({comm})"
    return ""


def _unproven(view: ProcessView) -> str:
    """Why *view* cannot prove a tree unused, or ``""`` when it can."""
    if not view.available:
        return "unprovable: no /proc to check process use"
    if view.unreadable:
        names = ", ".join(view.unreadable_names[:3])
        more = ", ..." if view.unreadable > 3 else ""
        detail = f" ({names}{more})" if names else ""
        noun = "process" if view.unreadable == 1 else "processes"
        return f"unprovable: {view.unreadable} {noun} unreadable{detail}"
    if view.unplaced:
        return (
            f"unprovable: {view.unplaced[0]}, resolved against the directory it "
            "started in, which /proc does not record"
        )
    return ""


def _holds_socket(tree: Path) -> bool:
    """Whether a socket file sits anywhere under *tree* (no link followed; doubt is True)."""
    try:
        stack = [tree]
        while stack:
            current = stack.pop()
            for entry in os.scandir(current):
                mode = os.lstat(entry.path).st_mode
                if stat.S_ISSOCK(mode):
                    return True
                if stat.S_ISDIR(mode):
                    stack.append(Path(entry.path))
    except OSError:
        return True
    return False


def _socket_doubt(tree: Path) -> str:
    """A reason to keep *tree* when it holds a socket file, or ``""``.

    Nothing readable without privilege ties a live socket to its file:
    ``/proc/net/unix`` keeps the name a socket was bound by, which may be
    relative or may have moved, and a descriptor reads only ``socket:[inode]``.
    So any tree holding a socket file is kept, and a tree with none cannot hold
    a bound socket. On the host this was built on, 1 of 1,135 top-level temp
    entries held a socket file.
    """
    return _SOCKET_REASON if _holds_socket(tree) else ""


_SOCKET_REASON = "unprovable: holds a socket file, and /proc cannot say whether it is live"


def _blocker(resolved: Path, view: ProcessView, *, is_dir: bool) -> str:
    """The first reason *view* gives to keep *resolved*, or ``""``."""
    return (
        _unproven(view)
        or _use_of(str(resolved), view)
        or (_socket_doubt(resolved) if is_dir else "")
    )


def _pinned_scan(root_fd: int) -> tuple[float, bool]:
    """``(newest mtime, holds a socket file)`` for the tree under *root_fd*.

    The approval's counterpart of :func:`agent_scratch._tree_newest_mtime` and
    :func:`_holds_socket`, read through descriptors only: each directory is
    opened relative to its parent's descriptor with ``O_NOFOLLOW``, so a tree
    renamed after it was pinned is still the tree walked, and no link is
    followed. An entry that cannot be read answers ``(+inf, True)``, which keeps it.
    """
    stack: list[int] = []
    socket = False
    try:
        newest = os.fstat(root_fd).st_mtime
        stack.append(os.dup(root_fd))
        while stack:
            fd = stack.pop()
            try:
                for name in os.listdir(fd):
                    entry = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    newest = max(newest, entry.st_mtime)
                    socket = socket or stat.S_ISSOCK(entry.st_mode)
                    if stat.S_ISDIR(entry.st_mode):
                        stack.append(os.open(name, pinned_fs.dir_flags(), dir_fd=fd))
            finally:
                os.close(fd)
    except OSError:
        return float("inf"), True
    finally:
        for fd in stack:
            os.close(fd)
    return newest, socket


def _remove(
    resolved: Path,
    info: os.stat_result,
    *,
    now: float,
    reread: Callable[[], ProcessView],
) -> Outcome:
    """Remove *resolved*, pinned to the ``(st_dev, st_ino, st_uid)`` the checks saw.

    The process table the checks used was read once for the whole run, so the
    removal re-reads it, and re-reads the mtimes, right before acting: a run
    started on the path since then keeps it. For a directory that happens inside
    the pinned approval; for a file, under a fresh ``lstat`` just before the
    pinned unlink. Every failure, an ``OSError`` from the pinned walk included,
    is reported as a kept path, never raised.
    """
    if stat.S_ISREG(info.st_mode):
        return _remove_file(resolved, info, now=now, reread=reread)

    def _approve(root_fd: int, _tree: object) -> str | None:
        opened = os.fstat(root_fd)
        same = (opened.st_dev, opened.st_ino, opened.st_uid) == (
            info.st_dev,
            info.st_ino,
            info.st_uid,
        )
        if not same:
            return "not the directory checked"
        newest, socket = _pinned_scan(root_fd)
        if now - newest < IDLE_SECONDS:
            return "written since the check"
        if socket:
            return _SOCKET_REASON
        view = reread()
        # A bind mount made inside the tree since the checks shares its device, so
        # the pinned walk would follow it; the mount table is read again.
        blocked = (
            _unproven(view)
            or _use_of(str(resolved), view)
            or _mount_doubt(resolved, opened.st_dev, resolved.parent)
        )
        if blocked:
            return blocked
        # The process table names paths, so it speaks for this tree only if the path
        # still names it after the read. A tree renamed since it was pinned is kept;
        # a process working in it under the new name would not have matched.
        try:
            here = os.lstat(resolved)
        except OSError:
            return "moved during the check"
        if (here.st_dev, here.st_ino) != (opened.st_dev, opened.st_ino):
            return "moved during the check"
        return None

    try:
        result = pinned_fs.remove_tree_pinned(
            str(resolved), what="temp tree", approve=_approve, refusal=pinned_fs.PinnedPathRefusal
        )
    except (pinned_fs.PinnedPathRefusal, OSError) as exc:
        return Outcome(str(resolved), KEPT, f"refused: {exc}")
    if result.removed:
        return Outcome(str(resolved), REMOVED)
    codes = {
        pinned_fs.REMOVAL_FAILED,
        pinned_fs.REMOVAL_IDENTITY_CHANGED,
        pinned_fs.REMOVAL_STAGE_FAILED,
        pinned_fs.REMOVAL_UNVERIFIABLE,
    }
    if result.reason not in codes:
        # The approval's own reason: nothing was touched.
        return Outcome(str(resolved), KEPT, result.reason or "refused")
    where = f", left at {resolved.parent / result.staged_name}" if result.staged_name else ""
    if result.reason == pinned_fs.REMOVAL_FAILED and result.survivors:
        return Outcome(str(resolved), KEPT, f"partial: {result.survivors} left{where}")
    return Outcome(str(resolved), KEPT, f"refused: {result.reason}{where}")


def _remove_file(
    resolved: Path,
    info: os.stat_result,
    *,
    now: float,
    reread: Callable[[], ProcessView],
) -> Outcome:
    """Unlink the regular file *resolved* after the same fresh checks a directory gets."""
    try:
        current = os.lstat(resolved)
    except OSError as exc:
        return Outcome(str(resolved), KEPT, f"refused: {exc}")
    if (current.st_dev, current.st_ino, current.st_uid) != (
        info.st_dev,
        info.st_ino,
        info.st_uid,
    ):
        return Outcome(str(resolved), KEPT, "changed: not the file checked")
    if now - current.st_mtime < IDLE_SECONDS:
        return Outcome(str(resolved), KEPT, "written since the check")
    blocked = _blocker(resolved, reread(), is_dir=False)
    if blocked:
        return Outcome(str(resolved), KEPT, blocked)
    try:
        ok = pinned_fs.unlink_verified_by_name(
            resolved.parent, resolved.name, (info.st_dev, info.st_ino)
        )
    except (pinned_fs.PinnedPathRefusal, OSError) as exc:
        return Outcome(str(resolved), KEPT, f"refused: {exc}")
    if not ok:
        return Outcome(str(resolved), KEPT, "changed: not the file checked")
    return Outcome(str(resolved), REMOVED)


def reclaim_one(
    path: str,
    view: ProcessView,
    *,
    dry_run: bool = False,
    now: float | None = None,
    roots: Sequence[str] = TEMP_ROOTS,
    uid: int | None = None,
    reread: Callable[[], ProcessView] = read_processes,
) -> Outcome:
    """Check one requested path and remove it when every check passes (see the module doc).

    *view* is the process table read once for the run; *reread* reads it again
    right before the removal, for a file and a directory alike.
    """
    uid = os.getuid() if uid is None else uid
    now = time.time() if now is None else now
    # A trailing slash makes ``lstat`` follow a link the name ends in, so it goes.
    path = path.rstrip("/") or "/"
    try:
        info = os.lstat(path)
    except OSError as exc:
        return Outcome(path, KEPT, f"missing: {exc.strerror or exc}")
    if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
        return Outcome(path, KEPT, "not a plain directory or file")
    resolved = Path(os.path.realpath(path))
    root = _temp_root(resolved, roots)
    if root is None or resolved.parent != root:
        return Outcome(
            str(resolved), KEPT, "outside-scope: not a top-level entry of " + " or ".join(roots)
        )
    if not _user(resolved, uid):
        return Outcome(str(resolved), KEPT, "outside-scope: not owned by you")
    # Before any walk: a FUSE or network mount can be slow, and its files are not temp.
    mounted = _mount_doubt(resolved, info.st_dev, root)
    if mounted:
        return Outcome(str(resolved), KEPT, mounted)
    is_dir = stat.S_ISDIR(info.st_mode)
    # An unreadable entry answers +inf, so a tree that cannot be walked reads as recent.
    newest = _tree_newest_mtime(resolved, fallback=float("inf")) if is_dir else info.st_mtime
    if newest == float("inf"):
        return Outcome(str(resolved), KEPT, "recent: part of it could not be read")
    if now - newest < IDLE_SECONDS:
        return Outcome(
            str(resolved), KEPT, f"recent: written {int(max(now - newest, 0) // 60)} min ago"
        )
    if is_dir and _holds_repository(resolved):
        return Outcome(
            str(resolved), KEPT, "holds-repository: use git worktree remove, or move the work"
        )
    blocked = _blocker(resolved, view, is_dir=is_dir)
    if blocked:
        return Outcome(str(resolved), KEPT, blocked)
    if dry_run:
        return Outcome(str(resolved), WOULD_REMOVE)
    return _remove(resolved, info, now=now, reread=reread)


def reclaim(
    paths: Iterable[str], *, dry_run: bool = False, view: ProcessView | None = None
) -> list[Outcome]:
    """Run :func:`reclaim_one` over *paths*, reading the process table once.

    One path's ``OSError`` keeps that path and moves on, so a failure late in a
    run never hides what earlier paths came to.
    """
    view = read_processes() if view is None else view
    started = time.monotonic()
    out = []
    for path in paths:
        if time.monotonic() - started > DEADLINE_SECONDS:
            out.append(Outcome(path, KEPT, "timeout: not checked"))
            continue
        try:
            out.append(reclaim_one(path, view, dry_run=dry_run))
        except OSError as exc:
            out.append(Outcome(path, KEPT, f"refused: {exc}"))
    return out


def run_command(paths: Sequence[str], *, dry_run: bool) -> None:
    """``kirocrew tmp reclaim``: print one line per path; exit 0 only when none was kept."""
    if not sys.platform.startswith("linux"):
        print("kirocrew tmp reclaim needs /proc and runs on Linux only.", file=sys.stderr)
        sys.exit(2)
    view = read_processes()
    outcomes = reclaim(paths, dry_run=dry_run, view=view)
    for o in outcomes:
        print(f"{o.outcome:<13} {o.path}" + (f"  ({o.reason})" if o.reason else ""))
    if view.not_inspected:
        names = ", ".join(view.not_inspected[:5])
        more = ", ..." if len(view.not_inspected) > 5 else ""
        print(
            f"\nNot inspected: {names}{more}. The kernel marks these processes of yours as "
            "not dumpable, so their open files cannot be read; like another user's "
            "processes they are not counted as using any path.",
            file=sys.stderr,
        )
    if view.unreadable:
        print(
            f"\n{view.unreadable} of your processes refused access to their /proc entries, "
            "so no path is proven unused. An agent sandbox does this for every process "
            "outside it; run the command from a terminal outside the sandbox.",
            file=sys.stderr,
        )
    sys.exit(1 if any(o.outcome == KEPT for o in outcomes) else 0)
