"""Per-session liveness oracle — wellness is the detector, timeouts are the backstop.

The per-session watchdogs in ``session_handle.py`` historically used *timeouts
as death detectors*: a stale-turn window (90s) and a tool-stall window (600s)
that killed healthy-but-slow work — a silent 30-minute redirected build, a
``wait(1800)`` poll, or a long non-streamed reasoning stretch. This module
inverts that: an EFFECTIVE per-session oracle returns a verdict with evidence,
so the watchdog acts FASTER on real deaths and never on healthy work.

    verdict = oracle(session) -> WORKING | DEAD | STUCK_INPUT | UNKNOWN

Policy (enforced by the caller in ``session_handle._dispatch_events``):

- ``WORKING``     -> never act (log once per interval at most).
- ``DEAD``        -> act immediately: recovery lands seconds after actual death
                     instead of at a blanket 90s/600s timeout.
- ``STUCK_INPUT`` -> act immediately, with a cause the recovery nudge can name
                     ("re-run non-interactively").
- ``UNKNOWN``     -> the only timeout-governed class, with non-lethal actions.

Evidence sources (Linux ``/proc`` first-class, a ``libproc`` backend on
macOS, no new dependencies):

- **Shell tool in flight**: scan the runtime's descendant tree for a non-zombie
  child whose cmdline matches the session's cached command. A live match is
  definite WORKING (a 40-minute build runs untouched). Once matched, the pid is
  tracked so exit detection is exact: a tracked child that exits without a tool
  result frame flips to DEAD after a short grace. A matched-but-frozen subtree
  blocked reading a tty/stdin is STUCK_INPUT. When nothing matches, the two
  reasons are kept apart: an OBSERVABLE tree in which no live descendant is
  young enough to have been started by this dispatch carries the
  :data:`EVIDENCE_SHELL_CHILD_ABSENT` tag (the command is not running — the
  caller narrows the UNKNOWN window), while a tree that does hold such a
  descendant, or one whose child list cannot be read at all, keeps the plain
  evidence and the full window: the match heuristic may simply have failed to
  recognize live work.
- **MCP ``wait`` tool**: declared-duration contract — WORKING until the parsed
  ``seconds`` (+ slack) elapse, then UNKNOWN.
- **Other MCP tools**: sample the descendant tree's CPU/IO movement across
  successive checks; moving -> WORKING, flat -> UNKNOWN.
- **Model-wait (no tool in flight)**: sample the tree's IO/CPU counters across
  checks (token/keepalive receipt moves them) and its established TCP sockets.
  Flat counters with NO established backend socket is the done-but-lost-frame
  wedge signature -> DEAD. Established-but-flat is UNKNOWN with the
  :data:`EVIDENCE_ESTABLISHED_FLAT` tag so the caller can extend the probe
  window for probably-thinking (non-streamed reasoning) turns.

On macOS there is no procfs, so the tree is read in-process through ``libproc``
(:class:`LibprocBackend`, selected once per oracle when the platform is darwin
and ``proc_root`` does not exist): ``proc_listchildpids`` enumerates the
runtime's descendants, ``PROC_PIDTBSDINFO`` dates each one and tells a zombie
apart, ``KERN_PROCARGS2`` (falling back to ``proc_pidpath``) supplies the
cmdline the SAME matching rules run against, and ``PROC_PIDTASKINFO`` sums the
subtree's CPU time. That gives macOS the shell-child match / exit detection /
absence narrowing and the MCP-subtree movement probe, with movement being
CPU-only (the evidence string says so — there is no per-process IO counter to
read). What has no libproc equivalent stays exactly as absent: no socket
evidence (so a flat model wait is UNKNOWN, never DEAD, and never tagged
``established_flat``), no wchan / blocked-fd evidence (so STUCK_INPUT is never
claimed). The backend is injectable (``darwin_backend`` ctor arg), and the
Linux path is untouched by its presence.

Every probe is wrapped: any error degrades the verdict to UNKNOWN — never to a
kill. Each check is cheap (<10ms of file reads); two-sample deltas are computed
across successive calls (the dispatch loop's queue-timeout ticks) rather than
sleeping inline, gated on ``sample_min_secs`` between samples.

Attribution on a shared runtime: each handle probes only ITS OWN runtime pid,
and the cmdline match keys shell evidence to THIS session's in-flight command.
Where attribution is impossible the verdict degrades to UNKNOWN.

Unit-testable against a fake ``/proc`` tree via the ``proc_root`` ctor arg and
an injectable ``now`` clock.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time
from concurrent.futures import Executor
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Protocol

from kiro_crew import platform_compat

logger = logging.getLogger(__name__)

# ── Verdicts ──

VERDICT_WORKING = "working"
VERDICT_DEAD = "dead"
VERDICT_UNKNOWN = "unknown"
VERDICT_STUCK_INPUT = "stuck_input"

# Evidence prefix for the model-wait "established backend socket but flat
# counters" shape. The caller extends the UNKNOWN probe window for this tag
# (probably a non-streamed server-side think) instead of probing at the
# ordinary stale window.
EVIDENCE_ESTABLISHED_FLAT = "established_flat"

# Evidence prefix for a shell tool in flight whose command CANNOT be running:
# the runtime's tree is readable, holds live descendants, and not one of them is
# young enough to have been started by this dispatch. The caller narrows the
# UNKNOWN window on this tag (see the shell branch of
# ``session_handle._dispatch_events``) instead of spending the build-scale
# suspect window on a command that already exited — the sub-second shell tool
# whose result frame was lost is never observed alive, so the plain
# "no matching shell child" evidence used to buy it the full forbearance.
#
# Deliberately NOT a DEAD verdict: absence is inferred from process start times,
# and a live command that exec'd into something the cmdline heuristic misses is
# still covered by the "young descendant exists" test below, so the narrowing
# only shortens a non-lethal cancel, never skips straight to one.
EVIDENCE_SHELL_CHILD_ABSENT = "shell_child_absent"

# Evidence for a movement probe that stored a BASELINE and has nothing to
# compare it against yet. Structurally non-informative: it says "ask me again",
# never "this process is idle". A caller whose non-WORKING branch destroys work
# must therefore defer on it rather than treat it as a negative verdict — see
# ``client._prompt_loop``'s stale-turn gate, which reaped a live turn on its
# first silent read because the priming answer landed past the cutoff.
EVIDENCE_SAMPLING = "sampling"

# Tool names that are known to wrap a model call (e.g. kiro-cli's use_subagent
# which starts a sub-agent turn inside the current tool call). The
# established_flat narrowing is ONLY applied when the in-flight tool's trusted
# ``tool_name`` (from ``_meta.kiro.toolName``) is in this set. Without
# positive attribution the socket may be a persistent keepalive for an
# unrelated backend connection (e.g. model-service keep-alive) while an
# ordinary quiet MCP tool is running — narrowing the build-scale suspect window to 15
# minutes in that case would incorrectly penalise long-running ordinary tools.
#
# COUPLING: these are kiro-cli TOOL NAMES, matched verbatim against the ACP
# tool-call frame. If kiro-cli renames use_subagent or ships another
# model-wrapping tool, narrowing silently stops applying and those stalls
# fall back to the build-scale window (fail-safe direction: too patient,
# never a surprise cancel — but the fast-detection this list exists for is
# lost). No handshake advertises the model-wrapping property today, so a
# name list is the only attribution available; update it alongside any
# kiro-cli subagent-tool surface change.
_MODEL_WRAPPING_TOOLS: frozenset[str] = frozenset({"use_subagent"})

# ── Tunables (contract constants, not config — config governs the caller) ──

# A tracked shell child that exited gets this long for its tool result frame to
# arrive on the session queue before the verdict flips to DEAD.
CHILD_EXIT_GRACE_SECS = 15.0
# Declared-duration slack for the MCP wait tool: WORKING until seconds + this.
WAIT_TOOL_SLACK_SECS = 120.0
# Minimum cmdline fragment length for a definite shell-child match.
_MIN_MATCH_FRAGMENT = 8
# How much a process may predate its tool's dispatch and still count as started
# BY that dispatch. Both timestamps come from the boot clock (see
# :func:`boottime_now`), so this covers only the dispatch-to-spawn gap, not clock
# skew.
_DISPATCH_START_TOLERANCE_SECS = 10.0
# wchan values that indicate a process blocked reading interactive input.
_STUCK_INPUT_WCHANS = frozenset({"n_tty_read", "pipe_read", "wait_woken"})
# read(2) syscall numbers: x86_64 = 0, aarch64 = 63.
_READ_SYSCALL_NRS = frozenset({0, 63})
# Shell wrappers whose bare program name is too generic to count as a match.
_GENERIC_PROGRAMS = frozenset({"bash", "sh", "zsh", "env", "sudo", "nohup", "timeout"})

# Sample key for the portable (no-procfs) CPU probe. Deliberately not one of the
# ``/proc`` walk's keys: that counter is jiffies and this one is nanoseconds, so
# a host whose procfs became readable mid-turn must not diff one against the
# other.
_PORTABLE_CPU_KEY = "portable_cpu"

_WAIT_SECONDS_RE = re.compile(r"[\"']?seconds[\"']?\s*[:=]\s*(\d+)")


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
    fields = raw[rparen + 1:].split()
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
    return any(
        _read_text(f"{proc_root}/{pid}/task/{tid}/children") is not None for tid in tids
    )


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
            inodes.add(target[len("socket:["):-1])
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


def boottime_now() -> float | None:
    """Now, on the clock this host dates process starts against.

    Linux: ``CLOCK_BOOTTIME`` counts time spent suspended, exactly as
    ``/proc/uptime`` and the ``starttime`` field of ``/proc/<pid>/stat`` do.
    ``time.monotonic()`` (``CLOCK_MONOTONIC``) does not, so the two MUST NOT be
    mixed in one comparison: after a suspend of S seconds, a boot-clock age minus
    a monotonic stamp places a process S seconds EARLIER than it really started,
    which is how a live shell child comes to look like it predates its own
    dispatch.

    macOS: ``libproc`` reports a process's start as an absolute wall-clock
    instant (``pbi_start_tvsec``), so the stamp is ``time.time()`` — the same
    clock, suspend included. That clock can STEP (NTP correction after a VM
    resume, an admin reset), and a backward step between the stamp and the
    runtime's fork dates a live child before its own dispatch. The oracle pairs
    this stamp with :func:`steady_now` and refuses to attribute by start time
    once the two disagree (see :meth:`LivenessOracle._started_after_dispatch`);
    the stamp alone cannot tell a step from a slow spawn.

    Returns None where no such clock is available, which every caller must read
    as "cannot attribute" rather than as a time.
    """
    try:
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    except (AttributeError, OSError):  # pragma: no cover - platform dependent
        if sys.platform == "darwin":
            return time.time()
        return None


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


def process_start_boot_secs(starttime_ticks: float) -> float | None:
    """A process's ``starttime`` ticks as seconds on the :func:`boottime_now` clock.

    None when the tick rate cannot be read — including on a platform with no
    ``os.sysconf`` at all (Windows raises AttributeError, not OSError), where
    there is no ``/proc`` to date processes against either. Callers read None as
    "cannot attribute", never as a time.
    """
    try:
        hz = os.sysconf("SC_CLK_TCK")
    except (AttributeError, OSError, ValueError):
        return None
    if hz <= 0:  # pragma: no cover - defensive
        return None
    return starttime_ticks / hz


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
    """

    def descendants(self, root_pid: int) -> list[int] | None: ...

    def row(self, pid: int) -> ProcessRow | None: ...

    def cpu_nanos(self, pid: int) -> int | None: ...


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


