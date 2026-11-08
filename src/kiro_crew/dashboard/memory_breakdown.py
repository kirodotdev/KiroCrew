"""Everything Kiro Crew runs on this host, grouped, for the Sessions tab's total.

The rows of ``GET /api/sessions/memory`` measure agent runtimes only. The total
under them is the figure an operator holds up against the OS's own number for
the app (Activity Monitor and the macOS force-quit dialog both sum every process
the app started), so it covers everything the product runs on the host, in
:data:`CATEGORY_ORDER`:

* ``sessions``: the live sessions' runtimes, the figure the rows already sum.
* ``warm_pool``: runtimes started ahead of a chat (``session.pool_size``).
* ``other_agents``: runtimes still spawning, companion runtimes and task runtimes.
* ``mcp_gateway``: the MCP gateway daemon and the shared servers under it.
* ``gateway``: this process alone. The local embedding model is loaded here.
* ``gateway_helpers``: everything else this process started (app backends, Dev
  Fleet tunnels, worker interpreters).
* ``desktop``: the desktop shell, when this gateway runs from a desktop bundle.

Each process is attributed ONCE, to the first category in that order whose roots
reach it, so the categories add up to the total. The gateway and its helpers are
claimed before the desktop shell because the shell can be the gateway's parent:
walking the shell first would hand it the whole gateway tree.

Every root is walked through ONE host parent map, the map the session rows are
walked through on the same poll, and every process is read the way the rows read
it: ``phys_footprint`` on macOS (the figure Activity Monitor reports, compressed
pages included), the resident set elsewhere. Without a parent map (Windows) the
roots are measured as whole trees and the two categories that need the map, the
gateway's helpers and the desktop shell, are reported as unmeasured.

A process is named by its executable's base name only, and no command line is
ever read here: argv can carry tokens. Finding and verifying the MCP gateway
daemon is ``daemon_control``'s job, and this module asks it.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Optional

from kiro_crew.mcp_gateway.daemon_control import _is_daemon_process, describe_daemon
from kiro_crew.platform_compat import (
    BUNDLED_BACKEND_DIST_DIRNAME,
    is_bundled_interpreter,
    linux_process_name,
    process_executable_path,
)

logger = logging.getLogger(__name__)

#: Attribution order, which is also the order the page lists them in.
CATEGORY_ORDER: tuple[str, ...] = (
    "sessions",
    "warm_pool",
    "other_agents",
    "mcp_gateway",
    "gateway",
    "gateway_helpers",
    "desktop",
)

#: Executable names listed per category, largest first. Enough to tell seven
#: ssh tunnels from one interpreter without turning the panel into a process list.
_TOP_NAMES = 4

_MIB = 1024.0 * 1024.0


@dataclass(frozen=True)
class Roots:
    """The processes each manager-owned category starts from."""

    sessions: frozenset[int] = frozenset()
    warm_pool: frozenset[int] = frozenset()
    other_agents: frozenset[int] = frozenset()
    mcp_gateway: frozenset[int] = frozenset()


def _posix_path(path: str) -> str:
    return path.replace("\\", "/")


def _dir_prefix(parts: Sequence[str]) -> str:
    """*parts* as a case-folded directory prefix, trailing separator included, so
    a prefix test never matches a sibling whose name merely starts the same."""
    return str(PurePosixPath(*parts) / "_").casefold()[:-1]


def desktop_shell_matcher(executable: str) -> Optional[Callable[[str], bool]]:
    """A test for "this executable is part of the desktop shell", or None.

    The layout is read the way ``cli_server_stop._desktop_supervisor_pid`` reads
    it, off :data:`platform_compat.BUNDLED_BACKEND_DIST_DIRNAME`: the gateway runs
    from ``<app>/<resources>/<that directory>/``, and the desktop shell is every
    executable under ``<app>`` outside that directory (on macOS, the app, its
    helpers and the crash handler under ``Contents/MacOS`` and
    ``Contents/Frameworks``). None when *executable* (this gateway's
    interpreter) is not inside such a bundle. Paths compare case-folded, as there:
    macOS and Windows paths are case-insensitive.
    """
    segments = PurePosixPath(_posix_path(executable)).parts
    try:
        bundle_at = segments[:-1].index(BUNDLED_BACKEND_DIST_DIRNAME)
    except ValueError:
        return None
    # Two segments above the directory at the least (``<app>/<resources>/``),
    # and an ``<app>`` that is not the filesystem root: an app root of ``/``
    # would make every process on the host part of the desktop shell.
    if bundle_at < 3:
        return None
    app_root = _dir_prefix(segments[: bundle_at - 1])
    bundle_root = _dir_prefix(segments[: bundle_at + 1])

    def _in_shell(exe: str) -> bool:
        candidate = _posix_path(exe).casefold()
        return candidate.startswith(app_root) and not candidate.startswith(bundle_root)

    return _in_shell


def own_desktop_shell_matcher() -> Optional[Callable[[str], bool]]:
    """:func:`desktop_shell_matcher` for this process, or None off the bundle.

    Whether this gateway runs from the desktop bundle at all is
    :func:`platform_compat.is_bundled_interpreter`'s answer, the one predicate
    for it.
    """
    if not is_bundled_interpreter():
        return None
    try:
        executable = str(Path(sys.executable).resolve())
    except (OSError, ValueError):
        executable = sys.executable
    return desktop_shell_matcher(executable)


#: The last daemon ``daemon_control`` confirmed, as the ``(pid, start_time)`` its
#: pong reported. The confirmation reads the pid's command line, which spawns a
#: process on macOS (``ps``) and Windows (CIM), so it runs once per daemon rather
#: than on every Sessions-tab poll; a new pid or start time is checked afresh.
_confirmed_daemon: Optional[tuple[int, str]] = None


def mcp_daemon_roots(spawned_pid: Optional[int], socket_path: Optional[Path]) -> frozenset[int]:
    """The MCP gateway daemon's pid, as a root set. BLOCKING (pings the daemon).

    A daemon this gateway spawned is known by its handle. One it adopted (the
    daemon outlives a gateway restart by design) is asked for, the way every
    other reader asks: :func:`daemon_control.describe_daemon` pings the socket
    and the pong names the daemon's pid, and
    :func:`daemon_control._is_daemon_process` confirms that pid runs the daemon
    before anything is attributed to it. No answer, or one that fails the
    check, yields no root, and the category then reads as empty.
    """
    if isinstance(spawned_pid, int) and spawned_pid > 1:
        return frozenset({spawned_pid})
    if socket_path is None:
        return frozenset()
    global _confirmed_daemon
    info = describe_daemon(socket_path)
    if info is None or info.pid <= 1:
        return frozenset()
    identity = (info.pid, info.start_time)
    if not (info.start_time and _confirmed_daemon == identity):
        if not _is_daemon_process(info.pid):
            return frozenset()
        if info.start_time:
            _confirmed_daemon = identity
    return frozenset({info.pid})


def process_name(pid: int) -> Optional[str]:
    """The base name of *pid*'s executable, or its kernel name on Linux."""
    exe = process_executable_path(pid)
    if exe:
        return PurePosixPath(_posix_path(exe)).name or None
    return linux_process_name(pid)


