"""A process's descendants.

Walked from one snapshot of the host's parent map, each start identity bound to its
parentage, with the bounds that keep a Windows Toolhelp snapshot finite.

One of the owners ``kiro_crew.platform_compat`` is split into: every name here stays
readable and patchable as ``kiro_crew.platform_compat.<name>``.
"""

from __future__ import annotations

import ctypes.util
import enum
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Mapping, NamedTuple

from kiro_crew.platform_compat_identity import (
    ProcessIdentitySource,
    get_process_start_id,
    get_process_start_identity,
    process_start_time,
)
from kiro_crew.platform_compat_native import (
    IS_LINUX,
    IS_POSIX,
    IS_WINDOWS,
    _ProcessEntry32,
    _windows_last_error,
)
from kiro_crew.platform_compat_proctable import ProcessStartIdentity, darwin_child_pids
from kiro_crew.platform_compat_trust import trusted_system_bin


def _descendants_from_parent_map(
    root_pid: int, parent_map: dict[int, int], *, limit: int | None = None
) -> list[int]:
    """Return a breadth-first descendant list from a PID -> PPID snapshot.

    Sibling PIDs are in ascending order; ``limit`` bounds the result.
    """

    result: list[int] = []
    frontier = [root_pid]
    seen = {root_pid}
    while frontier:
        parents = set(frontier)
        frontier = []
        for child_pid, parent_pid in sorted(parent_map.items()):
            if parent_pid in parents and child_pid not in seen:
                if limit is not None and len(seen) >= limit:
                    raise _WindowsTreeOverflow("Windows cleanup identity capacity exceeded")
                seen.add(child_pid)
                result.append(child_pid)
                frontier.append(child_pid)
    return result


def _posix_process_parent_map() -> dict[int, int]:
    """Return the PID -> PPID view of the shared POSIX process snapshot."""

    processes = _posix_process_snapshot()
    if processes is None:
        return {}
    return {process_pid: process.ppid for process_pid, process in processes.items()}


class ProcessDescendantIdentity(NamedTuple):
    """A descendant PID bound to its parent, start identity, and source."""

    pid: int
    ppid: int
    start_time: str
    source: ProcessIdentitySource = ProcessIdentitySource.ATOMIC


class _ProcessStartOrder(enum.Enum):
    """Creation order for one process ancestry edge."""

    LATER = "later"
    EARLIER = "earlier"
    INCONCLUSIVE = "inconclusive"


# Named by the path callers import it from, so a traceback, a pickle or any
# ``module.qualname`` rendering of the class says ``kiro_crew.platform_compat``.
_ProcessStartOrder.__module__ = "kiro_crew.platform_compat"


def _process_start_order(
    child_token: str,
    parent_token: str,
) -> _ProcessStartOrder:
    """Compare process-start identities without collapsing uncertainty."""

    def _numeric_parts(token: str) -> tuple[int, str] | None:
        whole, separator, fraction = token.partition(".")
        if not whole.isdigit() or (separator and not fraction.isdigit()):
            return None
        return int(whole), fraction

    child_numeric = _numeric_parts(child_token)
    parent_numeric = _numeric_parts(parent_token)
    if child_numeric is not None or parent_numeric is not None:
        if child_numeric is None or parent_numeric is None:
            return _ProcessStartOrder.INCONCLUSIVE
        child_whole, child_fraction = child_numeric
        parent_whole, parent_fraction = parent_numeric
        if child_whole > parent_whole:
            return _ProcessStartOrder.LATER
        if child_whole < parent_whole:
            return _ProcessStartOrder.EARLIER
        width = max(len(child_fraction), len(parent_fraction))
        child_fraction = child_fraction.ljust(width, "0")
        parent_fraction = parent_fraction.ljust(width, "0")
        if child_fraction > parent_fraction:
            return _ProcessStartOrder.LATER
        if child_fraction < parent_fraction:
            return _ProcessStartOrder.EARLIER
        return _ProcessStartOrder.INCONCLUSIVE

    try:
        child_time = time.strptime(child_token, "%a %b %d %H:%M:%S %Y")[:6]
        parent_time = time.strptime(parent_token, "%a %b %d %H:%M:%S %Y")[:6]
    except ValueError:
        return _ProcessStartOrder.INCONCLUSIVE
    if child_time > parent_time:
        return _ProcessStartOrder.LATER
    if child_time < parent_time:
        return _ProcessStartOrder.EARLIER
    return _ProcessStartOrder.INCONCLUSIVE


