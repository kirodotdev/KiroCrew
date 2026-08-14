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

Evidence sources (all Linux ``/proc`` based, no new dependencies):

- **Shell tool in flight**: scan the runtime's descendant tree for a non-zombie
  child whose cmdline matches the session's cached command. A live match is
  definite WORKING (a 40-minute build runs untouched). Once matched, the pid is
  tracked so exit detection is exact: a tracked child that exits without a tool
  result frame flips to DEAD after a short grace. A matched-but-frozen subtree
  blocked reading a tty/stdin is STUCK_INPUT.
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

import logging
import os
import re
import time
from dataclasses import dataclass

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

# ── Tunables (contract constants, not config — config governs the caller) ──

# A tracked shell child that exited gets this long for its tool result frame to
# arrive on the session queue before the verdict flips to DEAD.
CHILD_EXIT_GRACE_SECS = 15.0
# Declared-duration slack for the MCP wait tool: WORKING until seconds + this.
WAIT_TOOL_SLACK_SECS = 120.0
# Minimum cmdline fragment length for a definite shell-child match.
_MIN_MATCH_FRAGMENT = 8
# wchan values that indicate a process blocked reading interactive input.
_STUCK_INPUT_WCHANS = frozenset({"n_tty_read", "pipe_read", "wait_woken"})
# read(2) syscall numbers: x86_64 = 0, aarch64 = 63.
_READ_SYSCALL_NRS = frozenset({0, 63})
# Shell wrappers whose bare program name is too generic to count as a match.
_GENERIC_PROGRAMS = frozenset({"bash", "sh", "zsh", "env", "sudo", "nohup", "timeout"})

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


def process_age_secs(proc_root: str, starttime_ticks: float) -> float | None:
    """Seconds since a process started, from its stat ``starttime`` ticks."""
    raw = _read_text(f"{proc_root}/uptime")
    if not raw:
        return None
    try:
        uptime = float(raw.split()[0])
        hz = os.sysconf("SC_CLK_TCK")
    except (ValueError, IndexError, OSError):
        return None
    if hz <= 0:
        return None
    return uptime - (starttime_ticks / hz)


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


class LivenessOracle:
    """Per-session liveness verdicts from /proc evidence.

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
    ) -> None:
        self._proc = str(proc_root)
        self._now = now
        self._sample_min_secs = sample_min_secs
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
            self._proc, now=self._now, sample_min_secs=self._sample_min_secs
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
        return VERDICT_UNKNOWN, f"mcp subtree flat ({evidence})"

    def _check_shell_child(self, runtime_pid: int, tool: ToolCallState) -> tuple[str, str]:
        descendants = iter_descendants(self._proc, runtime_pid)

        # Exact exit detection once a child was matched.
        if self._tracked_child is not None:
            stat = read_pid_stat(self._proc, self._tracked_child)
            alive = stat is not None and stat[0] != "Z" and self._tracked_child in descendants
            if alive:
                self._child_gone_ts = None
                stuck = self._stuck_input_check(self._tracked_child)
                if stuck:
                    return VERDICT_STUCK_INPUT, stuck
                return VERDICT_WORKING, f"shell child {self._tracked_child} alive"
            if self._child_gone_ts is None:
                self._child_gone_ts = self._now()
            gone_for = self._now() - self._child_gone_ts
            if gone_for > CHILD_EXIT_GRACE_SECS:
                return (
                    VERDICT_DEAD,
                    f"shell child {self._tracked_child} exited {gone_for:.0f}s ago, no result frame",
                )
            return VERDICT_UNKNOWN, f"shell child exited {gone_for:.0f}s ago (grace)"

        # Not matched yet: scan for a live non-zombie descendant whose cmdline
        # matches this session's cached command.
        fragment = match_fragment(tool.command)
        program = first_program(tool.command)
        for pid in descendants:
            if pid == runtime_pid:
                continue
            stat = read_pid_stat(self._proc, pid)
            if stat is None or stat[0] == "Z":
                continue
            cmdline = read_cmdline(self._proc, pid)
            if not cmdline:
                continue
            matched = bool(fragment) and fragment in cmdline
            if not matched and program:
                base_tokens = {t.rsplit("/", 1)[-1] for t in cmdline.split()}
                matched = program in base_tokens
            if not matched:
                continue
            # Best-effort start-time check: skip a process that predates the
            # dispatch by more than a small tolerance (a pre-existing lookalike).
            age = process_age_secs(self._proc, stat[1])
            if age is not None and tool.dispatch_ts:
                started_at = self._now() - age
                if started_at < tool.dispatch_ts - 10.0:
                    continue
            self._tracked_child = pid
            self._child_gone_ts = None
            # Prime the stuck-detection movement baseline now so the NEXT check
            # can already compare deltas (otherwise stuck detection needs three
            # ticks: match, baseline, compare).
            self._tree_movement(pid, key_prefix="stuck")
            return VERDICT_WORKING, f"shell child {pid} matched command"
        return VERDICT_UNKNOWN, "no matching shell child"

    def _stuck_input_check(self, pid: int) -> str:
        """STUCK_INPUT evidence for a live child subtree, or "" when not stuck.

        Requires (a) the ENTIRE matched subtree flat on CPU+IO across two
        samples, and (b) at least one process blocked reading a tty or its
        stdin pipe. A socket-blocked read is a network wait (not stuck); a
        futex/lock wait is ambiguous — both return "" (UNKNOWN-ish: the caller
        treats a live child without stuck evidence as WORKING, and genuinely
        ambiguous hangs are bounded by the UNKNOWN timeout class elsewhere).
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
        if evidence in ("sampling", "no readable counters"):
            # No baseline yet, or /proc unreadable — cannot attest either way.
            return VERDICT_UNKNOWN, evidence
        # Flat counters: distinguish the done-but-lost-frame wedge (no backend
        # connection at all) from a probably-thinking server-side silence
        # (established socket, nothing flowing yet).
        established = self._any_established(runtime_pid)
        if established:
            return VERDICT_UNKNOWN, f"{EVIDENCE_ESTABLISHED_FLAT}: {evidence}"
        return VERDICT_DEAD, f"no established backend socket and flat counters ({evidence})"

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
            return False, "sampling"
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
