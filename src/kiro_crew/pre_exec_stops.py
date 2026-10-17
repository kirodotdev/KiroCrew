"""Stops that must run before an in-place restart replaces this process image.

Both in-app restarts (the dashboard's ``_restart_gateway`` and the update
coordinator's ``GatewayOrchestrator._restart_after_update_claimed``) end in
``os.execv``. The exec skips every aiohttp cleanup hook but keeps the pid, so a
child process one of those hooks would have stopped still names this process as
its live parent after the exec, and the new image cannot tell it from its own.

An owner that supervises such a child registers its teardown here, next to the
cleanup hook that stops it on a normal shutdown, and unregisters it from that
hook. Both restart paths call :func:`run_pre_exec_stops` once, after the
sessions close and the MCP broker stops, and before the exec. They name no
owner, so a new owner needs no edit to either restart path.

Cleanup hooks whose resources do not outlive the exec (in-memory state, files,
sockets the exec closes) do not register: the exec already ends them.

Registration and the run happen on the gateway's event loop; nothing here is
thread-safe, and nothing needs to be.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

#: Ceiling on the whole pre-exec step. One instance forwarder's stop is a
#: SIGTERM with a 5 s wait and then a SIGKILL, so this covers one wedged child
#: plus the rest. The stops run concurrently, so this is also each stop's limit;
#: a stop that runs past it is abandoned and the exec goes on.
PRE_EXEC_STOP_TIMEOUT_SECS = 10.0

PreExecStop = Callable[[], Awaitable[object]]

_stops: dict[str, PreExecStop] = {}


def register_pre_exec_stop(name: str, stop: PreExecStop) -> Callable[[], None]:
    """Run *stop* before every in-place restart exec, until unregistered.

    *name* identifies the owner in logs; registering a name again replaces its
    stop. Returns the unregister callable. It removes this exact stop only, so
    an older owner's cleanup cannot drop a newer owner's registration under the
    same name.
    """
    _stops[name] = stop

    def _unregister() -> None:
        if _stops.get(name) is stop:
            del _stops[name]

    return _unregister


async def _run_one(name: str, stop: PreExecStop, timeout: float) -> None:
    """Await one stop under *timeout*; never raise.

    *stop* is called inside the guard, so a synchronous raise is contained too.
    """
    try:
        await asyncio.wait_for(stop(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning(
            "%s stop before restart did not finish in %.0fs; restarting anyway", name, timeout
        )
    except Exception:
        logger.warning("%s stop before restart failed", name, exc_info=True)


async def run_pre_exec_stops(timeout: float | None = None) -> None:
    """Run every registered stop concurrently under one *timeout*; never raise.

    *timeout* defaults to :data:`PRE_EXEC_STOP_TIMEOUT_SECS`, read at call time.
    Called past the point of no return, with the sessions closed: a stop that
    fails or overruns is logged and the exec still runs. With nothing
    registered it returns at once.
    """
    if timeout is None:
        timeout = PRE_EXEC_STOP_TIMEOUT_SECS
    stops = list(_stops.items())
    if stops:
        await asyncio.gather(*(_run_one(name, stop, timeout) for name, stop in stops))