class _PosixProcessSnapshotRow(NamedTuple):
    """One parsed POSIX process row; identity may be unavailable."""

    ppid: int
    start_time: str | None


def _linux_direct_child_pids(pid: int, proc_root: Path) -> list[int] | None:
    """Return direct children from this pid's kernel ``children`` files."""
    task_root = proc_root / str(pid) / "task"
    try:
        tasks = list(task_root.iterdir())
    except OSError:
        return None
    children: set[int] = set()
    read_any = False
    for task in tasks:
        if not task.name.isdigit():
            continue
        try:
            raw = (task / "children").read_text(encoding="ascii")
        except FileNotFoundError:
            continue
        except OSError:
            return None
        read_any = True
        try:
            children.update(int(token) for token in raw.split())
        except ValueError:
            return None
    return sorted(children) if read_any else None


def _direct_child_pids_for_identity_walk(pid: int, proc_root: Path | None) -> list[int] | None:
    """Return direct children without scanning unrelated host processes."""
    if IS_LINUX:
        root = proc_root if proc_root is not None else Path("/proc")
        return _linux_direct_child_pids(pid, root)
    if sys.platform == "darwin":
        return darwin_child_pids(pid)
    return None


def _atomic_process_descendant_identities(
    root_pid: int, proc_root: Path | None
) -> list[ProcessDescendantIdentity] | None:
    """Walk descendants whose parentage and start ID share one kernel read."""

    def _identity(pid: int) -> ProcessStartIdentity | None:
        if IS_LINUX:
            root = proc_root if proc_root is not None else Path("/proc")
            return get_process_start_identity(pid, proc_root=root)
        return get_process_start_identity(pid)

    root_identity = _identity(root_pid)
    if root_identity is None:
        return None
    frontier = [(root_pid, root_identity)]
    seen = {root_pid}
    descendants: list[ProcessDescendantIdentity] = []
    index = 0
    while index < len(frontier):
        parent_pid, expected_parent = frontier[index]
        index += 1
        children = _direct_child_pids_for_identity_walk(parent_pid, proc_root)
        if children is None:
            return None
        # The child list belongs to the expected parent only if that parent kept
        # the same kernel identity across the enumeration.
        if _identity(parent_pid) != expected_parent:
            return None
        for child_pid in children:
            if child_pid in seen or child_pid <= 0:
                continue
            child_identity = _identity(child_pid)
            # The child-list row and identity read must describe one stable task.
            # Disappearance or reparenting makes the whole walk inconclusive.
            if child_identity is None or child_identity.ppid != parent_pid:
                return None
            start_order = _process_start_order(
                child_identity.start_id,
                expected_parent.start_id,
            )
            if start_order is _ProcessStartOrder.INCONCLUSIVE:
                return None
            if start_order is _ProcessStartOrder.EARLIER:
                continue
            seen.add(child_pid)
            descendants.append(
                ProcessDescendantIdentity(
                    child_pid,
                    child_identity.ppid,
                    child_identity.start_id,
                )
            )
            frontier.append((child_pid, child_identity))
    return descendants


def _posix_process_snapshot() -> dict[int, _PosixProcessSnapshotRow] | None:
    """Parse one trusted ``ps`` snapshot without aborting on malformed rows."""
    if IS_WINDOWS:
        return None
    ps_bin = trusted_system_bin("ps")
    if ps_bin is None:
        return None
    try:
        output = subprocess.check_output(
            [ps_bin, "-A", "-o", "pid=", "-o", "ppid=", "-o", "lstart="],
            timeout=5,
            stderr=subprocess.DEVNULL,
        ).decode(errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    processes: dict[int, _PosixProcessSnapshotRow] = {}
    for line in output.splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) < 2:
            continue
        try:
            process_pid = int(parts[0])
            parent_pid = int(parts[1])
        except ValueError:
            continue
        if process_pid <= 0 or parent_pid < 0:
            continue
        start_time = parts[2].strip() if len(parts) == 3 else ""
        processes[process_pid] = _PosixProcessSnapshotRow(
            parent_pid,
            start_time or None,
        )
    return processes


