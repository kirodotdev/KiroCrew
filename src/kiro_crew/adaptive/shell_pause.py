"""Pause agent shell command groups under severe memory pressure, and resume them.

The spawn gate's pause only refuses NEW starts. Work that is already running keeps
growing: on a 184 GiB host with no swap, ~40 pytest-xdist workers kept allocating
after the gate paused, the gateway loop stalled, and the kernel OOM killer fired
eight times. Stopping the growth needs the running work itself to stop.

What is paused is exactly an agent's SHELL COMMAND GROUP: a session leader
(``pid == pgid == sid``) found in a session runtime's process tree, other than the
runtime's own session. On Linux that is what the kiro-cli shell tool starts (each
command is ``setsid``'d under the runtime's tool process). It is never:

* the gateway, its group or its session;
* a session runtime (the sandbox launcher, ``aim``, ``kiro-cli``): they share the
  runtime's session, which is excluded by construction;
* kiro-cli's own helper groups (the KAS ``node`` process and the MCP launchers it
  runs): they are process groups INSIDE the runtime's session, not session leaders;
* the MCP daemon and its backends: they are not in any runtime's tree.

Pausing frees no memory (there is no swap to page out to), so it only stops
growth, and nothing is ever killed. The one actuator is ``SIGSTOP`` / ``SIGCONT``
on EVERY process group in the shell's session: a command can start its own (GNU
``timeout`` does).

A group left stopped forever is the failure this module must never cause, so:

* every group -- a late subgroup too -- is written to the state file BEFORE its
  ``SIGSTOP``, and one that cannot be written is left running;
* a record is dropped only after a ``SIGCONT`` that succeeded, or once its process
  is confirmed gone; a ``SIGCONT`` that fails keeps the record, logs it, and the
  next resume retries it;
* :meth:`ShellPauser.resume_all` runs on controller stop, on a disable, and from
  ``atexit``; a gateway that died without any of those (SIGKILL, OOM) resumes what
  the file lists on its next start (:meth:`ShellPauser.recover`, run whether or not
  adaptive concurrency is enabled).

Linux only: discovery reads ``/proc``. Elsewhere :func:`discover_shell_groups`
finds nothing and the pauser does nothing.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable, Optional

logger = logging.getLogger(__name__)

PROC_ROOT = "/proc"

#: Windows has neither signal. Production never sends them there (no
#: ``os.killpg``), but an injected ``killpg`` in a test still needs a value.
_SIGSTOP = int(getattr(signal, "SIGSTOP", 19))
_SIGCONT = int(getattr(signal, "SIGCONT", 18))


@dataclass(frozen=True)
class ShellGroup:
    """One agent shell command group. ``started`` is the leader's start tick."""

    pgid: int
    session: str
    started: float


#: A process group and its leader's start tick (``-1`` when its leader had
#: already exited). Every signal checks the tick first, so a pgid that has been
#: reused by an unrelated process since is never signalled.
_GroupId = tuple[int, float]


@dataclass(frozen=True)
class _Paused:
    group: ShellGroup
    #: Every process group in the leader's session at pause time, the leader's
    #: first. ``SIGSTOP`` goes to each: a command can start its own groups.
    pgids: tuple[_GroupId, ...] = ()
    #: Monotonic time of the pause, for the starvation bound.
    paused_at: float = 0.0


def iter_descendants(proc_root: str, pid: int) -> list[int]:
    """``[pid, *descendants]`` from ``/proc/<p>/task/<tid>/children``.

    Local rather than imported: the ACP layer's walk is off limits outside the
    agent SDK (``scripts/check_agent_sdk_boundary.py``), and this module already
    reads ``/proc`` itself. An unreadable branch is skipped, not fatal.
    """
    order: list[int] = []
    seen: set[int] = set()
    stack = [pid]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        order.append(current)
        try:
            tids = os.listdir(f"{proc_root}/{current}/task")
        except OSError:
            continue
        for tid in tids:
            try:
                with open(f"{proc_root}/{current}/task/{tid}/children", encoding="ascii") as fh:
                    tokens = fh.read().split()
            except OSError:
                continue
            stack.extend(int(tok) for tok in tokens if tok.isdigit() and int(tok) not in seen)
    return order


