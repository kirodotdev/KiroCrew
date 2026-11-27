"""Host memory guard: at critical memory, stop Crew's heaviest non-gateway tool tree.

The fast layer of ``docs/request-for-change/rfc-host-memory-guard.md`` (§4).
``ResourcePressureNotifier`` calls :func:`request` on the loop when its guard trigger
holds (critical posture; tight with PSI ``full avg10`` >= 10; or, while the memory
cgroup is out of view, the visible cgroup stalled 10% of the time since the last
sample); :func:`request` schedules one single-flight :func:`pressure_reclaim` pass,
which:

* collects every live session runtime from the session manager's map, with no skip
  of persistent or channel sessions (this pass never recycles a session);
* walks each runtime's descendants and detached leftovers OFF the loop, never
  entering a protected pid (every runtime, warm-pool spare, companion runtime,
  in-flight launch, the gateway itself) or an MCP root, and walking through a
  managed agent harness without ever counting it in a tree;
* picks ONE tree: the largest test-runner tree (tier 1), else the largest tree
  rooted at a shell the agent process itself started (tier 2), never one under :data:`TREE_FLOOR_MB`, preferring a
  session not stopped in the last :data:`COOLDOWN_SECS`;
* on Linux stops it leaf first through ``terminate_pids_pinned`` (SIGTERM, SIGKILL
  after :data:`GRACE_SECS`), which re-identifies each pid before signalling it;
  elsewhere it stops nothing, because the pinned terminator signals nothing there;
* writes one log line, and hands the owning chat's notice and, for a stop, the
  ledger summary to ``SessionManager.on_memory_guard_stop``.

The next pass runs on the next notifier sample and only while the trigger holds,
so one tree is stopped per pass and memory is re-read in between.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.session_pid_harness import (
    _MANAGED_AGENT_RUNTIME_BASENAMES,
    _argv_tokens,
    _basename_of,
    _cmdline_names_a_harness,
    _pid_cmdline,
    _work_orphan_basename,
)
from kiro_crew.session_pid_orphan_scan import (
    _is_marked_mcp_launcher,
    _is_orphan_mcp,
    _work_sweep_cmdline_is_test_runner,
)

logger = logging.getLogger(__name__)

#: A tree under this is never picked: stopping it frees too little to be worth the work.
#: shortcut: one fixed floor; make it a fraction of RAM if small hosts show it never fires.
TREE_FLOOR_MB = 512
#: SIGTERM to SIGKILL. Test runners exit on SIGTERM within a second; a longer grace
#: keeps memory held while the host is critical.
GRACE_SECS = 5.0
#: A session just stopped is skipped while another session has a candidate.
COOLDOWN_SECS = 60.0
#: Roots a tier-2 tree may have: the agent's shell tool, a shell whose parent is the
#: agent process itself (``hosts_tools`` in :func:`scan`), so a shell an MCP launcher
#: such as ``npx`` runs below itself is never a root.
SHELL_BASENAMES = frozenset(
    {b"sh", b"bash", b"zsh", b"dash", b"fish", b"cmd.exe", b"powershell", b"pwsh"}
)
#: Tree identities already reported, kept so a tree that cannot be stopped (or one
#: the host forbids stopping) is told once, not on every 5 s sample.
_HANDLED_MAX = 256

TIER_TEST = "test"
TIER_TOOL = "tool"


@dataclass(frozen=True)
class ProcessView:
    """The blocking reads one pass is built from; a test seam for every host."""

    children: Callable[[int], list[int]]
    cmdline: Callable[[int], bytes]
    rss_mb: Callable[[int], float]
    start_id: Callable[[int], str | None]
    #: ``(pid, start_id)`` of each detached leftover, read with the session listing.
    leftovers: Callable[[int], list[tuple[int, str]]]
    #: The parent of *pid* read now, or None. With ``verifies_parents``, a pid whose
    #: current parent differs from the walk's edge is a recycled pid, never a target.
    parent_of: Callable[[int], int | None] = lambda _pid: None
    verifies_parents: bool = False
    #: Whether *pid* carries a harness's shell-tool launch marker. A tier-2 root must:
    #: an MCP server whose configured command is ``bash -c ...`` has the same shape
    #: and parent as the shell tool, but not its environment.
    is_shell_tool: Callable[[int], bool] = lambda _pid: False


@dataclass(frozen=True)
class Candidate:
    """One tree the guard may stop."""

    session_key: str
    runtime_pid: int
    tier: str
    root_pid: int
    root_start: str
    label: str
    rss_mb: int
    procs: int
    #: ``(pid, start_id)`` leaf first, as ``terminate_pids_pinned`` takes them.
    targets: tuple[tuple[int, str], ...]


@dataclass
class MemoryGuardState:
    """Owned by the session manager; touched only on the loop."""

    task: asyncio.Task[Any] | None = None
    stopped_at: dict[str, float] = field(default_factory=dict)
    handled: set[tuple[int, str]] = field(default_factory=set)
    #: Trees whose failed stop the chat was already told about: told once, retried
    #: on every pass, and a later successful stop is still told.
    failed_told: set[tuple[int, str]] = field(default_factory=set)
    unsupported_warned: bool = False


def _safe_label(raw: bytes) -> str:
    """A process name safe for a log line or a chat notice: ``[A-Za-z0-9._+-]``, 32 max."""
    text = raw.decode("ascii", errors="ignore")
    kept = "".join(ch for ch in text if ch.isalnum() or ch in "._+-")
    return kept[:32] or "?"


def _leftover_pids(root: int) -> list[tuple[int, str]]:
    from kiro_crew.session_pid_rss import find_session_leftovers

    found = find_session_leftovers(root)
    return [] if found is None else [(item.pid, item.start_id) for item in found.items]


def _rss_mb(pid: int) -> float:
    from kiro_crew.session_pid_rss import _process_mb

    return float(_process_mb(pid))


#: The variable kiro-cli sets on the commands its shell tool runs and on no MCP server
#: it starts. shortcut: kiro-cli only; another harness's shell trees stay out of tier 2
#: (its test runners stay in tier 1) until its own marker is listed here.
SHELL_TOOL_MARKERS = (b"KIRO_SESSION_ID=",)


def _linux_is_shell_tool(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/environ", "rb") as handle:
            env = handle.read().split(b"\x00")
    except OSError:
        return False
    return any(item.startswith(SHELL_TOOL_MARKERS) for item in env)


def _darwin_is_shell_tool(pid: int) -> bool:
    env = platform_compat.darwin_process_environ(pid) or []
    return any(item.startswith(SHELL_TOOL_MARKERS) for item in env)


def _darwin_cmdline(pid: int) -> bytes:
    argv = platform_compat.darwin_process_argv(pid)
    return b"\x00".join(arg.encode("utf-8", errors="replace") for arg in argv or ())


def host_view() -> ProcessView | None:
    """This host's reads, or None where no process tree with argv can be read (Windows)."""
    if platform_compat.IS_LINUX:
        child_map = platform_compat.proc_child_map() or {}
        return ProcessView(
            children=lambda pid: list(child_map.get(pid, ())),
            cmdline=_pid_cmdline,
            rss_mb=_rss_mb,
            start_id=platform_compat.get_process_start_id,
            leftovers=_leftover_pids,
            parent_of=platform_compat.parent_pid,
            verifies_parents=True,
            is_shell_tool=_linux_is_shell_tool,
        )
    if platform_compat.IS_MACOS:
        # Nothing is signalled here (``can_stop_trees``), so no parent re-read.
        return ProcessView(
            children=lambda pid: platform_compat.darwin_child_pids(pid) or [],
            cmdline=_darwin_cmdline,
            rss_mb=_rss_mb,
            start_id=platform_compat.get_process_start_id,
            leftovers=_leftover_pids,
            is_shell_tool=_darwin_is_shell_tool,
        )
    return None


