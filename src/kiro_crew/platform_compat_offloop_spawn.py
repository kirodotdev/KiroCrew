"""Process creation off the event loop, for the gateway's Proactor and Unix loops.

CPython's Proactor loop builds a child inside the subprocess transport's
constructor: ``_WindowsSubprocessTransport._start`` calls ``windows_utils.Popen``,
which reaches ``subprocess.Popen._execute_child`` and ``CreateProcess`` on the
calling thread, before the transport's first await. On a host whose endpoint
protection holds ``CreateProcess`` for seconds, that is the gateway loop held for
seconds, and the loop-stall watchdog kills the gateway at 25. The Unix loop does
the same with ``fork``, whose cost grows with the size of the gateway, so
:class:`OffLoopSpawnUnixSelectorEventLoop` takes the same path there.

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
import contextlib
import importlib
import logging
import subprocess
import threading
import types
import warnings
from collections.abc import Callable
from typing import Any

from kiro_crew.enqueue_only_pool import EnqueueOnlyThreadPoolExecutor
from kiro_crew.platform_compat_native import IS_WINDOWS

logger = logging.getLogger(__name__)

#: Workers that may sit inside a slow ``CreateProcess`` at once. A started
#: ``run_in_executor`` call cannot be cancelled, so a spawn held by a filter driver
#: keeps its worker until the OS returns; this bound caps those threads and queues
#: the rest here, apart from the pools teardown and maintenance use.
_MAX_SPAWN_WORKERS = 8

_pool: EnqueueOnlyThreadPoolExecutor | None = None
_pool_lock = threading.Lock()


def spawn_executor() -> EnqueueOnlyThreadPoolExecutor:
    """Return the process-wide pool that builds children off the loop.

    The loop thread submits every spawn here, so its ``submit`` only enqueues: a
    stdlib pool's ``submit`` waits while any thread in the process starts a pool
    worker.
    """
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = EnqueueOnlyThreadPoolExecutor(
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
        except Exception:  # noqa: BLE001 - only the pool fails it: settled below
            break
    # An empty box: the pool failed the call before it ran (no worker could start),
    # so no child exists and the pool's error is the outcome.
    if box:
        ok, value = box.pop()
    else:
        ok, value = False, future.exception()
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


def _posix_popen(args: Any, **kwargs: Any) -> Any:
    """The stock Unix ``_start``'s ``Popen`` call, made on the spawn worker.

    ``subprocess.Popen`` is looked up per call, as the stock ``_start`` looks it up.
    """
    return subprocess.Popen(args, universal_newlines=False, **kwargs)


def _posix_child_watcher(loop: asyncio.AbstractEventLoop) -> Any:
    """The watcher that reaps *loop*'s children, as a context manager, or ``None``.

    ``None`` sends the spawn down the stock path, which raises the stock error for
    a watcher that is not active. CPython 3.14 keeps the watcher on the loop;
    3.12 and 3.13 keep it on the policy. ``FastChildWatcher`` is refused: it reaps
    every child with ``waitpid(-1)`` and drops the exit of a pid nobody registered
    yet, and a child built on a worker is registered only after it exists.
    """
    watcher = getattr(loop, "_watcher", None)
    if watcher is not None:
        return contextlib.nullcontext(watcher)
    get_child_watcher = getattr(asyncio, "get_child_watcher", None)
    if get_child_watcher is None:
        return None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        watcher = get_child_watcher()
    fast_watcher = getattr(asyncio, "FastChildWatcher", None)
    if fast_watcher is not None and isinstance(watcher, fast_watcher):
        return None
    if not watcher.is_active():
        return None
    return watcher


_stock_spawn_fallback_logged = False


def _log_stock_spawn_fallback_once() -> None:
    """Say once per process that children are built on the loop thread again."""
    global _stock_spawn_fallback_logged
    if _stock_spawn_fallback_logged:
        return
    _stock_spawn_fallback_logged = True
    logger.warning(
        "off-loop spawn unavailable (no usable child watcher on this interpreter); "
        "child processes start on the event loop thread, and a slow fork can stall it"
    )


if not IS_WINDOWS:

    class OffLoopSpawnUnixSelectorEventLoop(asyncio.SelectorEventLoop):
        """A Unix selector loop whose subprocess transports build their child off-loop.

        ``fork`` copies the parent's memory map before ``Popen`` returns, so its
        cost grows with the gateway: a gateway with a large heap and many threads
        held the loop long enough in one ``fork`` for the loop-stall watchdog to
        restart it, and the App Store refresh, which starts a ``git clone`` per
        catalog row, did it several times a day.
        """

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
            watcher = _posix_child_watcher(self)
            if watcher is None:
                _log_stock_spawn_fallback_once()
                return await super()._make_subprocess_transport(  # type: ignore[misc]
                    protocol, args, shell, stdin, stdout, stderr, bufsize, extra, **kwargs
                )
            unix_events: Any = importlib.import_module("asyncio.unix_events")
            with watcher as active:

                def watch(transport: Any) -> None:
                    callback = self._child_watcher_callback  # type: ignore[attr-defined]
                    pid = transport.get_pid()  # pid-owner-ok: the transport this spawn built
                    active.add_child_handler(pid, callback, transport)

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
                    popen_factory=_posix_popen,
                    transport_cls=unix_events._UnixSubprocessTransport,
                    popen_global="subprocess",
                    after_init=watch,
                    **kwargs,
                )

    def _new_posix_gateway_loop() -> asyncio.AbstractEventLoop:
        loop = OffLoopSpawnUnixSelectorEventLoop()
        # ``asyncio.run`` makes a loop it built itself the current loop, which on
        # 3.12 and 3.13 is what attaches a SIGCHLD child watcher to it. It does not
        # do that for a loop from a factory, so the factory does.
        asyncio.set_event_loop(loop)
        return loop


def gateway_loop_factory() -> Callable[[], asyncio.AbstractEventLoop] | None:
    """The ``loop_factory`` the gateway's ``asyncio.run`` uses.

    Windows: :class:`OffLoopSpawnProactorEventLoop`. Elsewhere a factory for
    :class:`OffLoopSpawnUnixSelectorEventLoop`.
    """
    if IS_WINDOWS:  # pragma: no cover - exercised on the Windows CI lane
        return OffLoopSpawnProactorEventLoop
    return _new_posix_gateway_loop