def select_darwin_backend(proc_root: str) -> DarwinProcessBackend | None:
    """The backend an oracle uses for *proc_root*, or None to walk ``/proc``.

    Selected on the platform AND on ``proc_root`` being absent, so a fake
    ``/proc`` tree handed to the oracle under test keeps the ``/proc`` walk on
    any host, and a Linux host with procfs never sees libproc.
    """
    if sys.platform == "darwin" and not os.path.exists(proc_root):
        return LibprocBackend()
    return None


# ── Command matching ──


def match_fragment(command: str) -> str:
    """Extract the most distinctive fragment of a cached tool command.

    The cached input is the redacted rendering of the shell tool's input —
    usually the command text itself (possibly JSON-wrapped). kiro-cli runs
    shell tools via ``bash -c <command>``, so the child's cmdline contains the
    command text near-verbatim; a long contiguous fragment is a strong match
    key. Redaction markers and shell metacharacters split the text into
    fragments; the longest one wins. Returns "" when nothing distinctive
    survives (caller degrades to the weaker program-name match).
    """
    text = command or ""
    m = re.search(r"[\"']command[\"']\s*:\s*\"((?:[^\"\\]|\\.)*)\"", text)
    if m:
        text = m.group(1)
    # Split on redaction markers and quoting/control chars that differ between
    # the cached rendering and the real argv.
    fragments = re.split(r"\*{3,}|\[REDACTED[^\]]*\]|[\"'\\\n\r]", text)
    best = max((f.strip() for f in fragments), key=len, default="")
    return best if len(best) >= _MIN_MATCH_FRAGMENT else ""