def _posix_process_identity_map(
    root_pid: int | None = None,
) -> dict[int, ProcessDescendantIdentity] | None:
    """Keep stable identities; require a complete first-snapshot root subtree."""
    before = _posix_process_snapshot()
    if before is None:
        return None
    after = _posix_process_snapshot()
    if after is None:
        return None
    if root_pid is not None and root_pid in before:
        parent_map = {pid: row.ppid for pid, row in before.items()}
        root_subtree = [root_pid, *_descendants_from_parent_map(root_pid, parent_map)]
        if any(
            before[pid].start_time is None or after.get(pid) != before[pid] for pid in root_subtree
        ):
            return None
    return {
        process_pid: ProcessDescendantIdentity(
            process_pid,
            process.ppid,
            process.start_time,
            ProcessIdentitySource.LSTART,
        )
        for process_pid, process in before.items()
        if process.start_time is not None and after.get(process_pid) == process
    }


def _ordered_posix_descendant_identities(
    root_pid: int,
    identities: Mapping[int, ProcessDescendantIdentity],
) -> list[ProcessDescendantIdentity] | None:
    """Return ordered fallback descendants, preserving uncertain edges."""
    parent_map = {process_pid: identity.ppid for process_pid, identity in identities.items()}
    admitted = {root_pid}
    descendants: list[ProcessDescendantIdentity] = []
    for process_pid in _descendants_from_parent_map(root_pid, parent_map):
        identity = identities[process_pid]
        parent = identities.get(identity.ppid)
        if identity.ppid not in admitted or parent is None:
            continue
        start_order = _process_start_order(
            identity.start_time,
            parent.start_time,
        )
        if start_order is _ProcessStartOrder.INCONCLUSIVE:
            return None
        if start_order is _ProcessStartOrder.EARLIER:
            continue
        admitted.add(process_pid)
        descendants.append(identity)
    return descendants


def _windows_chain_to_root(
    candidate_pid: int,
    root_pid: int,
    parent_map: dict[int, int],
) -> tuple[int, ...] | None:
    """Return candidate-to-root PIDs, empty when foreign, or none on a cycle."""
    chain: list[int] = []
    seen: set[int] = set()
    current = candidate_pid
    while current != root_pid:
        if current in seen:
            return None
        seen.add(current)
        parent = parent_map.get(current)
        if type(parent) is not int or parent <= 0:
            return ()
        chain.append(current)
        current = parent
    chain.append(root_pid)
    return tuple(chain)


