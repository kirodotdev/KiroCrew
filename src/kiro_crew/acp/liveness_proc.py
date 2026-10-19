"""Process-tree readers for the liveness oracle: the ``/proc`` helpers and the darwin
``libproc`` backend.

Every reader is best-effort and answers None or empty on any error, so a probe never
raises into the oracle. The ``/proc`` helpers take ``proc_root``, so a test hands them a
fake tree; :func:`select_darwin_backend` picks :class:`LibprocBackend` on a host without
procfs. A new process-tree reader goes in this module; :mod:`kiro_crew.acp.liveness`
re-exports every name here.
"""

from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Protocol

from kiro_crew import platform_compat
from kiro_crew.acp.liveness_evidence import _READ_SYSCALL_NRS

# ── /proc helpers (pure, best-effort — return None/empty on any error) ──


def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def read_pid_stat(proc_root: str, pid: int) -> tuple[str, float, int] | None:
    """Return ``(state, starttime_ticks, cpu_ticks)`` from ``/proc/<pid>/stat``.

    ``comm`` may contain spaces/parens, so fields are parsed after the LAST
    ``)``. Returns None when the process is gone or the line is malformed.
    """
    raw = _read_text(f"{proc_root}/{pid}/stat")
    if not raw:
        return None
    rparen = raw.rfind(")")
    if rparen < 0:
        return None
    fields = raw[rparen + 1 :].split()
    # fields[0] is stat field 3 (state); utime=14, stime=15, starttime=22
    # (1-based stat numbering) -> indexes 11, 12, 19 here.
    try:
        state = fields[0]
        cpu = int(fields[11]) + int(fields[12])
        starttime = float(fields[19])
    except (IndexError, ValueError):
        return None
    return state, starttime, cpu


def iter_descendants(proc_root: str, pid: int) -> list[int]:
    """Return ``[pid, *descendants]`` via ``/proc/<p>/task/<tid>/children``.

    Best-effort BFS (mirrors runtime._iter_descendant_pids, parameterized on
    ``proc_root`` so the oracle stays a leaf module and tests can fake the
    tree). Returns ``[pid]`` when the children interface is unavailable.
    """
    order: list[int] = []
    visited: set[int] = set()
    stack = [pid]
    while stack:
        p = stack.pop()
        if p in visited:
            continue
        visited.add(p)
        order.append(p)
        try:
            entries = os.listdir(f"{proc_root}/{p}/task")
        except OSError:
            continue
        for tid in entries:
            tokens = (_read_text(f"{proc_root}/{p}/task/{tid}/children") or "").split()
            for tok in tokens:
                try:
                    cpid = int(tok)
                except ValueError:
                    continue
                if cpid not in visited:
                    stack.append(cpid)
    return order


def direct_children(proc_root: str, pid: int) -> list[int]:
    """*pid*'s direct children from ``/proc/<pid>/task/<tid>/children``.

    Empty both for a childless process and for an unreadable interface; the one
    caller only uses it to widen an exclusion, where either reading is safe.
    """
    children: list[int] = []
    try:
        tids = os.listdir(f"{proc_root}/{pid}/task")
    except OSError:
        return children
    for tid in tids:
        for tok in (_read_text(f"{proc_root}/{pid}/task/{tid}/children") or "").split():
            try:
                children.append(int(tok))
            except ValueError:
                continue
    return children


def children_interface_readable(proc_root: str, pid: int) -> bool:
    """Whether *pid*'s child list can be read at all.

    ``iter_descendants`` walks ``/proc/<p>/task/<tid>/children`` and returns just
    ``[pid]`` both for a genuinely childless process and for a host where that
    interface does not exist (no procfs, a kernel built without
    CONFIG_PROC_CHILDREN, a sandbox that hides the subtree). Only the first case
    is evidence about the tree, so the absent-shell-child claim requires this to
    be True — the file reading as EMPTY is the proof, its content is not.
    """
    try:
        tids = os.listdir(f"{proc_root}/{pid}/task")
    except OSError:
        return False
    return any(_read_text(f"{proc_root}/{pid}/task/{tid}/children") is not None for tid in tids)


def read_cmdline(proc_root: str, pid: int) -> str:
    raw = _read_text(f"{proc_root}/{pid}/cmdline")
    if not raw:
        return ""
    return raw.replace("\0", " ").strip()


def read_io_bytes(proc_root: str, pid: int) -> int | None:
    """Sum of rchar+wchar from ``/proc/<pid>/io`` (any byte movement counts)."""
    raw = _read_text(f"{proc_root}/{pid}/io")
    if not raw:
        return None
    total = 0
    seen = False
    for line in raw.splitlines():
        if line.startswith(("rchar:", "wchar:")):
            try:
                total += int(line.split(":", 1)[1])
                seen = True
            except (IndexError, ValueError):
                continue
    return total if seen else None


def read_wchan(proc_root: str, pid: int) -> str:
    return (_read_text(f"{proc_root}/{pid}/wchan") or "").strip()