def first_program(command: str) -> str:
    """First non-generic program token of a command ("" when too generic)."""
    for token in re.split(r"[\s;|&]+", (command or "").strip()):
        base = token.rsplit("/", 1)[-1]
        if not base or "=" in base:
            continue  # env assignments
        if base in _GENERIC_PROGRAMS or base == "-c":
            continue
        return base if len(base) >= 3 else ""
    return ""


def parse_wait_seconds(command: str) -> int | None:
    """Parse the declared ``seconds`` from a cached MCP wait-tool input."""
    m = _WAIT_SECONDS_RE.search(command or "")
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def is_wait_tool(title: str) -> bool:
    """True when a tool title names the kirocrew-core ``wait`` tool.

    Titles vary by transport ("wait", "kirocrew-core___wait", "wait (mcp)");
    match on the last alphanumeric token equalling "wait".
    """
    tokens = re.split(r"[^a-zA-Z0-9]+", (title or "").strip().lower())
    return "wait" in [t for t in tokens if t]


@dataclass
class ToolCallState:
    """Snapshot of the in-flight tool call the oracle reasons about."""

    title: str = ""
    command: str = ""  # redacted cached tool input
    dispatch_ts: float = 0.0  # time.monotonic() at EVENT_TOOL_CALL
    is_shell: bool = False
    # ``boottime_now()`` at EVENT_TOOL_CALL — the SAME clock /proc dates process
    # start times against, so a child's start can be compared to its dispatch
    # without mixing clocks (see :func:`boottime_now`). Separate from
    # ``dispatch_ts``, which stays monotonic because it measures ELAPSED time (the
    # wait tool's declared duration), where excluding suspend is correct. None
    # when the clock is unavailable; every consumer must then decline to attribute
    # a process to this dispatch rather than guess.
    dispatch_boot_ts: float | None = None
    # ``steady_now()`` taken alongside ``dispatch_boot_ts``. On darwin that stamp
    # is the wall clock, which can step; this one cannot, so the pair lets the
    # oracle detect a step since dispatch before it grounds an absence claim on
    # start-time attribution. None where the platform needs no check (Linux) or
    # the caller took no such stamp — on darwin the oracle then declines to
    # attribute at all rather than trust an unvalidated wall clock.
    dispatch_steady_ts: float | None = None
    # Consumer parking banked in THIS TURN before the stamp above was taken
    # (``AcpSessionHandle._parked_total``, which is per-turn and includes the whole
    # of a human approval wait). The stamp is taken when the tool_call frame is
    # PROCESSED, and the dispatch loop is suspended at its yield for every
    # consumer-side await — an approval, an IM send, a hook — so a tool whose
    # frame queued behind one of those was already spawned by the time its stamp
    # is taken. The child cannot predate the turn, so parking-so-far bounds that
    # lag and widens the attribution window by exactly as much as the loop itself
    # measured. Left 0.0 by a dispatch path with no consumer parking.
    dispatch_parked_secs: float = 0.0
    # Trusted tool name from ``_meta.kiro.toolName`` (empty when the backend
    # does not emit ``_meta``; fail-closed). Required for established_flat
    # attribution: the narrowing applies ONLY when this matches a known
    # model-wrapping tool (see ``_MODEL_WRAPPING_TOOLS``).
    tool_name: str = ""