def _windows_process_descendant_identities(
    root_pid: int,
    candidate_pids: set[int] | None = None,
) -> list[ProcessDescendantIdentity] | None:
    """Return creation-time-bound Windows descendants or listener candidates.

    Toolhelp supplies PID-to-PPID edges but no process identity. Take the edge
    snapshot twice and bracket it with the query-only creation-time primitive.
    Each candidate must keep the same candidate-to-root chain, including every
    edge and creation ID. A strictly earlier child is excluded; an equal or
    unparseable order makes that candidate inconclusive. Unrelated helper
    siblings may appear or disappear without invalidating a stable listener
    owner's chain.
    """
    try:
        first_map = _windows_process_parent_map()
        if root_pid not in first_map:
            return None
        if candidate_pids is None:
            candidates = _descendants_from_parent_map(root_pid, first_map)
        else:
            candidates = sorted(
                process_pid
                for process_pid in candidate_pids
                if type(process_pid) is int and process_pid > 1 and process_pid != root_pid
            )
        first_chains = {
            process_pid: _windows_chain_to_root(process_pid, root_pid, first_map)
            for process_pid in candidates
        }
        tracked = {root_pid}
        for chain in first_chains.values():
            if chain:
                tracked.update(chain)
        first_ids = {process_pid: get_process_start_id(process_pid) for process_pid in tracked}

        second_map = _windows_process_parent_map()
        if root_pid not in second_map:
            return None
        second_chains = {
            process_pid: _windows_chain_to_root(process_pid, root_pid, second_map)
            for process_pid in candidates
        }
        second_ids = {process_pid: get_process_start_id(process_pid) for process_pid in tracked}
    except Exception:
        return None

    first_root_id = first_ids.get(root_pid)
    second_root_id = second_ids.get(root_pid)
    if (
        not isinstance(first_root_id, str)
        or not first_root_id
        or second_root_id != first_root_id
        or first_map[root_pid] != second_map[root_pid]
    ):
        return None

    identities: list[ProcessDescendantIdentity] = []
    inconclusive = False
    for candidate_pid in candidates:
        first_chain = first_chains[candidate_pid]
        second_chain = second_chains[candidate_pid]
        if first_chain is None or second_chain is None:
            inconclusive = True
            continue
        if first_chain != second_chain:
            if first_chain or second_chain:
                inconclusive = True
            continue
        if not first_chain:
            continue
        if any(
            not isinstance(first_ids.get(process_pid), str)
            or not first_ids[process_pid]
            or second_ids.get(process_pid) != first_ids[process_pid]
            for process_pid in first_chain
        ):
            inconclusive = True
            continue
        creation_order_valid = True
        creation_order_inconclusive = False
        for child_pid, parent_pid in zip(first_chain, first_chain[1:]):
            child_start = first_ids.get(child_pid)
            parent_start = first_ids.get(parent_pid)
            if not isinstance(child_start, str) or not isinstance(parent_start, str):
                creation_order_inconclusive = True
                break
            start_order = _process_start_order(child_start, parent_start)
            if start_order is _ProcessStartOrder.INCONCLUSIVE:
                creation_order_inconclusive = True
                break
            if start_order is _ProcessStartOrder.EARLIER:
                creation_order_valid = False
                break
        if creation_order_inconclusive:
            inconclusive = True
            continue
        if not creation_order_valid:
            continue
        start_id = first_ids[candidate_pid]
        if not isinstance(start_id, str):
            inconclusive = True
            continue
        identities.append(
            ProcessDescendantIdentity(
                candidate_pid,
                first_map[candidate_pid],
                start_id,
            )
        )
    if identities:
        return identities
    return None if inconclusive else []


def process_descendant_identities(
    pid: int,
    *,
    proc_root: Path | None = None,
    candidate_pids: set[int] | None = None,
) -> list[ProcessDescendantIdentity] | None:
    """Return descendants with start identity bound atomically to parentage.

    Linux and macOS walk only the root's current descendants. Each candidate
    pid's start ID and PPID come from one ``/proc/<pid>/stat`` or
    ``PROC_PIDTBSDINFO`` kernel record. A strictly later child is accepted, a
    strictly earlier child is excluded, and equal or unparseable order is
    inconclusive. Windows validates each requested listener candidate's complete
    listener candidate's complete ancestry chain across two Toolhelp snapshots;
    unrelated siblings do not invalidate that proof. With no candidates it
    returns every independently stable descendant chain. If the native POSIX
    path is unavailable, other POSIX hosts fall back to two whole-process ``ps``
    snapshots and retain only rows whose parentage and one-second ``lstart``
    identity are unchanged.
    """
    if type(pid) is not int or pid <= 1:
        return []
    if IS_WINDOWS:
        return _windows_process_descendant_identities(pid, candidate_pids)
    if IS_LINUX or sys.platform == "darwin":
        descendants = _atomic_process_descendant_identities(pid, proc_root)
        if descendants is not None:
            return descendants
    if not IS_POSIX:
        return []
    identities = _posix_process_identity_map(pid)
    if identities is None:
        return None
    if pid not in identities:
        return []
    return _ordered_posix_descendant_identities(pid, identities)


