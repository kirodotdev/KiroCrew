"""The kernel's process table, read directly: macOS through ``libproc`` and
``sysctl``, Linux through ``/proc``.

A pid's parent, start instant, state, cwd, argv, environment, executable path, CPU
time and memory footprint, its children and process-group members, its TCP sockets,
and which processes hold a file open or its ``flock``. Every reader answers
``None`` (or its own sentinel) where the kernel will not say.

One of the owners ``kiro_crew.platform_compat`` is split into: every name here stays
readable and patchable as ``kiro_crew.platform_compat.<name>``.
"""

from __future__ import annotations

import ctypes.util
import logging
import os
import struct
import sys
import threading
from pathlib import Path
from typing import Any, Iterator, NamedTuple

from kiro_crew.platform_compat_native import IS_LINUX, IS_MACOS

# The whole compatibility layer logs under one name, which operator log filters and
# the tests' caplog scopes key on.
logger = logging.getLogger("kiro_crew.platform_compat")


# Byte layout of the macOS ``proc_vnodepathinfo`` struct filled by
# ``proc_pidinfo(PROC_PIDVNODEPATHINFO)``: two ``vnode_info_path`` records (the
# process's cwd, then its root), each a fixed-size ``vnode_info`` header
# followed by a NUL-terminated path of up to ``MAXPATHLEN``. Only the header
# size matters to us, since it is the offset the cwd path starts at.
_DARWIN_PROC_PIDVNODEPATHINFO = 9
_DARWIN_VNODE_INFO_SIZE = 152
_DARWIN_MAXPATHLEN = 1024
_DARWIN_VNODE_INFO_PATH_SIZE = _DARWIN_VNODE_INFO_SIZE + _DARWIN_MAXPATHLEN
_DARWIN_PROC_VNODEPATHINFO_SIZE = 2 * _DARWIN_VNODE_INFO_PATH_SIZE

# ``proc_pidinfo(PROC_PIDTBSDINFO)`` fills a ``proc_bsdinfo`` struct whose
# parent pid and start-time pair live at fixed offsets. ``pbi_ppid`` is the
# fifth uint32 (16), followed later by ``pbi_start_tvsec`` and
# ``pbi_start_tvusec`` as two uint64s (120 / 128, struct size 136). The total
# size doubles as the layout check.
_DARWIN_PROC_PIDTBSDINFO = 3
_DARWIN_PROC_BSDINFO_SIZE = 136
_DARWIN_PBI_PPID_OFFSET = 16
_DARWIN_PBI_START_TVSEC_OFFSET = 120
_DARWIN_PBI_START_TVUSEC_OFFSET = 128


class ProcessStartIdentity(NamedTuple):
    """A process start ID and parent PID captured by one kernel read."""

    start_id: str
    ppid: int


# ``proc_pidinfo(PROC_PIDTASKINFO)`` fills a ``proc_taskinfo`` struct that opens
# with six uint64 fields — ``pti_virtual_size``, ``pti_resident_size``,
# ``pti_total_user``, ``pti_total_system``, ``pti_threads_user``,
# ``pti_threads_system`` (48 bytes) — followed by twelve int32 counters (48),
# for a struct size of 96. Only the two total-CPU fields matter here, and both
# are already NANOSECONDS; the total size doubles as the layout check.
_DARWIN_PROC_PIDTASKINFO = 4
_DARWIN_PROC_TASKINFO_SIZE = 96
_DARWIN_PTI_TOTAL_USER_OFFSET = 16
_DARWIN_PTI_TOTAL_SYSTEM_OFFSET = 24

# ``proc_pid_rusage(pid, RUSAGE_INFO_V2, buf)`` fills a ``rusage_info_v2``: a
# 16-byte ``ri_uuid`` followed by 18 uint64 fields, of which
# ``ri_phys_footprint`` is the eighth (after user/system time, the two wakeup
# counters, pageins, wired and resident size). Unlike ``proc_pidinfo`` it
# returns 0/-1 rather than a byte count, so there is no fill size to check the
# layout against here; test_macos_phys_footprint.py::TestTheRealDarwinAbi checks
# the offsets against a live libproc on a Mac.
_DARWIN_RUSAGE_INFO_V2 = 2
_DARWIN_RUSAGE_INFO_V2_SIZE = 16 + 18 * 8
_DARWIN_RI_PHYS_FOOTPRINT_OFFSET = 16 + 7 * 8

_darwin_libproc: Any = None
_darwin_libproc_loaded = False


def _darwin_libproc_handle() -> Any:
    """Cached ``libproc`` handle, or None when it cannot be loaded.

    Cached rather than opened per call because the cwd probe runs on a poll
    cadence per open terminal, and a fresh ``CDLL`` would dlopen every time.
    """
    global _darwin_libproc, _darwin_libproc_loaded
    if _darwin_libproc_loaded:
        return _darwin_libproc
    _darwin_libproc_loaded = True
    try:
        path = ctypes.util.find_library("proc")
        if path is None:
            return None
        lib = ctypes.CDLL(path)
        lib.proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        lib.proc_pidinfo.restype = ctypes.c_int
        # Configured on its own: a libproc without this symbol must still
        # serve every proc_pidinfo probe; only the footprint read goes dark.
        rusage = getattr(lib, "proc_pid_rusage", None)
        if rusage is not None:
            rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
            rusage.restype = ctypes.c_int
        _darwin_libproc = lib
    except Exception:
        _darwin_libproc = None
    return _darwin_libproc


def _darwin_process_cwd(pid: int) -> str | None:
    """macOS cwd of *pid* via ``libproc``, or None when it cannot be read.

    Requires no entitlement for a same-uid process. The kernel reports how many
    bytes it filled; anything other than the exact struct size means the layout
    assumed by the offsets above does not hold, so the answer is refused
    rather than sliced out of the wrong place.
    """
    lib = _darwin_libproc_handle()
    if lib is None:
        return None
    try:
        buf = ctypes.create_string_buffer(_DARWIN_PROC_VNODEPATHINFO_SIZE)
        filled = lib.proc_pidinfo(
            pid,
            _DARWIN_PROC_PIDVNODEPATHINFO,
            0,
            buf,
            _DARWIN_PROC_VNODEPATHINFO_SIZE,
        )
        if filled != _DARWIN_PROC_VNODEPATHINFO_SIZE:
            return None
        raw = buf.raw[_DARWIN_VNODE_INFO_SIZE:_DARWIN_VNODE_INFO_PATH_SIZE]
        cwd = raw.split(b"\0", 1)[0].decode("utf-8", errors="replace")
        return cwd or None
    except Exception:
        return None


def _darwin_process_start_identity(pid: int) -> ProcessStartIdentity | None:
    """Read macOS start ID and PPID atomically from one ``proc_bsdinfo``.

    ``proc_pidinfo(PROC_PIDTBSDINFO)`` needs no entitlement for a same-uid
    process and never spawns a subprocess. Reading both fields from this one
    kernel-filled struct prevents a recycled pid from pairing old parentage
    with a new process identity. The start instant is absolute wall time at
    microsecond resolution, six decimal orders finer than ``ps -o lstart=``.
    """
    lib = _darwin_libproc_handle()
    if lib is None:
        return None
    try:
        buf = ctypes.create_string_buffer(_DARWIN_PROC_BSDINFO_SIZE)
        filled = lib.proc_pidinfo(
            pid,
            _DARWIN_PROC_PIDTBSDINFO,
            0,
            buf,
            _DARWIN_PROC_BSDINFO_SIZE,
        )
        if filled != _DARWIN_PROC_BSDINFO_SIZE:
            return None
        # Both x86_64 and arm64 macOS are little-endian.
        ppid = int.from_bytes(
            buf.raw[_DARWIN_PBI_PPID_OFFSET : _DARWIN_PBI_PPID_OFFSET + 4],
            "little",
        )
        sec = int.from_bytes(
            buf.raw[_DARWIN_PBI_START_TVSEC_OFFSET:_DARWIN_PBI_START_TVUSEC_OFFSET],
            "little",
        )
        usec = int.from_bytes(
            buf.raw[_DARWIN_PBI_START_TVUSEC_OFFSET:_DARWIN_PROC_BSDINFO_SIZE],
            "little",
        )
        if sec <= 0:
            return None
        return ProcessStartIdentity(f"{sec}.{usec:06d}", ppid)
    except Exception:
        return None


