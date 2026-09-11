"""Client side of the out-of-process symlink resolver (see ``pathres_helper``).

Owns ONE long-lived helper process per gateway. A request is one pipe
round-trip made from an ``mc-pathres`` pool worker, so the worker thread
re-acquires the GIL twice per request (after the write, after the read)
instead of once per path component. The pool, the resolve budget, the
per-prefix cooldown and the ``/proc`` stall classifier in ``paths.py`` keep their
roles; the classifier now samples the HELPER first (``blocked_in_filesystem``,
below) and falls back to the worker thread, and this module changes what the
worker does with its budget.

Properties the gate leans on:

* **The helper's code is fixed at gateway boot.** The helper source is read ONCE
  when this module imports (before any agent turn) and the child runs it as
  ``python -I -c <source>``; the file on disk is never re-read for a spawn. The
  helper respawns on demand after a kill, so if it ran BY PATH an agent able to
  write ``pathres_helper.py`` (an editable install is exactly a working tree the
  agent edits) would get its code running with the gateway's privileges outside
  the sandbox on the next path check, with no restart. Captured source puts an
  edit on the same footing as every other product file: it takes effect at the
  next gateway restart, and a restart is the operator's action.
* **A wedged helper is killed, not waited for.** ``abort()`` SIGKILLs the child
  and detaches; the worker blocked in ``readline`` then reads EOF and returns, so
  the pool worker is FREED. A timed-out thread doing ``realpath`` itself could
  never be reclaimed.
* **The helper's syscall is the truthful stall signal, attributed to its
  requester.** The child is single-threaded and holds no GIL, so
  ``/proc/<pid>/syscall`` reads ``lstat``/``readlink`` exactly when it is blocked
  on the filesystem and ``running`` or a pipe ``read`` when it is not.
  ``blocked_in_filesystem`` answers only the worker whose request is in flight:
  a worker queued behind a wedged request must not read the OTHER request's
  stall as its own and charge its own healthy prefix.
* **In-process fallback latches on a helper that cannot RUN, never on one we
  killed, and re-probes after a cool-off.** Two shapes latch: ``Popen`` raising
  (no interpreter, a sandboxed install -- but also ``EAGAIN``/``ENOMEM`` on a
  loaded host), and a child that exits ON ITS OWN before answering its first
  request (an interpreter that cannot come up in the child environment -- the
  Windows failure mode without ``SystemRoot`` -- but also a child the OOM killer
  took). The latch cannot tell a permanent host fact from a transient one, and
  the transient ones cluster exactly when the gateway is loaded, which is when
  the helper matters; so a latch holds for ``_LATCH_COOLOFF_SECS`` and the next
  request after that spawns again. A permanent fault re-latches (one failed
  ``Popen`` a minute, logged at debug after the first warning); a transient one
  clears itself. An EOF the gateway caused itself (``abort()`` at the deadline)
  never latches: that helper was healthy and merely wedged on a mount, and the
  next request respawns. A crash AFTER a successful answer is a transport fault
  for that one request -- refused fail-closed by the caller -- and the next
  request respawns; it does not latch, because a helper that has answered has
  proven it can run.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from .pathres_helper import _resolve_one

logger = logging.getLogger(__name__)

#: How long an in-process latch holds before the next request spawns again. Long
#: enough that a host with no usable interpreter pays one failed ``Popen`` a
#: minute; short enough that a fork failure under load costs the helper's
#: protection for a minute, not for the life of the gateway.
_LATCH_COOLOFF_SECS = 60.0


def _inprocess(path: str) -> tuple[str | None, str | None]:
    """The helper's own resolution, run in this interpreter when no helper can."""
    real, resolved = _resolve_one(path)
    return real, resolved


#: The helper program, read ONCE at import (gateway boot) -- see the module
#: docstring for why it is captured rather than run by path.
_HELPER_SOURCE: str = Path(__file__).with_name("pathres_helper.py").read_text(encoding="utf-8")

#: The interpreter that runs it: the gateway's own, captured at import alongside the
#: source. It is the same trust root the OS sandbox launcher spawns itself with
#: (``sandbox.namespace_argv`` is ``[sys.executable, "-I", "-S", ...]``): an agent
#: that can replace this binary already owns the process that decides what the
#: agent may touch, so pinning it any harder here would protect nothing.
_INTERPRETER: str = sys.executable