def blocked_read_fd(proc_root: str, pid: int) -> int | None:
    """The fd a process is blocked in read(2) on, from ``/proc/<pid>/syscall``.

    Returns None when the process is not blocked in a read syscall (or the
    interface is unavailable). Accepts the x86_64 and aarch64 read numbers.
    """
    raw = _read_text(f"{proc_root}/{pid}/syscall")
    if not raw:
        return None
    parts = raw.split()
    if len(parts) < 2:
        return None
    try:
        nr = int(parts[0])
        fd = int(parts[1], 16)
    except ValueError:
        return None
    if nr not in _READ_SYSCALL_NRS:
        return None
    return fd


def fd_target(proc_root: str, pid: int, fd: int) -> str:
    try:
        return os.readlink(f"{proc_root}/{pid}/fd/{fd}")
    except OSError:
        return ""


def pipe_writer_pid(proc_root: str, pids: list[int], target: str, reader: int) -> int | None:
    """A pid in *pids* other than *reader* holding pipe *target* open for writing.

    Reads ``/proc/<pid>/fd`` links and the ``flags`` line of ``fdinfo``
    (``O_WRONLY`` / ``O_RDWR``). None when no such holder is found.
    """
    for p in pids:
        if p == reader:
            continue
        try:
            fds = os.listdir(f"{proc_root}/{p}/fd")
        except OSError:
            continue
        for fd in fds:
            if not fd.isdigit() or fd_target(proc_root, p, int(fd)) != target:
                continue
            m = re.search(
                r"^flags:\s*([0-7]+)$", _read_text(f"{proc_root}/{p}/fdinfo/{fd}") or "", re.M
            )
            if m and int(m.group(1), 8) & 3:
                return p
    return None


def socket_inodes(proc_root: str, pid: int) -> set[str]:
    """Socket inode numbers held open by *pid* (from ``/proc/<pid>/fd``)."""
    inodes: set[str] = set()
    try:
        fds = os.listdir(f"{proc_root}/{pid}/fd")
    except OSError:
        return inodes
    for fd in fds:
        try:
            target = os.readlink(f"{proc_root}/{pid}/fd/{fd}")
        except OSError:
            continue
        if target.startswith("socket:["):
            inodes.add(target[len("socket:[") : -1])
    return inodes


def established_inodes(proc_root: str, pid: int) -> set[str]:
    """Inodes of ESTABLISHED TCP sockets in *pid*'s network namespace.

    Reads ``/proc/<pid>/net/tcp{,6}`` (namespace-correct — the sandboxed
    runtime may not share the host's net view). State ``01`` = ESTABLISHED.
    """
    inodes: set[str] = set()
    for name in ("tcp", "tcp6"):
        raw = _read_text(f"{proc_root}/{pid}/net/{name}")
        if not raw:
            continue
        for line in raw.splitlines()[1:]:
            parts = line.split()
            if len(parts) > 9 and parts[3] == "01":
                inodes.add(parts[9])
    return inodes


def _is_loopback_hex(addr: str) -> bool:
    """Whether a ``/proc/net/tcp{,6}`` address column names a loopback peer.

    IPv4 is one little-endian word, so the first octet is the LAST byte
    (``0100007F`` is 127.0.0.1). IPv6 is four little-endian words: ``::1`` is
    ``...01000000`` and a v4-mapped ``::ffff:127.x`` ends in a word whose last
    byte is ``7F``.
    """
    host = addr.split(":", 1)[0].upper()
    if len(host) == 8:
        return host.endswith("7F")
    if len(host) == 32:
        if host == "00000000000000000000000001000000":
            return True
        return host[:24] == "0000000000000000FFFF0000" and host.endswith("7F")
    return False


def remote_established_inodes(proc_root: str, pid: int) -> set[str]:
    """Inodes of ESTABLISHED TCP sockets whose peer is not loopback.

    Same source as :func:`established_inodes`. A loopback peer is a local
    service, most often Kiro Crew's own gateway, which the in-tree MCP servers
    reach over ``127.0.0.1`` and which a long-blocking tool such as
    ``spawn_sub_agents`` legitimately waits on for its whole run.
    """
    inodes: set[str] = set()
    for name in ("tcp", "tcp6"):
        raw = _read_text(f"{proc_root}/{pid}/net/{name}")
        if not raw:
            continue
        for line in raw.splitlines()[1:]:
            parts = line.split()
            if len(parts) > 9 and parts[3] == "01" and not _is_loopback_hex(parts[2]):
                inodes.add(parts[9])
    return inodes