def scan_host(
    runtimes: dict[int, str], protected: frozenset[int], spawned: dict[int, str | None]
) -> list[Candidate] | None:
    """:func:`host_view` and :func:`scan` in one job; None where no view exists.

    *spawned* is each runtime's start identity as its client recorded it at spawn.
    A runtime whose pid now names another process (or that recorded none) is not
    walked: the tree below it is not Crew's.

    BLOCKING: the Linux view reads every ``/proc/<pid>/stat`` on the host, so it is
    built here, on the maintenance executor, never on the loop.
    """
    view = host_view()
    if view is None:
        return None
    ours = {
        pid: key
        for pid, key in runtimes.items()
        if spawned.get(pid) and view.start_id(pid) == spawned[pid]
    }
    return scan(ours, protected, view)


def can_stop_trees() -> bool:
    """Whether this host can stop a tree: the pinned terminator signals on Linux only."""
    return platform_compat.IS_LINUX is True


def is_test_runner(cmdline: bytes) -> bool:
    """The orphan sweep's three test-runner shapes, plus pytest's console script.

    A ``pytest`` entry script exec'd through its shebang reads ``python /venv/bin/pytest
    ...`` in ``/proc/<pid>/cmdline``: argv0 names the interpreter and argv1 the script,
    so the sweep's argv0 test misses the most usual way pytest starts. Kept here, not in
    the sweep's classifier, because that one gates kills of detached orphans.
    """
    if _work_sweep_cmdline_is_test_runner(cmdline):
        return True
    tokens = _argv_tokens(cmdline)
    return (
        len(tokens) >= 2
        and _basename_of(tokens[0]).startswith(b"python")
        and _basename_of(tokens[1]) in (b"pytest", b"py.test")
    )


