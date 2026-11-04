"""Process creation off the event loop, for the Windows Proactor loop.

CPython's Proactor loop builds a child inside the subprocess transport's
constructor: ``_WindowsSubprocessTransport._start`` calls ``windows_utils.Popen``,
which reaches ``subprocess.Popen._execute_child`` and ``CreateProcess`` on the
calling thread, before the transport's first await. On a host whose endpoint
protection holds ``CreateProcess`` for seconds, that is the gateway loop held for
seconds, and the loop-stall watchdog kills the gateway at 25.

The transport's work splits into two steps, and only the first is slow:

1. Build the ``Popen``: ``CreateProcess`` plus the overlapped pipe handles. This
   touches no loop state, so it runs on a worker thread here.
2. Bind it to the loop: ``wait_for_handle`` on the process handle in ``_start``,
   then ``_connect_pipes`` scheduled by ``BaseSubprocessTransport.__init__``. This
   runs on the loop, unchanged, from the interpreter's own ``_start`` with its
   ``Popen`` lookup answered by the object step 1 built.

The caller therefore receives the same loop-bound ``asyncio.subprocess.Process`` a
stock spawn returns. A cancellation that lands while step 1 runs cannot stop the
thread, so the spawn waits for it, adopts any child it created and tears that
child down exactly as a cancellation at the stock transport's ``await waiter``
does (``close()`` then ``_wait()``) before re-raising.

:func:`gateway_loop_factory` gives the gateway a Proactor loop whose every
``subprocess_exec`` / ``subprocess_shell`` takes this path, so no call site can
drift back onto the loop. The owned spawn in ``platform_compat_wintree`` calls
:func:`make_subprocess_transport_off_loop` with its capturing ``Popen`` directly.
"""

from __future__ import annotations

import asyncio
import atexit
import importlib
import threading
import types
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from kiro_crew.platform_compat_native import IS_WINDOWS

#: Workers that may sit inside a slow ``CreateProcess`` at once. A started
#: ``run_in_executor`` call cannot be cancelled, so a spawn held by a filter driver
#: keeps its worker until the OS returns; this bound caps those threads and queues
#: the rest here, apart from the pools teardown and maintenance use.
_MAX_SPAWN_WORKERS = 8

_pool: ThreadPoolExecutor | None = None
_pool_lock = threading.Lock()