def _darwin_process_phys_footprint_bytes(pid: int) -> int | None:
    """macOS ``phys_footprint`` of *pid* in bytes via ``proc_pid_rusage``.

    The footprint is what jetsam acts on and what Activity Monitor's "Memory"
    column shows: dirty anonymous memory INCLUDING its compressed and swapped
    pages. ``ps`` RSS counts only what is resident right now, and on macOS an
    idle process's grown heap is mostly compressed, so RSS can read a small
    fraction of the real cost (16x measured on an operator Mac). Needs no
    entitlement for a same-uid process and never spawns a subprocess.
    """
    lib = _darwin_libproc_handle()
    rusage = getattr(lib, "proc_pid_rusage", None) if lib is not None else None
    if rusage is None:
        return None
    try:
        buf = ctypes.create_string_buffer(_DARWIN_RUSAGE_INFO_V2_SIZE)
        if rusage(pid, _DARWIN_RUSAGE_INFO_V2, buf) != 0:
            return None
        off = _DARWIN_RI_PHYS_FOOTPRINT_OFFSET
        # Both x86_64 and arm64 macOS are little-endian.
        return int.from_bytes(buf.raw[off : off + 8], "little")
    except Exception:
        return None


def proc_phys_footprint_bytes_for_pid(pid: int) -> int | None:
    """``phys_footprint`` of *pid* in bytes on macOS; None elsewhere or unreadable.

    The macOS memory figure the runtime ceilings judge by (see
    ``_darwin_process_phys_footprint_bytes``). Other platforms have no
    equivalent and answer None, so a caller keeps its own RSS reading there.
    """
    if not IS_MACOS:
        return None
    return _darwin_process_phys_footprint_bytes(pid)


def darwin_libproc_available() -> bool:
    """True when ``libproc`` loads, so the macOS memory and tree probes can answer."""
    return _darwin_libproc_handle() is not None


def darwin_footprint_tree_mb(
    pid: int, exclude_pids: "frozenset[int] | set[int]" = frozenset()
) -> float | None:
    """Footprint (MiB) of *pid* plus its live descendants on macOS; None elsewhere.

    The session RSS ceiling's macOS reading. Each process is measured by
    ``phys_footprint`` (see ``_darwin_process_phys_footprint_bytes``), the figure
    the runtime ceilings judge by. Children come from the kernel's own
    ``proc_listchildpids`` list of a live parent, so no stale parent field can
    attach a stranger's subtree. A pid in *exclude_pids* is skipped with its
    subtree, and a descendant whose footprint cannot be read adds nothing.

    None when the root itself cannot be read: "unknown, do not judge", the
    contract :func:`proc_rss_tree_mb_for_pid` keeps on Windows. In-process
    libproc calls only, one or two per process; executor thread, never the loop.
    """
    if not IS_MACOS:
        return None
    if pid in exclude_pids:
        return 0.0
    total = _darwin_process_phys_footprint_bytes(pid)
    if total is None:
        return None
    seen = {pid}
    frontier = [pid]
    while frontier:
        for child in darwin_child_pids(frontier.pop()) or ():
            if child in seen or child in exclude_pids:
                continue
            seen.add(child)
            total += _darwin_process_phys_footprint_bytes(child) or 0
            frontier.append(child)
    return total / (1024 * 1024)


def _darwin_process_start_microtime(pid: int) -> str | None:
    """macOS start time of *pid* via the atomic ``proc_bsdinfo`` reader."""
    identity = _darwin_process_start_identity(pid)
    return identity.start_id if identity is not None else None


def _darwin_process_cpu_nanos(pid: int) -> int | None:
    """macOS total (user+system) CPU nanoseconds of *pid* via ``libproc``.

    Same contract as the start-time probe above: no entitlement is needed for a
    same-uid process, nothing is exec'd, and a fill size other than the exact
    struct size means the assumed layout does not hold, so the answer is
    refused rather than sliced out of the wrong place.
    """
    lib = _darwin_libproc_handle()
    if lib is None:
        return None
    try:
        buf = ctypes.create_string_buffer(_DARWIN_PROC_TASKINFO_SIZE)
        filled = lib.proc_pidinfo(
            pid,
            _DARWIN_PROC_PIDTASKINFO,
            0,
            buf,
            _DARWIN_PROC_TASKINFO_SIZE,
        )
        if filled != _DARWIN_PROC_TASKINFO_SIZE:
            return None
        # Both x86_64 and arm64 macOS are little-endian.
        user = int.from_bytes(
            buf.raw[_DARWIN_PTI_TOTAL_USER_OFFSET:_DARWIN_PTI_TOTAL_SYSTEM_OFFSET], "little"
        )
        system = int.from_bytes(
            buf.raw[_DARWIN_PTI_TOTAL_SYSTEM_OFFSET : _DARWIN_PTI_TOTAL_SYSTEM_OFFSET + 8],
            "little",
        )
        return user + system
    except Exception:
        return None


# Further ``proc_bsdinfo`` fields the liveness backend reads: ``pbi_status`` is
# the second uint32 (offset 4). ``SZOMB`` is the BSD process-state code for
# a zombie; in practice the kernel refuses ``PROC_PIDTBSDINFO`` for a zombie
# outright, so the code is a second line of defence, not the primary test.
_DARWIN_PBI_STATUS_OFFSET = 4
_DARWIN_SZOMB = 5

# ``proc_pidpath`` fails unless handed ``PROC_PIDPATHINFO_MAXSIZE`` bytes, which
# is four times ``MAXPATHLEN``.
_DARWIN_PIDPATH_MAXSIZE = 4 * _DARWIN_MAXPATHLEN

# ``sysctl(CTL_KERN, KERN_PROCARGS2, pid)`` returns an ``int`` argc, the exec
# path, NUL padding, then the argv strings and finally the environment. The
# kernel truncates silently to the buffer handed in (no error), so a bounded
# buffer costs at most the tail of an unusually long argv — never a failure.
_DARWIN_CTL_KERN = 1
_DARWIN_KERN_PROCARGS2 = 49
_DARWIN_PROCARGS_BUFSIZE = 64 * 1024

# The environment sits AFTER argv in that same record, so the environ probe
# cannot share the argv probe's bound: the kernel truncates silently, and a
# process with a long argv -- a recursively self-appending launcher is exactly
# that shape -- would have its environment cut off and read as "no marker",
# failing closed on the very process an identity gate most needs to place. Sized
# at the kernel's own ``ARG_MAX`` ceiling on argv plus environment instead, so
# truncation is impossible rather than merely unlikely.
_DARWIN_PROCARGS_ENV_BUFSIZE = 1024 * 1024

# ``sysctl(CTL_KERN, KERN_PROC, KERN_PROC_PID, pid)`` answers for a ZOMBIE where
# ``proc_pidinfo`` refuses: the kernel walks its zombie list for this query as
# well as the live one, and a zombie's ``proc`` still carries its start instant.
# The record is a ``kinfo_proc`` whose leading ``extern_proc`` holds the same
# ``p_start`` the ``PROC_PIDTBSDINFO`` probe reports -- ``p_starttime`` (offset
# 0: int64 seconds, int32 microseconds) -- and the BSD state code ``p_stat``
# (offset 36; ``SZOMB`` above). The kernel writes exactly ``sizeof(kinfo_proc)``
# bytes per process, 648 on every 64-bit macOS; any other length means the
# layout these offsets assume does not hold, so the answer is refused rather
# than sliced out of the wrong place (the rule the libproc probes follow). A pid
# that does not exist is not an error: the call succeeds with a zero-length
# answer. ``KERN_PROC_PGRP`` lists every member of a process group the same way.
_DARWIN_KERN_PROC = 14
_DARWIN_KERN_PROC_PID = 1
_DARWIN_KERN_PROC_PGRP = 2
_DARWIN_KINFO_PROC_SIZE = 648
_DARWIN_KP_START_TVSEC_OFFSET = 0
_DARWIN_KP_START_TVUSEC_OFFSET = 8
_DARWIN_KP_STAT_OFFSET = 36
_DARWIN_KP_PID_OFFSET = 40
# Headroom for processes that join a group between the size query and the read,
# doubled on each of the retries a still-growing group is given.
_DARWIN_KINFO_PGRP_SLACK = 16
_DARWIN_KINFO_PGRP_ATTEMPTS = 4