def _is_mcp(cmdline: bytes) -> bool:
    # Exclusion only, so the launcher shape alone is enough: a false match keeps a
    # process alive, never ends one.
    return _is_orphan_mcp(cmdline) or _is_marked_mcp_launcher(cmdline)


def scan(runtimes: dict[int, str], protected: frozenset[int], view: ProcessView) -> list[Candidate]:
    """Every tier-1 and tier-2 tree under *runtimes* (pid -> owning session key).

    BLOCKING: run on the maintenance executor, never the loop.
    """
    out: list[Candidate] = []
    for runtime, key in runtimes.items():
        cmds: dict[int, bytes] = {}
        # The start identity read BEFORE each argv: every verdict drawn from that
        # argv (MCP wall, harness, test runner, shell) belongs to this identity.
        seen_as: dict[int, str | None] = {}

        def cmd(pid: int) -> bytes:
            if pid not in cmds:
                seen_as[pid] = view.start_id(pid)
                cmds[pid] = view.cmdline(pid)
            return cmds[pid]

        def walled(pid: int) -> bool:
            if pid in protected or pid == runtime or pid <= 1:
                return True
            line = cmd(pid)
            if not line:
                return True  # unreadable: fail closed, never a target
            return _is_mcp(line)

        def is_harness(pid: int) -> bool:
            line = cmd(pid)
            return _work_orphan_basename(
                line
            ) in _MANAGED_AGENT_RUNTIME_BASENAMES or _cmdline_names_a_harness(line)

        # The runtime's live tree, then its detached leftovers (same session id,
        # reparented to init). A wall is neither kept nor entered. A harness is
        # walked through (the agent's tools hang below it) but is never in a target.
        kids: dict[int, list[int]] = {}
        left_start = dict(view.leftovers(runtime))
        # Seed only the topmost leftovers: one whose parent is a leftover too is
        # reached through that parent, so an MCP launcher's wall covers its children.
        seeds = [pid for pid in left_start if view.parent_of(pid) not in left_start]
        frontier = list(view.children(runtime)) + seeds
        while frontier:
            pid = frontier.pop()
            if pid in kids or walled(pid):
                continue
            kids[pid] = list(view.children(pid))
            frontier.extend(kids[pid])
        top = list(view.children(runtime))
        parent = {
            child: pid for pid, children in kids.items() for child in children if child in kids
        }
        parent.update({child: runtime for child in top if child in kids})

        def is_test(pid: int) -> bool:
            return is_test_runner(cmd(pid))

        def is_shell(pid: int) -> bool:
            return _work_orphan_basename(cmd(pid)) in SHELL_BASENAMES

        def hosts_tools(pid: int) -> bool:
            """Whether *pid* is the agent process its shell tool runs commands under.

            The runtime, a harness, or a harness's ONLY child (its engine: kiro-cli
            v3 runs tools under one node process). A launcher such as ``npx`` runs an
            MCP server's bin through ``sh -c``; it is a harness's child with
            siblings, or a grandchild, so a shell below it is never a tier-2 root.
            shortcut: structural, not an identity read; a harness that spawns a
            lone MCP launcher before any shell would pass, so read a launch marker
            if one appears.
            """
            if pid == runtime or (pid in kids and is_harness(pid)):
                return True
            up = parent.get(pid)
            if up == runtime:
                # The runtime's ONLY child is its engine when the launcher execs the
                # bundled KAS node directly (no kiro-cli process between them).
                return top == [pid]
            if up is None or up not in kids or not is_harness(up):
                return False
            return kids[up] == [pid]

        roots: list[tuple[str, int]] = [
            (TIER_TEST, pid)
            for pid in kids
            if is_test(pid) and not (pid in parent and is_test(parent[pid]))
        ]
        roots += [
            (TIER_TOOL, pid)
            for pid in kids
            if is_shell(pid)
            and view.is_shell_tool(pid)
            and (pid not in parent or hosts_tools(parent[pid]))
        ]
        for tier, root in roots:
            candidate = _measure(
                key, runtime, tier, root, kids, parent, left_start, seen_as, cmd, view
            )
            # A tree holding a harness is another agent's, never a tool to stop.
            if candidate is not None and not any(is_harness(p) for p, _ in candidate.targets):
                out.append(candidate)
    return out