def spawn_executor() -> ThreadPoolExecutor:
    """Return the process-wide pool that builds children off the loop."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ThreadPoolExecutor(
                    max_workers=_MAX_SPAWN_WORKERS, thread_name_prefix="mc-spawn"
                )
                atexit.register(_shutdown_spawn_executor)
    return _pool


def _shutdown_spawn_executor() -> None:
    global _pool
    with _pool_lock:
        pool, _pool = _pool, None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


def _adopting_start(start: Any, popen_global: str, popen: Any) -> Any:
    """``start`` with its ``<popen_global>.Popen`` lookup answering *popen*.

    The clone keeps the interpreter's own ``_start`` body, so the loop-side binding
    (the process-handle wait, the exit callback) is exactly the stock one.
    """
    real = start.__globals__[popen_global]

    class _Namespace:
        def __getattr__(self, name: str) -> Any:
            return getattr(real, name)

        @staticmethod
        def Popen(*_args: Any, **_kwargs: Any) -> Any:
            return popen

    cloned = types.FunctionType(
        start.__code__,
        dict(start.__globals__, **{popen_global: _Namespace()}),
        start.__name__,
        start.__defaults__,
        start.__closure__,
    )
    cloned.__kwdefaults__ = start.__kwdefaults__
    return cloned


async def _build_off_loop(
    loop: asyncio.AbstractEventLoop, build: Callable[[], Any]
) -> tuple[Any, bool]:
    """Run *build* on :func:`spawn_executor`; return ``(result, cancelled)``.

    A cancellation does not abandon the build: the child it may be creating would
    have no owner. The wait goes on until the thread returns, and the caller learns
    it was cancelled so it can tear the child down before re-raising.

    The outcome travels in *box*, never as the executor future's result or
    exception: that future stays reachable from the worker thread until the worker
    takes its next item, and a failed build's traceback holds the half-built
    ``Popen``. Emptying the box here leaves the failure owned by the raise alone,
    as it is when the stock transport builds the child on the loop.
    """
    box: list[tuple[bool, Any]] = []

    def boxed() -> None:
        try:
            box.append((True, build()))
        except BaseException as exc:  # noqa: B036 - re-raised on the loop below
            box.append((False, exc))

    future = loop.run_in_executor(spawn_executor(), boxed)
    cancelled = False
    while True:
        try:
            await asyncio.shield(future)
            break
        except asyncio.CancelledError:
            if future.done() and not future.cancelled():
                # The build itself finished (or raised) while this task was being
                # cancelled: settle on its outcome.
                cancelled = True
                break
            if future.cancelled():
                raise
            cancelled = True
    ok, value = box.pop()
    if ok:
        return value, cancelled
    try:
        if cancelled:
            # The task was cancelled while the build ran: the cancellation is what
            # it must see, as at the stock await. The build error rides along as
            # the cause.
            raise asyncio.CancelledError() from value
        raise value
    finally:
        del value


async def make_subprocess_transport_off_loop(
    loop: asyncio.AbstractEventLoop,
    protocol: Any,
    args: Any,
    shell: bool,
    stdin: Any,
    stdout: Any,
    stderr: Any,
    bufsize: int,
    extra: Any = None,
    *,
    popen_factory: Callable[..., Any],
    transport_cls: type | None = None,
    popen_global: str = "windows_utils",
    after_init: Callable[[Any], None] | None = None,
    **kwargs: Any,
) -> Any:
    """``_make_subprocess_transport`` with the child built on a worker thread.

    *popen_factory* is called on the worker with the arguments the stock ``_start``
    hands ``Popen``. *transport_cls* defaults to the Proactor loop's
    ``_WindowsSubprocessTransport``; *popen_global* names the module global its
    ``_start`` spawns through. *after_init*, when given, runs on the loop right
    after the transport is built (a child-watcher registration, for a loop that
    needs one). The waiter handling is the stock Proactor loop's.
    """
    if transport_cls is None:
        transport_cls = _windows_transport_class()
    start: Any = getattr(transport_cls, "_start")
    if not {popen_global, "Popen"}.issubset(start.__code__.co_names):
        raise RuntimeError(
            "off-loop spawn refused: this interpreter's subprocess transport does not "
            f"build its child through {popen_global}.Popen"
        )

    def build() -> Any:
        return popen_factory(
            args, shell=shell, stdin=stdin, stdout=stdout, stderr=stderr, bufsize=bufsize, **kwargs
        )

    popen, cancelled = await _build_off_loop(loop, build)

    class _Adopting(transport_cls):  # type: ignore[misc, valid-type]
        _start = _adopting_start(start, popen_global, popen)

    waiter = loop.create_future()
    transport = _Adopting(
        loop, protocol, args, shell, stdin, stdout, stderr, bufsize, waiter=waiter, extra=extra
    )
    if after_init is not None:
        after_init(transport)
    try:
        if cancelled:
            # The stock spawn's cancel lands on ``await waiter`` and cancels it; do
            # the same so the pipe-connect task settles a cancelled waiter rather
            # than leaving an unretrieved result behind.
            waiter.cancel()
            raise asyncio.CancelledError
        await waiter
    except (SystemExit, KeyboardInterrupt):
        raise
    except BaseException:
        transport.close()
        await transport._wait()
        raise
    return transport


def _windows_transport_class() -> type:
    windows_events: Any = importlib.import_module("asyncio.windows_events")
    return windows_events._WindowsSubprocessTransport  # type: ignore[no-any-return]


def _windows_popen() -> Callable[..., Any]:
    windows_utils: Any = importlib.import_module("asyncio.windows_utils")
    return windows_utils.Popen  # type: ignore[no-any-return]


if IS_WINDOWS:  # pragma: no cover - exercised on the Windows CI lane

    class OffLoopSpawnProactorEventLoop(asyncio.ProactorEventLoop):  # type: ignore[name-defined, misc]
        """A Proactor loop whose subprocess transports build their child off-loop."""

        async def _make_subprocess_transport(  # type: ignore[override]
            self,
            protocol: Any,
            args: Any,
            shell: bool,
            stdin: Any,
            stdout: Any,
            stderr: Any,
            bufsize: int,
            extra: Any = None,
            **kwargs: Any,
        ) -> Any:
            return await make_subprocess_transport_off_loop(
                self,
                protocol,
                args,
                shell,
                stdin,
                stdout,
                stderr,
                bufsize,
                extra,
                popen_factory=_windows_popen(),
                **kwargs,
            )


def gateway_loop_factory() -> Callable[[], asyncio.AbstractEventLoop] | None:
    """The ``loop_factory`` the gateway's ``asyncio.run`` uses.

    Windows: :class:`OffLoopSpawnProactorEventLoop`. Elsewhere ``None``, the
    interpreter's default loop, whose spawn is a ``fork``/``posix_spawn`` that
    returns at once.
    """
    if IS_WINDOWS:  # pragma: no cover - exercised on the Windows CI lane
        return OffLoopSpawnProactorEventLoop
    return None
