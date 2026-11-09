"""Resident memory of a session's process tree, which the session RSS ceiling compares.

Linux sums ``/proc/<pid>/statm`` pages over one child map; macOS sums libproc footprints;
Windows asks the platform layer; any other host measures nothing and says so once.

Split out of ``kiro_crew.session_pid``, which stays the import path and the patch target:
every name here is reached as ``kiro_crew.session_pid.<name>``, and a patch through that
module reaches this one (``kiro_crew._facade_mirror``). Nothing here imports it.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import NamedTuple

from kiro_crew import platform_compat

#: The family logs as ``kiro_crew.session_pid``, the name callers, log filters and
#: ``caplog`` assertions use.
logger = logging.getLogger("kiro_crew.session_pid")


_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


def _read_rss_pages(pid: int, proc_root: Path | None = None) -> int:
    """Resident *pages* of a single PID via ``/proc/<pid>/statm`` (Linux only).

    Returns pages, NOT MiB: callers accumulate the whole process tree and
    convert to MiB once at the end, so per-PID sub-MiB remainders are not
    truncated away. (A per-PID ``// MiB`` would under-count a tree by up to
    ~1 MiB per process, i.e. the recycle could fire late or never for a tree
    sitting just over the ceiling.) Returns 0 if the process is gone or the
    field can't be read — a missing PID simply contributes nothing to the sum.

    Windows never reaches here: ``get_session_rss_mb`` measures whole trees
    through ``platform_compat.proc_rss_tree_mb_for_pid`` instead.

    *proc_root* overrides the ``/proc`` mount (test seam only).
    """
    root = proc_root if proc_root is not None else Path("/proc")
    try:
        # statm fields are in pages; field 2 (index 1) is resident set size.
        fields = (root / str(pid) / "statm").read_text().split()
        return int(fields[1])
    except (FileNotFoundError, ProcessLookupError, ValueError, IndexError, OSError):
        return 0


def _build_child_map(
    proc_root: Path | None = None, *, session_members: bool = False
) -> dict[int, list[int]]:
    """Parent-PID -> direct-children map from one pass over ``/proc/<pid>/stat``.

    ``session_members=True`` is the RSS ceiling's map: it also hangs each process
    under its session leader, so a job a timed-out shell command left running
    (reparented to init, still in the runtime's session) is counted in that
    runtime's tree (``platform_compat.proc_child_map``). Measurement only; the
    kill paths call this without it.

    ``platform_compat.proc_child_map``, with "cannot list ``/proc``" (which it
    logs) and "not Linux" both answered as an empty map: the RSS watchdog then
    measures each root alone. Windows never comes here -- ``get_session_rss_mb``
    routes it through ``platform_compat.proc_rss_tree_mb_for_pid``, which
    validates every parent->child edge. *proc_root* overrides the ``/proc``
    mount (test seam only).
    """
    child_map = platform_compat.proc_child_map(proc_root=proc_root, session_members=session_members)
    return {} if child_map is None else child_map


_rss_ceiling_inert_warned = False


def _warn_rss_ceiling_inert_once(reason: str) -> None:
    """Say once per process that the session RSS ceiling cannot measure here.

    Without it an unsupported host reads every tree as 0 MiB and silently never
    recycles a session, however large it grows.
    """
    global _rss_ceiling_inert_warned
    if _rss_ceiling_inert_warned:
        return
    _rss_ceiling_inert_warned = True
    logger.warning(
        "session RSS ceiling cannot measure on this host (%s): every tree reads 0 MiB "
        "and no session is recycled for memory",
        reason,
    )


def _off_linux_tree_mb(pid: int, exclude_pids: set[int]) -> int:
    """Tree reading (MiB) where there is no ``/proc``: macOS walks libproc.

    It does not reuse the runtime watchdog's ``acp.runtime._get_rss_tree_mb``:
    this module sits outside the agent-SDK boundary, and
    ``scripts/check_agent_sdk_boundary.py`` refuses new ACP-layer imports here.

    Any other host, or a Mac whose libproc does not load, cannot measure; it
    answers 0 and warns once. Windows never comes here (see ``get_session_rss_mb``).
    """
    if sys.platform != "darwin":
        _warn_rss_ceiling_inert_once(f"platform {sys.platform}")
        return 0
    if not platform_compat.darwin_libproc_available():
        _warn_rss_ceiling_inert_once("libproc unavailable")
        return 0
    tree_mb = platform_compat.darwin_footprint_tree_mb(pid, exclude_pids)
    return 0 if tree_mb is None else int(tree_mb)


def _rss_mb_from_tree(
    pid: int,
    child_map: dict[int, list[int]],
    exclude_pids: set[int] = frozenset(),  # type: ignore[assignment]
    proc_root: Path | None = None,
) -> int:
    """RSS (MiB) of *pid* + its descendant tree using a PREBUILT child map.

    Split out from ``get_session_rss_mb`` so a caller measuring many session
    trees in one sweep can build the ``/proc`` parent->child map ONCE (via
    ``_build_child_map``) and reuse it across every tree, rather than re-scanning
    all of ``/proc`` per tree. The map is read-only here, so it is safe to share
    across sequential/threaded calls. Any PID in *exclude_pids* is skipped along
    with its subtree. Resident pages are summed across the tree and converted to
    MiB once at the end.

    Off Linux (no *proc_root*) the map is not used: macOS sums each process's
    footprint over libproc's live child lists instead (``_off_linux_tree_mb``),
    honouring *exclude_pids* the same way.
    """
    if proc_root is None and sys.platform != "linux":
        return _off_linux_tree_mb(pid, exclude_pids)
    total_pages = 0
    seen: set[int] = set()
    frontier = [pid]
    while frontier:
        current = frontier.pop()
        if current in seen or current in exclude_pids:
            continue
        seen.add(current)
        total_pages += _read_rss_pages(current, proc_root)
        frontier.extend(child_map.get(current, ()))
    return (total_pages * _PAGE_SIZE) // (1024 * 1024)


def get_session_rss_mb(
    pid: int,
    exclude_pids: set[int] = frozenset(),  # type: ignore[assignment]
    proc_root: Path | None = None,
) -> int:
    """Total RSS (MiB) of *pid* plus its descendant tree, via ``/proc``.

    Single-tree convenience over ``_build_child_map`` + ``_rss_mb_from_tree``; to
    measure many trees, build the map once and call ``_rss_mb_from_tree()`` per tree
    so ``/proc`` is scanned once.

    A PID in *exclude_pids* is skipped with its whole subtree (e.g. a pooled
    backend). Pages are converted to MiB once at the end, so per-PID truncation does
    not bias the reading down. *proc_root* is a test seam for the ``/proc`` mount.

    Windows has no ``/proc`` or safe parent->child walk, so it uses
    ``platform_compat.proc_rss_tree_mb_for_pid`` (lineage-validated descendants
    only; a 0 MiB reading would never recycle a session). macOS sums libproc
    footprints (``_off_linux_tree_mb``). Other hosts return 0 and warn once.
    *exclude_pids* is not honoured on Windows: exclude the pid before calling.
    """
    if platform_compat.IS_WINDOWS and proc_root is None:
        tree_mb = platform_compat.proc_rss_tree_mb_for_pid(pid)
        return 0 if tree_mb is None else int(tree_mb)
    if proc_root is None and sys.platform != "linux":
        return _off_linux_tree_mb(pid, exclude_pids)
    child_map = _build_child_map(proc_root)
    return _rss_mb_from_tree(pid, child_map, exclude_pids, proc_root)


#: Longest process name a leftover report carries.
_LEFTOVER_NAME_MAX = 32


class Leftover(NamedTuple):
    """One process in a runtime's session that its process tree does not reach."""

    pid: int
    start_id: str
    rss_mb: int
    name: str


class SessionLeftovers(NamedTuple):
    """What :func:`find_session_leftovers` read for one runtime root."""

    root_start: str
    items: list[Leftover]


def _session_member_pids(sid: int) -> set[int] | None:
    """Running members of session *sid*, or None where they cannot be read."""
    if platform_compat.IS_LINUX:
        members = platform_compat.linux_session_members(sid)
        return None if members is None else set(members)
    if platform_compat.IS_MACOS:
        pids = platform_compat.darwin_session_pids(sid)
        return None if pids is None else set(pids)
    return None


def _live_tree(root: int) -> set[int]:
    """*root* and the descendants its parent edges reach right now."""
    seen: set[int] = set()
    frontier = [root]
    child_map = _build_child_map() if platform_compat.IS_LINUX else None
    while frontier:
        current = frontier.pop()
        if current in seen:
            continue
        seen.add(current)
        if child_map is not None:
            frontier.extend(child_map.get(current, ()))
        else:
            frontier.extend(platform_compat.darwin_child_pids(current) or ())
    return seen


def _process_mb(pid: int) -> int:
    """Resident MiB of one process: statm pages on Linux, the footprint on macOS."""
    if platform_compat.IS_LINUX:
        return (_read_rss_pages(pid) * _PAGE_SIZE) // (1024 * 1024)
    footprint = platform_compat.proc_phys_footprint_bytes_for_pid(pid)
    return 0 if footprint is None else footprint // (1024 * 1024)


def _process_label(pid: int) -> str:
    """A short, plain name for *pid*, safe to put in a log line or a chat notice.

    The name is whatever the process set for itself, so only letters, digits and
    ``._+-`` are kept, cut to :data:`_LEFTOVER_NAME_MAX`.
    """
    if platform_compat.IS_LINUX:
        raw = platform_compat.linux_process_name(pid) or ""
    else:
        raw = os.path.basename(platform_compat.darwin_process_path(pid) or "")
    kept = "".join(ch for ch in raw if ch.isascii() and (ch.isalnum() or ch in "._+-"))
    return kept[:_LEFTOVER_NAME_MAX] or "?"


def find_session_leftovers(root: int) -> SessionLeftovers | None:
    """The processes in runtime *root*'s session that its process tree does not reach.

    A runtime is spawned as a session leader, so its pid is its session id. A job
    a timed-out shell command left running is reparented to init but keeps that
    id, and so does one started under ``set -m``, which also left the runtime's
    process group. Those are the leftovers: session members outside the live
    parent tree of *root*.

    Read only while *root* itself is alive, and only then trusted: the session
    is listed, each leftover's start identity is read, then the session and the
    tree are read again, keeping only processes in both session listings and
    outside both trees, and only when *root* is still the process it was. A pid
    that exited and was reused in between can then only be a new process in the
    same session and outside the tree, which is a leftover in its own right.

    None when *root* cannot be identified or the session cannot be read (Windows
    has no sessions). Blocking: executor thread, never the loop.
    """
    if type(root) is not int or root <= 1 or root == os.getpid():
        return None
    root_start = platform_compat.get_process_start_id(root)
    if root_start is None:
        return None
    members = _session_member_pids(root)
    if members is None:
        return None
    outside = sorted(members - _live_tree(root) - {root, os.getpid()})
    starts = {pid: platform_compat.get_process_start_id(pid) for pid in outside}
    again = _session_member_pids(root) or set()
    tree_after = _live_tree(root)
    if platform_compat.get_process_start_id(root) != root_start:
        return None
    items = [
        Leftover(pid, start, _process_mb(pid), _process_label(pid))
        for pid, start in starts.items()
        if start and pid in again and pid not in tree_after
    ]
    return SessionLeftovers(root_start, items)


def end_session_leftovers(sid: int, leftovers: SessionLeftovers, *, only_if_root_gone: bool) -> int:
    """End the *leftovers* still in session *sid*; return how many ended.

    Each process is signalled through ``platform_compat.terminate_pids_pinned``,
    pinned to the start identity :func:`find_session_leftovers` read, after the
    session is listed once more. With *only_if_root_gone* nothing is signalled
    while *sid*'s leader is still the process it was: a runtime another session
    still uses keeps its processes. Linux only, like the pinned terminator;
    elsewhere 0. Blocking: executor thread, never the loop.
    """
    if not leftovers.items:
        return 0
    if only_if_root_gone and platform_compat.get_process_start_id(sid) == leftovers.root_start:
        return 0
    members = _session_member_pids(sid) or set()
    targets = [(item.pid, item.start_id) for item in leftovers.items if item.pid in members]
    if not targets:
        return 0
    return platform_compat.terminate_pids_pinned(targets, protected_pids=(sid,))
