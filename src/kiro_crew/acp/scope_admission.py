"""Scope admission: the startup stage between a scoped spawn and ``initialize``.

``sandbox.cgroup_scope_argv`` puts ``systemd-run --user --scope ... --`` in front of
an agent's argv. With ``--scope``, systemd-run asks the user systemd manager for a
transient scope, waits until the manager has moved systemd-run's own pid into it,
and then execs the wrapped command in that same pid. Until the manager has moved
the pid, no agent exists, so the time spent there belongs to the manager. On a host
whose user manager is slow to answer, that wait alone can use the whole
``initialize`` budget, and the timeout then reads as an agent that did not answer.

This module holds that stage: :func:`wait_for_admission` polls
``/proc/<pid>/cgroup`` until the pid's cgroup path holds the scope unit, under its
own bound, and the helpers here budget, log and word its outcome. The move into the
scope is the manager's own act, so the wait measures it directly. The process name
cannot mark it: the spawn's pid first runs the rlimit spawn shim, a Python
interpreter that execs systemd-run (``sandbox_rlimit.spawn_shim_argv``), and the
layer after systemd-run can be Python as well.

A host where the check cannot run counts as admitted and goes on to ``initialize``
as it would without the stage: at once for a permission error or a file it cannot
parse, and after :data:`MISSING_PID_GRACE_S` for a pid that procfs does not list (no
procfs, or one for another pid namespace). ``runtime_spawn_launch._admit_scope``
runs the wait before anything is written to the agent's stdin, and raises its errors.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.acp.runtime import AcpRuntime

#: The runtime's logger, so the stage's records sit beside the other
#: ``acp_startup_stage`` lines ``runtime_wire`` writes.
logger = logging.getLogger("kiro_crew.acp.runtime")

#: Interval between two reads of the pid's cgroup. A healthy manager admits in
#: milliseconds, so the first or second read already finds the pid in its scope.
POLL_INTERVAL_S = 0.05

#: An admission slower than this logs at WARNING: it is the first sign of a user
#: manager that is falling behind, before any start runs out of time.
SLOW_ADMISSION_S = 5.0

#: How long the pid can be absent from ``/proc`` with no exit seen before the check
#: counts as unavailable. The wrapper is this process's own child, so it leaves
#: ``/proc`` only when it is reaped, and the reap sets its returncode in the same
#: loop turn or the next. A pid that stays absent with no exit means this procfs
#: does not list the child at all (one mounted for another pid namespace), and the
#: wait must not turn that host's spawns into admission timeouts.
MISSING_PID_GRACE_S = 2.0

#: More than a ``/proc/<pid>/cgroup`` file holds: one line on cgroup v2, which the
#: scope wrap requires, and about a dozen short lines on a v1 host.
_CGROUP_READ_LIMIT = 8192

ADMITTED = "admitted"
UNCHECKED = "unchecked"
EXITED = "exited"
TIMEOUT = "timeout"


@dataclass(frozen=True)
class Admission:
    """What :func:`wait_for_admission` found: the outcome and the seconds it waited.

    ``detail`` is why the check could not run, for ``unchecked``. ``reads`` counts
    the reads that found the pid outside its scope, so an ``exited`` wait with
    ``reads == 0`` read nothing before the exit.
    """

    outcome: str
    waited_s: float
    detail: str = ""
    reads: int = 0


def in_scope(cgroup_text: str, unit: str) -> bool:
    """Whether a ``/proc/<pid>/cgroup`` text puts the pid in the scope *unit*, or below it.

    Each line is ``<id>:<controllers>:<path>``, and the pid is in the scope when
    *unit* is one component of a path. A process the agent starts in a child cgroup
    of the scope is still in it. The path is relative to the reader's cgroup
    namespace, so a namespace the agent's sandbox makes does not change it. Raises
    ``ValueError`` for a text with no such line.
    """
    paths = [line.split(":", 2)[2] for line in cgroup_text.splitlines() if line.count(":") >= 2]
    if not paths:
        raise ValueError("the cgroup file has no hierarchy line")
    return any(unit in PurePosixPath(path).parts for path in paths)


def read_proc_cgroup(pid: int) -> str:
    """The text of ``/proc/<pid>/cgroup``. Raises the ``OSError`` of the read."""
    with open(f"/proc/{pid}/cgroup", "rb") as handle:
        raw = handle.read(_CGROUP_READ_LIMIT)
    return raw.decode("utf-8", "replace")


async def read_proc_cgroup_off_loop(pid: int) -> str:
    """:func:`read_proc_cgroup` on a worker thread, because it is a file read."""
    return await asyncio.to_thread(read_proc_cgroup, pid)


async def wait_for_admission(
    pid: int,
    unit: str,
    *,
    exited: Callable[[], bool],
    bound_s: float,
    read_cgroup: Callable[[int], Awaitable[str]] = read_proc_cgroup_off_loop,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    poll_s: float = POLL_INTERVAL_S,
) -> Admission:
    """Wait until *pid* is in the scope *unit* (:func:`in_scope`), for at most *bound_s* seconds.

    A read that finds the pid in its scope is ``admitted``: the manager has moved it,
    and systemd-run execs the next layer (``env``, ``bwrap`` or the agent) when the
    manager reports the job done. *exited* says whether the process has exited (or
    its runtime was marked dead); it is asked before every read, so an exit ends the
    wait as ``exited`` even while ``/proc`` still lists the unreaped process.
    ``exited`` says only that no read found the pid in its scope: the manager can
    move it, and the next layer exit, between two reads. A read that cannot run is
    ``unchecked``, and so is a read still queued for a worker thread when the bound
    passes: a full executor says nothing about the manager. The bound passing with
    the pid still outside its scope is ``timeout``. *read_cgroup*, *clock* and
    *sleep* are the procfs reader, the monotonic clock and the pause between reads.
    """
    started = clock()
    missing_since: float | None = None
    reads = 0
    while True:
        if exited():
            return Admission(EXITED, clock() - started, reads=reads)
        read_bound = max(bound_s - (clock() - started), poll_s)
        try:
            admitted = in_scope(await asyncio.wait_for(read_cgroup(pid), read_bound), unit)
        except TimeoutError:
            return Admission(UNCHECKED, clock() - started, "read_timeout")
        except (FileNotFoundError, ProcessLookupError):
            if exited():
                return Admission(EXITED, clock() - started, reads=reads)
            now = clock()
            if missing_since is None:
                missing_since = now
            elif now - missing_since >= MISSING_PID_GRACE_S:
                return Admission(UNCHECKED, now - started, "pid_not_listed")
        except (OSError, ValueError) as exc:
            return Admission(UNCHECKED, clock() - started, type(exc).__name__)
        else:
            if admitted:
                return Admission(ADMITTED, clock() - started, reads=reads)
            reads += 1
            missing_since = None
            waited = clock() - started
            if waited >= bound_s:
                return Admission(TIMEOUT, waited, reads=reads)
        await sleep(poll_s)


def initialize_budget(window_s: float, reserve_s: float, admission_s: float | None) -> float:
    """The ``initialize`` budget left in the cold-start window after scope admission.

    *window_s* is the whole window and *admission_s* the time admission took, or
    ``None`` for a spawn with no scope, which leaves the whole window. Never less
    than *reserve_s*: an admission that ran to its bound still leaves the agent one
    plain request budget to answer in.
    """
    if admission_s is None:
        return window_s
    return max(window_s - admission_s, reserve_s)


async def exit_evidence(runtime: AcpRuntime) -> tuple[str, str]:
    """The exit status label and the published stderr tail of a start that died.

    A death is seen before the stderr drain and the reap have run, so the reason
    would often name no code and an empty tail. Both are waited for first: the drain
    under ``settle_stderr``'s bound, the exit under ``_KILL_REAP_TIMEOUT``. The tail
    is published only through ``safe_stderr_tail``, the gate the death summary uses,
    and a runtime that may not record shows ``<not retained>``. Shared by the
    ``initialize`` death path and this stage.
    """
    await runtime.settle_stderr()
    process = runtime._process
    if process is not None and process.returncode is None:
        try:
            await asyncio.wait_for(process.wait(), runtime._KILL_REAP_TIMEOUT)
        except Exception:
            logger.debug("AcpRuntime: exit status unread after a failed start", exc_info=True)
    rc = runtime._returncode_label()
    if runtime.recording_allowed:
        from kiro_crew.acp.stderr_tail import safe_stderr_tail

        tail = safe_stderr_tail(runtime._stderr_lines[-5:], " | ") or "<none>"
    else:
        tail = "<not retained>"
    return rc, tail


def timeout_message(scope_unit: str, pid: int, bound_s: float) -> str:
    """The text of ``AcpScopeAdmissionTimeout``: what failed, and what to count."""
    return (
        f"systemd did not move the agent process (PID {pid}) into its scope {scope_unit} "
        f"within {bound_s:g}s, so the agent has not started. The user systemd manager is "
        "slow to answer. Count its failed units with "
        "'systemctl --user list-units --state=failed --no-legend | wc -l'."
    )


def died_message(scope_unit: str, pid: int, returncode: str, stderr_tail: str, reads: int) -> str:
    """The text of ``AcpScopeAdmissionDied``: what the reads found, the process, its last words.

    It claims no more than the wait measured. *reads* reads found the pid outside its
    scope (``reads=0``: none ran before the exit). The manager can still have moved
    it, and the next layer exited, between two reads, so the stderr tail is what
    names the layer that failed.
    """
    return (
        "the agent process exited during the scope admission wait, before a read found "
        f"it in its scope [stage=scope_admission reads={reads} pid={pid} scope={scope_unit} "
        f"returncode={returncode}] stderr_tail: {stderr_tail}"
    )


def log_stage(
    admission: Admission, *, bound_s: float, pid: int, unit: str, returncode: str | None = None
) -> None:
    """One ``acp_startup_stage`` record for this stage, in ``runtime_wire``'s key=value shape.

    ``unchecked`` is logged as ``admitted`` with ``proc_check=unavailable``: the
    spawn goes on as it would without the stage. An admission slower than
    :data:`SLOW_ADMISSION_S` logs at WARNING, as do a timeout and an exit.
    """
    outcome = ADMITTED if admission.outcome == UNCHECKED else admission.outcome
    extra = ""
    if admission.outcome == UNCHECKED:
        extra = f" proc_check=unavailable reason={admission.detail}"
    if admission.outcome == EXITED:
        extra = f" reads={admission.reads}"
    if returncode is not None:
        extra += f" returncode={returncode}"
    slow = admission.waited_s > SLOW_ADMISSION_S
    level = logging.INFO if outcome == ADMITTED and not slow else logging.WARNING
    logger.log(
        level,
        "acp_startup_stage stage=scope_admission outcome=%s duration_ms=%.1f budget_s=%g "
        "pid=%s scope=%s%s",
        outcome,
        admission.waited_s * 1000.0,
        bound_s,
        pid,
        unit,
        extra,
    )