def _measure(
    key: str,
    runtime: int,
    tier: str,
    root: int,
    kids: dict[int, list[int]],
    parent: dict[int, int],
    left_start: dict[int, str],
    seen_as: dict[int, str | None],
    cmd: Callable[[int], bytes],
    view: ProcessView,
) -> Candidate | None:
    order: list[int] = []
    queue = [root]
    while queue:
        pid = queue.pop(0)
        if pid in order:
            continue
        order.append(pid)
        queue.extend(child for child in kids.get(pid, ()) if child in kids)
    starts = {pid: view.start_id(pid) for pid in order}
    # Bind each identity to the edge the walk saw: read AFTER the start id, a
    # parent that still matches means that id belongs to a process in this tree.
    # A pid reused in between has another parent and is dropped with everything
    # below it; a leftover root must still carry the identity its session listed.
    kept: set[int] = set()
    for pid in order:
        start = starts[pid]
        if not start:
            continue  # cannot be pinned, so never signalled
        if pid in seen_as and seen_as[pid] != start:
            continue  # recycled after its argv was judged: the verdict is another's
        if pid == root and root in left_start:
            ok = start == left_start[root]
        elif pid != root and parent.get(pid) not in kept:
            ok = False
        else:
            ok = not view.verifies_parents or view.parent_of(pid) == parent.get(pid)
        if ok:
            kept.add(pid)
    if root not in kept:
        return None
    order = [pid for pid in order if pid in kept]
    sizes = {pid: view.rss_mb(pid) for pid in order}
    root_start = starts[root]
    assert root_start is not None
    targets = tuple((pid, starts[pid] or "") for pid in reversed(order))
    if tier == TIER_TEST:
        label = "pytest"
    else:
        heaviest = max(order, key=lambda pid: sizes[pid])
        label = _safe_label(_work_orphan_basename(cmd(heaviest)))
    return Candidate(
        session_key=key,
        runtime_pid=runtime,
        tier=tier,
        root_pid=root,
        root_start=root_start,
        label=label,
        rss_mb=int(sum(sizes.values())),
        procs=len(order),
        targets=targets,
    )


def pick(candidates: list[Candidate], stopped_at: dict[str, float], now: float) -> Candidate | None:
    """The one tree to stop: tier 1 before tier 2, largest first, at or over the floor.

    A session stopped within :data:`COOLDOWN_SECS` is skipped while another session
    has an eligible tree in the same tier, and picked when it is the only one: the
    host must not die to honour a cool-down.
    """
    for tier in (TIER_TEST, TIER_TOOL):
        eligible = [c for c in candidates if c.tier == tier and c.rss_mb >= TREE_FLOOR_MB]
        if not eligible:
            continue
        fresh = [
            c
            for c in eligible
            if now - stopped_at.get(c.session_key, float("-inf")) >= COOLDOWN_SECS
        ]
        return max(fresh or eligible, key=lambda c: c.rss_mb)
    return None


def stop(candidate: Candidate, protected: frozenset[int]) -> tuple[int, str]:
    """Stop *candidate* leaf first; ``(processes ended, "TERM" | "KILL")``. Linux only.

    BLOCKING: run on the maintenance executor. ``terminate_pids_pinned`` waits the
    whole grace only when a target outlived SIGTERM, so the label is inferred from the
    elapsed time: ``KILL`` means the grace ran out and SIGKILL was sent to any
    survivor. shortcut: a thread descheduled for the whole grace reads ``KILL`` for a
    tree SIGTERM ended; read the escalation from the terminator if the log needs it exact.
    """
    started = time.monotonic()
    ended = platform_compat.terminate_pids_pinned(
        list(candidate.targets), protected_pids=tuple(sorted(protected)), grace_secs=GRACE_SECS
    )
    signal = "KILL" if ended and time.monotonic() - started >= GRACE_SECS else "TERM"
    return ended, signal


