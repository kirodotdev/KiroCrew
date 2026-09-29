"""Run syscall-shaped work in a separate interpreter, behind an ``Executor`` face.

WHY THIS EXISTS.  A thread pool does not isolate work that is syscall-shaped but
PYTHON-paced.  ``os.path.realpath`` is pure Python (``posixpath._joinrealpath``)
walking one ``lstat``/``readlink`` pair per path component, and every one of those
pairs releases and reacquires the GIL.  On a gateway carrying ~157 threads the
handoffs, not the disk, dominate: the same resolution measured 0.022 ms idle and
4136 ms with 48 CPU-bound siblings, on a host whose filesystem answered every
underlying ``realpath`` in 14-44 microseconds.  Adding workers to the thread pool
adds GIL contenders, so the pool size knob cannot reach this.  A child interpreter
can: it pays ONE GIL handoff (the parent's single blocking read) instead of
"components x 2", which is why the cross-process column of that measurement is
flat at ~5.1 ms from 8 siblings to 48.

THE WAIT HAS TO BE THE CALLER'S OWN.  A child does not help by existing; it helps
because the thread that wants the answer is the thread blocked in the read.  Hand
the transport to a pool worker and the answer crosses two more thread boundaries --
worker pickup, then the caller's wake-up from a condition variable -- each waiting
up to one ``sys.getswitchinterval()`` (measured 5.0 ms) behind whatever else is
runnable.  Measured on this host against pure-Python spinners, median ms per
resolution of a six-component path:

    contenders   in-thread   this class   same child via a thread pool
             0       0.067        0.102                          0.112
             8     179.538       10.292                        202.201
            24     760.313       25.377                       1097.773
            48    3480.685       81.241                       2680.580

The third column is the shape a pooled ``Executor`` has, and at 48 contenders it is
past the resolver's 2000 ms budget: a pool around this child does not fix the bug.
That is why the ``Executor`` face here is deliberately lazy (see
:class:`_CallerThreadFuture`) rather than backed by workers.  The remaining
10-81 ms is understood and bounded, not mysterious: a deadline costs TWO GIL
reacquisitions, one for the ``select`` and one for the ``read`` it clears, which is
the ~10 ms floor at 5 ms a handoff, plus the lease and future locks on top.  It
stays 24x inside the budget, and buying that last handoff back would mean moving
the deadline onto the reaper thread -- which is itself unschedulable under exactly
the contention the deadline exists for.

WHAT IT IS.  ``concurrent.futures.Executor`` is the extension point the standard
library documents for exactly this: three methods (``submit``, ``map``,
``shutdown``) and callers that only ever hold a ``Future``.  Note that it is a
PLAIN base class, not an ``abc.ABC`` -- ``Executor.__abstractmethods__`` is
``None`` and its MRO is ``(Executor, object)``, so ``submit`` raises
``NotImplementedError`` rather than failing at construction.  Nothing enforces the
contract, so a subclass has to be deliberate about honouring it.

WHY NOT ``ProcessPoolExecutor``.  Two objections, and the structural one is the
one that decides it.

First, it cannot take this work at all: it pickles the callable BY REFERENCE, and
a callable defined inside another function has no importable name, so submitting
one raises ``AttributeError: Can't get local object``, measured.  The resolver's
pool is fed exactly such a local wrapper.

Second, and the reason a warm pool would not rescue it either: its results come
back to the caller through a PARENT-SIDE manager thread, which reads the result
queue and completes the ``Future``.  So the caller is woken by another Python
thread, which is precisely the two-boundary crossing measured at 2680 ms above.
The child being a process does not help when the handoff is in the parent.  (That
figure is this module's own thread-pool control, not ``ProcessPoolExecutor``
itself; the mechanism is the same by construction, but it is an inference from a
shared mechanism rather than a direct measurement of that class.)

The import cost is real but the WEAKEST of the three, because a pool amortises it
over a warm child: measured on this host, ``python -S -c pass`` 9.9 ms,
``import os, posixpath`` 10.8 ms (what this child pays), ``import kiro_crew``
56.3 ms, ``import kiro_crew.security.paths`` 135.4 ms (what a
``ProcessPoolExecutor`` child would pay, that being the module defining the
submitted wrapper).  That is 135 ms, not seconds, so cite the structural objection
above rather than this one.

The transport here is therefore a plain ``Popen`` of ``python -S <leaf script>``:
a fresh exec'd interpreter, no ``multiprocessing`` pickling, no manager thread,
no semaphores.

WHY NOT ``fork``.  A forked child inherits every lock in whatever state the other
threads left it, and this gateway runs ~157 threads.  ``Popen`` without
``preexec_fn`` runs NO Python between fork and exec -- CPython uses
``posix_spawn``/``vfork`` plus ``exec`` there -- so the child is a clean
interpreter, which is what ``spawn`` semantics buy without ``multiprocessing``'s
overhead.  Do not add ``preexec_fn`` to the launch: that is the switch that puts
Python back in the forked child.

ADOPTING IT FOR ANOTHER POOL.  Four things, and only the last two need judgement:
1. Write a leaf child script shaped like :mod:`kiro_crew.security._child_realpath`
   that imports STDLIB ONLY and answers one op code.  Never ``-m``; always run it
   by path, or ``kiro_crew/__init__.py`` executes and the cold-start budget is gone.
2. Give it a request/response body in that module's shape: 4-byte
   big-endian outer length, request id, op code, payload.  Length prefixes, never
   a newline -- a filename may contain one.
3. Reach it with :meth:`SubprocessPoolExecutor.call_op` from the thread that
   wants the answer.  If that thread instead waits on another thread's result, the
   win is gone -- measured 81 ms against 2681 ms above -- and it will look like
   the child is at fault.  Check the work is genuinely syscall-shaped first: this
   converts "blocks in the kernel" into "blocks in the kernel one process over",
   so it does nothing for CPU-bound Python, which needs its own interpreter or a
   released GIL, not a pipe.
4. Decide what the CALLER does when a child dies or a request outlives the
   ceiling.  This class raises :class:`SubprocessPoolUnavailable` and never returns a
   partial or empty answer, because for a security gate "no answer" must fail
   closed; a pool that silently degrades to "resolved nothing" would turn a dead
   child into an open door.
"""