def _known_pids(children: Mapping[int, Sequence[int]]) -> set[int]:
    known: set[int] = set(children)
    for kids in children.values():
        known.update(kids)
    return known


def attribute(
    roots: Roots,
    children: Mapping[int, Sequence[int]],
    *,
    gateway_pid: int,
    executable: Callable[[int], Optional[str]] = process_executable_path,
    desktop: Optional[Callable[[str], bool]] = None,
) -> dict[str, list[int]]:
    """Which processes each category owns, every pid in at most one category.

    Only pids the parent map knows are attributed: a root that has exited since
    the manager last saw it contributes nothing rather than a phantom entry.
    """
    known = _known_pids(children)
    taken: set[int] = set()

    def claim(starts: Iterable[int], *, descend: bool = True) -> list[int]:
        owned: list[int] = []
        stack = sorted((p for p in starts if p in known), reverse=True)
        while stack:
            pid = stack.pop()
            if pid in taken:
                continue
            taken.add(pid)
            owned.append(pid)
            if descend:
                stack.extend(c for c in children.get(pid, ()) if c not in taken)
        return owned

    groups: dict[str, list[int]] = {
        "sessions": claim(roots.sessions),
        "warm_pool": claim(roots.warm_pool),
        "other_agents": claim(roots.other_agents),
        "mcp_gateway": claim(roots.mcp_gateway),
        "gateway": claim((gateway_pid,), descend=False),
        "gateway_helpers": claim(children.get(gateway_pid, ())),
    }
    shell: list[int] = []
    if desktop is not None:
        for pid in sorted(known - taken):
            exe = executable(pid)
            if exe and desktop(exe):
                shell.append(pid)
    groups["desktop"] = claim(shell)
    return groups