# ``proc_listchildpids`` writes ``pid_t`` values and returns HOW MANY it wrote.
# A childless parent and a pid that does not exist both answer 0, so a caller
# that needs to tell them apart reads the parent's own facts first.
_DARWIN_CHILD_LIST_INITIAL = 1024


class DarwinProcessFacts(NamedTuple):
    """One process as ``PROC_PIDTBSDINFO`` describes it.

    ``start_secs`` is the absolute wall-clock start instant (``pbi_start_tvsec``
    + ``pbi_start_tvusec``), i.e. the same clock ``time.time()`` reads — the pair
    a caller compares to date a process against an event it stamped itself.
    """

    zombie: bool
    start_secs: float


_darwin_libproc_tree_bound = False


def _darwin_libproc_tree_handle() -> Any:
    """The cached ``libproc`` handle with the tree-walk entry points declared.

    Declared lazily and separately from :func:`_darwin_libproc_handle` so a
    libproc missing either symbol still serves the cwd / start-time / CPU probes
    that need only ``proc_pidinfo``. Returns None when the extra entry points
    cannot be bound.
    """
    global _darwin_libproc_tree_bound
    lib = _darwin_libproc_handle()
    if lib is None:
        return None
    if _darwin_libproc_tree_bound:
        return lib
    try:
        lib.proc_listchildpids.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        lib.proc_listchildpids.restype = ctypes.c_int
        lib.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        lib.proc_pidpath.restype = ctypes.c_int
    except Exception:
        return None
    _darwin_libproc_tree_bound = True
    return lib


def darwin_child_pids(ppid: int) -> list[int] | None:
    """Direct children of *ppid* via ``proc_listchildpids``; None when unreadable.

    In-process and unprivileged: pid enumeration needs no entitlement for any
    process. The buffer grows until the kernel's answer fits, so a parent with
    more children than the initial capacity is enumerated completely rather than
    truncated. An empty list is a genuine answer only for a parent that exists —
    the syscall answers 0 for an unknown pid too — so callers that assert
    absence pair this with :func:`darwin_process_facts` on the parent.
    """
    lib = _darwin_libproc_tree_handle()
    if lib is None:
        return None
    capacity = _DARWIN_CHILD_LIST_INITIAL
    try:
        while True:
            buf = ctypes.create_string_buffer(capacity * 4)
            count = lib.proc_listchildpids(ppid, buf, capacity * 4)
            if count < 0:
                return None
            if count < capacity:
                return list(struct.unpack_from(f"<{count}i", buf.raw, 0))
            capacity *= 2
    except Exception:
        return None


def darwin_process_facts(pid: int) -> DarwinProcessFacts | None:
    """``PROC_PIDTBSDINFO`` of *pid*, or None when the process cannot be read.

    None covers "gone", "zombie" (the kernel refuses the query for one) and
    "not ours to inspect" (another user's process) alike: each is a process the
    caller cannot attribute work to. A same-uid live process always answers.
    """
    lib = _darwin_libproc_handle()
    if lib is None:
        return None
    try:
        buf = ctypes.create_string_buffer(_DARWIN_PROC_BSDINFO_SIZE)
        filled = lib.proc_pidinfo(pid, _DARWIN_PROC_PIDTBSDINFO, 0, buf, _DARWIN_PROC_BSDINFO_SIZE)
        if filled != _DARWIN_PROC_BSDINFO_SIZE:
            return None
        status = struct.unpack_from("<I", buf.raw, _DARWIN_PBI_STATUS_OFFSET)[0]
        sec = struct.unpack_from("<Q", buf.raw, _DARWIN_PBI_START_TVSEC_OFFSET)[0]
        usec = struct.unpack_from("<Q", buf.raw, _DARWIN_PBI_START_TVUSEC_OFFSET)[0]
        if sec <= 0:
            return None
        return DarwinProcessFacts(zombie=status == _DARWIN_SZOMB, start_secs=sec + usec / 1_000_000)
    except Exception:
        return None


def darwin_process_path(pid: int) -> str | None:
    """Executable path of *pid* via ``proc_pidpath``, or None when unreadable."""
    lib = _darwin_libproc_tree_handle()
    if lib is None:
        return None
    try:
        buf = ctypes.create_string_buffer(_DARWIN_PIDPATH_MAXSIZE)
        length = lib.proc_pidpath(pid, buf, _DARWIN_PIDPATH_MAXSIZE)
        if length <= 0:
            return None
        return buf.raw[:length].decode("utf-8", errors="replace") or None
    except Exception:
        return None


# ``proc_pidinfo(PROC_PIDLISTFDS)`` lists a process's open descriptors as
# ``proc_fdinfo`` records: an int32 ``proc_fd`` and a uint32 ``proc_fdtype``
# (8 bytes each). ``PROX_FDTYPE_SOCKET`` marks a socket.
_DARWIN_PROC_PIDLISTFDS = 1
_DARWIN_PROC_FDINFO_SIZE = 8
_DARWIN_PROX_FDTYPE_SOCKET = 2
# ``proc_pidfdinfo(PROC_PIDFDSOCKETINFO)`` fills a ``socket_fdinfo``: a 24-byte
# ``proc_fileinfo`` then a ``socket_info``. Inside ``socket_info`` the
# 136-byte ``vinfo_stat`` is followed by the socket fields, putting ``soi_kind``
# at 232 and the ``soi_proto`` union at 240; for ``SOCKINFO_TCP`` that union is
# a ``tcp_sockinfo`` whose ``tcpsi_state`` follows the 80-byte ``in_sockinfo``.
# The union is sized by ``un_sockinfo`` (528), so the whole record is 792 bytes;
# the fill size doubles as the layout check, as for the probes above.
_DARWIN_PROC_PIDFDSOCKETINFO = 3
_DARWIN_SOCKET_FDINFO_SIZE = 792
_DARWIN_SOI_KIND_OFFSET = 24 + 232
_DARWIN_TCPSI_STATE_OFFSET = 24 + 240 + 80
_DARWIN_SOCKINFO_TCP = 2
_DARWIN_TSI_S_ESTABLISHED = 4
# Inside ``in_sockinfo`` (which opens ``tcp_sockinfo``): ``insi_vflag`` at 24,
# then ``insi_faddr`` at 32, a 16-byte union whose IPv4 form keeps the address
# in its last four bytes. ``INI_IPV4`` / ``INI_IPV6`` are the vflag bits.
_DARWIN_INSI_VFLAG_OFFSET = 24 + 240 + 24
_DARWIN_INSI_FADDR_OFFSET = 24 + 240 + 32
_DARWIN_INI_IPV4 = 0x1
_DARWIN_INI_IPV6 = 0x2
_DARWIN_IN6_LOOPBACK = bytes(15) + b"\x01"
_DARWIN_IN6_V4MAPPED_PREFIX = bytes(10) + b"\xff\xff"
# Descriptor-count bound for one listing. A process past it is not enumerated
# rather than enumerated in part, so a partial answer never reads as "none".
_DARWIN_MAX_FDS = 16384

_darwin_libproc_fd_bound = False


def _darwin_libproc_fd_handle() -> Any:
    """The cached ``libproc`` handle with ``proc_pidfdinfo`` declared, or None.

    Bound separately so a libproc without the symbol still serves every probe
    that needs only ``proc_pidinfo``.
    """
    global _darwin_libproc_fd_bound
    lib = _darwin_libproc_handle()
    if lib is None:
        return None
    if _darwin_libproc_fd_bound:
        return lib
    try:
        lib.proc_pidfdinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        lib.proc_pidfdinfo.restype = ctypes.c_int
    except Exception:
        return None
    _darwin_libproc_fd_bound = True
    return lib