class LivenessOracle:
    """Per-session liveness verdicts from /proc evidence (libproc on macOS).

    One instance per :class:`~kiro_crew.acp.session_handle.AcpSessionHandle`.
    Stateful across checks: it tracks the matched shell child (so exit
    detection is exact) and prior counter samples (so movement deltas are
    computed across the dispatch loop's ticks instead of sleeping inline).
    Drop the baseline at turn start and on every new tool dispatch. Consumers that
    run the check inline may :meth:`reset`; one that offloads it must retire the
    instance with :meth:`fresh` instead, because a timed-out probe keeps writing
    into the object it was handed (see :meth:`reset`).

    Every public check is fully wrapped — any unexpected error returns
    ``(VERDICT_UNKNOWN, ...)``, never raises, never a kill.
    """

    def __init__(
        self,
        proc_root: str = "/proc",
        *,
        now=time.monotonic,
        sample_min_secs: float = 3.0,
        darwin_backend: DarwinProcessBackend | None = None,
        wall_now=time.time,
        steady_now_fn=steady_now,
    ) -> None:
        self._proc = str(proc_root)
        self._now = now
        # The darwin start-time ruler and its step detector (see
        # ``_started_after_dispatch``); injectable so a test can step one without
        # the other. Unused on a ``/proc`` host.
        self._wall_now = wall_now
        self._steady_now = steady_now_fn
        self._sample_min_secs = sample_min_secs
        # The one platform probe: decided at construction so every check on this
        # instance reads the same source. A caller passes its own backend to
        # drive the darwin paths without libproc (tests) or to pin the choice.
        self._darwin = (
            darwin_backend if darwin_backend is not None else select_darwin_backend(self._proc)
        )
        self._tracked_child: int | None = None
        self._child_gone_ts: float | None = None
        # sample key -> (ts, counter). Keys: "io", "cpu".
        self._samples: dict[str, tuple[float, int]] = {}

    def reset(self) -> None:
        """Clear tracked child + samples in place.

        Safe only when no detached worker still holds this instance. A consult
        offloaded to ``subprocess_executor()`` keeps a bound reference to the
        oracle it sampled into, so clearing in place leaves that writer pointed at
        the live baseline — where a late write becomes the next generation's
        starting point and any delta reads as movement. Prefer :meth:`fresh`.

        Neither in-tree consumer meets that condition: ``AcpClient`` and
        ``AcpSessionHandle`` both offload their consult, so both retire the whole
        instance at each liveness-state boundary instead of clearing it. This
        method remains for an inline caller, which has no detached writer to
        confine.
        """
        self._tracked_child = None
        self._child_gone_ts = None
        self._samples.clear()

    def fresh(self) -> "LivenessOracle":
        """A new oracle carrying this one's configuration and no samples.

        Callers retire an instance instead of ``reset()``-ing it when a detached
        worker may still hold a reference to it: samples are keyed without a PID,
        so a late write would otherwise land on the live baseline and read as
        movement.
        """
        return LivenessOracle(
            self._proc,
            now=self._now,
            sample_min_secs=self._sample_min_secs,
            darwin_backend=self._darwin,
            wall_now=self._wall_now,
            steady_now_fn=self._steady_now,
        )

    # ── Public checks ──

    def check_tool(self, runtime_pid: int | None, tool: ToolCallState) -> tuple[str, str]:
        """Verdict for an in-flight tool call. Never raises."""
        try:
            return self._check_tool(runtime_pid, tool)
        except Exception:
            logger.debug("liveness: check_tool failed", exc_info=True)
            return VERDICT_UNKNOWN, "oracle error"

    def check_model_wait(self, runtime_pid: int | None) -> tuple[str, str]:
        """Verdict for a model-wait (no tool in flight). Never raises."""
        try:
            return self._check_model_wait(runtime_pid)
        except Exception:
            logger.debug("liveness: check_model_wait failed", exc_info=True)
            return VERDICT_UNKNOWN, "oracle error"

    # ── Tool-in-flight evidence ──

    def _check_tool(self, runtime_pid: int | None, tool: ToolCallState) -> tuple[str, str]:
        if not runtime_pid:
            return VERDICT_UNKNOWN, "no runtime pid"

        # Declared-duration contract for the kirocrew-core wait tool: the
        # session is WORKING by definition until the declared sleep elapses.
        if not tool.is_shell and is_wait_tool(tool.title):
            secs = parse_wait_seconds(tool.command)
            if secs is not None:
                elapsed = self._now() - tool.dispatch_ts
                if elapsed < secs + WAIT_TOOL_SLACK_SECS:
                    return VERDICT_WORKING, f"wait tool declared {secs}s ({elapsed:.0f}s elapsed)"
                return VERDICT_UNKNOWN, f"wait tool declared {secs}s elapsed"
            return VERDICT_UNKNOWN, "wait tool without parseable seconds"

        if tool.is_shell:
            return self._check_shell_child(runtime_pid, tool)

        # Opaque MCP tool: any CPU/IO movement in the runtime's descendant
        # tree (which contains the serving MCP server process) reads WORKING.
        moved, evidence = self._tree_movement(runtime_pid)
        if moved:
            return VERDICT_WORKING, f"mcp subtree active ({evidence})"
        # LLM-turn shape inside a tool (e.g. a use_subagent call wrapping a
        # model turn in kiro-cli): the subtree is GENUINELY flat (a real
        # two-sample delta, not the baseline tick or unreadable counters) and
        # the RUNTIME PROCESS ITSELF holds an established backend socket. Tag
        # it — same tag as the model-wait branch — so the caller narrows the
        # UNKNOWN window to the model-silent budget instead of the build-scale
        # forbearance. Verdict stays UNKNOWN: the tag is evidence, never an
        # action. Deliberately NARROWER than the model-wait branch's full-tree
        # ``_any_established``: here the descendants include the tool's own
        # workers, and an MCP server blocked on ITS remote socket (a long
        # remote call, zero CPU/IO while in recv) must keep the full tool
        # windows — only kiro-cli's own backend connection is LLM-wait
        # evidence. Shell-child evidence never reaches this branch (it returns
        # from _check_shell_child above), and a flat subtree without the
        # runtime-held socket keeps the plain evidence. Under the OS sandbox
        # (pid = launcher parent) the runtime holds no sockets, so this fails
        # toward the old full-window behavior, never toward over-narrowing.
        #
        # F1 attribution guard: only apply established_flat narrowing when the
        # in-flight tool is a KNOWN model-wrapping operation (tool.tool_name in
        # _MODEL_WRAPPING_TOOLS). A persistent keepalive socket to the model
        # service can exist independently of the current MCP tool: tagging a
        # quiet ReadInternalWebsites with established_flat would incorrectly
        # narrow its build-scale window to 15 minutes. Without positive tool-identity
        # attribution (empty tool_name or an unknown tool), fall back to the
        # plain mcp_subtree_flat evidence so the full build-scale window holds.
        if (
            evidence not in (EVIDENCE_SAMPLING, "no readable counters")
            and tool.tool_name in _MODEL_WRAPPING_TOOLS
        ):
            held = socket_inodes(self._proc, runtime_pid)
            if held and held & established_inodes(self._proc, runtime_pid):
                return (
                    VERDICT_UNKNOWN,
                    f"{EVIDENCE_ESTABLISHED_FLAT}: mcp subtree flat ({evidence})",
                )
        return VERDICT_UNKNOWN, f"mcp subtree flat ({evidence})"

    def _check_shell_child(self, runtime_pid: int, tool: ToolCallState) -> tuple[str, str]:
        if self._darwin is not None:
            return self._check_shell_child_darwin(self._darwin, runtime_pid, tool)
        descendants = iter_descendants(self._proc, runtime_pid)

        # Exact exit detection once a child was matched.
        if self._tracked_child is not None:
            stat = read_pid_stat(self._proc, self._tracked_child)
            alive = stat is not None and stat[0] != "Z" and self._tracked_child in descendants
            return self._tracked_child_verdict(self._tracked_child, alive)

        # Not matched yet: scan for a live non-zombie descendant whose cmdline
        # matches this session's cached command. The same pass answers a second,
        # cmdline-INDEPENDENT question — is any live descendant young enough to
        # have been started by this dispatch — which is what separates "the
        # command already exited" from "the command is running unrecognized".
        matched, live_descendants, possibly_running = self._scan_for_child(
            self._proc_rows(runtime_pid, descendants), tool
        )
        if matched is not None:
            return self._track_child(matched)
        if possibly_running:
            return VERDICT_UNKNOWN, "no matching shell child"
        if not live_descendants and not children_interface_readable(self._proc, runtime_pid):
            # No live descendant AND no readable child list: the tree is not
            # observable (no procfs, no CONFIG_PROC_CHILDREN, a sandbox hiding
            # the subtree), which is indistinguishable from an empty one. Absence
            # is not assertable, so nothing narrows.
            return VERDICT_UNKNOWN, "no matching shell child"
        return self._absent_child_verdict(live_descendants)

    def _check_shell_child_darwin(
        self, backend: DarwinProcessBackend, runtime_pid: int, tool: ToolCallState
    ) -> tuple[str, str]:
        """The ``/proc`` shell-child check, fed by *backend* instead of procfs.

        Same tracked-child exit detection, same matching pass, same three-way
        unmatched verdict. The observability question the ``/proc`` walk answers
        with ``children_interface_readable`` is answered here by enumeration
        itself: a tree that could not be read is None, and only a read tree
        grounds an absence claim.
        """
        descendants = backend.descendants(runtime_pid)

        if self._tracked_child is not None:
            row = backend.row(self._tracked_child)
            # Membership is tested only against a tree that could be read: a
            # transient enumeration failure must not age a live child through the
            # grace into DEAD, the one verdict this branch can be wrong about.
            alive = row is not None and (descendants is None or self._tracked_child in descendants)
            return self._tracked_child_verdict(self._tracked_child, alive)

        if descendants is None:
            return VERDICT_UNKNOWN, "no matching shell child"
        rows = (row for row in map(backend.row, descendants) if row is not None)
        matched, live_descendants, possibly_running = self._scan_for_child(rows, tool)
        if matched is not None:
            return self._track_child(matched)
        if possibly_running:
            return VERDICT_UNKNOWN, "no matching shell child"
        return self._absent_child_verdict(live_descendants)

    def _proc_rows(self, runtime_pid: int, descendants: list[int]) -> Iterable[ProcessRow]:
        """The live, non-zombie descendants in *descendants*, read from ``/proc``.

        The start is dated before the cmdline is read, on purpose: a live
        descendant whose cmdline is momentarily unreadable (mid-exec,
        mid-teardown) still counts as possibly-this-tool's in the scan.
        """
        for pid in descendants:
            if pid == runtime_pid:
                continue
            stat = read_pid_stat(self._proc, pid)
            if stat is None or stat[0] == "Z":
                continue
            yield ProcessRow(
                pid=pid,
                started=process_start_boot_secs(stat[1]),
                cmdline=read_cmdline(self._proc, pid),
            )

    def _scan_for_child(
        self, rows: Iterable[ProcessRow], tool: ToolCallState
    ) -> tuple[int | None, int, bool]:
        """``(matched_pid, live_descendants, possibly_running)`` over *rows*.

        ``matched_pid`` is the first live descendant whose cmdline matches the
        cached command AND that is young enough to be this dispatch's own.
        ``possibly_running`` is True when something that could be this command
        is running unrecognized: a descendant started since the dispatch (the
        match heuristic may have missed a live command — a shell that exec'd
        away, a cached input redacted past any usable fragment), or one whose
        cmdline matches while predating a late-taken stamp. Either vetoes the
        absence claim; the caller keeps the plain evidence and the full window.
        """
        fragment = match_fragment(tool.command)
        program = first_program(tool.command)
        live_descendants = 0
        started_since_dispatch = False
        matched_but_older = False
        for row in rows:
            live_descendants += 1
            fresh = self._started_after_dispatch(row.started, tool)
            started_since_dispatch = started_since_dispatch or fresh
            if not row.cmdline:
                continue
            matched = bool(fragment) and fragment in row.cmdline
            if not matched and program:
                base_tokens = {t.rsplit("/", 1)[-1] for t in row.cmdline.split()}
                matched = program in base_tokens
            if not matched:
                continue
            if not fresh:
                # Cmdline matches but the process predates the dispatch stamp by
                # more than the tolerance. Two causes the oracle cannot tell
                # apart: a coincidental pre-existing lookalike, or THIS command,
                # whose tool_call frame reached the dispatch loop late — the
                # stamp is taken when that frame is PROCESSED, and the consumer
                # can park for minutes on an approval, an IM send or a hook
                # (see ``_parked`` in the dispatch loop) while kiro-cli has
                # already spawned. So refuse the match, but let it veto the
                # absence claim: a live process that looks like this command is
                # not evidence that nothing is running.
                matched_but_older = True
                continue
            return row.pid, live_descendants, True
        return None, live_descendants, started_since_dispatch or matched_but_older

    def _track_child(self, pid: int) -> tuple[str, str]:
        self._tracked_child = pid
        self._child_gone_ts = None
        # Prime the stuck-detection movement baseline now so the NEXT check
        # can already compare deltas (otherwise stuck detection needs three
        # ticks: match, baseline, compare).
        self._tree_movement(pid, key_prefix="stuck")
        return VERDICT_WORKING, f"shell child {pid} matched command"

    def _tracked_child_verdict(self, pid: int, alive: bool) -> tuple[str, str]:
        """Verdict for the tracked shell child *pid* given whether it is still alive."""
        if alive:
            self._child_gone_ts = None
            stuck = self._stuck_input_check(pid)
            if stuck:
                return VERDICT_STUCK_INPUT, stuck
            return VERDICT_WORKING, f"shell child {pid} alive"
        if self._child_gone_ts is None:
            self._child_gone_ts = self._now()
        gone_for = self._now() - self._child_gone_ts
        if gone_for > CHILD_EXIT_GRACE_SECS:
            return (
                VERDICT_DEAD,
                f"shell child {pid} exited {gone_for:.0f}s ago, no result frame",
            )
        return VERDICT_UNKNOWN, f"shell child exited {gone_for:.0f}s ago (grace)"

    def _absent_child_verdict(self, live_descendants: int) -> tuple[str, str]:
        # The tree is observable and nothing in it was started for this tool, so
        # the command is not running: the same physical state the DEAD branch
        # above reports as "exited, no result frame" — the oracle just never got
        # to see this one alive. Still UNKNOWN, not DEAD: the claim rests on
        # start-time attribution and on the child being a DESCENDANT of the
        # runtime (a double-forked, reparented child would escape the walk), so
        # the caller shortens its non-lethal cancel rather than acting at once.
        return (
            VERDICT_UNKNOWN,
            f"{EVIDENCE_SHELL_CHILD_ABSENT}: no shell child started since dispatch "
            f"({live_descendants} live descendants, none started since dispatch)",
        )

    def _started_after_dispatch(self, started: float | None, tool: ToolCallState) -> bool:
        """Whether a process started at *started* is young enough to be *tool*'s.

        Both sides are read on the same clock — the process's start and the stamp
        ``boottime_now()`` took at EVENT_TOOL_CALL — so a host suspend between
        dispatch and this probe moves neither. Deriving the start from an age
        instead (a boot-clock age subtracted from a monotonic stamp) placed a
        live child a full suspend EARLIER than it started, so a laptop resumed
        mid-command read as "this child predates its own dispatch".

        The window opens by ``dispatch_parked_secs`` as well, because the stamp
        marks when the tool_call frame was PROCESSED, not when the runtime
        spawned: a frame queued behind a consumer-side await (an approval, an IM
        send, a hook) is stamped that much late, and its child then looks older
        than its own dispatch. The dispatch loop already measures exactly that
        park, so the bound is measured rather than guessed.

        Fail-open by design: a missing stamp or an undatable process (no tick
        rate on Linux, no readable start elsewhere) answers True, so an
        unattributable process reads as possibly-this-tool's. That keeps a live
        command matched and keeps an absence claim from resting on evidence the
        oracle does not have.
        """
        if not tool.dispatch_boot_ts:
            return True
        if started is None:
            return True
        if self._darwin is not None and self._wall_clock_stepped_since(tool):
            return True
        tolerance = _DISPATCH_START_TOLERANCE_SECS + max(0.0, tool.dispatch_parked_secs)
        return started >= tool.dispatch_boot_ts - tolerance

    def _wall_clock_stepped_since(self, tool: ToolCallState) -> bool:
        """Whether the darwin start-time ruler has moved since *tool* was stamped.

        On darwin both sides of the start-time comparison are wall clock, and a
        wall clock steps. A backward step landing between the dispatch stamp and
        the runtime's fork dates a live child BEFORE its dispatch; a command the
        matchers cannot recognise (fully redacted input) then has nothing to veto
        the absence claim, and the caller narrows to the stale window and cancels
        live work. So the stamp is paired with a steady clock: when wall elapsed
        and steady elapsed disagree by more than the attribution tolerance, the
        wall clock is not a ruler this dispatch can be measured with, and every
        row reads as possibly-this-tool's (fail-open, like an undatable process).
        A missing steady stamp is the same answer: an absence claim needs a
        validated ruler, and there is none to validate.

        A suspend does not trip this: ``CLOCK_MONOTONIC`` on darwin counts sleep,
        as the wall clock does. If a host's steady clock did stop across a sleep,
        the disagreement would still fail toward the WIDER window, never toward
        a cancel.
        """
        if tool.dispatch_boot_ts is None or tool.dispatch_steady_ts is None:
            return True
        wall_elapsed = self._wall_now() - tool.dispatch_boot_ts
        steady_elapsed = self._steady_now() - tool.dispatch_steady_ts
        return abs(wall_elapsed - steady_elapsed) > _DISPATCH_START_TOLERANCE_SECS

    def _stuck_input_check(self, pid: int) -> str:
        """STUCK_INPUT evidence for a live child subtree, or "" when not stuck.

        Requires (a) the ENTIRE matched subtree flat on CPU+IO across two
        samples, and (b) at least one process blocked reading a tty or its
        stdin pipe. A socket-blocked read is a network wait (not stuck); a
        futex/lock wait is ambiguous — both return "".

        Returning "" for a LIVE child means the caller reports WORKING, and a
        WORKING verdict is NOT bounded by the UNKNOWN timeout class: the
        dispatch loop short-circuits on it (``session_handle`` defers WORKING
        unconditionally) BEFORE ``tool_stall_suspect_secs`` /
        ``tool_stall_hard_cap_secs`` are applied. So a genuinely ambiguous hang
        — a child alive but wedged on a futex, a socket read, or a hung mount —
        is deferred for as long as the turn's own wall-clock ceiling allows, not
        for a watchdog window. That is deliberate (a quiet long build must not
        be cancelled on a timer) but it is the only path with no window of its
        own, so it is logged with escalating severity rather than capped.
        """
        subtree = iter_descendants(self._proc, pid)
        moved, move_evidence = self._tree_movement(pid, key_prefix="stuck")
        if moved or move_evidence == "sampling":
            # Moving, or no second sample yet — cannot claim a flat subtree.
            return ""
        blocked = None
        for p in subtree:
            wchan = read_wchan(self._proc, p)
            if wchan not in _STUCK_INPUT_WCHANS:
                continue
            fd = blocked_read_fd(self._proc, p)
            if fd is None:
                continue
            target = fd_target(self._proc, p, fd)
            if target.startswith(("/dev/tty", "/dev/pts")) or (
                fd == 0 and target.startswith("pipe:")
            ):
                blocked = (p, target)
                break
        if blocked is None:
            return ""
        return f"stuck_input: pid {blocked[0]} blocked reading {blocked[1]} with flat subtree"

    # ── Model-wait evidence ──

    def _check_model_wait(self, runtime_pid: int | None) -> tuple[str, str]:
        if not runtime_pid:
            return VERDICT_UNKNOWN, "no runtime pid"
        moved, evidence = self._tree_movement(runtime_pid)
        if moved:
            return VERDICT_WORKING, f"backend activity ({evidence})"
        if evidence == "no readable counters":
            # No procfs AT ALL (macOS, Windows) — the /proc-shaped evidence is
            # ABSENT, not negative, so fall back to the portable probe before the
            # caller takes its conservative branch. Linux-only counters must not
            # read as "assume dead" on the platform most backends run on: that is
            # how a macOS turn was declared complete mid-tool (issue #8520).
            return self._portable_model_wait(runtime_pid)
        if evidence == EVIDENCE_SAMPLING:
            # No baseline yet — cannot attest either way.
            return VERDICT_UNKNOWN, evidence
        if self._darwin is not None:
            # A genuinely flat subtree, but the split below rests on socket
            # evidence that only ``/proc`` carries. Without it a flat wait is
            # neither the lost-frame wedge nor a probable think: UNKNOWN with
            # plain evidence, so the caller's timeout policy governs and
            # nothing is invented in either direction.
            return VERDICT_UNKNOWN, f"backend subtree flat ({evidence})"
        # Flat counters: distinguish the done-but-lost-frame wedge (no backend
        # connection at all) from a probably-thinking server-side silence
        # (established socket, nothing flowing yet).
        established = self._any_established(runtime_pid)
        if established:
            return VERDICT_UNKNOWN, f"{EVIDENCE_ESTABLISHED_FLAT}: {evidence}"
        return VERDICT_DEAD, f"no established backend socket and flat counters ({evidence})"

    def _portable_model_wait(self, runtime_pid: int) -> tuple[str, str]:
        """Model-wait verdict from platform-neutral evidence.

        Reached only when the ``/proc`` walk read no counter whatsoever, i.e. on
        a host that has no procfs. It can only ever FORGIVE silence: a CPU delta
        on a live pid reads WORKING, and anything else keeps the UNKNOWN such a
        host already returned, so the caller's conservative branch is untouched
        wherever the portable probe cannot attest either. It deliberately never
        answers DEAD — "no counter here" is not proof of death, and the caller
        reaps on UNKNOWN anyway, so claiming it would only add a way to be wrong.

        Both halves are load-bearing. The alive check alone would forgive a
        finished-but-lost-frame backend forever (a wedged process is alive too),
        trading a 90s truncation for a full-prompt-timeout hang; the CPU delta
        alone would attest to the work of a RECYCLED pid.
        """
        moved, evidence = self._portable_movement(runtime_pid)
        if moved and platform_compat.pid_exists(runtime_pid):
            return VERDICT_WORKING, f"backend activity ({evidence}, pid alive)"
        return VERDICT_UNKNOWN, evidence

    def _any_established(self, runtime_pid: int) -> bool:
        for pid in iter_descendants(self._proc, runtime_pid):
            held = socket_inodes(self._proc, pid)
            if not held:
                continue
            if held & established_inodes(self._proc, pid):
                return True
        return False

    # ── Movement sampling ──

    def _tree_movement(self, root_pid: int, key_prefix: str = "") -> tuple[bool, str]:
        """(moved, evidence) for CPU+IO deltas of *root_pid*'s subtree.

        Deltas are computed against the previous sample when it is at least
        ``sample_min_secs`` old; the first call stores a baseline and reports
        ``(False, "sampling")``. Counters can only shrink when processes exit,
        which itself is movement — negative deltas count as moved.
        """
        if self._darwin is not None:
            return self._darwin_tree_movement(self._darwin, root_pid, key_prefix)
        io_total = 0
        cpu_total = 0
        io_seen = False
        for pid in iter_descendants(self._proc, root_pid):
            io = read_io_bytes(self._proc, pid)
            if io is not None:
                io_total += io
                io_seen = True
            stat = read_pid_stat(self._proc, pid)
            if stat is not None:
                cpu_total += stat[2]
        if not io_seen and cpu_total == 0:
            return False, "no readable counters"
        now = self._now()
        io_key = f"{key_prefix}:io" if key_prefix else "io"
        cpu_key = f"{key_prefix}:cpu" if key_prefix else "cpu"
        prev_io = self._samples.get(io_key)
        prev_cpu = self._samples.get(cpu_key)
        if prev_io is None or prev_cpu is None:
            self._samples[io_key] = (now, io_total)
            self._samples[cpu_key] = (now, cpu_total)
            return False, EVIDENCE_SAMPLING
        if now - prev_io[0] < self._sample_min_secs:
            # Too soon for a fresh delta — report against the stored baseline
            # without advancing it.
            return (io_total != prev_io[1] or cpu_total != prev_cpu[1]), (
                f"io {io_total - prev_io[1]:+d}B cpu {cpu_total - prev_cpu[1]:+d}t (early)"
            )
        io_delta = io_total - prev_io[1]
        cpu_delta = cpu_total - prev_cpu[1]
        self._samples[io_key] = (now, io_total)
        self._samples[cpu_key] = (now, cpu_total)
        return (io_delta != 0 or cpu_delta != 0), f"io {io_delta:+d}B cpu {cpu_delta:+d}t"

    def _darwin_tree_movement(
        self, backend: DarwinProcessBackend, root_pid: int, key_prefix: str
    ) -> tuple[bool, str]:
        """(moved, evidence) for the CPU delta of *root_pid*'s subtree via *backend*.

        The ``/proc`` walk's two-sample contract, CPU-only: libproc exposes no
        per-process byte counter, so an IO-bound but CPU-idle subtree (a long
        ``recv`` on a remote call) reads flat here where Linux would see bytes.
        The evidence names that limit. A tree that cannot be enumerated reads as
        "no readable counters" — absent evidence, same as an unreadable procfs.
        """
        descendants = backend.descendants(root_pid)
        if descendants is None:
            return False, "no readable counters"
        cpu_total = 0
        cpu_seen = False
        for pid in (root_pid, *descendants):
            cpu = backend.cpu_nanos(pid)
            if cpu is not None:
                cpu_total += cpu
                cpu_seen = True
        if not cpu_seen:
            return False, "no readable counters"
        now = self._now()
        key = f"{key_prefix}:{_DARWIN_CPU_KEY}" if key_prefix else _DARWIN_CPU_KEY
        prev = self._samples.get(key)
        if prev is None:
            self._samples[key] = (now, cpu_total)
            return False, EVIDENCE_SAMPLING
        cpu_delta = cpu_total - prev[1]
        if now - prev[0] < self._sample_min_secs:
            # Too soon for a fresh delta — report against the stored baseline
            # without advancing it.
            return cpu_delta != 0, f"cpu {cpu_delta:+d}ns ({_DARWIN_CPU_LABEL}, early)"
        self._samples[key] = (now, cpu_total)
        return cpu_delta != 0, f"cpu {cpu_delta:+d}ns ({_DARWIN_CPU_LABEL})"

    def _portable_movement(self, root_pid: int) -> tuple[bool, str]:
        """(moved, evidence) for *root_pid*'s CPU delta, without ``/proc``.

        Same two-sample contract as :meth:`_tree_movement` — the first call after
        a boundary stores a baseline and reports ``(False, "sampling")``, and a
        second call inside ``sample_min_secs`` compares against that baseline
        without advancing it — but it reads ONE portable counter
        (:func:`platform_compat.proc_cpu_nanos_for_pid`) instead of walking
        procfs, so it answers where that walk is blind.

        Root pid only, so a busy DESCENDANT under an idle root reads flat here.
        That is the conservative direction (it never invents movement), and the
        case where the work lives in a child — an open tool call — is governed by
        the caller's tool-stall policy rather than by this probe. On macOS the
        darwin backend's subtree probe answers first; this one is reached there
        only when that backend could not enumerate the tree.
        """
        cpu = platform_compat.proc_cpu_nanos_for_pid(root_pid)
        if cpu is None:
            return False, "no readable counters"
        now = self._now()
        prev = self._samples.get(_PORTABLE_CPU_KEY)
        if prev is None:
            self._samples[_PORTABLE_CPU_KEY] = (now, cpu)
            return False, EVIDENCE_SAMPLING
        if now - prev[0] < self._sample_min_secs:
            return cpu != prev[1], f"cpu {cpu - prev[1]:+d}ns (early)"
        self._samples[_PORTABLE_CPU_KEY] = (now, cpu)
        return cpu != prev[1], f"cpu {cpu - prev[1]:+d}ns"


