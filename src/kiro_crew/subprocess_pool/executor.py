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
1. Write a leaf child script next to :mod:`_child_realpath` that imports STDLIB
   ONLY and answers one op code.  Never ``-m``; always run it by path, or
   ``kiro_crew/__init__.py`` executes and the cold-start budget is gone.
2. Give it a request/response body in :mod:`_child_realpath`'s shape: 4-byte
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
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# Mirrors ``_child_realpath``; pinned by ``test_protocol_round_trips``.
_LEN = struct.Struct(">I")
_REQ_HEADER = struct.Struct(">IB")

OP_REALPATH_SPELLINGS = 1

STATUS_OK = 0
STATUS_ERROR = 1

_CHILD_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_child_realpath.py")

# How long one request may hold a child before the child is killed rather than
# waited on.  It must sit ABOVE every caller budget (the resolver's largest is the
# 8 s anchor rebuild) so that the caller's own timeout is what normally fires: the
# ceiling is for a child that will never answer, not for a slow one.  Killing is
# the capability a thread pool does not have -- a thread wedged in the kernel holds
# its slot until the mount answers, while a wedged child is a process and can be
# reclaimed.
DEFAULT_REQUEST_CEILING_SECS = 20.0


class SubprocessPoolUnavailable(RuntimeError):
    """The child could not answer: it died, was killed at the ceiling, or faulted.

    Callers MUST treat this as a failure to establish the answer, never as an
    empty answer.  For a sensitive-path gate that means refusing the path.
    """


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


class _Child:
    """One child interpreter, used strictly one request at a time.

    Serial by design.  A demultiplexing reader thread would buy concurrency the
    workload does not need (one resolution costs ~22 microseconds, so a single
    child sustains ~45k/s) and would add the one bug class this protocol cannot
    tolerate: a response matched to the wrong request would hand one path's
    resolved form to another path's security decision.
    """

    __slots__ = ("_lock", "_next_id", "busy_since", "proc", "script")

    def __init__(self, script: str) -> None:
        self.script = script
        self.proc: subprocess.Popen[bytes] | None = None
        self.busy_since: float | None = None
        self._lock = threading.Lock()
        self._next_id = 0

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
        # It costs nothing here: this child is launched by ABSOLUTE PATH and imports
        # stdlib only, so it needs no ``sys.path`` entry of its own. And ``-I``
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
            [sys.executable, "-I", "-S", self.script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            bufsize=0,
        )

    def _kill_locked(self) -> None:
        proc = self.proc
        self.proc = None
        if proc is None:
            return
        try:
            proc.kill()
        except OSError:
            pass
        else:
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                logger.warning("subprocess-pool child %s ignored SIGKILL", proc.pid)
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

        Restart-on-demand rather than restart-on-death-notification: a child that
        died between requests is simply respawned here, so a crash costs one
        refused resolution and about eleven milliseconds, not a degraded gateway.

        Raises ``TimeoutError`` if *deadline* passes with the child silent, having
        first killed it -- a child that missed a microsecond-scale budget is wedged
        in the kernel, and its pipe is not known to be in frame.
        """
        with self._lock:
            proc = self.proc
            if proc is None or proc.poll() is not None:
                self._kill_locked()
                proc = self.proc = self._spawn()
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
                # destroyed rather than reused. Raising ``TimeoutError`` keeps this
                # substitutable for a pooled future, whose ``result(timeout=)``
                # raises the same class.
                self._kill_locked()
                raise TimeoutError("child did not answer within the budget") from None
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


# ``select`` accepts only SOCKETS on Windows, so it cannot watch a pipe there and
# the per-request read deadline is a POSIX capability. Where it is unavailable the
# read blocks and the ceiling reaper is what bounds a wedge, so a wedged child
# costs up to ``ceiling_secs`` rather than the caller's own budget. The gateway
# this serves runs on POSIX; making the deadline exact on Windows means moving the
# transport onto a socket pair so it becomes selectable.
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
        self._children = [_Child(script) for _ in range(workers)]
        self._free: queue.Queue[_Child] = queue.Queue()
        for child in self._children:
            self._free.put(child)
        # No thread pool for child work: it runs on the calling thread. The one
        # pool left serves ``submit``/``map`` for callables this class does not
        # recognise, which is what keeps it substitutable for a ThreadPoolExecutor.
        self._fallback = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix=f"{thread_name_prefix}-inline"
        )
        self._shutdown = threading.Event()
        self._reaper = threading.Thread(
            target=self._reap, name=f"{thread_name_prefix}-reaper", daemon=True
        )
        self._reaper.start()
        atexit.register(self.shutdown, wait=False)

    def _reap(self) -> None:
        """Kill any child whose request outlived the ceiling.

        This is the whole reason the work moved to a process. The parent thread
        waiting on that child is blocked in ``read`` and cannot cancel itself, so
        something else has to break the wait; killing the child makes the read
        return EOF at once and turns an unbounded wedge into one refused request.
        """
        while not self._shutdown.wait(0.5):
            now = time.monotonic()
            for child in self._children:
                started = child.busy_since
                if started is not None and now - started > self._ceiling:
                    logger.warning(
                        "subprocess-pool child exceeded the %.1fs request ceiling; killing it",
                        self._ceiling,
                    )
                    child.kill_async()

    def call_op(self, op: int, payload: bytes, timeout: float | None = None) -> bytes:
        """Run *op* in a child ON THE CALLING THREAD and return its response payload.

        This is the primitive; :meth:`submit_op` is a ``Future`` face over it. The
        budget is absolute from entry, so waiting for a free child spends the same
        clock as waiting for the answer -- a caller cannot be starved of its budget
        by lease contention and then given a fresh one.

        Raises ``TimeoutError`` if the budget expires, :class:`SubprocessPoolUnavailable`
        if the child faulted. Never returns a partial or empty answer.
        """
        if self._shutdown.is_set():
            raise RuntimeError("executor is shut down")
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            child = self._free.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("no free child within the budget") from None
        try:
            return child.request(op, payload, deadline)
        finally:
            self._free.put(child)

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
        self._fallback.shutdown(wait=wait, cancel_futures=cancel_futures)
        for child in self._children:
            child.kill()

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
