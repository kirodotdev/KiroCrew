"""Windows process objects.

Exact process handles (opened only when the object matches the identity captured
earlier), asyncio's handle duplicated for a tree anchor, the handles of a tree's
newly seen descendants, and closing a handle.

One of the owners ``kiro_crew.platform_compat`` is split into: every name here stays
readable and patchable as ``kiro_crew.platform_compat.<name>``.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes.util
from ctypes import wintypes
from typing import Mapping

from kiro_crew.executors import subprocess_executor
from kiro_crew.platform_compat_descendants import (
    _WINDOWS_CLEANUP_IDENTITY_LIMIT,
    _descendants_from_parent_map,
    _windows_chain_to_root,
    _windows_process_parent_map,
    _WindowsTreeOverflow,
)
from kiro_crew.platform_compat_identity import _windows_process_handle_identity
from kiro_crew.platform_compat_native import (
    IS_WINDOWS,
    _close_process_handle,
    _open_process_query_handle,
    _windows_last_error,
)


def _open_process_termination_handle(pid: int, *, failure: list[str] | None = None) -> int | None:
    """Open a termination handle; optionally capture a sanitized failure locally."""

    if not IS_WINDOWS:
        return None
    evidence = "unknown"
    try:
        process_terminate = 0x0001
        process_query_limited_information = 0x1000
        synchronize = 0x00100000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(
            process_terminate | process_query_limited_information | synchronize,
            False,
            pid,
        )
        if handle:
            return int(handle)
        # Read ctypes' saved error immediately, before any other Windows call.
        with contextlib.suppress(Exception):
            evidence = f"winerror={_windows_last_error()}"
    except Exception as exc:
        evidence = f"exception={type(exc).__name__[:64]}"
    if failure is not None:
        with contextlib.suppress(Exception):
            failure.append(evidence)
    return None


def open_process_termination_handle(pid: int, expected_token: str) -> int | None:
    """Open *pid* only when its exact process object matches *expected_token*.

    The caller owns a returned handle and closes it with
    :func:`close_process_handle`. Opening by numeric PID alone races PID reuse,
    so the handle's creation FILETIME is compared with the authoritative token
    before it is returned. A mismatch, unreadable identity, or malformed token
    closes the handle and returns ``None``.
    """

    if type(pid) is not int or pid <= 1:
        raise ValueError(f"open_process_termination_handle: refusing invalid pid {pid!r}")
    handle = _open_process_termination_handle(pid)
    if handle is None:
        return None
    try:
        expected_creation = int(expected_token)
        identity = _windows_process_handle_identity(handle)
        if identity is None or identity[0] != pid or identity[1] != expected_creation:
            close_process_handle(handle)
            return None
    except (TypeError, ValueError):
        close_process_handle(handle)
        return None
    return handle


def duplicate_asyncio_process_handle(process: object) -> int | None:
    """Duplicate asyncio's original Windows process handle for tree anchoring."""

    if not IS_WINDOWS:
        return None
    try:
        transport = getattr(process, "_transport", None)
        get_extra_info = getattr(transport, "get_extra_info", None)
        popen = get_extra_info("subprocess") if callable(get_extra_info) else None
        source = getattr(popen, "_handle", None)
        if not isinstance(source, int):
            return None
        source_value = int(source)
        if source_value <= 0:
            return None

        duplicate_same_access = 0x00000002
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.DuplicateHandle.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.DuplicateHandle.restype = wintypes.BOOL
        owner = kernel32.GetCurrentProcess()
        duplicate = wintypes.HANDLE()
        if not kernel32.DuplicateHandle(
            owner,
            wintypes.HANDLE(source_value),
            owner,
            ctypes.byref(duplicate),
            0,
            False,
            duplicate_same_access,
        ):
            return None
        return int(duplicate.value) if duplicate.value else None
    except Exception:
        return None


def _windows_lineage_matches_lifetimes(
    child_pid: int,
    root_pid: int,
    parent_map: Mapping[int, int],
    identities: Mapping[int, tuple[int, int, int | None]],
) -> bool:
    """Bind numeric Toolhelp ancestry to the exact handles' lifetimes."""

    current = child_pid
    seen: set[int] = set()
    while current != root_pid:
        if current in seen:
            return False
        seen.add(current)
        parent_pid = parent_map.get(current)
        child_identity = identities.get(current)
        parent_identity = identities.get(parent_pid) if parent_pid is not None else None
        if (
            parent_pid is None
            or child_identity is None
            or parent_identity is None
            or child_identity[0] != current
            or parent_identity[0] != parent_pid
        ):
            return False
        child_created = child_identity[1]
        parent_created = parent_identity[1]
        parent_exited = parent_identity[2]
        if child_created < parent_created:
            return False
        if parent_exited is not None and child_created >= parent_exited:
            return False
        current = parent_pid
    return True