def _stat(proc_root: str, pid: int) -> Optional[tuple[int, int, float]]:
    """``(pgrp, session, starttime)`` from ``/proc/<pid>/stat``, or None."""
    try:
        with open(f"{proc_root}/{pid}/stat", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except OSError:
        return None
    rparen = raw.rfind(")")
    fields = raw[rparen + 1 :].split() if rparen >= 0 else []
    try:
        return int(fields[2]), int(fields[3]), float(fields[19])
    except (IndexError, ValueError):
        return None


def discover_shell_groups(
    runtimes: Iterable[tuple[int, str]],
    *,
    proc_root: str = PROC_ROOT,
    gateway_pid: Optional[int] = None,
) -> list[ShellGroup]:
    """Every agent shell command group under *runtimes*, newest first.

    *runtimes* is ``(runtime pid, session key)`` pairs. *gateway_pid* defaults to
    this process; its group and session are never returned, and a runtime row that
    names it is skipped.
    """
    me = os.getpid() if gateway_pid is None else gateway_pid
    mine = _stat(proc_root, me) if me > 0 else None
    forbidden = {me}
    if mine is not None:
        forbidden.update(mine[:2])
    found: dict[int, ShellGroup] = {}
    for pid, key in runtimes:
        if pid <= 0 or pid in forbidden:
            continue
        own = _stat(proc_root, pid)
        if own is None:
            continue
        runtime_session = own[1]
        for child in iter_descendants(proc_root, pid)[1:]:
            st = _stat(proc_root, child)
            if st is None:
                continue
            pgrp, session, started = st
            if child == pgrp == session and session != runtime_session:
                if child not in forbidden and child not in found:
                    found[child] = ShellGroup(child, str(key), started)
    return sorted(found.values(), key=lambda g: g.started, reverse=True)


def _session_members(proc_root: str, leader: int) -> dict[int, int]:
    """``{pid: pgrp}`` for every live process whose session id is *leader*.

    Found by scanning ``/proc`` for the session id, not by walking the leader's
    children: a member whose parent died, or that a reaper inside the session
    adopted, is still in the session, and so is every member after the leader
    itself has exited. The session id is a fact about the processes: it cannot be
    reused while any process is still in that session, so everything found here
    belongs to the paused session -- as long as the leader's pid has not been
    reused (callers check that first). The group comes from the SAME ``stat`` read
    that checked the session, so a pid reused between two reads cannot bring in an
    unrelated group.
    """
    members: dict[int, int] = {}
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return members
    for name in entries:
        if not name.isdigit():
            continue
        st = _stat(proc_root, int(name))
        if st is not None and st[1] == leader:
            members[int(name)] = st[0]
    return members


def _session_pgids(proc_root: str, leader: int) -> list[int]:
    """Every process group in *leader*'s session under it, the leader's own first."""
    pgids = set(_session_members(proc_root, leader).values()) | {leader}
    return [leader, *sorted(pgids - {leader})]


def _subgroup_id(proc_root: str, pgid: int, session: int) -> _GroupId:
    """*pgid* with its leader's start tick, or ``-1`` when the pid that holds that
    number now is not that group's leader in *session* (or has exited)."""
    st = _stat(proc_root, pgid)
    if st is not None and st[0] == pgid and st[1] == session:
        return (pgid, st[2])
    return (pgid, -1.0)


def _leader_state(proc_root: str, group: ShellGroup) -> str:
    """``"same"``, ``"gone"`` or ``"replaced"`` for a paused group's leader pid."""
    st = _stat(proc_root, group.pgid)
    if st is None:
        return "gone"
    return "same" if st[2] == group.started else "replaced"


def _may_signal(proc_root: str, gid: _GroupId, *, fresh: bool) -> bool:
    """Whether *gid* is provably the group that was paused, or nothing at all.

    * Its leader's start tick was recorded: no process holds the number now (a
      live pgid is never handed to a new process, so a group whose leader exited
      keeps its id), or the process that does has that tick.
    * No tick (its leader had already exited when it was read): only when *fresh*,
      i.e. read from the shell session's live members a moment ago. A tickless id
      kept from an earlier read proves nothing -- the group can be gone and the
      number reused, even inside the same session -- so it is never signalled.
    """
    pgid, started = gid
    if started < 0:
        return fresh
    st = _stat(proc_root, pgid)
    return st is None or st[2] == started


#: Outcome of one signal: it landed, the group is confirmed gone, or it failed
#: and must be retried.
_OK, _GONE, _FAIL = "ok", "gone", "fail"


class ShellPauser:
    """Holds the set of shell groups this gateway has paused. Thread-safe.

    ALL pause and resume state lives here, in one list of paused groups in pause
    order, each with its pause time and the start ticks that identify it. The
    policy only says which way to move; it keeps no shell state of its own.

    Blocking (``/proc`` walks, file writes): call it off the event loop.
    """

    def __init__(
        self,
        state_path: Optional[Path],
        *,
        proc_root: str = PROC_ROOT,
        gateway_pid: Optional[int] = None,
        killpg: Optional[Callable[[int, int], None]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._state_path = state_path
        self._proc_root = proc_root
        self._gateway_pid = gateway_pid
        self._killpg = killpg if killpg is not None else getattr(os, "killpg", None)
        self._clock = clock
        self._lock = threading.Lock()
        #: Paused groups in pause order: the first was paused longest ago.
        self._paused: list[_Paused] = []
        #: Read-only copy for loop-side readers, replaced (never mutated) under the
        #: lock, so ``paused_count`` / ``paused`` never wait on a slow pause.
        self._snapshot: tuple[dict[str, object], ...] = ()
        #: False after a closing ``resume_all``: a pause still in flight on a worker
        #: thread (one that outlived a cancelled coroutine, or a decision taken
        #: before a disable) then pauses nothing.
        self._accepting = True

    @property
    def supported(self) -> bool:
        # No state file means no crash record, so no pause at all.
        return (
            self._state_path is not None
            and self._killpg is not None
            and os.path.isdir(self._proc_root)
        )

    @property
    def paused_count(self) -> int:
        return len(self._snapshot)

    def paused(self) -> list[dict[str, object]]:
        return [dict(row) for row in self._snapshot]

    def reopen(self) -> None:
        """Accept pauses again after a closing :meth:`resume_all` (a re-enable)."""
        with self._lock:
            self._accepting = True

    def pause(self, runtimes: Iterable[tuple[int, str]], *, everything: bool) -> list[ShellGroup]:
        """Pause running shell groups, newest first: half of them (at least one),
        or all of them when *everything*. Returns the groups paused."""
        if not self.supported:
            return []
        groups = discover_shell_groups(
            runtimes, proc_root=self._proc_root, gateway_pid=self._gateway_pid
        )
        with self._lock:
            if not self._accepting:
                return []
            self._retire_replaced_locked()
            # Identity, not the bare number: a pgid reused by a new shell is a
            # different group and is not exempt because an old record has it.
            held = {(p.group.pgid, p.group.started) for p in self._paused}
            running = [g for g in groups if (g.pgid, g.started) not in held]
            want = len(running) if everything else (len(running) + 1) // 2
            done: list[ShellGroup] = []
            for group in running[:want]:
                if _leader_state(self._proc_root, group) != "same":
                    continue  # exited or replaced since discovery: never adopt it
                if self._pause_one_locked(group):
                    done.append(group)
                    logger.warning(
                        "adaptive memory guard: paused shell group pid=%d session=%s",
                        group.pgid,
                        group.session,
                    )
            return done

    def _pause_one_locked(self, group: ShellGroup) -> bool:
        """Record, then stop, every group in *group*'s session. True if it stopped.

        The leader's identity is the one DISCOVERY read, never a re-read: if the pid
        has been reused since, the stop refuses it. Each group is on disk BEFORE its
        ``SIGSTOP``; one that cannot be written is left running.
        """
        entry = _Paused(group, ((group.pgid, group.started),), self._clock())
        self._paused.append(entry)
        stopped = False
        known: set[int] = set()
        # The first pass covers the session as discovered; later passes catch a
        # group a member created between the scan and the stop (GNU ``timeout``
        # calls ``setpgid`` right after it starts). A stopped process creates no
        # new group, so a few passes reach a fixed set.
        for _ in range(4):
            if not _leader_state(self._proc_root, group) == "same":
                break
            fresh = [
                g if g[0] != group.pgid else (group.pgid, group.started)
                for g in (
                    _subgroup_id(self._proc_root, pgid, group.pgid)
                    for pgid in _session_pgids(self._proc_root, group.pgid)
                )
                if g[0] not in known
            ]
            if not fresh:
                break
            for gid in fresh:
                known.add(gid[0])
                if gid not in entry.pgids:
                    entry = replace(entry, pgids=entry.pgids + (gid,))
                    self._paused[-1] = entry
                if not self._save_locked():
                    # No record on disk is no record: this group stays running.
                    entry = replace(entry, pgids=tuple(g for g in entry.pgids if g != gid))
                    self._paused[-1] = entry
                    continue
                # Read from the live session a moment ago: a tickless id is fresh.
                if self._signal(entry, gid, _SIGSTOP, fresh=True) == _OK:
                    stopped = True
        if not stopped:
            self._paused.pop()
            self._save_locked()
        return stopped

    def resume_one(self, *, older_than: float = 0.0) -> Optional[ShellGroup]:
        """Resume the group paused longest ago. None when there is none to resume.

        With *older_than* (seconds), only if that group has been paused at least
        that long: the starvation bound, applied while memory is still under the
        pause line. A group resumed that way that grows memory back under the line
        is simply paused again by the next sample, at the back of the order.

        A record whose groups are all confirmed gone costs no sample: it is dropped
        and the next one is tried. One whose ``SIGCONT`` fails stays where it is,
        so the next resume retries it.
        """
        with self._lock:
            while self._paused:
                entry = self._paused[0]
                if older_than > 0 and self._clock() - entry.paused_at < older_than:
                    return None
                done, reached = self._cont(entry)
                if not done:
                    logger.warning(
                        "adaptive memory guard: could not resume shell group pid=%d "
                        "session=%s; keeping it to retry",
                        entry.group.pgid,
                        entry.group.session,
                    )
                    return None
                self._paused.pop(0)
                self._save_locked()
                if reached:
                    logger.warning(
                        "adaptive memory guard: resumed shell group pid=%d session=%s%s",
                        entry.group.pgid,
                        entry.group.session,
                        " (paused too long)" if older_than > 0 else "",
                    )
                    return entry.group
            self._save_locked()
            return None

    def resume_all(self, why: str = "", *, close: bool = False) -> int:
        """Resume every paused group; return how many were reached. Never raises.

        Safe from ``atexit``. A group whose ``SIGCONT`` fails stays recorded, on
        disk too, so the next resume -- or the next gateway's recovery -- retries
        it. *close* also refuses every later :meth:`pause` until :meth:`reopen`, so
        a pause already running on a worker thread cannot land after a stop or a
        disable. Blocking: the controller calls it off the loop.
        """
        count = 0
        try:
            with self._lock:
                if close:
                    self._accepting = False
                kept: list[_Paused] = []
                for entry in reversed(self._paused):
                    done, reached = self._cont(entry)
                    if not done:
                        kept.append(entry)
                        logger.warning(
                            "adaptive memory guard: could not resume shell group pid=%d "
                            "session=%s; keeping it to retry",
                            entry.group.pgid,
                            entry.group.session,
                        )
                        continue
                    if reached:
                        count += 1
                        logger.warning(
                            "adaptive memory guard: resumed shell group pid=%d session=%s (%s)",
                            entry.group.pgid,
                            entry.group.session,
                            why or "resume all",
                        )
                self._paused = list(reversed(kept))
                self._save_locked()
        except Exception:
            logger.warning("adaptive memory guard: resume_all failed", exc_info=True)
        return count

    def recover(self) -> int:
        """Resume what a previous gateway left paused, from the state file.

        Every recorded group is checked against its start tick first, so a pgid
        reused by an unrelated process since (after a reboot, say) is left alone.
        A group whose ``SIGCONT`` fails stays in the file, and in this pauser, to be
        retried.
        """
        path = self._state_path
        if path is None or self._killpg is None:
            return 0
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return 0
        except (OSError, ValueError):
            logger.warning("adaptive memory guard: unreadable state file %s", path)
            return 0
        count = 0
        with self._lock:
            for row in rows if isinstance(rows, list) else []:
                entry = _entry_from_row(row)
                if entry is None:
                    continue
                done, reached = self._cont(entry)
                if not done:
                    self._paused.append(entry)
                    logger.warning(
                        "adaptive memory guard: could not resume shell group pid=%d "
                        "session=%s left paused by a previous gateway; keeping it",
                        entry.group.pgid,
                        entry.group.session,
                    )
                    continue
                if reached:
                    count += 1
                    logger.warning(
                        "adaptive memory guard: resumed shell group pid=%d session=%s "
                        "(left paused by a previous gateway)",
                        entry.group.pgid,
                        entry.group.session,
                    )
            self._save_locked()
        return count

    # -- signals -------------------------------------------------------------

    def _signal(self, entry: _Paused, gid: _GroupId, sig: int, *, fresh: bool) -> str:
        """Send *sig* to one of *entry*'s groups if it is provably that group.

        ``_GONE`` when the group is confirmed gone (no such group, or its id now
        belongs to another process); ``_FAIL`` when the signal could not be sent.
        """
        if self._killpg is None:
            return _FAIL
        if not _may_signal(self._proc_root, gid, fresh=fresh):
            return _GONE
        try:
            self._killpg(gid[0], sig)
        except ProcessLookupError:
            return _GONE
        except OSError:
            logger.warning(
                "adaptive memory guard: signal %d to pgid=%d session=%s failed",
                sig,
                gid[0],
                entry.group.session,
                exc_info=True,
            )
            return _FAIL
        return _OK

    def _retire_replaced_locked(self) -> None:
        """Drop records whose leader pid now belongs to another process.

        A pid that is reused means its session is gone entirely (the kernel never
        reuses a live session or group id), so there is nothing left to continue.
        """
        kept = [p for p in self._paused if _leader_state(self._proc_root, p.group) != "replaced"]
        if len(kept) != len(self._paused):
            self._paused = kept
            self._save_locked()

    def _cont(self, entry: _Paused) -> tuple[bool, bool]:
        """Continue *entry*. Returns ``(done, reached)``.

        *done* is False when any ``SIGCONT`` failed: the record must be kept and
        retried. *reached* is True when at least one group got its ``SIGCONT``.
        The recorded groups are continued, plus every group the session holds NOW,
        found by session id (``_session_members``) -- which works after the leader
        exited too, so a tickless subgroup is continued from the live session,
        never from the record's bare number.
        """
        if _leader_state(self._proc_root, entry.group) == "replaced":
            # The pid is someone else's, so the session id was free: no member of
            # the paused session is left to continue.
            return True, False
        recorded = {gid[0]: gid for gid in entry.pgids}
        recorded.setdefault(entry.group.pgid, (entry.group.pgid, entry.group.started))
        live = set(_session_pgids(self._proc_root, entry.group.pgid))
        outcomes = []
        for pgid in sorted(set(recorded) | live):
            if pgid in live:
                gid = _subgroup_id(self._proc_root, pgid, entry.group.pgid)
                if pgid in recorded and recorded[pgid][1] >= 0:
                    gid = recorded[pgid]
                outcomes.append(self._signal(entry, gid, _SIGCONT, fresh=True))
            else:
                outcomes.append(self._signal(entry, recorded[pgid], _SIGCONT, fresh=False))
        return _FAIL not in outcomes, _OK in outcomes

    def _refresh_snapshot_locked(self) -> None:
        self._snapshot = tuple(
            {"pgid": p.group.pgid, "session": p.group.session} for p in self._paused
        )

    def _save_locked(self) -> bool:
        """Write the paused set; True when it is on disk (or there is no file)."""
        self._refresh_snapshot_locked()
        path = self._state_path
        if path is None:
            return True
        rows = [
            {
                "pgid": p.group.pgid,
                "started": p.group.started,
                "session": p.group.session,
                "pgids": [list(gid) for gid in p.pgids],
            }
            for p in self._paused
        ]
        try:
            if not rows:
                path.unlink(missing_ok=True)
                return True
            from kiro_crew.atomic_write import atomic_write
            from kiro_crew.owner_only_files import mkdirs_owner_only

            mkdirs_owner_only(path.parent)
            atomic_write(path, json.dumps(rows), mode=0o600)
        except OSError:
            logger.warning("adaptive memory guard: could not write %s", path, exc_info=True)
            return False
        return True


def _entry_from_row(row: object) -> Optional[_Paused]:
    """A state-file row back as a paused entry, or None when it is malformed."""
    if not isinstance(row, dict):
        return None
    try:
        pgid = int(row["pgid"])
        started = float(row.get("started", -1.0))
    except (KeyError, TypeError, ValueError):
        return None
    if pgid <= 1:
        return None
    gids: list[_GroupId] = []
    raw = row.get("pgids")
    for item in raw if isinstance(raw, list) else []:
        if (
            isinstance(item, list)
            and len(item) == 2
            and isinstance(item[0], int)
            and item[0] > 1
            and isinstance(item[1], (int, float))
        ):
            gids.append((item[0], float(item[1])))
    group = ShellGroup(pgid, str(row.get("session", "")), started)
    return _Paused(group, tuple(gids))


def recover_paused_shells(state_path: Path) -> int:
    """Resume what a previous gateway left paused. For the gateway's start, whether
    or not adaptive concurrency is enabled; a no-op when there is no state file."""
    return ShellPauser(state_path).recover()


__all__ = [
    "ShellGroup",
    "ShellPauser",
    "discover_shell_groups",
    "recover_paused_shells",
]