from __future__ import annotations

import atexit
import logging
import os
import queue
import select
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# Mirrors ``_child_realpath``; pinned by ``test_protocol_round_trips``.
_LEN = struct.Struct(">I")
_REQ_HEADER = struct.Struct(">IB")

OP_REALPATH_SPELLINGS = 1
OP_REALPATH_MANY = 2

STATUS_OK = 0
STATUS_ERROR = 1

# The first consumer's child, kept beside the resolver it serves
# (``security/_child_realpath.py``); an adopter passes its own ``script=``.
_CHILD_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "security",
    "_child_realpath.py",
)


def _read_child_source(script: str) -> str:
    """The program text a child runs, read ONCE and handed to the interpreter as ``-c``.

    A child is spawned from these captured bytes, never from the file at spawn time.
    The child runs outside the agent sandbox (it must, to ``lstat`` the very paths the
    gate checks), and the package tree is writable, so respawning from disk would let
    an agent edit the script, kill a child it can see, and have the reaper execute the
    edit with credential-read access.  Reading at import (the default script, below,
    is read when this module loads, before any agent turn) or at executor construction
    (an adopter's ``script=``) closes that: a later edit to the file reaches no child
    of this process.  ``-c`` puts the text on the child's command line, which is fine:
    it is the module's own source, and no request data ever travels there.
    """
    with open(script, "rb") as fh:
        return fh.read().decode("utf-8")


# Captured when this module loads, so no later write to the file reaches a respawn.
_CHILD_SOURCE = _read_child_source(_CHILD_SCRIPT)

# How long one request may hold a child before the child is killed rather than
# waited on.  It must sit ABOVE every caller budget (the resolver's largest is the
# 8 s anchor rebuild) so that the caller's own timeout is what normally fires: the
# ceiling is for a child that will never answer, not for a slow one.  Killing is
# the capability a thread pool does not have -- a thread wedged in the kernel holds
# its slot until the mount answers, while a wedged child is a process and can be
# reclaimed.
DEFAULT_REQUEST_CEILING_SECS = 20.0

# How long ``shutdown`` waits for the reaper thread to finish its current tick
# before it kills the children.  One tick is a poll of every slot plus at most one
# ``Popen`` per slot (~11 ms each, more on a loaded host), so this is a ceiling
# against a wedged host rather than a wait anyone expects to spend.
_SHUTDOWN_REAPER_JOIN_SECS = 5.0


class SubprocessPoolUnavailable(RuntimeError):
    """The child could not answer: it died, was killed at the ceiling, or faulted.

    Callers MUST treat this as a failure to establish the answer, never as an
    empty answer.  For a sensitive-path gate that means refusing the path.
    """


class SubprocessPoolTimeout(TimeoutError):
    """A child took the request and had not answered by the caller's deadline.

    The child is already destroyed when this is raised, so the one thing a caller
    could still have asked it -- WHAT it was doing when the budget ran out -- is
    sampled here first and carried as ``child_syscall``: the first token of
    ``/proc/<pid>/syscall`` (a syscall number, or ``b"running"``), ``None`` where
    that file is unreadable (non-Linux, or the child was already gone).  The
    resolver uses it to tell "blocked in the filesystem, charge the mount" from
    "starved of the CPU, charge nothing", which the thread pool it replaced read
    off the worker thread and which has no referent once the work is a process.
    A plain ``TimeoutError`` from :meth:`SubprocessPoolExecutor.call_op` means the
    opposite thing: no child ever took the request.
    """

    def __init__(self, message: str, *, child_syscall: bytes | None) -> None:
        super().__init__(message)
        self.child_syscall = child_syscall


def proc_syscall(pid: int) -> bytes | None:
    """First token of ``/proc/<pid>/syscall``, or ``None`` where it cannot be read.

    A thread id works too: Linux exposes every task under ``/proc/<tid>``.
    """
    try:
        with open(f"/proc/{pid}/syscall", "rb") as fh:
            head = fh.read().split()
    except OSError:
        return None
    return head[0] if head else None


def pack_strings(values: Iterable[bytes]) -> bytes:
    """Length-prefixed encoding of a byte-string sequence (parent-side mirror)."""
    items = list(values)
    body = [_LEN.pack(len(items))]
    for value in items:
        body.append(_LEN.pack(len(value)))
        body.append(value)
    return b"".join(body)


def unpack_strings(payload: bytes) -> list[bytes]:
    """Inverse of :func:`pack_strings`; raises ``ValueError`` on a truncated frame."""
    if len(payload) < _LEN.size:
        raise ValueError("truncated count")
    (count,) = _LEN.unpack_from(payload, 0)
    offset = _LEN.size
    out: list[bytes] = []
    for _ in range(count):
        if len(payload) < offset + _LEN.size:
            raise ValueError("truncated length")
        (size,) = _LEN.unpack_from(payload, offset)
        offset += _LEN.size
        if len(payload) < offset + size:
            raise ValueError("truncated value")
        out.append(payload[offset : offset + size])
        offset += size
    return out


def _write_all(stream: Any, data: bytes) -> None:
    """Write every byte of *data*. Unbuffered pipe writes may be short.

    A short write on an unbuffered stream would truncate the length-prefixed frame
    and desynchronise the protocol, which is the one failure mode that could pair
    one path's answer with another path's question. Loop rather than trust one call.
    """
    view = memoryview(data)
    while view:
        written = stream.write(view)
        if not written:  # pragma: no cover - would mean a closed pipe without error
            raise OSError("child stdin accepted no bytes")
        view = view[written:]


