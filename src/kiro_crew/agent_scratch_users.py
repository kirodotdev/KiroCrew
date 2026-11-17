"""Which live processes still use an agent scratch tree, read from ``/proc``.

The scratch sweep (:func:`kiro_crew.agent_scratch.sweep_dead_scratch`) keeps a
tree while a process group its owner marker names is alive. A process that
left those groups cannot be seen that way: a tool that daemonized out of an
agent runtime (fork, ``setsid``, fork) and outlived it, which the kill path no
longer reaches once it has been reparented. A runtime swap leaves exactly that
behind, still holding the dead runtime's own directory as its ``TMPDIR``, and
deleting the tree under it makes every later temp write fail with ``ENOENT``.

This is the sweep's second liveness signal: the live processes whose
exec-time scratch exports, working directory or an open descriptor name a
path inside a tree. It answers on Linux only. Elsewhere nothing here lists
every process's environment, so the answer is empty and the sweep keeps its
owner-group rule alone.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from pathlib import Path

from kiro_crew import platform_compat


def scratch_users(
    root: Path, env_keys: Iterable[str], *, proc_root: Path | None = None
) -> dict[str, int]:
    """Map each direct child of *root* that a live process uses to one pid using it.

    A process uses a tree when the value of one of *env_keys* in its exec-time
    environment, its working directory, or the target of one of its open
    descriptors is a path inside it. Paths are compared lexically against
    *root* both as spelled and as resolved: the kernel reports a working
    directory or a descriptor by its resolved path, while an environment value
    keeps the spelling it was given.

    The calling process's own environment is not read, nor that of an exec
    supervisor above it (:func:`_own_images`). The kernel's copy is fixed at
    exec, so a gateway started from inside an agent shell still carries that
    shell's scratch exports there after it dropped them from ``os.environ``
    (``agent_scratch.drop_own_inherited_scratch_env``), and so does the
    launcher that runs it as a child for its whole life (``toolbox-exec``):
    reading either would keep that shell's tree for the gateway's whole life.
    Their working directories and descriptors are read like any other
    process's.

    Best effort, and a lower bound: a process whose entries cannot be read
    (another user's, or one that exited mid-read) is skipped, and a ``/proc``
    that cannot be listed answers empty. *proc_root* is a fixture seam; when
    given it is read on any host.
    """
    if proc_root is None:
        if not platform_compat.IS_LINUX:
            return {}
        proc_root = Path("/proc")
    prefixes = _prefixes(root)
    keys = frozenset(os.fsencode(key) for key in env_keys)
    try:
        names = os.listdir(proc_root)
    except OSError:
        return {}
    own = _own_images()
    users: dict[str, int] = {}
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        entry = os.path.join(proc_root, name)
        paths = list(_descriptor_targets(entry))
        cwd = _readlink(os.path.join(entry, "cwd"))
        if cwd is not None:
            paths.append(cwd)
        if pid not in own:
            paths.extend(_env_values(entry, keys))
        for path in paths:
            tree = _tree_name(path, prefixes)
            if tree:
                users.setdefault(tree, pid)
    return users


def _own_images() -> frozenset[int]:
    """This process and each exec supervisor above it.

    An exec supervisor (:func:`platform_compat.is_exec_supervisor_of_this_process`)
    is a live non-Python ancestor with only non-Python processes between: the
    launcher an in-app restart exec-ed into, or the one the gateway was started
    under, which stays its parent and keeps the environment it was started with.
    """
    pids = {os.getpid()}
    current = os.getppid()
    while current not in pids and platform_compat.is_exec_supervisor_of_this_process(current):
        pids.add(current)
        current = platform_compat.get_ppid(current)
    return frozenset(pids)


def _prefixes(root: Path) -> tuple[str, ...]:
    """*root* as spelled and as resolved, each ending in a separator."""
    spellings = {str(root), os.path.realpath(root)}
    return tuple(sorted(spelling.rstrip(os.sep) + os.sep for spelling in spellings))


def _tree_name(path: str, prefixes: tuple[str, ...]) -> str | None:
    """The first component of *path* below one of *prefixes*, or None outside them."""
    for prefix in prefixes:
        if path.startswith(prefix):
            return path[len(prefix) :].split(os.sep, 1)[0] or None
    return None


def _readlink(path: str) -> str | None:
    try:
        return os.readlink(path)
    except OSError:
        return None


def _descriptor_targets(entry: str) -> Iterator[str]:
    """What each open descriptor of the process at *entry* names, where readable."""
    fd_dir = os.path.join(entry, "fd")
    try:
        fds = os.listdir(fd_dir)
    except OSError:
        return
    for fd in fds:
        target = _readlink(os.path.join(fd_dir, fd))
        if target is not None:
            yield target


def _env_values(entry: str, keys: frozenset[bytes]) -> list[str]:
    """The non-empty values of *keys* in the exec-time environment at *entry*."""
    try:
        with open(os.path.join(entry, "environ"), "rb") as handle:
            environ = handle.read()
    except OSError:
        return []
    values: list[str] = []
    for item in environ.split(b"\0"):
        key, sep, value = item.partition(b"=")
        if sep and value and key in keys:
            values.append(os.fsdecode(value))
    return values