def _gb(mb: int) -> str:
    return f"{mb / 1024:.1f} GB"


def _state_words(trigger: str) -> str:
    # The PSI arms fire on a stall: memory stalls, it is not below critical.
    return "under heavy pressure" if trigger in ("psi", "psi-cgroup") else "critically low"


#: What a stop on the visible cgroup's stall measured: that stall, and neither the
#: cgroup's memory use nor any limit, since the gateway cannot read them there; the free
#: figure beside it is the host's.
_OUT_OF_VIEW = "stalled in Crew's cgroup, whose memory use Crew cannot read"


def _subject_and_reading(status: Any, trigger: str) -> tuple[str, str]:
    """What the figure measures and how it reads; the host figure when out of view."""
    if trigger == "psi-cgroup":
        return "Memory", f"{_OUT_OF_VIEW}; ~{status.available_gb:.1f} GB free on the host"
    return status.memory_subject, status.memory_reading()


def _was_low(status: Any, trigger: str) -> str:
    """``Host memory was critically low (~1.4 GB free)``, naming the agent ceiling when it binds."""
    subject, reading = _subject_and_reading(status, trigger)
    return f"{subject} was {_state_words(trigger)} ({reading})"


def _is_low(status: Any, trigger: str) -> str:
    subject, reading = _subject_and_reading(status, trigger)
    return f"{subject} is {_state_words(trigger)} ({reading})"


def chat_notice(
    candidate: Candidate, status: Any, *, stopped: bool, can_stop: bool, trigger: str = "critical"
) -> str:
    """The one notice the owning chat gets about this tree."""
    what = f"`{candidate.label}` ({_gb(candidate.rss_mb)}, {candidate.procs} processes)"
    low = _was_low(status, trigger)
    if not can_stop:
        return (
            f"{_is_low(status, trigger)}. {what}, which this "
            "chat started, is the largest Crew tool process tree, but Crew cannot stop it "
            "safely on this host, so it keeps running. Stop it, or wait until memory frees."
        )
    if not stopped:
        return (
            f"{low}, so Crew tried to stop {what} that this chat started, but it could not "
            "be stopped. Stop it, or wait until memory frees."
        )
    return (
        f"{low}, so Crew stopped {what} that this chat started. The session keeps "
        "running. Retry with fewer parallel workers, or wait until memory frees."
    )


def shared_notice(
    candidate: Candidate,
    status: Any,
    tenants: int,
    *,
    stopped: bool,
    can_stop: bool,
    trigger: str = "critical",
) -> str:
    """The notice each chat on a shared runtime gets: which of them started it is unknown."""
    what = f"`{candidate.label}` ({_gb(candidate.rss_mb)}, {candidate.procs} processes)"
    if stopped:
        outcome = f"{_was_low(status, trigger)}, so Crew stopped {what}."
    elif can_stop:
        outcome = (
            f"{_was_low(status, trigger)}, so Crew tried to "
            f"stop {what}, but it could not be stopped."
        )
    else:
        outcome = (
            f"{_is_low(status, trigger)}. {what} is the largest "
            "Crew tool process tree, but Crew cannot stop it safely on this host."
        )
    return (
        f"{outcome} It runs in the agent process this chat shares with {tenants - 1} other "
        "chat(s), and Crew cannot tell which of them started it, so each of them gets "
        "this notice."
    )


def ledger_summary(candidate: Candidate, status: Any, trigger: str = "critical") -> str:
    """The ``summary`` of the ``question`` report on the owning worker's item."""
    if trigger == "psi-cgroup":
        reading = f"memory {_OUT_OF_VIEW} (~{status.available_gb:.1f} GB free on the host)"
    else:
        reading = f"{status.memory_subject.lower()} {status.memory_reading()}"
    return (
        f"memory guard: stopped {candidate.label} ({_gb(candidate.rss_mb)}); "
        f"{reading}; the worker keeps running"
    )


def request(manager: Any, status: Any, psi: float | None, trigger: str) -> asyncio.Task[Any]:
    """Schedule one guard pass unless one is running; the pass that runs. On the loop."""
    state: MemoryGuardState = manager._memory_guard
    if state.task is not None and not state.task.done():
        return state.task
    state.task = asyncio.get_running_loop().create_task(
        pressure_reclaim(manager, status, psi, trigger)
    )
    return state.task


