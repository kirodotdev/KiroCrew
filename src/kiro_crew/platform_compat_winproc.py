"""Windows process objects.

Exact process handles (opened only when the object matches the identity captured
earlier), asyncio's handle duplicated for a tree anchor, the handles of a tree's
newly seen descendants and closing a handle, and the Job object and main-thread
resume a suspended child is bounded and started with.

One of the owners ``kiro_crew.platform_compat`` is split into: every name here stays
readable and patchable as ``kiro_crew.platform_compat.<name>``.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes.util
import logging
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
    IS_POSIX,
    IS_WINDOWS,
    _close_process_handle,
    _JobObjectExtendedLimitInformation,
    _open_process_query_handle,
    _ThreadEntry32,
    _windows_last_error,
)

# The whole compatibility layer logs under one name, which operator log filters and
# the tests' caplog scopes key on.
logger = logging.getLogger("kiro_crew.platform_compat")


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


# ---------------------------------------------------------------------------
# Windows Job objects — the cgroup-v2-scope analogue
# ---------------------------------------------------------------------------
# On Linux, ``sandbox.cgroup_scope_argv`` bounds an agent subprocess AND all its
# descendants as one cgroup (``TasksMax`` = fork-bomb ceiling, ``MemoryMax`` =
# RSS-balloon ceiling). That wrapper is a no-op on Windows (there is no systemd)
# and logs a one-time loud SECURITY warning, so Windows had NO fork-bomb and NO
# memory ceiling on the agent tree at all.
#
# A Job object is the native equivalent: limits apply to every process in the
# job, and descendants of a job member join the job automatically. Unlike the
# cgroup path this cannot be expressed as an argv prefix — there is no wrapper
# binary to prepend — so it is applied to an already-spawned pid instead. See the
# race note in :func:`apply_job_limits` for why that pid must be SUSPENDED.
_JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
_JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
# JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE (0x2000) is deliberately NOT set. It would
# terminate the agent tree as soon as the last job handle closed, changing
# process LIFECYCLE (a gateway exit would kill running agents) rather than merely
# adding a resource ceiling. Omitting it also means the handle need not be held:
# a job object stays alive while processes are assigned to it, so the limits keep
# being enforced after CloseHandle. That makes this fire-and-forget, with no
# handle registry and no teardown semantics to get wrong.
_JOBOBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9  # JobObjectExtendedLimitInformation


def apply_job_limits(pid: int, *, max_procs: int, max_memory_bytes: int) -> bool:
    """Bound *pid* and its descendants with a Windows Job object.

    The Windows analogue of ``sandbox.cgroup_scope_argv``:

    ==============================  ====================================
    cgroup v2                       Job object
    ==============================  ====================================
    ``TasksMax`` (fork bomb)        ``ActiveProcessLimit``
    ``MemoryMax`` (RSS balloon)     ``JobMemoryLimit``
    ==============================  ====================================

    The memory row is a true equivalent; the process row is NOT one-for-one.
    ``TasksMax`` counts tasks — every thread — while ``ActiveProcessLimit``
    counts processes, so the same numeric budget is a LOOSER bound here: a
    tree of N processes holds at least N tasks and usually many more. It still
    bounds a fork bomb, which is the control's purpose, but do not read the two
    limits as equal strictness, and do not "fix" the gap by scaling the number
    without deciding what a process budget should be — the units differ, so
    there is no arithmetic conversion between them.

    Enforcement is by DENIAL, matching the cgroup tier's practical behavior:
    once ``ActiveProcessLimit`` is reached the member's ``CreateProcess`` calls
    fail with ``ERROR_NOT_ENOUGH_QUOTA``, and an allocation past
    ``JobMemoryLimit`` fails, rather than the tree being killed outright.
    Nothing about process lifetime changes (see the ``KILL_ON_JOB_CLOSE`` note
    above).

    Returns ``True`` when the limits were applied. Returns ``False`` — never
    raises — on POSIX (where ``cgroup_scope_argv`` owns this), on a non-positive
    limit, or on any Win32 failure; the caller treats that as "no ceiling
    enforced" exactly as it already treats the cgroup probe failing.

    Race-free ONLY when paired with :data:`CREATE_SUSPENDED`. Job membership
    covers a member's FUTURE descendants but not ones it already spawned, so
    assigning a *running* child leaves a window in which it could have forked
    something that escapes the job. Callers therefore create the child with
    ``creationflags |= CREATE_SUSPENDED`` — a suspended process has executed no
    instructions and so provably has no descendants — call this, then
    :func:`resume_process_main_thread`. That closes the window by construction
    rather than merely making it small. This function still works on an
    already-running pid (the ceiling then applies from that moment on); the
    suspended handshake is what makes it airtight.
    """
    if IS_POSIX:
        return False
    if max_procs <= 0 or max_memory_bytes <= 0:
        logger.debug(
            "apply_job_limits: skipping non-positive limits (procs=%s, mem=%s)",
            max_procs,
            max_memory_bytes,
        )
        return False
    job = None
    proc_handle = None
    kernel32 = None
    # pragma: no cover below — the ctypes plumbing is Windows-only, and the
    # Windows CI shards run with --no-cov (only the Ubuntu 3.12 shards measure
    # coverage), so these statements are unmeasurable ANYWHERE rather than
    # merely untested. Charging them to the denominator understates the file's
    # real rate, the same reasoning setup.cfg records for the CI-deselected
    # suites it omits. Everything above stays measured: the POSIX early-out is
    # exercised by test_windows_job_limits.py's ungated inertness tests, and the
    # Windows behavior itself is asserted against the live kernel there.
    try:  # pragma: no cover
        _PROCESS_SET_QUOTA = 0x0100  # noqa: N806 — Windows API constant
        _PROCESS_TERMINATE = 0x0001  # noqa: N806 — AssignProcessToJobObject needs it
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        # Anonymous job (NULL name): nothing else can open it by name.
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            logger.warning(
                "SECURITY: CreateJobObject failed (err=%s); fork-bomb / memory-DoS "
                "ceilings are NOT enforced for pid %d",
                _windows_last_error(),
                pid,
            )
            return False

        info = _JobObjectExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = (
            _JOB_OBJECT_LIMIT_ACTIVE_PROCESS | _JOB_OBJECT_LIMIT_JOB_MEMORY
        )
        info.BasicLimitInformation.ActiveProcessLimit = max_procs
        info.JobMemoryLimit = max_memory_bytes
        if not kernel32.SetInformationJobObject(
            job,
            _JOBOBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            logger.warning(
                "SECURITY: SetInformationJobObject failed (err=%s); ceilings NOT "
                "enforced for pid %d",
                _windows_last_error(),
                pid,
            )
            return False

        proc_handle = kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not proc_handle:
            logger.warning(
                "SECURITY: OpenProcess(SET_QUOTA|TERMINATE) failed for pid %d (err=%s); "
                "ceilings NOT enforced",
                pid,
                _windows_last_error(),
            )
            return False
        if not kernel32.AssignProcessToJobObject(job, proc_handle):
            logger.warning(
                "SECURITY: AssignProcessToJobObject failed for pid %d (err=%s); "
                "ceilings NOT enforced",
                pid,
                _windows_last_error(),
            )
            return False
        logger.info(
            "Job object ceilings applied to pid %d (max_procs=%d, max_mem=%dMB)",
            pid,
            max_procs,
            max_memory_bytes // (1024 * 1024),
        )
        return True
    except Exception:
        logger.warning("apply_job_limits failed for pid %s", pid, exc_info=True)
        return False
    finally:
        # Safe to close BOTH handles: without KILL_ON_JOB_CLOSE the job object
        # outlives our handle for as long as processes remain assigned, so the
        # limits stay in force. Leaking these would be a per-spawn handle leak in
        # a long-lived gateway.
        for handle in (proc_handle, job):
            if handle and kernel32 is not None:
                try:
                    kernel32.CloseHandle(handle)
                except Exception:
                    logger.debug("CloseHandle failed", exc_info=True)


_TH32CS_SNAPTHREAD = 0x00000004
_THREAD_SUSPEND_RESUME = 0x0002
_INVALID_HANDLE_VALUE = -1
# ResumeThread returns the thread's PREVIOUS suspend count, or (DWORD)-1 on
# failure. Compared as an unsigned 32-bit value because the restype is DWORD.
_RESUME_THREAD_FAILED = 0xFFFFFFFF


def resume_process_main_thread(pid: int) -> bool:
    """Resume every suspended thread of *pid*. Returns True iff one was resumed.

    The other half of race-free Job object assignment. A child spawned with
    :data:`CREATE_SUSPENDED` has executed no instructions, so
    :func:`apply_job_limits` can put it in a job knowing no descendant escaped;
    this then lets it run.

    kernel32 has no ``ResumeProcess``, so the main thread has to be reached by
    ID: snapshot the system thread list
    (``CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD)``), select entries whose
    ``th32OwnerProcessID`` matches, and ``ResumeThread`` each. A freshly created
    suspended process has exactly one thread, but every match is resumed rather
    than only the first — resuming a thread that is not suspended is a harmless
    no-op (its suspend count is already 0), whereas guessing wrong about which
    thread is "main" would leave the process wedged forever.

    Returns ``False`` — never raises — on POSIX (nothing is ever suspended there)
    or on any Win32 failure. A ``False`` return is SERIOUS for the caller: the
    child is alive but frozen, and the only safe response is to kill it rather
    than let a suspended process masquerade as a running agent. See
    ``acp.client.finish_suspended_spawn``, which implements that policy.
    """
    if IS_POSIX:
        return False
    snapshot = None
    kernel32 = None
    # Windows-only ctypes plumbing, unmeasurable on every runner — see the note
    # in :func:`apply_job_limits`.
    try:  # pragma: no cover
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
        kernel32.Thread32First.restype = wintypes.BOOL
        kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
        kernel32.Thread32Next.restype = wintypes.BOOL
        kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenThread.restype = wintypes.HANDLE
        kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
        kernel32.ResumeThread.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
        if not snapshot or snapshot == _INVALID_HANDLE_VALUE:
            logger.error(
                "resume_process_main_thread: thread snapshot failed for pid %d (err=%s)",
                pid,
                _windows_last_error(),
            )
            return False
        entry = _ThreadEntry32()
        entry.dwSize = ctypes.sizeof(_ThreadEntry32)
        resumed = 0
        ok = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while ok:
            if entry.th32OwnerProcessID == pid:
                thread = kernel32.OpenThread(_THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                if thread:
                    try:
                        if kernel32.ResumeThread(thread) != _RESUME_THREAD_FAILED:
                            resumed += 1
                        else:
                            logger.error(
                                "ResumeThread failed for tid %d of pid %d (err=%s)",
                                entry.th32ThreadID,
                                pid,
                                _windows_last_error(),
                            )
                    finally:
                        kernel32.CloseHandle(thread)
                else:
                    logger.error(
                        "OpenThread(SUSPEND_RESUME) failed for tid %d of pid %d (err=%s)",
                        entry.th32ThreadID,
                        pid,
                        _windows_last_error(),
                    )
            # Thread32Next overwrites the entry, dwSize included, so it must be
            # reset before every call or the next iteration fails.
            entry.dwSize = ctypes.sizeof(_ThreadEntry32)
            ok = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
        if not resumed:
            logger.error("resume_process_main_thread: no threads resumed for pid %d", pid)
        return resumed > 0
    except Exception:
        logger.error("resume_process_main_thread failed for pid %s", pid, exc_info=True)
        return False
    finally:
        if snapshot and snapshot != _INVALID_HANDLE_VALUE and kernel32 is not None:
            try:
                kernel32.CloseHandle(snapshot)
            except Exception:
                logger.debug("CloseHandle(snapshot) failed", exc_info=True)