def _darwin_peer_is_loopback(record: bytes) -> bool:
    """Whether a ``socket_fdinfo`` record's foreign address is loopback.

    An unknown address family reads as loopback, so a record this parser does
    not understand is never counted as a remote call.
    """
    vflag = record[_DARWIN_INSI_VFLAG_OFFSET]
    faddr = record[_DARWIN_INSI_FADDR_OFFSET : _DARWIN_INSI_FADDR_OFFSET + 16]
    if vflag & _DARWIN_INI_IPV4:
        return faddr[12] == 127
    if vflag & _DARWIN_INI_IPV6:
        if faddr == _DARWIN_IN6_LOOPBACK:
            return True
        return faddr[:12] == _DARWIN_IN6_V4MAPPED_PREFIX and faddr[12] == 127
    return True


def darwin_established_tcp_count(pid: int) -> int | None:
    """How many ESTABLISHED TCP sockets to a non-loopback peer *pid* holds.

    None when unreadable. A loopback peer is a local service, not a remote call.

    The macOS counterpart of reading ``/proc/<pid>/fd`` against
    ``/proc/<pid>/net/tcp``: in-process, no entitlement for a same-uid process,
    nothing exec'd. A socket whose record does not come back at the exact
    ``socket_fdinfo`` size is skipped, and a listing that cannot be read at all
    is None, so a caller never reads a layout mismatch as "no connections".
    """
    lib = _darwin_libproc_fd_handle()
    if lib is None:
        return None
    try:
        needed = lib.proc_pidinfo(pid, _DARWIN_PROC_PIDLISTFDS, 0, None, 0)
        if needed <= 0:
            return None
        # Headroom for descriptors opened between the size query and the read.
        size = min(
            needed + 32 * _DARWIN_PROC_FDINFO_SIZE, _DARWIN_MAX_FDS * _DARWIN_PROC_FDINFO_SIZE
        )
        buf = ctypes.create_string_buffer(size)
        filled = lib.proc_pidinfo(pid, _DARWIN_PROC_PIDLISTFDS, 0, buf, size)
        if filled <= 0 or filled % _DARWIN_PROC_FDINFO_SIZE:
            return None
        if filled >= size:
            # A full buffer may be a truncated one: refuse rather than undercount.
            return None
        info = ctypes.create_string_buffer(_DARWIN_SOCKET_FDINFO_SIZE)
        count = 0
        for off in range(0, filled, _DARWIN_PROC_FDINFO_SIZE):
            fd, fdtype = struct.unpack_from("<iI", buf.raw, off)
            if fdtype != _DARWIN_PROX_FDTYPE_SOCKET:
                continue
            got = lib.proc_pidfdinfo(
                pid, fd, _DARWIN_PROC_PIDFDSOCKETINFO, info, _DARWIN_SOCKET_FDINFO_SIZE
            )
            if got != _DARWIN_SOCKET_FDINFO_SIZE:
                continue
            kind = struct.unpack_from("<i", info.raw, _DARWIN_SOI_KIND_OFFSET)[0]
            if kind != _DARWIN_SOCKINFO_TCP:
                continue
            state = struct.unpack_from("<i", info.raw, _DARWIN_TCPSI_STATE_OFFSET)[0]
            if state == _DARWIN_TSI_S_ESTABLISHED and not _darwin_peer_is_loopback(info.raw):
                count += 1
        return count
    except Exception:
        return None


_darwin_libc_sysctl: Any = None
_darwin_libc_sysctl_loaded = False
_darwin_libc_sysctl_lock = threading.Lock()


def _darwin_sysctl_handle() -> Any:
    """Cached ``libc`` handle with ``sysctl`` and ``sysctlbyname`` declared, or None.

    Cached for the same reason as the libproc handle: the argv probe runs per
    descendant on the liveness oracle's cadence, and a fresh ``CDLL`` per call
    would dlopen every time. :func:`memory_pressure_level` reads through the
    same handle. Built once under a lock, with both prototypes declared before
    the handle is published and the loaded flag set last, so a concurrent first
    call waits for the build instead of reading a half-declared handle or None.
    """
    global _darwin_libc_sysctl, _darwin_libc_sysctl_loaded
    if _darwin_libc_sysctl_loaded:
        return _darwin_libc_sysctl
    with _darwin_libc_sysctl_lock:
        if _darwin_libc_sysctl_loaded:
            return _darwin_libc_sysctl
        handle: Any = None
        try:
            path = ctypes.util.find_library("c")
            if path is not None:
                libc = ctypes.CDLL(path)
                libc.sysctl.argtypes = [
                    ctypes.POINTER(ctypes.c_int),
                    ctypes.c_uint,
                    ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_size_t),
                    ctypes.c_void_p,
                    ctypes.c_size_t,
                ]
                libc.sysctl.restype = ctypes.c_int
                libc.sysctlbyname.argtypes = [
                    ctypes.c_char_p,
                    ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_size_t),
                    ctypes.c_void_p,
                    ctypes.c_size_t,
                ]
                handle = libc
        except Exception:
            handle = None
        _darwin_libc_sysctl = handle
        _darwin_libc_sysctl_loaded = True
        return handle


def darwin_process_argv(pid: int) -> list[str] | None:
    """argv of *pid* via ``sysctl KERN_PROCARGS2``, or None when unreadable.

    Same-uid processes only (the kernel answers EPERM for another user's), and
    bounded to :data:`_DARWIN_PROCARGS_BUFSIZE` — the kernel truncates rather
    than fails, so a very long argv comes back with its tail cut, which still
    carries the program and the head of its arguments. Returns None rather than
    an empty list when the record cannot be parsed, so a caller can fall back to
    the executable path instead of treating "unreadable" as "no arguments".
    """
    libc = _darwin_sysctl_handle()
    if libc is None:
        return None
    try:
        mib = (ctypes.c_int * 3)(_DARWIN_CTL_KERN, _DARWIN_KERN_PROCARGS2, pid)
        buf = ctypes.create_string_buffer(_DARWIN_PROCARGS_BUFSIZE)
        size = ctypes.c_size_t(_DARWIN_PROCARGS_BUFSIZE)
        if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
            return None
        raw = buf.raw[: size.value]
        if len(raw) < 4:
            return None
        argc = struct.unpack_from("<i", raw, 0)[0]
        if argc <= 0:
            return None
        rest = raw[4:]
        exe_end = rest.find(b"\0")
        if exe_end < 0:
            return None
        rest = rest[exe_end:].lstrip(b"\0")
        parts = rest.split(b"\0")[:argc]
        argv = [p.decode("utf-8", errors="replace") for p in parts if p]
        return argv or None
    except Exception:
        return None


def darwin_process_environ(pid: int) -> list[bytes] | None:
    """Exec-time environment of *pid* via ``sysctl KERN_PROCARGS2``, or None.

    Returns the raw ``KEY=VALUE`` entries. ``None`` means the record could not
    be read or parsed -- never an empty list for an unreadable process, so a
    caller can tell "no such variable" apart from "could not look".

    Same-uid processes only, and no entitlement or elevated privilege for our
    own: the same kernel record and the same permission contract
    :func:`darwin_process_argv` already reads. The environment here is the
    kernel's copy fixed at exec, which is why it is ownership evidence a
    process cannot forge for another, unlike anything on disk.

    The ``argc`` argv entries are skipped BY COUNT, empty strings included, so
    an *argument* that merely looks like an environment entry can never be read
    as one -- the point of the read is that a user's own shell can reproduce any
    argv.

    An Apple PLATFORM binary (``/bin/sleep``, ``/usr/bin/env``) is one of the
    ``None`` cases on macOS 26: the kernel answers with an argv-only record for
    it even to a same-uid reader (``ps -E`` shows no environment either), so
    the read fails closed and such a process is never identified as ours. The
    launchers this oracle exists for (``node``, ``python``, an MCP CLI) are
    never platform binaries; a test that needs a readable child must spawn one
    of those, not ``sleep``.
    """
    libc = _darwin_sysctl_handle()
    if libc is None:
        return None
    try:
        mib = (ctypes.c_int * 3)(_DARWIN_CTL_KERN, _DARWIN_KERN_PROCARGS2, pid)
        buf = ctypes.create_string_buffer(_DARWIN_PROCARGS_ENV_BUFSIZE)
        size = ctypes.c_size_t(_DARWIN_PROCARGS_ENV_BUFSIZE)
        if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
            return None
        raw = buf.raw[: size.value]
        if len(raw) < 4:
            return None
        argc = struct.unpack_from("<i", raw, 0)[0]
        if argc <= 0:
            return None
        rest = raw[4:]
        exe_end = rest.find(b"\0")
        if exe_end < 0:
            return None
        rest = rest[exe_end:].lstrip(b"\0")
        entries = [token for token in rest.split(b"\0")[argc:] if token]
        # No entries past argv is a record whose environment is missing, not a
        # process running with an empty one: every exec'd process has some.
        return entries or None
    except Exception:
        return None