# ── Offloaded-consult guard (shared by AcpClient and AcpSessionHandle) ──

# Upper bound on one awaited oracle consult. A /proc walk wedged on a stuck fd
# does not stop when this expires — the shield below detaches the awaiter and
# the guard answers "prior consult still in flight" until the worker finishes.
OFFLOADED_CONSULT_TIMEOUT_SECS = 10.0


def _consume_future_exception(future: asyncio.Future[tuple[str, str]]) -> None:
    """Retrieve a liveness consult's exception so asyncio does not report it.

    The /proc walk keeps running after its awaiter goes away, so it can finish
    with an exception nobody reads. ``Future.__del__`` reports that through the
    loop exception handler, which the gateway records as an unhandled-asyncio
    crash for what is an ordinary probe failure.
    """
    if not future.cancelled():
        future.exception()


class ConsultFutureHolder(Protocol):
    """The one field :func:`consult_offloaded` needs on its caller.

    Both consumers (``AcpClient`` and ``AcpSessionHandle``) track their single
    outstanding oracle walk in an attribute of this exact shape; the guard
    reads and writes it through the holder so the handle stays where each
    caller's liveness-state boundary (turn start, ``_reset_state`` /
    ``_retire_liveness_state``) can retire it.
    """

    _consult_future: asyncio.Future[tuple[str, str]] | None