class _Unreaped:
    """The pool's killed-but-not-yet-exited children, bounded.

    A child killed at a caller's deadline is presumed wedged in an uninterruptible
    syscall, where SIGKILL takes effect only when the syscall returns -- for a hung
    network mount, possibly never.  Two bounds meet here.  The slot must NOT wait for
    that exit before respawning, or one permanently wedged mount retires the slot for
    good and, prefix by prefix, the whole pool (found in review).  But it must not
    respawn without limit either, or the same mount grows a killed-but-alive child per
    deadline until PIDs run out (also found in review).  So ``capacity`` is a RESPAWN
    THRESHOLD: below it a slot respawns at once; at or above it a slot stays empty
    until one exits, and the executor reports exhaustion to callers rather than
    letting them spend their budget on an empty queue.  ``add`` itself never refuses
    -- the killed process exists whether or not it is counted -- so deadline arms
    that fire concurrently can carry the count past ``capacity`` by at most one per
    leased slot, i.e. by ``workers``.  Thread-safe: the deadline arm adds from the
    calling thread, the reaper collects.
    """

    __slots__ = ("_lock", "_procs", "capacity")

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self._procs: list[subprocess.Popen[bytes]] = []
        self._lock = threading.Lock()

    def add(self, proc: subprocess.Popen[bytes]) -> None:
        with self._lock:
            self._procs.append(proc)

    def collect(self) -> None:
        """Forget every child that has exited by now."""
        with self._lock:
            self._procs = [proc for proc in self._procs if proc.poll() is None]

    def full(self) -> bool:
        with self._lock:
            return len(self._procs) >= self.capacity

    def __len__(self) -> int:
        with self._lock:
            return len(self._procs)