class DarwinKinfoProc(NamedTuple):
    """One process as ``sysctl KERN_PROC`` describes it -- zombies included.

    ``start_id`` is formatted exactly as :func:`get_process_start_id` formats
    the libproc answer for the same process, so the two are comparable: both
    read the kernel's ``p_start`` instant.
    """

    pid: int
    zombie: bool
    start_id: str


def _darwin_kinfo_proc_parse(raw: bytes) -> DarwinKinfoProc | None:
    """One ``kinfo_proc`` record, or None when its start instant is implausible."""
    sec = struct.unpack_from("<q", raw, _DARWIN_KP_START_TVSEC_OFFSET)[0]
    usec = struct.unpack_from("<i", raw, _DARWIN_KP_START_TVUSEC_OFFSET)[0]
    stat = struct.unpack_from("<b", raw, _DARWIN_KP_STAT_OFFSET)[0]
    pid = struct.unpack_from("<i", raw, _DARWIN_KP_PID_OFFSET)[0]
    if sec <= 0 or usec < 0 or pid <= 0:
        return None
    return DarwinKinfoProc(pid=pid, zombie=stat == _DARWIN_SZOMB, start_id=f"{sec}.{usec:06d}")


_darwin_kinfo_size_mismatch_logged = False


def _darwin_kinfo_query(selector: int, arg: int, capacity: int) -> bytes | None:
    """Raw ``sysctl KERN_PROC/<selector>/<arg>`` bytes, or None when unreadable.

    Empty bytes is a real answer ("no such process / empty group"); None is
    "could not ask". A result that is not a whole number of records means the
    struct size assumed by the offsets is wrong, and is refused the same way --
    with one warning per process, because that refusal silently disables every
    zombie reader on this host and the teardown falls back to waiting out its
    grace on an exited leader. The callers poll, so it is not logged per call.
    """
    global _darwin_kinfo_size_mismatch_logged
    libc = _darwin_sysctl_handle()
    if libc is None:
        return None
    try:
        mib = (ctypes.c_int * 4)(_DARWIN_CTL_KERN, _DARWIN_KERN_PROC, selector, arg)
        buf = ctypes.create_string_buffer(capacity)
        size = ctypes.c_size_t(capacity)
        if libc.sysctl(mib, 4, buf, ctypes.byref(size), None, 0) != 0:
            return None
        raw = buf.raw[: size.value]
        if len(raw) % _DARWIN_KINFO_PROC_SIZE:
            if not _darwin_kinfo_size_mismatch_logged:
                _darwin_kinfo_size_mismatch_logged = True
                logger.warning(
                    "sysctl KERN_PROC returned %d bytes, not a multiple of the %d-byte "
                    "kinfo_proc this build expects; the macOS zombie readers are "
                    "disabled and an exited provider root is read as still running",
                    len(raw),
                    _DARWIN_KINFO_PROC_SIZE,
                )
            return None
        return raw
    except Exception:
        return None


def darwin_kinfo_proc(pid: int) -> DarwinKinfoProc | None:
    """``sysctl KERN_PROC_PID`` facts for *pid*, or None when it cannot be read.

    Unlike :func:`darwin_process_facts` this ANSWERS for a zombie, which is the
    one state the teardown code needs to see: an exited-but-unreaped child still
    owns its pid (and, for a group leader, its pgid), and its identity must stay
    readable so the reaper can prove the pid was not recycled before waiting on
    it. None covers "gone" and "unreadable" alike; callers that must tell those
    apart use :func:`darwin_pid_is_zombie`.
    """
    if pid <= 0:
        return None
    raw = _darwin_kinfo_query(_DARWIN_KERN_PROC_PID, pid, _DARWIN_KINFO_PROC_SIZE)
    if not raw:
        return None
    facts = _darwin_kinfo_proc_parse(raw)
    if facts is None or facts.pid != pid:
        return None
    return facts


def darwin_pid_is_zombie(pid: int) -> bool | None:
    """Whether *pid* is a zombie: True / False, or None when it cannot be read.

    "Gone" is reported as None by :func:`darwin_kinfo_proc`; here it is
    distinguished, because a caller asking "has this process finished running"
    needs a pid the kernel does not list to read as finished, not as unknown.
    """
    if pid <= 0:
        return None
    raw = _darwin_kinfo_query(_DARWIN_KERN_PROC_PID, pid, _DARWIN_KINFO_PROC_SIZE)
    if raw is None:
        return None
    if not raw:
        return True  # the kernel has no such process: it exited and was reaped
    facts = _darwin_kinfo_proc_parse(raw)
    if facts is None or facts.pid != pid:
        return None
    return facts.zombie


def darwin_pgroup_members(pgid: int) -> list[DarwinKinfoProc] | None:
    """Every process the kernel lists in group *pgid*, or None when unreadable.

    Zombies are included and flagged, so a caller can tell a group that is
    genuinely empty from one held open only by its retained zombie leader --
    ``killpg(pgid, 0)`` cannot make that distinction. Sized by a first query
    with slack for processes that join between the two calls. A group that
    outgrows the slack fails the read (``ENOMEM``), and that failure is retried
    with the slack doubled each time rather than reported: the caller reads None
    as "cannot prove the group is ours" and withholds the group SIGKILL, so a
    tree forking fast enough during the teardown must not be able to make its
    own group unreadable. An answer still overflowing after the last attempt is
    refused (None) rather than returned truncated.
    """
    if pgid <= 0:
        return None
    libc = _darwin_sysctl_handle()
    if libc is None:
        return None
    mib = (ctypes.c_int * 4)(_DARWIN_CTL_KERN, _DARWIN_KERN_PROC, _DARWIN_KERN_PROC_PGRP, pgid)
    slack = _DARWIN_KINFO_PGRP_SLACK
    for _attempt in range(_DARWIN_KINFO_PGRP_ATTEMPTS):
        try:
            size = ctypes.c_size_t(0)
            if libc.sysctl(mib, 4, None, ctypes.byref(size), None, 0) != 0:
                return None
        except Exception:
            return None
        raw = _darwin_kinfo_query(
            _DARWIN_KERN_PROC_PGRP, pgid, size.value + slack * _DARWIN_KINFO_PROC_SIZE
        )
        if raw is not None:
            members: list[DarwinKinfoProc] = []
            for start in range(0, len(raw), _DARWIN_KINFO_PROC_SIZE):
                facts = _darwin_kinfo_proc_parse(raw[start : start + _DARWIN_KINFO_PROC_SIZE])
                if facts is not None:
                    members.append(facts)
            return members
        slack *= 2
    return None


# macOS ``struct proc_bsdinfo`` (PROC_PIDTBSDINFO, 136 bytes) field offsets used
# below. Verified empirically against ``ps -o lstart=`` on darwin: ``pbi_ppid``
# at 16 (see get_ppid), ``pbi_start_tvsec`` at 120, ``pbi_start_tvusec`` at 128.
_DARWIN_BSDINFO_SIZE = 136
_DARWIN_OFF_START_TVSEC = 120
_DARWIN_OFF_START_TVUSEC = 128