def steady_now() -> float | None:
    """A step-immune reading paired with :func:`boottime_now` on darwin.

    ``CLOCK_MONOTONIC`` there keeps counting through sleep (unlike
    ``time.monotonic()``, which is ``mach_absolute_time`` and stops), so the pair
    (wall stamp, steady stamp) taken at dispatch lets a later probe check whether
    the wall clock has since moved by anything other than elapsed time. None on
    a platform whose process-start clock cannot step (Linux dates processes on
    ``CLOCK_BOOTTIME``), and wherever the clock is unavailable — a consumer
    reads None as "cannot validate the wall clock", never as a time.
    """
    if sys.platform != "darwin":
        return None
    try:
        return time.clock_gettime(time.CLOCK_MONOTONIC)
    except (AttributeError, OSError):  # pragma: no cover - platform dependent
        return None


# ── Darwin process backend (a host without procfs) ──

# Sample key for the darwin subtree CPU probe. Distinct from the ``/proc``
# walk's jiffies keys and from the portable root-only nanosecond key: three
# counters in three units, and a host must never diff one against another.
_DARWIN_CPU_KEY = "darwin_cpu"
# Evidence label naming what the darwin movement probe can and cannot see: CPU
# time across the subtree, no IO — libproc has no per-process byte counter.
_DARWIN_CPU_LABEL = "darwin cpu-only"


@dataclass(frozen=True)
class ProcessRow:
    """One live, non-zombie process in the shape the shell-child matcher reads.

    ``started`` is the process's start on the :func:`boottime_now` clock (None
    when it cannot be dated, which the matcher reads as possibly-this-tool's).
    ``cmdline`` is argv joined by single spaces — the executable path alone when
    argv is unreadable, "" when nothing is — so the fragment match and the
    program-basename match run unchanged against it.
    """

    pid: int
    started: float | None
    cmdline: str


class DarwinProcessBackend(Protocol):
    """What the oracle asks of a host that has no ``/proc``.

    ``descendants`` answers the whole live subtree under ``root_pid`` (the root
    itself excluded, any order, zombies may be included) or None when the tree
    cannot be enumerated. The two are kept apart because only an enumerated
    tree supports the absent-shell-child claim. ``row`` describes one live
    process, or answers None for one that is gone, a zombie, or unreadable —
    the shapes the ``/proc`` walk skips as ``stat is None or state == "Z"``.
    ``cpu_nanos`` is the process's total CPU time, None when unreadable.
    ``established_tcp`` counts the process's ESTABLISHED TCP sockets to a
    non-loopback peer, None when unreadable. It is optional: the oracle reads a backend without it as having
    no socket view, so the socket-keyed tag is simply never set.
    """

    def descendants(self, root_pid: int) -> list[int] | None: ...

    def row(self, pid: int) -> ProcessRow | None: ...

    def cpu_nanos(self, pid: int) -> int | None: ...

    def established_tcp(self, pid: int) -> int | None: ...


class LibprocBackend:
    """The production darwin backend, over ``platform_compat``'s libproc probes.

    Every call is an in-process syscall on a same-uid process: no fork, no
    ``ps``, so it holds up on the oracle's cadence and inside the offloaded
    consult's timeout. Matching fidelity is argv when ``KERN_PROCARGS2`` reads
    (the normal case for the runtime's own children) and the executable path
    otherwise, where only the program-basename rule can match.
    """

    def descendants(self, root_pid: int) -> list[int] | None:
        # A root that cannot be read has no observable tree: proc_listchildpids
        # answers 0 for an unknown pid exactly as for a childless one, so the
        # root's own facts are what separate "empty" from "gone".
        if platform_compat.darwin_process_facts(root_pid) is None:
            return None
        order: list[int] = []
        visited = {root_pid}
        stack = [root_pid]
        while stack:
            parent = stack.pop()
            children = platform_compat.darwin_child_pids(parent)
            if children is None:
                # One unreadable branch makes the whole tree unobservable — a
                # partial list would ground an absence claim on missing data.
                return None
            for child in children:
                if child not in visited:
                    visited.add(child)
                    order.append(child)
                    stack.append(child)
        return order

    def row(self, pid: int) -> ProcessRow | None:
        facts = platform_compat.darwin_process_facts(pid)
        if facts is None or facts.zombie:
            return None
        argv = platform_compat.darwin_process_argv(pid)
        if argv:
            cmdline = " ".join(argv)
        else:
            cmdline = platform_compat.darwin_process_path(pid) or ""
        return ProcessRow(pid=pid, started=facts.start_secs, cmdline=cmdline)

    def cpu_nanos(self, pid: int) -> int | None:
        return platform_compat.proc_cpu_nanos_for_pid(pid)

    def established_tcp(self, pid: int) -> int | None:
        return platform_compat.darwin_established_tcp_count(pid)


def select_darwin_backend(proc_root: str) -> DarwinProcessBackend | None:
    """The backend an oracle uses for *proc_root*, or None to walk ``/proc``.

    Selected on the platform AND on ``proc_root`` being absent, so a fake
    ``/proc`` tree handed to the oracle under test keeps the ``/proc`` walk on
    any host, and a Linux host with procfs never sees libproc.
    """
    if sys.platform == "darwin" and not os.path.exists(proc_root):
        return LibprocBackend()
    return None