#: Environment variables the child interpreter needs to START, by platform. The
#: helper itself reads no variables; this is what CPython needs to come up. On
#: Windows the interpreter cannot initialise without ``SystemRoot`` (it fails to
#: load its own DLLs and to seed the CRT), and a child that dies before its first
#: answer on every spawn is the crash loop the first-request latch below is for.
_CHILD_ENV_KEYS: tuple[str, ...] = (
    ("PATH", "SystemRoot", "SYSTEMROOT", "TEMP", "TMP") if os.name == "nt" else ("PATH",)
)


class ResolverHelper:
    """One helper process; requests are serialised through ``_request_lock``.

    Serialising is deliberate: two pool workers sharing one child would
    interleave lines. A second worker waits on the lock for the microseconds a
    healthy request takes; when the first worker's request is wedged, the
    caller's budget expires, ``abort()`` kills the child, the wedged worker
    unwinds, and the waiting worker gets a fresh child.
    """

    def __init__(self) -> None:
        self._proc: subprocess.Popen[bytes] | None = None
        self._spawn_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._seq = 0
        self._inflight_pid: int | None = None
        self._inflight_tid: int | None = None
        #: Native tid -> "queued" (waiting for the request lock), "spawning" (starting
        #: the child), "inflight" (written, waiting for the answer). Read by the stall
        #: classifier: only "inflight" can be a filesystem wait, so a budget that
        #: expires in the other two phases is the load arm (uncharged probe), never a
        #: mount stall charged to the path's prefix -- including on hosts with no
        #: ``/proc`` syscall table, whose thread sample cannot tell the phases apart.
        self._phase: dict[int, str] = {}
        #: Monotonic time until which requests resolve in-process because a helper
        #: could not run here; ``None`` when the helper is in use. Never set by a
        #: kill the gateway performed itself.
        self._inprocess_until: float | None = None
        self._latches = 0
        #: Whether the CURRENT child has answered at least one request. A child
        #: that dies before it has is a child that could not run here.
        self._answered_once = False
        #: Children this client killed on purpose (``abort``). An EOF from one of
        #: these is the gateway's doing and must not read as "cannot run".
        self._killed_pids: set[int] = set()
        atexit.register(self.close)

    # ── lifecycle ──────────────────────────────────────────────────────────

    def _spawn(self) -> subprocess.Popen[bytes] | None:
        """Start the helper, or return ``None`` (and remember) when it cannot run."""
        with self._spawn_lock:
            proc = self._proc
            if proc is not None and proc.poll() is None:
                return proc
            try:
                proc = subprocess.Popen(
                    # ``-I -S``: isolated and no ``site`` -- no user site, no PYTHON* env,
                    # no script dir on sys.path, and no ``.pth`` or ``sitecustomize``
                    # executed at startup (``-I`` alone still runs those); the helper
                    # imports only the stdlib, which needs none of it. The sandbox
                    # launcher spawns itself with the same two flags.
                    # ``-c``: the source captured at boot, never the file on disk.
                    [_INTERPRETER, "-I", "-S", "-c", _HELPER_SOURCE],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    # The interpreter's startup needs and nothing else: inheriting
                    # the gateway's environment would hand a resolver its credentials.
                    env={k: os.environ[k] for k in _CHILD_ENV_KEYS if k in os.environ},
                )
            except (OSError, ValueError) as exc:
                self._latch_inprocess(f"could not start ({exc})")
                return None
            self._proc = proc
            self._answered_once = False
            return proc

    def _latch_inprocess(self, why: str) -> None:
        self._inprocess_until = time.monotonic() + _LATCH_COOLOFF_SECS
        self._latches += 1
        # The first latch is news; a permanent fault re-latching every cool-off is not.
        logger.log(
            logging.WARNING if self._latches == 1 else logging.DEBUG,
            "sensitive-path resolver helper %s; resolving in-process for the next %.0f s, "
            "where a busy interpreter can make a healthy resolution exceed its budget",
            why,
            _LATCH_COOLOFF_SECS,
        )

    def _latched(self) -> bool:
        until = self._inprocess_until
        return until is not None and time.monotonic() < until

    def close(self) -> None:
        """Terminate the helper (interpreter exit, or a test tearing down)."""
        with self._spawn_lock:
            proc, self._proc = self._proc, None
        if proc is None:
            return
        self._killed_pids.add(proc.pid)
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=1.0)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def abort(self) -> None:
        """Kill the helper so a worker blocked on it unwinds; the next request respawns.

        Called by the resolve-budget owner when the helper was sampled blocked in a
        filesystem syscall at the deadline. Killing is what makes the pool worker
        reclaimable: its ``readline`` returns EOF instead of waiting on the mount.

        Kill and DETACH, never wait: the caller is ``_run_resolution_bounded`` on
        the event loop, and a ``wait`` there is the loop stall this whole module
        exists to prevent. The worker blocked on the pipe reads EOF and reaps the
        child (``_reap``) on its own thread; a child with no such worker is
        collected by the interpreter's SIGCHLD handling as a zombie that holds
        no pipe.
        """
        with self._spawn_lock:
            proc, self._proc = self._proc, None
        if proc is None:
            return
        self._killed_pids.add(proc.pid)
        try:
            proc.kill()
        except OSError:
            pass

    def abort_if_inflight(self, tid: int | None) -> bool:
        """``abort()`` only if the request in flight belongs to worker *tid*.

        For a deadline owner that knows which worker it timed out: if that worker
        is the one blocked in the helper, the helper is wedged on ITS path and the
        kill frees it; if the worker is merely queued on the lock behind another
        request, that other request has a deadline of its own, and killing a
        healthy helper here would only fault it. Returns whether a kill happened.
        """
        if tid is None or self._inflight_tid != tid:
            return False
        self.abort()
        return True

    # ── requests ───────────────────────────────────────────────────────────

    def resolve(self, path: str) -> tuple[str | None, str | None] | None:
        """``(realpath, resolve)`` for *path*, or ``None`` on a transport fault.

        Runs on an ``mc-pathres`` worker. The helper is chosen UNDER the request
        lock, never before it: a waiter that picked its handle while another
        request was in flight would hold exactly the child that request's deadline
        then killed, read EOF, and be answered with a fault it did not earn. Under
        the lock the waiter runs after the abort, sees the dropped handle, and gets
        a fresh child.

        An EOF is NOT retried. The common EOF is the deadline's own ``abort()``:
        this request wedged on a mount, its caller has already refused and moved
        on, and a retry would submit the SAME wedged path to a fresh helper and
        pin this worker again -- exactly what the kill was for. A crash costs this
        one request a fail-closed refusal (both callers turn ``None`` into
        ``PathResolutionStalled``); a crash BEFORE the child's first answer latches
        to in-process (see the module docstring); the next request respawns.
        """
        if self._latched():
            return _inprocess(path)
        answers = self._request(path, [path])
        return None if answers is None else answers[0]

    def resolve_many(self, paths: list[str]) -> list[tuple[str | None, str | None]] | None:
        """``resolve`` for several paths in ONE round-trip, in order.

        For the root anchors every gate call re-resolves (``_resolve_root_anchors``:
        ``$HOME`` plus the override roots): seven single requests per call were
        measured on Windows CI at ~80,000 round-trips for one test file, where each
        round-trip costs a scheduler tick, so the round-trips were the time. One
        request carries them all. Same contract as ``resolve``: ``None`` is a
        transport fault for the whole batch.
        """
        if not paths:
            return []
        if self._latched():
            return [_inprocess(p) for p in paths]
        return self._request(paths, paths)

    def _request(
        self, payload: str | list[str], paths: list[str]
    ) -> list[tuple[str | None, str | None]] | None:
        tid = threading.get_native_id()
        self._phase[tid] = "queued"
        try:
            with self._request_lock:
                return self._request_locked(payload, paths, tid)
        finally:
            self._phase.pop(tid, None)

    def _request_locked(
        self, payload: str | list[str], paths: list[str], tid: int
    ) -> list[tuple[str | None, str | None]] | None:
        self._phase[tid] = "spawning"
        proc = self._spawn()
        if proc is None:
            return [_inprocess(p) for p in paths]
        assert proc.stdin is not None and proc.stdout is not None
        self._seq += 1
        seq = self._seq
        self._inflight_pid = proc.pid
        self._inflight_tid = tid
        self._phase[tid] = "inflight"
        try:
            line = json.dumps({"i": seq, "p": payload}, ensure_ascii=True) + "\n"
            proc.stdin.write(line.encode("ascii"))
            proc.stdin.flush()
            raw = proc.stdout.readline()
        except (OSError, ValueError):
            raw = b""
        finally:
            self._inflight_pid = None
            self._inflight_tid = None
        if not raw:
            # EOF. Decide what it MEANS before reaping (``_reap`` forgets the pid):
            # a kill we performed is a wedge, not a defect; a child that died
            # unbidden before ever answering could not run here, so the host
            # latches to in-process. Reaping happens here, off the loop, on this
            # worker thread.
            killed_by_us = proc.pid in self._killed_pids
            self._reap(proc)
            if not killed_by_us and not self._answered_once:
                self._latch_inprocess("exited before answering its first request")
                return [_inprocess(p) for p in paths]
            return None
        try:
            reply = json.loads(raw.decode("ascii"))
            if reply["i"] != seq:
                raise ValueError("out-of-order reply")
            pairs = [reply["r"]] if isinstance(payload, str) else reply["r"]
            if len(pairs) != len(paths):
                raise ValueError("reply length")
            answers: list[tuple[str | None, str | None]] = []
            for real, resolved in pairs:
                if (real is not None and not isinstance(real, str)) or (
                    resolved is not None and not isinstance(resolved, str)
                ):
                    raise ValueError("non-string spelling")
                answers.append((real, resolved))
        except (ValueError, KeyError, TypeError, UnicodeDecodeError):
            # A helper that answers the wrong shape cannot be trusted with the
            # next question either.
            self._reap(proc)
            return None
        self._answered_once = True
        self._inprocess_until = None  # a helper that answers has proven it can run
        return answers

    def _reap(self, proc: subprocess.Popen[bytes]) -> None:
        with self._spawn_lock:
            if self._proc is proc:
                self._proc = None
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=1.0)
        except (OSError, subprocess.TimeoutExpired):
            pass
        self._killed_pids.discard(proc.pid)

    # ── stall classification ───────────────────────────────────────────────

    def worker_phase(self, tid: int | None) -> str | None:
        """Where worker *tid*'s request is: ``"queued"``, ``"spawning"``, ``"inflight"``,
        or ``None`` when it has no request here (in-process fallback, a test stub).

        Only ``"inflight"`` can be a filesystem wait. A budget that expires while the
        worker is still queued behind another request, or still starting the child
        (a cold ``python -I -S -c`` spawn on a loaded host), is the load arm: the
        path's prefix must not be charged with a mount stall it never reached, and
        that holds on hosts whose thread sample cannot say so (no ``/proc``).
        """
        if tid is None:
            return None
        return self._phase.get(tid)

    def blocked_in_filesystem(self, fs_syscalls: frozenset[int], tid: int | None) -> bool | None:
        """Whether the helper answering *tid*'s request sits in a filesystem syscall.

        *fs_syscalls* is the caller's per-architecture table of blocking syscall
        numbers (``paths._FS_BLOCKING_SYSCALLS``), so the helper sample and the
        thread sample cannot disagree about what "blocked in the filesystem" means.
        *tid* is the native id of the pool worker whose resolution timed out.

        The sample is attributed, not global: requests serialise through one
        helper, so a worker queued on ``_request_lock`` behind a wedged request
        can have ITS budget expire while the helper is busy with the OTHER
        request. Sampling the helper then would charge the queued worker's own,
        healthy prefix with the other mount's stall. So the answer is only given
        when the in-flight request belongs to *tid*; otherwise ``None``, and the
        caller falls back to its own thread's sample (which for a worker parked
        on a lock reads as not blocked in the filesystem -- the load arm).

        ``None`` also when there is no in-flight request (in-process fallback, a
        test stub), when the table is empty (unmapped architecture) or when
        ``/proc`` cannot be read. Otherwise a definite answer: the child is
        single-threaded, so the syscall it reports IS what it is doing.
        """
        pid = self._inflight_pid
        if pid is None or not fs_syscalls or tid is None or self._inflight_tid != tid:
            return None
        try:
            with open(f"/proc/{pid}/syscall", "rb") as fh:
                head = fh.read().split()
        except OSError:
            return None
        if not head:
            return None
        sampled = head[0]
        if sampled == b"running":
            return False
        try:
            return int(sampled) in fs_syscalls
        except ValueError:
            return None


_helper: ResolverHelper | None = None
_helper_lock = threading.Lock()


def helper() -> ResolverHelper:
    """The process-wide helper client, created on first use."""
    global _helper
    if _helper is None:
        with _helper_lock:
            if _helper is None:
                _helper = ResolverHelper()
    return _helper