class _DarwinLibprocUnavailable(Exception):
    """``libproc`` could not be loaded -- distinct from it refusing one pid."""


# Named by the path callers import it from, so a traceback, a pickle or any
# ``module.qualname`` rendering of the class says ``kiro_crew.platform_compat``.
_DarwinLibprocUnavailable.__module__ = "kiro_crew.platform_compat"


def _darwin_libproc_start_id(pid: int) -> str | None:
    """macOS start instant of *pid* via ``proc_pidinfo``, or None when refused.

    Refused for a zombie (the kernel has no task to describe) and for another
    user's process alike; :func:`get_process_start_id` owns the zombie fallback.
    Raises :class:`_DarwinLibprocUnavailable` when the library itself cannot be
    loaded, so the caller can tell "this pid was refused" (fall back) from
    "nothing here can be asked" (no identity).
    """
    try:
        path = ctypes.util.find_library("proc")
        lib = ctypes.CDLL(path) if path is not None else None
    except Exception:
        lib = None
    if lib is None:
        raise _DarwinLibprocUnavailable
    try:
        lib.proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        lib.proc_pidinfo.restype = ctypes.c_int
        buf = ctypes.create_string_buffer(_DARWIN_BSDINFO_SIZE)
        ret = lib.proc_pidinfo(pid, 3, 0, buf, _DARWIN_BSDINFO_SIZE)  # PROC_PIDTBSDINFO=3
        if ret <= 0:
            return None
        sec = struct.unpack_from("<Q", buf.raw, _DARWIN_OFF_START_TVSEC)[0]
        usec = struct.unpack_from("<Q", buf.raw, _DARWIN_OFF_START_TVUSEC)[0]
        if sec == 0:
            return None  # implausible — treat as unknown rather than a value
        return f"{sec}.{usec:06d}"
    except Exception:
        return None


def _file_identity(target: str | os.PathLike | os.stat_result) -> os.stat_result | None:
    """*target* itself when it is already a stat result, else ``os.stat`` of the path."""
    if isinstance(target, os.stat_result):
        return target
    try:
        return os.stat(target)
    except OSError:
        return None


def flock_owner_pid(path: str | os.PathLike | os.stat_result) -> int | None:
    """PID recorded against an ``flock`` on *path*, via ``/proc/locks``.

    *path* may instead be a stat result already taken of the file -- the
    ``fstat`` of a descriptor the caller holds -- which is matched as given
    rather than by re-resolving a name that may since point elsewhere.

    This is the pid that ACQUIRED the lock, which is not always a live process:
    an ``flock`` belongs to the open file description, so when the acquirer dies
    and a forked child still holds the inherited fd, the kernel keeps reporting
    the DEAD acquirer here. Verified on Linux 5.x -- an orphaned lock listed
    ``FLOCK ADVISORY WRITE <dead parent pid>`` while the surviving child held the
    fd. So a returned pid that is dead is positive evidence of an inherited fd,
    and no ``/proc`` surface names the inheritor: use
    :func:`pids_holding_file` for candidates and do not present them as owners.

    Returns ``None`` on non-Linux, when ``/proc`` is unreadable, or when no
    ``FLOCK`` entry matches the file. Blocked waiters (``->`` rows) are skipped.

    Matching is on the full ``major:minor:inode`` triple that ``/proc/locks``
    prints, because inode numbers are only unique WITHIN a filesystem -- an
    unrelated flock on another device can share this inode's number, and
    accepting it would name a completely unrelated process. The device halves are
    hex (``%02x``), the inode decimal (``%lu``). On filesystems whose ``s_dev``
    differs from the ``st_dev`` that ``stat`` reports (btrfs subvolumes and
    overlayfs use anonymous devices) the triple will not match and this returns
    ``None``. That is the safe direction to fail: the caller degrades to its
    "holder could not be identified" wording instead of naming a wrong process.

    Two kernel surfaces are read, because the first is not a snapshot.
    ``/proc/locks`` is a ``seq_file`` over a per-cpu list that the kernel hands
    out one page (~65 rows) per ``read()`` and resumes BY ORDINAL INDEX: a lock
    that any process on the host releases between two of those reads shifts
    every later row down by one, and the row that lands on the resume index is
    never printed. A lock that is held the whole time therefore goes missing
    from one scan in a while -- measured at 7.7% of scans on a 32-cpu host with
    four processes churning flocks beside a held one -- and a startup refusal
    that names "a running gateway" instead of its pid, or a ``stop`` that
    cannot say who holds the home, is the user-visible result. So a scan that
    finds no row is corroborated by ``/proc/<pid>/fdinfo/<fd>`` of every
    descriptor open on the inode (:func:`_descriptors_on`): its ``lock:`` rows
    are one atomic record per descriptor, in the same format, matched on the
    same triple. That walk costs one ``stat`` per open descriptor on the host
    and reaches only processes of this user, so it runs on a miss, not first.
    """
    if sys.platform != "linux":
        return None
    info = _file_identity(path)
    if info is None:
        return None
    want = (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)
    pid = _flock_acquirer_in_proc_locks(want)
    if pid is None:
        pid = _flock_acquirer_in_fdinfo(info, want)
    return pid


def _flock_acquirer_row(fields: list[str], want: tuple[int, int, int]) -> int | None:
    """The acquirer pid of a ``/proc/locks``-format row on the *want* triple, else ``None``.

    *fields* is the row split on whitespace, starting at its ordinal:
    ``"23: FLOCK ADVISORY WRITE 39542 103:01:146872 0 EOF"``. A blocked waiter is
    ``"23: -> FLOCK ..."`` and owns nothing; other lock types, short rows and
    unparsable fields are skipped the same way.
    """
    if len(fields) < 6 or "->" in fields[:2] or fields[1] != "FLOCK":
        return None
    try:
        pid = int(fields[4])
        major, minor, ino = fields[5].split(":")
        found = (int(major, 16), int(minor, 16), int(ino))
    except (ValueError, IndexError):
        return None
    return pid if found == want else None


def _flock_acquirer_in_proc_locks(want: tuple[int, int, int]) -> int | None:
    """First ``FLOCK`` acquirer ``/proc/locks`` prints on *want*; ``None`` on a miss or an unreadable file."""
    try:
        # Streamed, not slurped: /proc/locks is unbounded (one row per lock
        # system-wide) and this runs on a startup failure path.
        with open("/proc/locks", encoding="utf-8") as handle:
            for row in handle:
                pid = _flock_acquirer_row(row.split(), want)
                if pid is not None:
                    return pid
    except OSError:
        return None
    return None


def _flock_acquirer_in_fdinfo(target: os.stat_result, want: tuple[int, int, int]) -> int | None:
    """First ``FLOCK`` acquirer any ``/proc/<pid>/fdinfo/<fd>`` on *target*'s inode prints on *want*.

    ``fdinfo`` prints the locks taken THROUGH that one open file description as
    ``"lock:\t1: FLOCK ADVISORY WRITE 39542 103:01:146872 0 EOF"``, the
    ``/proc/locks`` row behind a ``lock:`` tag, written in one record and so
    immune to the page-boundary skip of the global list. The triple is still
    checked: the descriptor can be closed and reused for another file between
    the ``stat`` that selected it and the read. ``None`` when ``/proc`` cannot
    be listed or no readable descriptor carries a matching row.
    """
    try:
        for pid, fd_name in _descriptors_on(target):
            try:
                with open(f"/proc/{pid}/fdinfo/{fd_name}", encoding="utf-8") as handle:
                    for row in handle:
                        if not row.startswith("lock:"):
                            continue
                        acquirer = _flock_acquirer_row(row.split()[1:], want)
                        if acquirer is not None:
                            return acquirer
            except OSError:
                # Closed, or the process is gone, between the listing and here.
                continue
    except OSError:
        return None
    return None