def _top_names(
    pids: Sequence[int], sizes: Mapping[int, int], name: Callable[[int], Optional[str]]
) -> list[dict[str, object]]:
    by_name: dict[str, list[int]] = {}
    for pid in pids:
        label = name(pid)
        if not label:
            continue
        entry = by_name.setdefault(label, [0, 0])
        entry[0] += 1
        entry[1] += sizes.get(pid, 0)
    ranked = sorted(by_name.items(), key=lambda item: (-item[1][1], item[0]))
    return [
        {"name": label, "procs": count, "mb": round(total / _MIB, 1)}
        for label, (count, total) in ranked[:_TOP_NAMES]
    ]


def _category(
    category: str, mb: Optional[float], procs: Optional[int], top: list[dict[str, object]]
) -> dict[str, object]:
    return {
        "id": category,
        "mb": round(mb, 1) if mb is not None else None,
        "procs": procs,
        "top": top,
    }


def build(
    roots: Roots,
    *,
    sessions_mb: float,
    gateway_pid: int,
    children: Optional[Mapping[int, Sequence[int]]],
    memory_bytes: Callable[[int], Optional[int]],
    tree_mb: Callable[[int], Optional[float]],
    name: Callable[[int], Optional[str]] = process_name,
    executable: Callable[[int], Optional[str]] = process_executable_path,
    desktop: Optional[Callable[[str], bool]] = None,
    host_mb: Optional[float] = None,
) -> dict[str, object]:
    """The breakdown payload. BLOCKING: reads every attributed process.

    ``sessions_mb`` is the total the session rows already measured, used as the
    sessions figure so the panel and the rows can never disagree about it.
    ``tree_mb`` measures a whole tree from its root, for the host without a
    parent map. ``partial`` is true when a category could not be measured.
    """
    categories: list[dict[str, object]] = []
    partial = False
    if children is not None:
        groups = attribute(
            roots, children, gateway_pid=gateway_pid, executable=executable, desktop=desktop
        )
        for category in CATEGORY_ORDER:
            pids = groups.get(category, [])
            sizes = {pid: size for pid in pids if (size := memory_bytes(pid)) is not None}
            if category == "sessions":
                mb: float = sessions_mb
            else:
                mb = sum(sizes.values()) / _MIB
            if not pids and mb <= 0:
                continue
            categories.append(_category(category, mb, len(pids), _top_names(pids, sizes, name)))
    else:
        partial = True
        # A root goes to the first category in CATEGORY_ORDER that holds it, as
        # attribute() claims: one pid can sit in two root sets at once (a swept
        # warm-pool runtime a chat has since claimed), and measuring its tree
        # twice would add up to more than the host holds.
        held = roots.sessions
        per_root: dict[str, frozenset[int]] = {}
        for category, starts in (
            ("warm_pool", roots.warm_pool),
            ("other_agents", roots.other_agents),
            ("mcp_gateway", roots.mcp_gateway),
        ):
            per_root[category] = starts - held
            held |= starts
        if sessions_mb > 0:
            categories.append(_category("sessions", sessions_mb, None, []))
        for category, starts in per_root.items():
            readings = [tree_mb(pid) for pid in sorted(starts)]
            measured = [r for r in readings if r is not None]
            if measured:
                categories.append(_category(category, sum(measured), None, []))
        own = memory_bytes(gateway_pid)
        categories.append(_category("gateway", own / _MIB if own is not None else None, 1, []))
        categories.append(_category("gateway_helpers", None, None, []))
        if desktop is not None:
            categories.append(_category("desktop", None, None, []))

    total_mb = 0.0
    for entry in categories:
        part = entry["mb"]
        if isinstance(part, float):
            total_mb += part
    return {
        "total_mb": round(total_mb, 1),
        "host_pct": round(total_mb / host_mb * 100, 2) if host_mb else None,
        "partial": partial,
        "categories": categories,
    }