def _windows_process_query_diagnostic(pid: int) -> str:
    """Observe an unvalidated exact object, never acquire termination authority."""

    handle = None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION only
        if not handle:
            return f"winerror={_windows_last_error()}"
        return f"identity={_windows_process_handle_identity(int(handle))}"
    except Exception as exc:
        return f"exception={type(exc).__name__[:64]}"
    finally:
        if handle:
            close_process_handle(int(handle))


def _windows_descendant_failure_details(
    failed: set[int],
    first_map: Mapping[int, int],
    fresh_map: Mapping[int, int],
    pinned: Mapping[int, int],
    root_identity: tuple[int, int, int | None],
    opening_errors: Mapping[int, str],
) -> str:
    """Bounded, failure-only observations; nothing here certifies death or ancestry."""

    root_pid = root_identity[0]
    identities: dict[int, tuple[int, int, int | None] | None] = {}

    def chain(child: int, parents: Mapping[int, int]) -> list[int]:
        result: list[int] = []
        while child not in result and len(result) < 8:
            result.append(child)
            if child == root_pid or child not in parents:
                break
            child = parents[child]
        return result

    details = []
    for child in sorted(failed)[:3]:
        first_chain = chain(child, first_map)
        fresh_chain = chain(child, fresh_map)
        # Only already-pinned objects on the relevant chains are queried.
        relevant = sorted({root_pid, *first_chain, *fresh_chain}.intersection(pinned))
        for process_pid in relevant:
            if process_pid not in identities:
                identities[process_pid] = _windows_process_handle_identity(pinned[process_pid])
        facts = {process_pid: identities[process_pid] for process_pid in relevant}
        details.append(
            f"pid={child} open={opening_errors.get(child, 'unknown')} "
            f"first_chain={first_chain} fresh_chain={fresh_chain} pinned={facts} "
            f"query_unvalidated={_windows_process_query_diagnostic(child)}"
        )
    return (
        f"diagnostic_only(total={len(failed)}, candidates<=3, chain<=8, "
        f"root_at_scan={root_identity}): " + "; ".join(details)
    )


def _windows_process_query_creation(pid: int) -> int | None:
    """Return *pid*'s creation FILETIME through a query-only handle, or ``None``.

    Termination rights are not requested, so this answers for a process whose
    termination handle is refused -- which is the only case that needs it.
    ``None`` means the instant is unknown and nothing may be concluded from it.
    """

    handle = _open_process_query_handle(pid)
    if handle is None:
        return None
    try:
        identity = _windows_process_handle_identity(handle)
    finally:
        _close_process_handle(handle)
    if identity is None or identity[0] != pid:
        return None
    return identity[1]


def _windows_foreign_descendant_pids(
    candidates: set[int],
    observed: set[int],
    parent_map: dict[int, int],
    pinned: Mapping[int, int],
    root_pid: int,
    root_created: int,
) -> set[int]:
    """Return observed PIDs whose numeric ancestry creation order disproves them.

    A descendant is created after the root it descends from, so a chain node that
    already existed before the root holds a recycled PID naming an unrelated
    process. Every observed PID reaching the root only through such a node is
    foreign as well: its claimed parent's PID was taken over by a process older
    than the root, so that parent died before the root started and cannot have
    belonged to this tree.

    Only *candidates* seed the reads, so a scan that opened every handle pays
    nothing. A node in *pinned* is read from its own handle, whose object cannot
    have been recycled; the rest are read once each by PID. An unreadable instant
    disproves nothing and leaves its chain intact.
    """

    creations: dict[int, int | None] = {}

    def created(process_pid: int) -> int | None:
        if process_pid not in creations:
            handle = pinned.get(process_pid)
            if handle is None:
                creations[process_pid] = _windows_process_query_creation(process_pid)
            else:
                identity = _windows_process_handle_identity(handle)
                creations[process_pid] = (
                    identity[1] if identity is not None and identity[0] == process_pid else None
                )
        return creations[process_pid]

    disproven: set[int] = set()
    for candidate_pid in sorted(candidates):
        chain = _windows_chain_to_root(candidate_pid, root_pid, parent_map)
        if not chain:
            continue
        for process_pid in chain:
            if process_pid == root_pid:
                continue
            instant = created(process_pid)
            if instant is not None and instant < root_created:
                disproven.add(process_pid)
    if not disproven:
        return set()
    foreign: set[int] = set()
    for process_pid in observed:
        chain = _windows_chain_to_root(process_pid, root_pid, parent_map)
        if chain and not disproven.isdisjoint(chain):
            foreign.add(process_pid)
    return foreign


