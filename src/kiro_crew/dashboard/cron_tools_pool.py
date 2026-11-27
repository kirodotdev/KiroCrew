"""The thread pool ``POST /api/crons/tools`` runs each cron tool call on.

A call waits, from its worker thread, for work it hands to the route's event loop:
the chat folder checks, the folder-store hold and the tab move
(:mod:`kiro_crew.mcp_cron_chat_folder`). That loop work runs jobs on the loop's
default executor itself (the tab move's slot save, the folder store's confirmed
write). A call parked on a default-executor worker could therefore hold the last
worker that work needs, and enough concurrent calls would wait on each other
forever. On its own pool, a wait for the loop never holds a worker the loop's own
work needs.
"""

from __future__ import annotations

import asyncio
import atexit
import functools
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from typing import TypeVar

_T = TypeVar("_T")

#: Agent-paced, and a healthy call takes milliseconds; the cron store serializes
#: the writes, so more workers would only queue on its lock. Bounded because a
#: started call cannot be cancelled: a wedged one keeps its worker.
_MAX_CRON_TOOLS_WORKERS = 4

_pool: ThreadPoolExecutor | None = None
_pool_lock = threading.Lock()


def cron_tools_executor() -> ThreadPoolExecutor:
    """Return the process-wide cron tool call pool, creating it on first use.

    Threads are named ``mc-crontools``. Kept off the loop's default executor
    because a call waits on loop work that runs default-executor jobs (see the
    module docstring).
    """
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ThreadPoolExecutor(
                    max_workers=_MAX_CRON_TOOLS_WORKERS, thread_name_prefix="mc-crontools"
                )
                atexit.register(_pool.shutdown, wait=False, cancel_futures=True)
    return _pool


async def run_cron_tool_call(call: Callable[[], _T]) -> _T:
    """Await *call* on :func:`cron_tools_executor`, in a copy of the caller's context.

    The copy is what ``asyncio.to_thread`` gives a call: it reads the request's
    context variables, and what it sets stays out of the caller's context.
    """
    bound = functools.partial(copy_context().run, call)
    return await asyncio.get_running_loop().run_in_executor(cron_tools_executor(), bound)
