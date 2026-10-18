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


def _build_child_map(proc_root: Path | None = None) -> dict[int, list[int]]:
    """Parent-PID -> direct-children map from one pass over ``/proc/<pid>/stat``.

    ``platform_compat.proc_child_map``, with "cannot list ``/proc``" (which it
    logs) and "not Linux" both answered as an empty map: the RSS watchdog then
    measures each root alone. Windows never comes here -- ``get_session_rss_mb``
    routes it through ``platform_compat.proc_rss_tree_mb_for_pid``, which
    validates every parent->child edge. *proc_root* overrides the ``/proc``
    mount (test seam only).
    """
    child_map = platform_compat.proc_child_map(proc_root=proc_root)
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

    Single-tree convenience: builds the parent->child map with one
    ``/proc/*/stat`` scan (see ``_build_child_map``) and delegates to
    ``_rss_mb_from_tree``. To measure MANY trees in one sweep, build the map
    once with ``_build_child_map()`` and call ``_rss_mb_from_tree()`` per tree so
    ``/proc`` is scanned only once, not once per tree.

    Any PID in *exclude_pids* is skipped along with the entire subtree beneath
    it — a defensive barrier so a caller can exclude a shared sub-tree (e.g. a
    pooled backend). Resident pages are summed and converted to MiB once at the
    end, so the reading is not biased downward by per-PID truncation.

    *proc_root* overrides the ``/proc`` mount (test seam only).

    Linux reads ``/proc``. Windows has neither ``/proc`` nor a safe parent->child
    walk (see ``_build_child_map``), so it delegates to
    ``platform_compat.proc_rss_tree_mb_for_pid``, which sums only
    lineage-validated descendants; without that the ceiling measured every tree
    as 0 MiB there and no session was ever recycled. macOS has no ``/proc``
    either; it sums footprints over libproc (``_off_linux_tree_mb``). Any other
    host returns 0 and logs one warning that the ceiling cannot measure.

    *exclude_pids* is honoured on the ``/proc`` and macOS routes. The Windows
    route derives its own validated descendant set, so a caller that needs a
    subtree barrier there must exclude the pid before calling.
    """
    if platform_compat.IS_WINDOWS and proc_root is None:
        tree_mb = platform_compat.proc_rss_tree_mb_for_pid(pid)
        return 0 if tree_mb is None else int(tree_mb)
    if proc_root is None and sys.platform != "linux":
        return _off_linux_tree_mb(pid, exclude_pids)
    child_map = _build_child_map(proc_root)
    return _rss_mb_from_tree(pid, child_map, exclude_pids, proc_root)