async def consult_offloaded(
    holder: ConsultFutureHolder,
    call: Callable[..., tuple[str, str]],
    args: tuple[Any, ...],
    *,
    executor_factory: Callable[[], Executor],
    log_label: str = "liveness consult",
) -> tuple[str, str]:
    """One guarded oracle consult, offloaded off the event loop. Never raises.

    The oracle's evidence gathering is a synchronous /proc filesystem walk that
    can block on a wedged fd, so ``call`` runs on ``executor_factory()`` under a
    bounded ``wait_for``. This helper owns the full non-obvious sequence both
    watchdog paths depend on, so a fix to it lands at both call sites at once:

    - **One outstanding walk per holder.** A timed-out await does not stop its
      executor thread, so the submitted future is tracked on the holder and any
      poll that finds it unfinished answers UNKNOWN without submitting again —
      otherwise a permanently wedged /proc read grows a new blocked worker per
      tick and starves the shared pool teardown also draws from. The holder's
      liveness-state boundary retires the handle so a walk abandoned by one
      generation never gates the next.
    - **Submission stays inside the guard.** The callers are silent-read polls
      and watchdog ticks, so a refused executor job (shut down during teardown,
      thread creation refused under load) must read as UNKNOWN rather than
      abort the live turn.
    - **Exception retrieval rides a callback attached at SUBMISSION**, not only
      an ``except`` arm: a turn that ends on this verdict returns with the walk
      still running and may never look again, and ``CancelledError`` is a
      ``BaseException`` an ``except Exception`` arm would miss. ``wait_for``
      cancels the shield's outer future and shield detaches its inner-done
      callback in exactly that case, so the pre-submission consume below
      additionally covers an already-completed prior that never went through
      the callback. Retrieval is not destructive — the await still sees the
      result.
    - **Any failure degrades to UNKNOWN**, never to a raise: the callers fail
      toward their own timeout policy (reaping at the cutoff), never toward
      hanging or killing on a probe error.

    ``executor_factory`` is passed by the caller (not resolved here) so each
    call site keeps its own module-level ``subprocess_executor`` binding — the
    seam its tests patch.
    """
    prior = holder._consult_future
    if prior is not None:
        if not prior.done():
            return VERDICT_UNKNOWN, "prior consult still in flight"
        _consume_future_exception(prior)

    try:
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(executor_factory(), call, *args)
        future.add_done_callback(_consume_future_exception)
        holder._consult_future = future
        return await asyncio.wait_for(
            asyncio.shield(future), timeout=OFFLOADED_CONSULT_TIMEOUT_SECS
        )
    except Exception:
        logger.debug("%s failed/timed out", log_label, exc_info=True)
        return VERDICT_UNKNOWN, "oracle offload error"