async def pressure_reclaim(manager: Any, status: Any, psi: float | None, trigger: str) -> None:
    """One guard pass. Never raises: a failure is logged and the next sample retries."""
    try:
        await _pass(manager, status, psi, trigger)
    except Exception:
        logger.exception("Memory guard: pass failed")


async def _pass(manager: Any, status: Any, psi: float | None, trigger: str) -> None:
    from kiro_crew.executors import maintenance_executor

    state: MemoryGuardState = manager._memory_guard
    loop = asyncio.get_running_loop()
    tenants: dict[int, list[str]] = {}
    spawned: dict[int, str | None] = {}
    async with manager._lock:
        for key, pid, session in manager._cleanup_boundary().session_runtime_pids():
            tenants.setdefault(pid, []).append(key)
            client = getattr(getattr(session, "provider", None), "client", None)
            spawned[pid] = getattr(client, "_start_time", None)
    # A tree is credited to its runtime's one session; on a runtime several sessions
    # share (the chat-runtime-sharing preview) nothing says which one started it.
    runtimes = {pid: keys[0] for pid, keys in tenants.items()}
    # Read on the loop: ``_pool_pids`` drains the warm pool's asyncio queue.
    protected = frozenset(
        set(runtimes)
        | manager._pool_pids()
        | manager._in_flight_pids()
        | manager._companion_runtime_pids()
        | {os.getpid()}
    )
    executor = maintenance_executor()
    candidates = await loop.run_in_executor(executor, scan_host, runtimes, protected, spawned)
    if candidates is None:
        if not state.unsupported_warned:
            state.unsupported_warned = True
            logger.warning(
                "Memory guard: memory is critical or stalling (%.1f GB free) but this host cannot "
                "read tool process trees, so nothing was stopped",
                status.available_gb,
            )
        return
    now = time.monotonic()
    for key in [k for k, at in state.stopped_at.items() if now - at >= COOLDOWN_SECS]:
        del state.stopped_at[key]
    chosen = pick(candidates, state.stopped_at, now)
    if chosen is None:
        return
    identity = (chosen.root_pid, chosen.root_start)
    can_stop = can_stop_trees()
    first = identity not in state.handled
    if not can_stop and not first:
        return
    ended, signal = 0, "-"
    if can_stop:
        ended, signal = await loop.run_in_executor(executor, stop, chosen, protected)
        state.stopped_at[chosen.session_key] = time.monotonic()
    if len(state.handled) >= _HANDLED_MAX:
        state.handled.clear()
        state.failed_told.clear()
    if ended or not can_stop:
        # A failed stop is not "handled": the next pass retries it, and a retry
        # that succeeds still tells the chat and files the report.
        state.handled.add(identity)
    logger.warning(
        "Memory guard: %s tier=%s session=%s tenants=%d root_pid=%d start=%s cmd=%s tree_rss_mb=%d "
        "procs=%d available_gb=%.1f %s=%s trigger=%s signal=%s freed=%s",
        "stopped" if can_stop else "could not stop (unsupported host)",
        chosen.tier,
        chosen.session_key,
        len(tenants.get(chosen.runtime_pid, ())),
        chosen.root_pid,
        chosen.root_start,
        chosen.label,
        chosen.rss_mb,
        chosen.procs,
        status.available_gb,
        # ``psi-cgroup`` hands over the visible cgroup's stall share, not an avg10.
        "psi_full_pct" if trigger == "psi-cgroup" else "psi_full_avg10",
        "-" if psi is None else f"{psi:.2f}",
        trigger,
        signal,
        "yes" if ended else "no",
    )
    if not first:
        return  # a tree already told: the log line is enough
    if can_stop and not ended:
        if identity in state.failed_told:
            return  # its failed stop was told; this retry failed too
        state.failed_told.add(identity)
    callback = getattr(manager, "on_memory_guard_stop", None)
    if callback is None:
        return
    stopped = bool(ended)
    sharers = tenants.get(chosen.runtime_pid, [chosen.session_key])
    if len(sharers) > 1:
        # No owner can be named, so no item gets a report: a report on the wrong
        # item would replace another worker's status. Every sharer is told.
        text = shared_notice(
            chosen, status, len(sharers), stopped=stopped, can_stop=can_stop, trigger=trigger
        )
        for key in sharers:
            callback(key, text, None)
        return
    notice = chat_notice(chosen, status, stopped=stopped, can_stop=can_stop, trigger=trigger)
    summary = ledger_summary(chosen, status, trigger) if stopped else None
    callback(chosen.session_key, notice, summary)
