"""The code-intel app's worker pools.

Two small pools, owned by the app rather than by :mod:`kiro_crew.executors`, so
core carries nothing of this feature: a language-server request can wait seconds
on an indexing server, so it never borrows a worker another subsystem needs, and
the work that waits on no server never queues behind the work that does.

Both are created on first use and shut by :func:`shutdown_pools`, which the
app's ``on_shutdown`` hook runs when the app is disabled or the gateway stops.
Shutting them does not latch anything: the next request after a re-enable
creates fresh pools.
"""

from __future__ import annotations

import atexit
import threading
from concurrent.futures import ThreadPoolExecutor

#: Binding requests (go-to-definition, references, hover, outline). Each blocks
#: on a language server for up to its request timeout.
_MAX_REQUEST_WORKERS = 4
#: The Language servers tab's own operations (pool status, stop, start, detection)
#: and the dashboard path checks that precede every request: none waits on a
#: language server, so four slow requests never stall the tab.
_MAX_CONTROL_WORKERS = 2

_lock = threading.Lock()
_request_pool: ThreadPoolExecutor | None = None
_control_pool: ThreadPoolExecutor | None = None
_atexit_registered = False


def _register_atexit() -> None:
    global _atexit_registered
    if not _atexit_registered:
        atexit.register(shutdown_pools)
        _atexit_registered = True


def request_executor() -> ThreadPoolExecutor:
    """The pool for work that waits on a language server (threads ``mc-code-intel``)."""
    global _request_pool
    if _request_pool is None:
        with _lock:
            if _request_pool is None:
                _request_pool = ThreadPoolExecutor(
                    max_workers=_MAX_REQUEST_WORKERS, thread_name_prefix="mc-code-intel"
                )
                _register_atexit()
    return _request_pool


def control_executor() -> ThreadPoolExecutor:
    """The pool for work that waits on no server (threads ``mc-code-intel-ctl``)."""
    global _control_pool
    if _control_pool is None:
        with _lock:
            if _control_pool is None:
                _control_pool = ThreadPoolExecutor(
                    max_workers=_MAX_CONTROL_WORKERS, thread_name_prefix="mc-code-intel-ctl"
                )
                _register_atexit()
    return _control_pool


def shutdown_pools() -> None:
    """Shut both pools without waiting; a later request creates new ones."""
    global _request_pool, _control_pool
    with _lock:
        request_pool, _request_pool = _request_pool, None
        control_pool, _control_pool = _control_pool, None
    for pool in (request_pool, control_pool):
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