def parent_pid(pid: int) -> int | None:
    """PPID of *pid* from ``/proc/<pid>/stat``, or ``None`` if unknowable.

    It corroborates that a candidate process really is orphaned: a child
    reparented to init after its parent died reports ``1``. ``None`` means
    "unknown", which callers must not read as either answer.
    """
    if sys.platform != "linux":
        return None
    stat = read_proc_stat(pid)
    return stat.ppid if stat is not None else None


def pids_holding_file(path: str | os.PathLike | os.stat_result) -> list[int] | None:
    """PIDs with an open fd on *path*, matched by inode via ``/proc/*/fd``.

    *path* may be a stat result instead, as for :func:`flock_owner_pid`.

    These are CANDIDATE OPENERS, not lock owners. Any process may open the file
    without locking it, and an inherited ``flock`` has no live owner to identify
    (see :func:`flock_owner_pid`), so callers must not present a pid from here as
    the holder. Answering "who has this file open" is still the only way to reach
    the process that inherited a dead acquirer's descriptor.

    Matching is by ``(st_dev, st_ino)`` rather than by link target so a bind
    mount (a jailed gateway sees a different path for the same inode) still
    resolves. PIDs whose ``/proc`` entry we cannot read are skipped: the result
    is a best-effort lower bound.

    Returns ``None`` on non-Linux, where there is no ``/proc`` to walk.
    """
    if sys.platform != "linux":
        return None
    target = _file_identity(path)
    if target is None:
        return None
    holders: list[int] = []
    try:
        for pid, _fd_name in _descriptors_on(target):
            # Descriptors arrive grouped by pid, so one pid is named once.
            if not holders or holders[-1] != pid:
                holders.append(pid)
    except OSError:
        return None
    return holders


def _descriptors_on(target: os.stat_result) -> Iterator[tuple[int, str]]:
    """``(pid, fd)`` for every descriptor open on *target*'s inode, via ``/proc/*/fd``.

    Yielded grouped by pid, in ``/proc`` order. Matching is by
    ``(st_dev, st_ino)`` of the descriptor's link target, so a bind mount (a
    jailed gateway sees a different path for the same inode) still resolves. A
    pid whose ``fd`` directory cannot be read -- dead between the listing and
    here, or another user's process -- is skipped, as is a descriptor that
    closes under the ``stat``; raises ``OSError`` only when ``/proc`` itself
    cannot be listed. ``/proc/<pid>/fd`` is enumerated by descriptor NUMBER,
    not by ordinal, so a neighbouring close does not hide an open descriptor
    the way a released lock hides a ``/proc/locks`` row.
    """
    key = (target.st_dev, target.st_ino)
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        fd_dir = f"/proc/{entry}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd_name in fds:
            try:
                st = os.stat(f"{fd_dir}/{fd_name}")
            except OSError:
                continue
            if (st.st_dev, st.st_ino) == key:
                yield int(entry), fd_name


def _proc_status_rss_kb(pid: int) -> int:
    """RSS (KiB) of a single *pid* from ``/proc/<pid>/status``, or -1.

    Reads ``VmRSS``, so the figure matches ``ps -o rss=`` for that one process.
    Distinct from :func:`proc_rss_bytes_for_pid`, which reads ``statm`` pages and
    has a Windows path: this one is the Linux subtree walk's per-process read and
    keeps ``-1`` as its "unreadable" sentinel rather than ``None``.
    """
    rss_kb = read_proc_status_int(pid, "VmRSS")
    return -1 if rss_kb is None else rss_kb


def _stat_tokens(stat: bytes) -> "list[bytes] | None":
    """The fields after ``comm`` in raw ``/proc/<pid>/stat`` bytes, or None.

    ``comm`` is parenthesised and may contain spaces and ``)``, so the split is
    after the LAST ``)``. It is also arbitrary bytes -- any process may name
    itself through ``prctl(PR_SET_NAME)``, and the kernel truncates a multibyte
    name at 15 bytes mid-character -- so the line is never decoded. Index 0 is
    ``state`` (field 3), so field *N* is index *N - 3*. None only when the line
    has no ``)`` at all; any other damage shows up as missing or non-numeric
    tokens.
    """
    rparen = stat.rfind(b")")
    if rparen < 0:
        return None
    return stat[rparen + 1 :].split()


#: Longest stat token read as a number: a 64-bit counter has 20 digits, and the
#: bound keeps ``int()`` clear of the interpreter's digit limit on a hostile line.
_STAT_TOKEN_MAX_DIGITS = 20


def _stat_token_int(tokens: "list[bytes] | None", index: int) -> "int | None":
    """Post-``comm`` token *index* as a non-negative int, or None when absent or not digits."""
    if tokens is None or index >= len(tokens):
        return None
    token = tokens[index]
    if not token.isdigit() or len(token) > _STAT_TOKEN_MAX_DIGITS:
        return None
    return int(token)


def _parse_ppid(stat: bytes) -> "int | None":
    """The parent pid (field 4) from raw ``/proc/<pid>/stat`` bytes, or None."""
    return _stat_token_int(_stat_tokens(stat), 1)


class ProcStat(NamedTuple):
    """Fields of one ``/proc/<pid>/stat`` line.

    Each is None on its own when its token is missing or not a number, which no
    line the kernel writes produces; a fixture or a truncated read can.
    ``ProcStat()`` is the reading with every field unknown.
    """

    state: str | None = None
    ppid: int | None = None
    pgrp: int | None = None
    session: int | None = None
    start_ticks: int | None = None
    rss_pages: int | None = None
    cpu_ticks: int | None = None


def _linux_proc_root(proc_root: "Path | None") -> "Path | None":
    """*proc_root* when a fixture process table is given, else ``/proc`` on Linux.

    None off Linux with no fixture: the ``/proc`` readers then answer "unknown".
    """
    if proc_root is not None:
        return proc_root
    return Path("/proc") if IS_LINUX else None


def read_proc_stat(pid: int, *, proc_root: "Path | None" = None) -> "ProcStat | None":
    """*pid*'s ``/proc/<pid>/stat`` from ONE bytes read, or None. Linux only.

    The stat reader new code uses. It never decodes ``comm`` (see :func:`_stat_tokens`): a
    text read raises ``UnicodeDecodeError`` on a process whose name is not
    UTF-8, which an ``except OSError`` does not catch. Every field comes from the
    same read, so a caller needing several never mixes two processes behind a
    recycled pid. ``start_ticks`` is in clock ticks since boot;
    :func:`process_age_secs` turns it into an age. ``rss_pages`` is in pages of
    ``SC_PAGE_SIZE``. ``cpu_ticks`` is utime plus stime in clock ticks, so a
    caller differencing two reads gets a CPU rate whose identity check
    (``start_ticks``) came from the same read; it is None unless both are present.

    None when the file cannot be read (gone, permission, no ``/proc``) or the
    line has no ``)``; otherwise a :class:`ProcStat` whose fields may each be
    None. *proc_root* substitutes a fixture process table on every host.
    """
    proc_root = _linux_proc_root(proc_root)
    if proc_root is None:
        return None
    try:
        raw = (proc_root / str(pid) / "stat").read_bytes()
    except OSError:
        return None
    tokens = _stat_tokens(raw)
    if tokens is None:
        return None
    utime, stime = _stat_token_int(tokens, 11), _stat_token_int(tokens, 12)
    return ProcStat(
        state=tokens[0].decode("ascii", "replace") if tokens else None,
        ppid=_stat_token_int(tokens, 1),
        pgrp=_stat_token_int(tokens, 2),
        session=_stat_token_int(tokens, 3),
        start_ticks=_stat_token_int(tokens, 19),
        rss_pages=_stat_token_int(tokens, 21),
        cpu_ticks=None if utime is None or stime is None else utime + stime,
    )


#: ``stat`` states of a process that has finished running: a zombie, or one
#: being torn down (``X``; older kernels print ``x``).
_LINUX_EXITED_STATES = frozenset({"Z", "X", "x"})