class _Child:
    """One child interpreter, used strictly one request at a time.

    Serial by design.  A demultiplexing reader thread would buy concurrency the
    workload does not need (one resolution costs ~22 microseconds, so a single
    child sustains ~45k/s) and would add the one bug class this protocol cannot
    tolerate: a response matched to the wrong request would hand one path's
    resolved form to another path's security decision.
    """

    __slots__ = (
        "_lock",
        "_next_id",
        "_unreaped",
        "busy_since",
        "proc",
        "queued",
        "source",
        "spawn_error",
    )

    def __init__(self, source: str, unreaped: _Unreaped | None = None) -> None:
        # The program text every child of this slot runs (see ``_read_child_source``).
        self.source = source
        self.proc: subprocess.Popen[bytes] | None = None
        self.busy_since: float | None = None
        # Whether this slot is currently on the executor's ready queue (guarded by the
        # executor's queue lock), so a slot is never enqueued twice.
        self.queued = False
        # The ``OSError`` the last spawn attempt raised, ``None`` once one succeeds.
        self.spawn_error: OSError | None = None
        self._lock = threading.Lock()
        self._next_id = 0
        # A child killed at its deadline is handed to the executor's bounded
        # ``_Unreaped`` list for its REAPER to wait on, so the calling thread never
        # does: SIGKILL does not complete while the child is inside an uninterruptible
        # syscall, and a child killed at its deadline is presumed to be in exactly
        # such a wait on a wedged mount, so a ``wait`` here would burn its timeout on
        # the calling thread -- the event loop, for the resolver -- past the budget
        # the caller was promised.  A child outside an executor (``None``) waits
        # inline.  The list is what bounds the pool's process count: a slot respawns
        # while its killed child is still alive, so one permanently wedged mount
        # cannot retire the slot for good, but only while the list has room.
        self._unreaped = unreaped

    def ensure_spawned(self) -> bool:
        """Spawn a replacement for a dead or never-started child; True if one was spawned.

        The ONLY place a child is spawned.  Called from the executor's reaper tick, so
        the fork/exec of a fresh interpreter (~11 ms, more on a loaded host) is paid on
        that background thread and never by a caller inside its budget -- for the
        resolver the caller is the event loop, and a ``Popen`` there is a loop freeze
        per empty slot.  Non-blocking: a child mid-request holds the lock and is
        skipped.  A slot is left empty only while the pool's bounded list of
        killed-but-unexited children is full.  A spawn that raises
        leaves the slot empty and the ``OSError`` in ``spawn_error`` for the executor
        to report to its callers.
        """
        if not self._lock.acquire(blocking=False):
            return False
        try:
            proc = self.proc
            if proc is not None and proc.poll() is None:
                return False
            if proc is not None:
                self._kill_locked()
            if self._unreaped is not None and self._unreaped.full():
                # Every killed child the pool may hold is still alive: spawning would
                # take the process count past its bound, so the slot stays empty until
                # one of them exits.
                return False
            try:
                self.proc = self._spawn()
            except OSError as exc:
                # Cannot spawn here (a hardened host, a broken venv): the slot stays
                # empty and the executor reports the failure to its callers.
                self.spawn_error = exc
                return False
            self.spawn_error = None
            return True
        finally:
            self._lock.release()

    def _spawn(self) -> subprocess.Popen[bytes]:
        # ``-S`` skips site initialisation, which is most of an interpreter's
        # start-up cost and all of the part that could pull in installed packages.
        #
        # ``-I`` (isolated) is REQUIRED, not optional hardening. This child is
        # deliberately allowlisted to run OUTSIDE the agent sandbox, because its job
        # is to read the very paths the sensitive-path gate checks -- which makes it
        # a high-value target. Without ``-I`` the interpreter honours ``PYTHONPATH``,
        # so a writable directory ahead on ``sys.path`` could plant a module named
        # for one of the four stdlib imports below and get code execution in an
        # unsandboxed process that can read credential files. ``-I`` implies ``-E``
        # (no ``PYTHON*`` variables), ``-s`` (no user site) and ``-P`` (the script's
        # own directory is not on ``sys.path``), which together close that path.
        #
        # It costs nothing here: this child runs source text passed as ``-c`` (captured
        # once, see ``_read_child_source``) and imports stdlib only, so it needs no
        # ``sys.path`` entry of its own. And ``-I``
        # affects neither the working directory nor ``realpath`` semantics -- the
        # caller absolutizes before sending anyway -- so the answer is unchanged.
        #
        # stderr goes to DEVNULL because a traceback there would carry the
        # agent-supplied path into the gateway's logs, which is exactly what the
        # sensitive-path gate exists to prevent; the child reports failure in-band
        # as a status byte with a class name and no path.
        # ``bufsize=0`` is load-bearing, not a micro-optimisation: the read
        # deadline is armed with ``select`` on the pipe's fd, and a BufferedReader
        # would answer "not ready" for bytes already sitting in its own buffer.
        # Unbuffered streams make readiness on the fd mean readiness to the caller.
        # The child INHERITS the parent's working directory, and must. ``realpath``
        # resolves a relative path against the CWD, so pinning the child to the
        # package directory would make it answer a different question than the
        # in-process resolver it stands in for: a relative symlink into a credential
        # store would resolve to a non-existent path under the package and MISS the
        # sensitive-path match, which fails OPEN on a security gate. Callers also
        # absolutize before sending (see ``realpath_spellings``), so this is the
        # second of two defences, not the only one.
        return subprocess.Popen(
            [sys.executable, "-I", "-S", "-c", self.source],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            bufsize=0,
        )

    def _kill_locked(self, *, wait: bool = True) -> None:
        """Kill the current child and forget it.

        With *wait* (a child that died or faulted, so its exit is immediate) the exit is
        collected here.  Without it (the deadline arm) the killed process becomes the
        executor's ``_Unreaped`` list for its reaper to collect: it is presumed
        blocked in an uninterruptible syscall on a wedged mount, where SIGKILL takes
        effect only once the syscall returns, and waiting for that on the calling thread
        would extend the caller's bound by up to the wait timeout.
        """
        proc = self.proc
        self.proc = None
        if proc is None:
            return
        try:
            proc.kill()
        except OSError:
            pass
        else:
            if wait or self._unreaped is None:
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    logger.warning("subprocess-pool child %s ignored SIGKILL", proc.pid)
            else:
                self._unreaped.add(proc)
        for stream in (proc.stdin, proc.stdout):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass

    def kill_async(self) -> None:
        """SIGKILL the current child WITHOUT taking the request lock.

        The reaper must never take ``_lock``. A wedged request holds that lock for
        the whole of its blocking read, so a locked kill would wait on exactly the
        wedge it exists to clear, making the ceiling a no-op and leaving the test
        that asserts it to hang until the pytest timeout. Killing is safe without
        the lock because it MUTATES NOTHING here: it only signals the process, and
        the request thread that owns the lock then sees EOF and cleans up itself.
        """
        proc = self.proc
        if proc is None:
            return
        try:
            proc.kill()
        except OSError:
            pass

    def kill(self) -> None:
        """Reclaim this child; the next request spawns a replacement.

        Signals first, then cleans up under the lock only if no request holds it,
        so this is safe to call on a child that is mid-wedge.
        """
        self.kill_async()
        if self._lock.acquire(blocking=False):
            try:
                self._kill_locked()
            finally:
                self._lock.release()

    def request(self, op: int, payload: bytes, deadline: float | None = None) -> bytes:
        """Send one request, return its response payload, or raise on any fault.

        MUST be called on the thread that wants the answer. Handing this to a pool
        thread reintroduces the bug the class exists to remove: the caller then
        waits on a Python-level ``Future``, so the answer has to cross two extra
        GIL handoffs (worker pickup, caller wake-up) instead of only the ones its
        own blocking wait costs. Measured at 48 pure-Python contenders, 81 ms from
        the calling thread against 2681 ms through a thread pool -- see the module
        docstring's table.

        Never spawns: a child that died between requests is one refused resolution
        (:class:`SubprocessPoolUnavailable`) and the executor's reaper respawns the
        slot off this thread, so a crash costs one refusal and about eleven
        milliseconds of background work, not a degraded gateway.

        Raises ``TimeoutError`` if *deadline* passes with the child silent, having
        first killed it -- a child that missed a microsecond-scale budget is wedged
        in the kernel, and its pipe is not known to be in frame.
        """
        with self._lock:
            proc = self.proc
            if proc is None or proc.poll() is not None:
                # Only a slot with a LIVE child is ever leased; finding none means the
                # child died while the slot sat idle on the ready queue.  Never spawn
                # here: this is the caller's thread, the event loop for the resolver,
                # and a ``Popen`` on it is the freeze the reaper exists to keep off it.
                # The executor hands the slot back to the reaper on release.
                self._kill_locked()
                raise SubprocessPoolUnavailable("child died between requests")
            stdin, stdout = proc.stdin, proc.stdout
            if stdin is None or stdout is None:  # pragma: no cover - Popen contract
                self._kill_locked()
                raise SubprocessPoolUnavailable("child pipes unavailable")
            # 1..0xFFFFFFFF, never 0: the child answers 0 when it could not read the
            # header at all, so 0 must not be a real request id.
            self._next_id = (self._next_id % 0xFFFFFFFF) + 1
            request_id = self._next_id
            body = _REQ_HEADER.pack(request_id, op) + payload
            self.busy_since = time.monotonic()
            try:
                _write_all(stdin, _LEN.pack(len(body)) + body)
                header = _read_exactly(stdout, _LEN.size, deadline)
                if header is None:
                    raise SubprocessPoolUnavailable("child closed the connection")
                (length,) = _LEN.unpack(header)
                response = _read_exactly(stdout, length, deadline)
                if response is None:
                    raise SubprocessPoolUnavailable("child truncated its response")
                if len(response) < 5:
                    raise SubprocessPoolUnavailable("child response too short")
                (echoed,) = _LEN.unpack_from(response, 0)
                if echoed != request_id:
                    # Impossible while this child is serial, and fatal if it ever
                    # happens: a mismatched id means one path's answer could be
                    # attributed to another path. Discard rather than reason about it.
                    raise SubprocessPoolUnavailable("child response id mismatch")
            except _ChildTimeout:
                # The pipe is mid-frame and the child is presumed wedged, so it is
                # destroyed rather than reused -- but sampled FIRST, because what
                # it was blocked in is the only evidence the caller gets about
                # whether the mount or the scheduler ate the budget. Raising a
                # ``TimeoutError`` subclass keeps this substitutable for a pooled
                # future, whose ``result(timeout=)`` raises the same class.
                sampled = proc_syscall(proc.pid)
                self._kill_locked(wait=False)
                raise SubprocessPoolTimeout(
                    "child did not answer within the budget", child_syscall=sampled
                ) from None
            except SubprocessPoolUnavailable:
                # Includes the kill-at-ceiling path, which surfaces here as EOF.
                self._kill_locked()
                raise
            except OSError as exc:
                self._kill_locked()
                raise SubprocessPoolUnavailable(
                    f"child transport failed: {type(exc).__name__}"
                ) from exc
            finally:
                self.busy_since = None
            if response[4] != STATUS_OK:
                # The child is still healthy: it answered, the answer is a refusal.
                detail = response[5:].decode("ascii", "replace")
                raise SubprocessPoolUnavailable(f"child reported {detail or 'failure'}")
            return response[5:]