def descendant_termination_handles(
    pid: int,
    retained_handles: Mapping[int, int] | None = None,
    root_handle: int | None = None,
) -> dict[int, int]:
    """Return exact Windows process handles for newly observed descendants.

    Toolhelp exposes numeric parent PIDs, which can be recycled after an
    unpinned process exits. Every edge is therefore checked against creation/exit times
    from exact root, retained-parent, and newly-opened child handles in two
    snapshots. This admits a genuine child created before an immediate launcher
    exit while rejecting a tree attached to a recycled root or intermediate PID.
    An unopenable candidate requires fresh full-snapshot absence, or a creation
    instant proving it predates the root; unreadable identities or incomplete
    ancestry raise OSError, never certify a subset.
    On failure only newly opened handles are closed; retained/root handles
    stay caller-owned.
    """

    if type(pid) is not int or pid <= 1:
        raise ValueError(f"descendant_termination_handles: refusing non-int/reserved pid {pid!r}")
    if not IS_WINDOWS:
        return {}
    if type(root_handle) is not int or root_handle <= 0:
        raise ValueError("descendant_termination_handles: exact root handle required")
    if retained_handles is not None and len(retained_handles) > _WINDOWS_CLEANUP_IDENTITY_LIMIT:
        raise _WindowsTreeOverflow("Windows cleanup retained identity capacity exceeded")
    retained = dict(retained_handles or {})
    root_identity = _windows_process_handle_identity(root_handle)
    if root_identity is None or root_identity[0] != pid:
        raise ValueError("descendant_termination_handles: root handle identity mismatch")

    first_map = _windows_process_parent_map()
    first = set(_descendants_from_parent_map(pid, first_map, limit=_WINDOWS_CLEANUP_IDENTITY_LIMIT))
    # Check the union without allocating an oversized union or opening a child.
    count = len(retained) + (pid not in retained)
    for child in first:
        if child not in retained:
            count += 1
            if count > _WINDOWS_CLEANUP_IDENTITY_LIMIT:
                raise _WindowsTreeOverflow("Windows cleanup identity capacity exceeded")
    opened: dict[int, int] = {}
    try:
        unopened: set[int] = set()
        opening_errors: dict[int, str] = {}
        # Retained handles stay open across scans, pinning each process object
        # and preventing PID reuse even after exit; no replacement can hide here.
        for child_pid in sorted(first - set(retained)):
            failure: list[str] = []
            handle = _open_process_termination_handle(child_pid, failure=failure)
            if handle is None:
                unopened.add(child_pid)
                opening_errors[child_pid] = failure[0] if failure else "unknown"
            else:
                opened[child_pid] = handle
        if unopened:
            # OpenProcess failure (including ACCESS_DENIED) is not proof of
            # death. Require a fresh, successful FULL snapshot and account for
            # surviving entries that still reference a vanished candidate.
            # Enumeration errors propagate; pid_exists(False) is ambiguous.
            fresh_map = _windows_process_parent_map()
            remaining = unopened.intersection(fresh_map)
            if remaining:
                # A process that already existed before the root cannot descend
                # from it, and that instant is readable through a query-only
                # handle where a termination handle is refused. Drop such a
                # stranger together with everything whose only route to the root
                # runs through it, so a PID it inherited cannot turn the owned
                # tree into a fatal incomplete one. A chain implicating a pinned
                # retained identity stays fail-closed: dropping it would discard
                # authority an earlier scan already proved.
                foreign = _windows_foreign_descendant_pids(
                    remaining,
                    first,
                    first_map,
                    {**retained, **opened, pid: root_handle},
                    pid,
                    root_identity[1],
                )
                if foreign and foreign.isdisjoint(retained):
                    unopened -= foreign
                    for child_pid in sorted(foreign.intersection(opened)):
                        close_process_handle(opened.pop(child_pid))
                    remaining = unopened.intersection(fresh_map)
            if remaining:
                errors = {child: opening_errors[child] for child in sorted(remaining)[:3]}
                details = f"diagnostic_only=unknown; open_errors={errors}"
                with contextlib.suppress(Exception):
                    details = _windows_descendant_failure_details(
                        remaining,
                        first_map,
                        fresh_map,
                        {**retained, **opened, pid: root_handle},
                        root_identity,
                        opening_errors,
                    )
                raise OSError(
                    f"Windows descendant handles unavailable: {sorted(remaining)[:3]}; {details}"
                )
            vanished = unopened.difference(fresh_map)
            dangling = sorted(
                (child, parent) for child, parent in fresh_map.items() if parent in vanished
            )
            if dangling:
                # These children may have appeared only after the first scan.
                # Numeric edges cannot prove identity or grant kill authority,
                # but they prevent certifying this observed branch as gone.
                raise OSError(
                    "Windows descendant ancestry incomplete: vanished unopened parents; "
                    f"diagnostic_only(total={len(dangling)}, child_parent_ids<=3): {dangling[:3]}"
                )
        if not opened:
            return {}

        handles = {
            **{child_pid: handle for child_pid, handle in retained.items() if child_pid in first},
            **opened,
            pid: root_handle,
        }

        def read_identities() -> dict[int, tuple[int, int, int | None]]:
            identities: dict[int, tuple[int, int, int | None]] = {}
            for process_pid, handle in handles.items():
                identity = _windows_process_handle_identity(handle)
                if identity is None:
                    raise OSError(f"Windows descendant handle identity unreadable: {process_pid}")
                identities[process_pid] = identity
            return identities

        first_identities = read_identities()
        # A vanished, unpinned intermediary cannot supply a lifetime bound for
        # a surviving child. Do not silently drop that child's unknown chain.
        for child_pid in first.intersection(handles):
            if first_map[child_pid] not in handles:
                raise OSError(f"Windows descendant ancestry incomplete: {child_pid}")
        eligible = {
            child_pid
            for child_pid in opened
            if child_pid in first
            and _windows_lineage_matches_lifetimes(
                child_pid,
                pid,
                first_map,
                first_identities,
            )
        }

        second_map = _windows_process_parent_map()
        second_identities = {
            process_pid: identity
            for process_pid, identity in read_identities().items()
            if (previous := first_identities.get(process_pid)) is not None
            and identity[:2] == previous[:2]
            and (previous[2] is None or identity[2] == previous[2])
        }
        for child_pid in eligible:
            current = child_pid
            while current != pid and current in second_identities:
                identity = second_identities[current]
                if current not in second_map and identity[2] is None:
                    raise OSError(f"Windows live descendant missing from snapshot: {current}")
                current = first_map[current]
        # Toolhelp drops exited intermediaries even while their exact handles
        # remain pinned. Preserve an observed edge only if its object is still
        # identical and either its PPID agrees or its absence is explained by a
        # proven exit. Recheck every lifetime with the newly read exit bounds;
        # a live child born after that exit belongs to a recycled PID, not us.
        continuous_map = {
            process_pid: parent_pid
            for process_pid, parent_pid in first_map.items()
            if process_pid in second_identities
            and (
                second_map.get(process_pid) == parent_pid
                or (process_pid not in second_map and second_identities[process_pid][2] is not None)
            )
        }
        for child_pid in tuple(opened):
            if child_pid not in eligible or not _windows_lineage_matches_lifetimes(
                child_pid,
                pid,
                continuous_map,
                second_identities,
            ):
                close_process_handle(opened.pop(child_pid))
        return opened
    except Exception:
        for handle in opened.values():
            close_process_handle(handle)
        raise


def close_process_handle(handle: int) -> None:
    """Close a handle returned by :func:`descendant_termination_handles`."""

    if not IS_WINDOWS or type(handle) is not int or handle <= 0:
        return
    with contextlib.suppress(Exception):
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(wintypes.HANDLE(handle))


async def descendant_termination_handles_async(
    pid: int,
    retained_handles: Mapping[int, int] | None = None,
    root_handle: int | None = None,
) -> dict[int, int]:
    """Async variant of :func:`descendant_termination_handles`."""

    if not IS_WINDOWS:
        return {}
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        subprocess_executor(),
        descendant_termination_handles,
        pid,
        dict(retained_handles or {}),
        root_handle,
    )