def linux_pgroup_members(
    pgid: int, *, proc_root: "Path | None" = None
) -> "dict[int, int | None] | None":
    """``{pid: start_ticks}`` for every RUNNING member of process group *pgid*. Linux only.

    The Linux counterpart of :func:`darwin_pgroup_members`: one pass over
    ``/proc``, each ``stat`` read as bytes with ``comm`` never decoded, so a
    member whose name is not UTF-8 is still a member. A process that has
    finished running (``Z``/``X``) does not hold the group open and is left
    out. The start ticks come from the SAME read as the group and state, so a
    caller pinning a later signal to them never pairs a recycled pid with the
    member it admitted; a member whose start cannot be read maps to None.

    None -- off Linux, and when ``/proc`` cannot be listed -- means "unknown",
    never "empty". *proc_root* substitutes a fixture process table on every host.
    """
    proc_root = _linux_proc_root(proc_root)
    if proc_root is None:
        return None
    try:
        names = os.listdir(proc_root)
    except OSError:
        logger.debug("linux_pgroup_members: cannot list %s", proc_root, exc_info=True)
        return None
    members: dict[int, int | None] = {}
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"{proc_root}/{name}/stat", "rb") as fh:
                tokens = _stat_tokens(fh.read())
        except OSError:
            continue  # exited between the listing and the read
        if (
            tokens
            and _stat_token_int(tokens, 2) == pgid
            and tokens[0].decode("ascii", "replace") not in _LINUX_EXITED_STATES
        ):
            members[int(name)] = _stat_token_int(tokens, 19)
    return members


def read_proc_status_int(pid: int, label: str, *, proc_root: "Path | None" = None) -> "int | None":
    """The first number on *label*'s line of ``/proc/<pid>/status``, or None. Linux only.

    ``PPid``, ``Threads`` and ``VmRSS`` (in KiB) are the labels callers read.
    ONE bytes read, never decoded as a whole: the ``Name:`` line is the raw
    comm, so a strict text read raises ``UnicodeDecodeError`` on a process whose
    name is not UTF-8 -- and a strict ASCII read on any non-ASCII name, even a
    valid UTF-8 one. None when the file is unreadable, the label is absent, or
    its value is not a number. *proc_root* substitutes a fixture process table
    on every host.
    """
    proc_root = _linux_proc_root(proc_root)
    if proc_root is None:
        return None
    prefix = label.encode("ascii") + b":"
    try:
        with open(f"{proc_root}/{pid}/status", "rb") as fh:
            for line in fh:
                if line.startswith(prefix):
                    value = line[len(prefix) :].split()
                    return _stat_token_int(value, 0)
    except OSError:
        return None
    return None


def proc_child_map(*, proc_root: "Path | None" = None) -> "dict[int, list[int]] | None":
    """Every live process's children, from ONE pass over ``/proc``. Linux only.

    For the caller that needs the subtrees of MANY roots at once. Asking the
    kernel per root (:func:`_proc_children`) costs one read per THREAD of every
    process visited, so a runtime carrying twenty threads is twenty reads and a
    sampler walking forty roots pays that on each of them. A parent map is one
    read per process on the host and answers every root from memory. Measured on
    a 1713-process host sampling 40 session trees of 734 processes in total:
    11296 children reads taking 432ms against 1717 ``stat`` reads taking 70ms,
    both routes returning the same 40 trees.

    The two routes agree for a process whose parent is alive, by construction:
    ``task/<tid>/children`` is the kernel's child list and ``stat``'s ppid is its
    inverse. Where they part, this map is the more complete of the two -- the
    child list is documented as reliable only for a frozen task, so a live tree
    can come back short through it. A process reparented after its parent died is
    outside both: its ppid becomes 1 and no surviving parent lists it.

    ``None`` -- off Linux, and when ``/proc`` cannot be listed -- means "walk the
    roots yourself", never "this host has no processes": an empty map would read
    as every tree being the root alone.

    Two host-wide snapshots already exist and neither is used here, for a
    measured reason each:

    * :func:`_posix_process_parent_map` spawns ``ps -Ao pid=,ppid=``. Its edges
      agree with this map exactly (measured on a 1743-process host: 335 parents
      in both, zero whose child sets differ), but it costs 122-129ms against
      66-86ms for this pass, and it puts a subprocess spawn on a 5-second
      browser poll. ``_get_rss_tree_mb``'s own note records the same choice for
      the same reason: the Linux branch reads ``/proc`` directly and never
      spawns.
    * :func:`parent_pid` answers the same question for one pid through
      :func:`read_proc_stat`, which parses every field of the line. This pass
      needs only the ppid of every process on the host, so it parses that one
      field with :func:`_parse_ppid`. Both read the file as bytes: a process
      whose ``comm`` is not valid UTF-8 -- any process may set its own name to
      arbitrary bytes with ``prctl(PR_SET_NAME)`` -- answers through either,
      and one test pins both.

    *proc_root* substitutes a fixture process table and is honoured on every
    host, as :func:`read_proc_stat` does.

    Windows deliberately has NO branch here: Toolhelp's ``th32ParentProcessID``
    is never cleared when a parent exits and Windows recycles PIDs aggressively,
    so a raw Toolhelp parent map can attach an unrelated subtree to a recycled
    PID. A Windows caller walks a lineage-validated route instead
    (:func:`proc_rss_tree_mb_for_pid`).

    Blocking: one read per process on the host. Executor thread, never the loop.
    """
    proc_root = _linux_proc_root(proc_root)
    if proc_root is None:
        return None
    try:
        names = os.listdir(proc_root)
    except OSError:
        logger.debug("proc_child_map: cannot list %s", proc_root, exc_info=True)
        return None
    children: dict[int, list[int]] = {}
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"{proc_root}/{name}/stat", "rb") as fh:
                raw = fh.read()
        except OSError:
            # Exited between the listing and the open: it is in no live tree.
            continue
        ppid = _parse_ppid(raw)
        if ppid is not None:
            children.setdefault(ppid, []).append(int(name))
    return children


def proc_cpu_jiffies_for_pids(pids: "list[int]") -> int:
    """Sum utime+stime (clock ticks) over pids already walked.

    The counterpart of :func:`proc_subtree_sample` for a caller that ALREADY
    holds the subtree, so the tree is not enumerated a second time to reach a
    total over the same processes. Each pid is read exactly as the walker reads
    it (:func:`_proc_cpu_jiffies`), so the figure is the walker's figure over
    that set and not a second definition of it.
    """
    return sum(_proc_cpu_jiffies(p) for p in pids)


def _proc_children(pid: int) -> list[int]:
    """Direct child PIDs of *pid* via ``/proc/<pid>/task/<tid>/children``.

    Uses the kernel-provided children list (``CONFIG_PROC_CHILDREN``), so no
    ``pgrep``/full-table scan. Returns ``[]`` if the file is unavailable.

    Per-root: it reads one ``children`` file per THREAD of *pid*. A caller that
    needs many roots' trees in one pass should build
    :func:`proc_child_map` instead.
    """
    kids: list[int] = []
    task_dir = f"/proc/{pid}/task"
    try:
        tids = os.listdir(task_dir)
    except OSError:
        return kids
    for tid in tids:
        try:
            with open(f"{task_dir}/{tid}/children", encoding="ascii") as fh:
                kids.extend(int(tok) for tok in fh.read().split())
        except (OSError, ValueError):
            continue
    return kids


def _parse_cpu_jiffies(stat: bytes) -> int:
    """Sum utime+stime (clock ticks) from raw ``/proc/<pid>/stat`` bytes.

    Splits after the final ``)`` so a ``comm`` containing spaces/parens is
    handled. utime/stime are fields 14/15 (1-indexed) → indices 11/12 of the
    post-comm tokens. Returns 0 on any parse error.
    """
    tokens = _stat_tokens(stat)
    utime, stime = _stat_token_int(tokens, 11), _stat_token_int(tokens, 12)
    if utime is None or stime is None:
        return 0
    return utime + stime


def _proc_cpu_jiffies(pid: int) -> int:
    """utime+stime (clock ticks) for a single pid, 0 on error."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            return _parse_cpu_jiffies(fh.read())
    except OSError:
        return 0