class _ChildTimeout(Exception):
    """Internal: the child had not answered by the caller's deadline."""


# Queued once by the reaper when no child can be started, so a waiting caller learns
# it now rather than at the end of its budget (see ``SubprocessPoolExecutor._lease``).
_SPAWN_FAILED = object()


class _ReaderLost(Exception):
    """Internal: a bounded read missed its deadline; the child is killed and the slot retired."""

    def __init__(self, proc: subprocess.Popen[bytes] | None, sampled: bytes | None) -> None:
        super().__init__("bounded reader did not return after its child was killed")
        self.proc = proc
        self.sampled = sampled


# ``select`` accepts only SOCKETS on Windows, so it cannot watch a pipe there and
# the per-request read deadline is a POSIX capability. Where it is unavailable the
# read blocks, so ``call_op`` runs it on an internal thread and bounds the CALLER
# with a timed future instead (:meth:`SubprocessPoolExecutor._request_bounded_by_thread`):
# the budget still fires at the caller's deadline, at the cost of the two extra GIL
# handoffs the module docstring measures. The ceiling reaper remains the bound for
# a request made with no deadline at all. Making the deadline exact on Windows
# means moving the transport onto a socket pair so it becomes selectable.
_CAN_SELECT_PIPES = os.name != "nt"


def _read_exactly(stream: Any, count: int, deadline: float | None) -> bytes | None:
    """Read exactly *count* bytes. ``None`` on EOF; :class:`_ChildTimeout` past *deadline*.

    A pipe read has no timeout of its own, so the deadline is armed with
    ``select``, which blocks IN THE KERNEL WITH THE GIL RELEASED and returns the
    moment this fd has data. That is the property the whole design rests on: the
    waiting thread costs one GIL reacquisition, not one per path component, and
    not a poll loop that would reacquire on every tick.
    """
    chunks: list[bytes] = []
    remaining = count
    fileno = stream.fileno()
    while remaining:
        if deadline is not None and _CAN_SELECT_PIPES:
            left = deadline - time.monotonic()
            if left <= 0:
                raise _ChildTimeout
            ready, _, _ = select.select([fileno], [], [], left)
            if not ready:
                raise _ChildTimeout
        chunk = stream.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class _CallerThreadFuture(Future[bytes]):
    """A ``Future`` that runs its work on the thread which calls ``result()``.

    The point of the indirection is that a pooled ``Future`` is the wrong shape for
    this workload. A pool worker computing the answer means the value crosses a
    thread boundary, and under pure-Python contention every crossing waits up to
    one ``sys.getswitchinterval()`` (measured 5 ms): worker pickup, then the
    caller's wake-up from the condition variable. Measured at 48 contenders with
    the same child, 81 ms this way against 2681 ms pooled -- and the pooled figure
    is past the resolver's 2000 ms budget, i.e. the pooled shape does not fix the
    bug it was added to fix. Doing the transport on the caller's own thread keeps
    the waiting inside the wait the caller was going to make anyway.

    Consequences a caller can observe, both deliberate:
    ``result(timeout=)`` raises ``TimeoutError`` exactly as a pooled future does,
    so the standard timeout arm needs no change. But nothing runs until ``result``
    is called, so ``cancel()`` before that always succeeds and truthfully reports
    that no work happened; after a ``result`` attempt it always returns ``False``.
    A caller that reads "cancel succeeded" as "the pool was saturated" is reading
    a pool-specific meaning into it and must be revisited.
    """

    __slots__ = ("_begun", "_begun_lock", "_run")

    def __init__(self, run: Callable[[float | None], bytes]) -> None:
        super().__init__()
        self._run = run
        self._begun = False
        self._begun_lock = threading.Lock()

    def result(self, timeout: float | None = None) -> bytes:  # type: ignore[override]
        with self._begun_lock:
            if self.done():
                return super().result(timeout=0)
            self._begun = True
            try:
                value = self._run(timeout)
            except BaseException as exc:
                # Recorded even for a timeout: that child was destroyed, so a
                # second result() would be a second request, not a resumed wait.
                self.set_exception(exc)
                raise
            self.set_result(value)
            return value

    def cancel(self) -> bool:
        if not self._begun_lock.acquire(blocking=False):
            return False  # another thread is inside result(): work has begun
        try:
            if self._begun:
                return False
            return super().cancel()
        finally:
            self._begun_lock.release()