def process_descendants(pid: int) -> list[int]:
    """Return *pid*'s descendants, breadth-first, from a single OS snapshot.

    Best-effort: an unreadable process table yields an empty list rather than
    raising, so callers using this to broaden a kill still perform their
    primary kill.

    Snapshot BEFORE killing anything. A kill reparents surviving orphans to
    init, erasing the PPID links that identify them, so a post-kill snapshot
    cannot find the very processes a caller needs to clean up.
    """

    if type(pid) is not int or pid <= 1:
        return []
    try:
        parent_map = _windows_process_parent_map() if IS_WINDOWS else _posix_process_parent_map()
    except Exception:  # noqa: BLE001 - introspection must never break a kill path
        return []
    return _descendants_from_parent_map(pid, parent_map)


def attributed_descendants(root_pid: int, root_token: str) -> list[int]:
    """*root_pid*'s descendants with EVERY parent-child edge attributed, not just the root.

    :func:`created_after` compares one child against one parent. Applying it with the
    ROOT's token for a whole flattened descendant list is weaker than it looks: it
    asks "was this process created after the root", which every process started since
    the root satisfies -- including a stale orphan that the parent map lists under a
    RECYCLED INTERMEDIATE pid. Such an orphan is unrelated to this tree, often
    belongs to the same user, and a caller acting on the set then terminates a
    stranger's process tree irreversibly.

    Walking level by level and attributing each edge against the parent it was
    reached THROUGH separates them: a real grandchild was created after its own
    parent, while the orphan of a recycled intermediate was created before the
    process that now holds that number. A child failing its edge is dropped WITH ITS
    SUBTREE -- everything below an unattributable edge is reached only through it, so
    none of it is provably part of this tree either.

    Still the token-only form: it needs no handles, so it serves the callers that
    cannot hold an exact root handle. :func:`descendant_termination_handles` remains
    the stronger answer where one IS available. A process whose creation identity
    cannot be read at all is left alone rather than guessed at, exactly as
    :func:`created_after` documents.

    Best-effort like :func:`process_descendants`: an unreadable process table yields
    an empty list rather than raising, and the SAME snapshot ordering rule applies --
    call this BEFORE killing anything.
    """

    if type(root_pid) is not int or root_pid <= 1 or not root_token:
        return []
    try:
        parent_map = _windows_process_parent_map() if IS_WINDOWS else _posix_process_parent_map()
    except Exception:  # noqa: BLE001 - introspection must never break a kill path
        return []

    children_of: dict[int, list[int]] = {}
    for child, parent in parent_map.items():
        children_of.setdefault(parent, []).append(child)

    out: list[int] = []
    seen = {root_pid}
    frontier = [(root_pid, root_token)]
    while frontier:
        next_frontier: list[tuple[int, str]] = []
        for parent_pid, parent_token in frontier:
            for child in sorted(children_of.get(parent_pid, ())):
                if child in seen:
                    continue
                child_token = process_start_time(child)
                if not child_token or not created_after(child_token, parent_token):
                    # Unattributable edge: this child is not provably ours, and
                    # nothing below it is reachable except through it.
                    continue
                seen.add(child)
                out.append(child)
                next_frontier.append((child, child_token))
        frontier = next_frontier
    return out


def created_after(child_token: str, parent_token: str) -> bool:
    """Whether a process the parent map lists under another is really its child.

    The Toolhelp snapshot behind :func:`process_descendants` records a parent as a
    bare pid, and Windows keeps that number after the parent dies. When the dead
    parent's pid is later recycled, an unrelated process appears as a child of the
    recycler -- and, being unrelated, is often one this user cannot terminate, so
    ending it fails and a caller that treats the set as a tree draws the wrong
    conclusion in whichever direction hurts it (killing a stranger, or calling a
    foreign listener its own).

    A genuine child was created after its parent, while such a stray was created
    while the pid still belonged to the process it was born under, so comparing the
    creation identities separates the two exactly. Both tokens are the creation
    ``FILETIME`` as decimal text (:func:`process_start_time`); a token that is not
    (nothing on Windows produces one) is not attributable, and the caller must leave
    that process alone rather than act on a guess.

    Lives HERE, beside the primitive whose staleness it compensates for, because
    three callers need the same Boolean rule: the pod backend's ``stop``,
    ``pod.runtime.port_owner``, and the test harness's Windows teardown. This thin
    wrapper validates their numeric FILETIME domain, then delegates ordering to
    the shared tri-state core so no second comparison implementation can drift.
    All three reach it through
    :func:`attributed_descendants`, which applies this comparison to EVERY
    parent-child edge -- applying it with only the ROOT's token admits a stale orphan
    sitting under a recycled INTERMEDIATE pid, which also postdates the root.
    :func:`descendant_termination_handles` is the stronger form still --
    exact per-process handles, every edge validated against creation AND exit times
    across two snapshots -- and is the right answer for a caller that holds an exact
    root handle; this is the token-only form for callers that do not.
    """
    try:
        child_numeric = str(int(child_token))
        parent_numeric = str(int(parent_token))
    except ValueError:
        return False
    return _process_start_order(child_numeric, parent_numeric) is _ProcessStartOrder.LATER


def _windows_process_parent_map() -> dict[int, int]:
    """Return one Toolhelp PID -> PPID snapshot, raising if enumeration fails."""

    if not IS_WINDOWS:
        return {}
    try:
        th32cs_snapprocess = 0x00000002
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

        entry_ptr = ctypes.POINTER(_ProcessEntry32)

        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32First.argtypes = [wintypes.HANDLE, entry_ptr]
        kernel32.Process32First.restype = wintypes.BOOL
        kernel32.Process32Next.argtypes = [wintypes.HANDLE, entry_ptr]
        kernel32.Process32Next.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        set_last_error = getattr(kernel32, "SetLastError", None)
        get_last_error = getattr(kernel32, "GetLastError", None)

        snapshot = kernel32.CreateToolhelp32Snapshot(th32cs_snapprocess, 0)
        if snapshot == wintypes.HANDLE(-1).value:
            raise OSError("Windows process snapshot creation failed")
        try:
            entry = _ProcessEntry32()
            entry.dwSize = ctypes.sizeof(_ProcessEntry32)
            if not kernel32.Process32First(snapshot, ctypes.byref(entry)):
                raise OSError("Windows first process enumeration failed")
            result: dict[int, int] = {}
            while True:
                if len(result) >= _WINDOWS_CLEANUP_SNAPSHOT_LIMIT:
                    raise _WindowsTreeOverflow("Windows cleanup process snapshot capacity exceeded")
                result[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                if callable(set_last_error):
                    set_last_error(0)
                else:
                    ctypes_set_last_error = getattr(ctypes, "set_last_error", None)
                    if callable(ctypes_set_last_error):
                        ctypes_set_last_error(0)
                if not kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                    error = (
                        int(get_last_error()) if callable(get_last_error) else _windows_last_error()
                    )
                    if error not in (0, 18):  # ERROR_NO_MORE_FILES
                        raise OSError(error, "Windows process enumeration failed")
                    return result
        finally:
            kernel32.CloseHandle(snapshot)
    except OSError:
        raise
    except Exception as exc:
        raise OSError("Windows process enumeration failed") from exc


# Separate cleanup bookkeeping budget, not a pool, RSS, or Job limit. Sixty-four
# physical trees leave room beyond ordinary concurrent ACP starts; 4096 exact
# identities per tree allow build/browser fan-out without unbounded retention.
_WINDOWS_CLEANUP_ROOT_LIMIT = 64
_WINDOWS_CLEANUP_IDENTITY_LIMIT = 4096
# Toolhelp includes unrelated host processes. Bound that temporary input too,
# before retention; an incomplete snapshot cannot prove a tree empty.
_WINDOWS_CLEANUP_SNAPSHOT_LIMIT = 65536


class WindowsCleanupCapacityError(OSError):
    """Cleanup admission refused, or an owned tree needs manual handling."""


# Named by the path callers import it from, so a traceback, a pickle or any
# ``module.qualname`` rendering of the class says ``kiro_crew.platform_compat``.
WindowsCleanupCapacityError.__module__ = "kiro_crew.platform_compat"


class _WindowsTreeOverflow(WindowsCleanupCapacityError):
    """A snapshot/identity bound prevented complete lineage observation."""


# Named by the path callers import it from, so a traceback, a pickle or any
# ``module.qualname`` rendering of the class says ``kiro_crew.platform_compat``.
_WindowsTreeOverflow.__module__ = "kiro_crew.platform_compat"