class SubprocessPoolExecutor(Executor):
    """An ``Executor`` whose registered ops run in child interpreters.

    Named for what it provides, as a peer of ``ThreadPoolExecutor`` rather than a
    remedy for one. It is deliberately NOT stdlib's ``ProcessPoolExecutor``: that
    one pickles an arbitrary callable and makes the child import the module defining
    it, which is exactly what this refuses to do, because the child's whole value is
    starting in about eleven milliseconds without the gateway's dependency graph.
    Nor is it stdlib's ``InterpreterPoolExecutor``, which runs subinterpreters in
    this process and so would still share this GIL.

    ``submit`` keeps the standard contract, so an existing caller holding a
    ``Future`` needs no change.  A callable the executor does not recognise runs on
    an internal thread pool exactly as it does today -- the fallback is what makes
    this substitutable for a ``ThreadPoolExecutor`` without auditing every caller.
    Work for a child is reached through :meth:`submit_op`, which names the op
    explicitly instead of inferring it from the callable: inference by
    ``__qualname__`` is not usable, because the pool's callers submit a
    local wrapper whose own side effects (the thread id it records, the
    started/abandoned handshake it performs) are read by the caller's stall
    classifier, so substituting the wrapper's body silently changes how a timeout
    is classified.
    """

    def __init__(
        self,
        *,
        workers: int = 2,
        script: str = _CHILD_SCRIPT,
        ceiling_secs: float = DEFAULT_REQUEST_CEILING_SECS,
        thread_name_prefix: str = "mc-subproc",
    ) -> None:
        # Default TWO, minimum one. A second child buys no capacity -- one sustains
        # roughly 45k resolutions/s at ~22 microseconds a call -- so it is
        # redundancy, not throughput. What it buys is the property the caller's gate
        # already assumes: one stalled resolution still leaves a free child for
        # every other path. ``workers=1`` is permitted for an adopter that can
        # tolerate the exact consequence -- while its single child is wedged, every
        # request refuses until the ceiling reclaims it -- which for the resolver
        # would mean one bad mount refusing paths under every other prefix too.
        if workers < 1:
            raise ValueError("workers must be >= 1")
        self._ceiling = ceiling_secs
        # Program text captured NOW (or at import, for the default): a respawn never
        # re-reads the file, so an edit to it after this point reaches no child.
        self._source = _CHILD_SOURCE if script == _CHILD_SCRIPT else _read_child_source(script)
        # Killed-but-unexited children (:class:`_Unreaped`): respawn stops once
        # ``2 * workers`` of them are alive, and concurrent deadline arms can add at
        # most ``workers`` more, so the pool never holds more than ``4 * workers``
        # processes however many deadline kills a wedged mount causes before the
        # kernel lets them exit; a slot is retired only while that list is full,
        # never for good.
        self._unreaped = _Unreaped(capacity=2 * workers)
        self._children = [_Child(self._source, self._unreaped) for _ in range(workers)]
        # The READY queue: only a slot with a live child is on it, put there by the
        # reaper after it spawns (never by a caller), so a lease never spawns inline
        # -- ``call_op`` runs on the calling thread, which for the resolver is the
        # event loop, and a ``Popen`` there is a loop freeze per empty slot.  A slot
        # released without a live child goes back to the reaper instead.  The one
        # other thing the queue can carry is ``_SPAWN_FAILED``, put there once by the
        # reaper when no child can be started, so a waiting caller learns that at
        # once (as the ``OSError``) instead of spending its whole budget on an empty
        # queue.
        self._free: queue.Queue[_Child | object] = queue.Queue()
        self._qlock = threading.Lock()
        self._spawn_error: OSError | None = None
        self._failed_queued = False
        # No thread pool for child work: it runs on the calling thread. The one
        # pool left serves ``submit``/``map`` for callables this class does not
        # recognise, which is what keeps it substitutable for a ThreadPoolExecutor.
        self._fallback = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix=f"{thread_name_prefix}-inline"
        )
        # Where ``select`` cannot arm a deadline on the pipe (Windows) the read runs on
        # one of THESE threads, kept apart from ``_fallback`` so a reader left blocked
        # on a dead child's pipe cannot starve the callables ``submit`` carries.
        self._bounded_readers = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix=f"{thread_name_prefix}-read"
        )
        self._shutdown = threading.Event()
        # Set whenever a slot needs the reaper now (an empty slot released, start-up),
        # so the first request after construction waits on the spawn rather than on
        # the reaper's next 0.5 s tick.
        self._wake = threading.Event()
        self._wake.set()
        self._reaper = threading.Thread(
            target=self._reap, name=f"{thread_name_prefix}-reaper", daemon=True
        )
        self._reaper.start()
        atexit.register(self.shutdown, wait=False)

    def _reap(self) -> None:
        """Kill any child whose request outlived the ceiling; collect killed children;
        keep every slot spawned and every spawned slot on the ready queue.

        The ceiling is the whole reason the work moved to a process. The parent thread
        waiting on that child is blocked in ``read`` and cannot cancel itself, so
        something else has to break the wait; killing the child makes the read
        return EOF at once and turns an unbounded wedge into one refused request.

        The other duties keep the CALLING thread's cost to the round trip alone: a
        child killed at its deadline is ``wait``ed for here, not by the caller
        (``_Child._kill_locked``), and every empty slot -- never filled, or emptied by
        a kill or a fault -- is spawned HERE and only then leased, so neither the
        fork/exec nor the reap of a wedged process lands inside a caller's budget.
        Runs every 0.5 s and at once when woken.
        """
        while True:
            self._wake.wait(0.5)
            self._wake.clear()
            if self._shutdown.is_set():
                break  # never respawn a child ``shutdown`` has just killed
            now = time.monotonic()
            for child in self._children:
                started = child.busy_since
                if started is not None and now - started > self._ceiling:
                    logger.warning(
                        "subprocess-pool child exceeded the %.1fs request ceiling; killing it",
                        self._ceiling,
                    )
                    child.kill_async()
            self._collect_pending()
            if self._shutdown.is_set():
                break
            for child in list(self._children):
                if child.ensure_spawned():
                    if self._shutdown.is_set():
                        # ``shutdown`` set the flag while this tick was already
                        # past its checks; its kill loop may have run before the
                        # ``Popen`` above, so the child it never saw is reaped by
                        # the thread that made it. This is what closes the refill
                        # window even when the join in ``shutdown`` times out.
                        child.kill()
                        continue
                    self._enqueue(child)
            self._note_spawn_errors()

    def _enqueue(self, child: _Child) -> None:
        """Put *child* on the ready queue, once, and only while it is still a slot.

        The reaper works from a snapshot of the roster, so a slot ``_replace_slot``
        retired mid-tick can arrive here already spawned.  Enqueuing it would lease a
        child no slot owns, and its next deadline would then miss the roster and raise
        out of the resolver's classification -- the one path that could read as a
        pool fault and keep the lexical forms.  Membership is checked under the same
        lock the swap takes, and a retired slot's fresh child is killed here.
        """
        with self._qlock:
            current = child in self._children
            if not current:
                pass
            elif child.queued:
                return
            else:
                child.queued = True
        if current:
            self._free.put(child)
        elif child.proc is not None and child.proc.poll() is None:
            child.kill()

    def _note_spawn_errors(self) -> None:
        """Publish or clear the spawn-failure verdict after a reaper tick.

        No live child AND every empty slot's last spawn raised means the pool cannot
        start children on this host: the ``OSError`` is recorded and the
        ``_SPAWN_FAILED`` marker queued once, so callers get the error now.  Any live
        child clears it; a marker still queued is then dropped by the caller that
        pulls it.
        """
        errors = [c.spawn_error for c in self._children if c.spawn_error is not None]
        alive = any(c.proc is not None and c.proc.poll() is None for c in self._children)
        with self._qlock:
            if alive or not errors:
                self._spawn_error = None
                return
            self._spawn_error = errors[0]
            if self._failed_queued:
                return
            self._failed_queued = True
        self._free.put(_SPAWN_FAILED)

    def _release(self, child: _Child) -> None:
        """Hand a leased slot back: to the ready queue if its child is live, else to the reaper."""
        proc = child.proc
        if proc is not None and proc.poll() is None:
            self._enqueue(child)
        else:
            self._wake.set()

    def _collect_pending(self) -> None:
        """Collect the exit of every deadline-killed child that has exited by now.

        One that has not stays on the bounded list for a later tick: it is inside an
        uninterruptible syscall, and nothing this process does can hurry it.
        """
        self._unreaped.collect()

    def live_children(self) -> int:
        """How many slots hold a live child right now (leased or ready)."""
        return sum(1 for c in self._children if c.proc is not None and c.proc.poll() is None)

    def serviceable_children(self) -> int:
        """Slots that can take a request now or after the reaper's next tick.

        Live children, plus empty slots the reaper can still fill -- none when the
        bounded list of killed-but-unexited children is full or spawning fails.  For a
        caller deciding whether to spend a slot on a request it already suspects will
        wedge: the resolver refuses to re-probe a prefix with stall history when this
        is at most one, so a known-bad mount cannot take the pool's last child.  A
        cold pool (spawning, nothing killed) counts every slot.
        """
        live = self.live_children()
        if self._unreaped.full():
            return live
        fillable = sum(
            1
            for c in self._children
            if (c.proc is None or c.proc.poll() is not None) and c.spawn_error is None
        )
        return live + fillable

    def _exhausted(self) -> str | None:
        """Why no request can be served now, or ``None`` if one can (or soon can).

        No live child anywhere, and the reaper cannot spawn one because the bounded
        list of killed-but-unexited children is full: waiting would spend the caller's
        whole budget on an empty queue, so ``_lease`` raises this instead.
        """
        if self.live_children():
            return None
        if self._unreaped.full():
            return (
                f"no live resolver child and {len(self._unreaped)} killed children have "
                "not exited (wedged filesystem); the pool cannot spawn until one does"
            )
        return None

    def call_op(self, op: int, payload: bytes, timeout: float | None = None) -> bytes:
        """Run *op* in a child ON THE CALLING THREAD and return its response payload.

        This is the primitive; :meth:`submit_op` is a ``Future`` face over it. The
        budget is absolute from entry, so waiting for a free child spends the same
        clock as waiting for the answer -- a caller cannot be starved of its budget
        by lease contention and then given a fresh one.

        Raises ``TimeoutError`` if the budget expires, :class:`SubprocessPoolUnavailable`
        if the child faulted, and the reaper's ``OSError`` if no child can be started
        on this host. Never returns a partial or empty answer.
        """
        if self._shutdown.is_set():
            raise RuntimeError("executor is shut down")
        deadline = None if timeout is None else time.monotonic() + timeout
        child = self._lease(deadline)
        release = child
        try:
            if deadline is None or _CAN_SELECT_PIPES:
                return child.request(op, payload, deadline)
            return self._request_bounded_by_thread(child, op, payload, deadline)
        except _ReaderLost as lost:
            # The reader thread may still be blocked on the killed child's pipe,
            # holding the slot's request lock, and the caller does not wait to find
            # out.  The slot is replaced: the killed process joins the bounded
            # unreaped list for the reaper to collect, and the reaper spawns the
            # fresh slot's child off this thread.
            release = self._replace_slot(child, lost.proc)
            raise SubprocessPoolTimeout(
                "child did not answer within the budget", child_syscall=lost.sampled
            ) from None
        finally:
            self._release(release)

    def _lease(self, deadline: float | None) -> _Child:
        """Take a READY slot off the queue within *deadline*; never spawns.

        Raises :class:`SubprocessPoolUnavailable` at once when the pool is exhausted
        (:meth:`_exhausted`), so a caller is refused fail-closed in microseconds rather
        than after its whole budget.

        A ``_SPAWN_FAILED`` marker on the queue means the reaper could start no child:
        it is left there for the next waiter and the recorded ``OSError`` is raised,
        unless a spawn has succeeded since, in which case the stale marker is dropped
        and the wait continues.
        """
        while True:
            exhausted = self._exhausted()
            if exhausted is not None:
                raise SubprocessPoolUnavailable(exhausted)
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            try:
                item = self._free.get(timeout=remaining)
            except queue.Empty:
                exhausted = self._exhausted()
                if exhausted is not None:
                    raise SubprocessPoolUnavailable(exhausted) from None
                raise TimeoutError("no free child within the budget") from None
            if item is _SPAWN_FAILED:
                with self._qlock:
                    error = self._spawn_error
                    if error is None:
                        self._failed_queued = False
                        continue
                self._free.put(item)
                raise type(error)(f"subprocess-pool child could not be started: {error}")
            child = item
            assert isinstance(child, _Child)
            with self._qlock:
                child.queued = False
            return child

    def _replace_slot(self, lost: _Child, proc: subprocess.Popen[bytes] | None) -> _Child:
        """Retire *lost* from the roster and return the fresh slot that takes its place.

        The swap happens under the queue lock BEFORE the reaper is woken, so a tick
        that spawned the retired slot from its snapshot finds it gone at ``_enqueue``
        and kills that child instead of leasing it.  A slot already retired (a stale
        object reaching here twice) is left alone: its replacement is in the roster.
        """
        if proc is not None:
            self._unreaped.add(proc)
        fresh = _Child(self._source, self._unreaped)
        with self._qlock:
            try:
                index = self._children.index(lost)
            except ValueError:
                fresh = lost  # retired already; nothing to swap, nothing to lease
            else:
                self._children[index] = fresh
        self._wake.set()
        return fresh

    def _request_bounded_by_thread(
        self, child: _Child, op: int, payload: bytes, deadline: float
    ) -> bytes:
        """The caller's bound where ``select`` cannot arm one on the pipe (Windows).

        The read runs on one of the internal pool's threads and the caller waits on
        its future with the same deadline, so a wedged child costs the CALLER its
        budget rather than the ceiling.  This is the slower shape the module
        docstring measures (two extra GIL handoffs per answer), accepted here
        because the alternative is a caller blocked until the reaper fires and a
        wedge that is never classified as a timeout -- on the resolver that meant
        the stall cooldown never opened on Windows.  A wedge at the deadline is
        sampled (``None`` off Linux), the child is killed so the blocked read sees
        EOF, and the caller returns AT the deadline: it never waits for the reader's
        own cleanup, which runs on the reader thread when the EOF arrives.  The slot
        is reported as :class:`_ReaderLost` so ``call_op`` retires it and leases a
        fresh one, rather than handing the next caller a slot whose lock the reader
        may still hold.
        """
        future = self._bounded_readers.submit(child.request, op, payload, None)
        try:
            return future.result(timeout=max(0.0, deadline - time.monotonic()))
        except FuturesTimeoutError:
            proc = child.proc
            sampled = proc_syscall(proc.pid) if proc is not None else None
            child.kill_async()
            raise _ReaderLost(proc, sampled) from None

    def submit_op(self, op: int, payload: bytes) -> Future[bytes]:
        """Run *op* on a child; the future raises :class:`SubprocessPoolUnavailable` on fault.

        The work runs when the future's ``result()`` is awaited, on that thread --
        see :class:`_CallerThreadFuture` for why a pool worker cannot carry it.
        """
        if self._shutdown.is_set():
            raise RuntimeError("executor is shut down")
        return _CallerThreadFuture(lambda timeout: self.call_op(op, payload, timeout))

    def submit(  # type: ignore[override]
        self, fn: Callable[..., _T], /, *args: Any, **kwargs: Any
    ) -> Future[_T]:
        """Standard ``Executor.submit``: runs *fn* on the internal thread pool.

        Unrecognised work is NOT an error and is not sent to a child. Keeping the
        thread-pool behaviour for it is what lets this class replace a
        ``ThreadPoolExecutor`` in a factory without changing any caller.
        """
        return self._fallback.submit(fn, *args, **kwargs)

    def map(  # type: ignore[override]
        self, fn: Callable[..., _T], *iterables: Iterable[Any], **kwargs: Any
    ) -> Iterator[_T]:
        return self._fallback.map(fn, *iterables, **kwargs)

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        self._shutdown.set()
        self._wake.set()
        # The reaper is the ONLY thing that spawns, and it works from a tick that
        # checks ``_shutdown`` twice and then spawns every empty slot. A tick already
        # past that second check keeps going, so a kill loop that ran concurrently
        # could empty a slot and have the reaper refill it a moment later with a
        # child nothing ever signals -- measured under a hygiene sweep as
        # a ``sleep(3600)`` test child still alive at teardown in every round, and
        # as 18 of 80 shut-down-at-once pools keeping a live child. So the reaper is
        # joined FIRST: bounded, because it wakes on the event set above and one
        # tick is a poll of each slot plus at most one ``Popen`` per slot. Only the
        # calling thread may not join itself (``atexit`` never runs on it; a caller
        # inside a submitted callable could).
        if self._reaper is not threading.current_thread():
            self._reaper.join(timeout=_SHUTDOWN_REAPER_JOIN_SECS)
            if self._reaper.is_alive():
                logger.warning(
                    "subprocess-pool reaper did not stop within %.1fs of shutdown",
                    _SHUTDOWN_REAPER_JOIN_SECS,
                )
        self._fallback.shutdown(wait=wait, cancel_futures=cancel_futures)
        self._bounded_readers.shutdown(wait=False, cancel_futures=True)
        for child in self._children:
            child.kill()
        self._collect_pending()

    # -- convenience for the first user -------------------------------------

    def realpath_spellings(self, path: str, timeout: float | None = None) -> set[str]:
        """Resolved spellings of *path*, computed in a child. Never returns empty on fault.

        *path* is absolutized against THIS process's working directory before it is
        sent. ``realpath`` resolves a relative path against the CWD, so without this
        the answer would depend on the child's CWD rather than the caller's, and a
        relative symlink into a credential store would resolve somewhere harmless
        and miss the sensitive-path match -- a gate that fails open. Absolutizing
        here also pins the base to the moment of the call, so another thread calling
        ``os.chdir`` cannot move it underneath the request. ``abspath`` only joins
        and normalises; every symlink is still resolved in the child, so the answer
        matches what the in-process resolver returns for the same input.

        Raises :class:`SubprocessPoolUnavailable` instead of an empty set on fault, so a
        caller cannot mistake a dead child for "this path resolves to nothing", and
        ``TimeoutError`` if *timeout* expires -- which means the mount did not
        answer: the child's own work is microseconds and does not wait behind the GIL.
        """
        payload = self.call_op(OP_REALPATH_SPELLINGS, os.fsencode(os.path.abspath(path)), timeout)
        return {os.fsdecode(value) for value in unpack_strings(payload)}
